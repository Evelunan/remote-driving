"""Authenticated latest-sample sender; recovery always needs fresh neutral input."""

import asyncio
import math
import time
from collections import OrderedDict
from uuid import UUID

from rd_core.control import is_newer
from rd_core.logging import SampledControlLog, get_logger
from rd_core.samples import ControlInput
from rd_core.settings import DrivingSettings
from rd_core.video_safety import VideoGate
from rd_core.wire import (
    CONTROL_REVISION,
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Telemetry,
    VehicleStatus,
    Window,
    conservative_age_ms,
)
from rd_quic.client import registered_connection
from rd_quic.framing import RpcError

logger = get_logger(__name__)


class DrivingControlClient:
    def __init__(
        self,
        settings: DrivingSettings,
        samples,
        *,
        on_status=None,
        on_connection_status=None,
        on_execution=None,
        on_telemetry=None,
        on_vehicle_status=None,
        video_gate=None,
    ):
        self.settings, self.samples = settings, samples
        self.video_gate = video_gate or VideoGate(
            stale_frame_ms=settings.video.stale_frame_ms,
            recovery_ms=settings.video.recovery_ms,
            max_capture_age_ms=settings.video.max_capture_age_ms,
        )
        self._lease_video = None
        self.on_status, self.on_execution, self.on_telemetry = (
            on_status,
            on_execution,
            on_telemetry,
        )
        self.latest = None
        self.on_connection_status = on_connection_status
        self.on_vehicle_status = on_vehicle_status
        self.connection = None
        self.epoch = None
        self.armed = False
        self._generation = None
        self._revoked = OrderedDict()
        self._windows = OrderedDict()
        self._last_window = None
        self._sent = OrderedDict()
        self._last_ack = None
        self._last_telemetry = None
        self._last_vehicle_status = None
        self._last_execution = 0
        self._seq = 0
        self._tx_log = SampledControlLog(logger)
        self._ack_log = SampledControlLog(logger)
        self.state = "disconnected"
        self._connection_status = (False, "")

    def _report_connection(self, online, detail=""):
        status = (online, detail)
        if status != self._connection_status:
            self._connection_status = status
            if self.on_connection_status is not None:
                self.on_connection_status(*status)

    def _status(self, state, detail=""):
        if self.state != state:
            logger.info(
                "Driving control state=%s session=%s epoch=%s",
                state,
                self.settings.session,
                self.epoch,
            )
        self.state = state
        if self.on_status is not None:
            self.on_status(state, detail)

    async def _collect(self):
        while True:
            self.latest = await self.samples.get()

    def _fresh(self):
        return isinstance(self.latest, ControlInput) and self.latest.fresh(
            self.settings.control.input_timeout_ms / 1000
        )

    def _neutral(self):
        return self._fresh() and self.latest.throttle == 0 and self.latest.steering == 0

    def _picture_ready(self, permit):
        return (
            permit.ready
            and permit.source is not None
            and permit.source.session == self.settings.session
        )

    def _lease_picture_valid(self):
        permit = self.video_gate.snapshot()
        return self._picture_ready(permit) and self._lease_video == (
            permit.revision,
            permit.source,
        )

    def _disconnected(self, generation, protocol):
        if generation is self._generation:
            self.epoch = None
            self.armed = False
            self._report_connection(False, "disconnected")
            self._status("disconnected")

    def _event(self, generation, method, params):
        if generation is not self._generation:
            return
        if method != "lease_revoked" or set(params) != {"epoch", "reason"}:
            raise RpcError("unexpected_lease_event")
        try:
            epoch = UUID(params["epoch"])
        except (ValueError, TypeError, AttributeError):
            raise RpcError("invalid_epoch") from None
        self._revoked[epoch] = None
        while len(self._revoked) > 64:
            self._revoked.popitem(last=False)
        if epoch == self.epoch:
            self.epoch = None
            self.armed = False
            self._status("revoked", str(params["reason"])[:64])

    def _datagram(self, generation, data):
        if generation is not self._generation or self.connection is None:
            return
        try:
            packet = Packet.decode(data)
        except (ValueError, ConnectionError):
            return
        if packet.session != self.settings.session:
            return
        if isinstance(packet.payload, VehicleStatus):
            self._receive_vehicle_status(packet)
            return
        if packet.epoch != self.epoch or not self._lease_picture_valid():
            return
        if isinstance(packet.payload, Window):
            if is_newer(packet.seq, self._last_window):
                self._last_window = packet.seq
                self._windows[packet.payload.token] = time.monotonic()
                while len(self._windows) > 32:
                    self._windows.popitem(last=False)
            return
        if isinstance(packet.payload, Execution):
            sample = self._sent.get(packet.seq)
            now = time.monotonic()
            if (
                sample is None
                or now - sample.sampled_at >= self.connection.max_control_age_ms / 1000
                or now - self._last_execution
                >= self.settings.control.command_timeout_ms / 1000
                or sample.timestamp_ms != packet.source_time_ms
                or not is_newer(packet.seq, self._last_ack)
            ):
                return
            if packet.payload.status != ExecutionStatus.EXECUTED:
                logger.warning(
                    "Execution failed session=%s epoch=%s seq=%d status=%s",
                    packet.session,
                    packet.epoch,
                    packet.seq,
                    packet.payload.status.name,
                )
                self.connection.protocol.close(
                    error_code=0x105, reason_phrase="execution_failed"
                )
                return
            self._last_ack = packet.seq
            self._last_execution = sample.sampled_at
            self._ack_log.write(
                packet.epoch,
                "Command acknowledged session=%s epoch=%s seq=%d sample_to_ack_ms=%.1f",
                packet.session,
                packet.epoch,
                packet.seq,
                (now - sample.sampled_at) * 1000,
                all_zero=sample.throttle == sample.brake == sample.steering == 0,
            )
            if not self.armed and sample.throttle == 0 and sample.steering == 0:
                self.armed = True
                self._status("active")
            if self.on_execution is not None:
                self.on_execution(
                    sample,
                    packet.payload,
                    (time.monotonic() - sample.sampled_at) * 1000,
                )
        elif (
            isinstance(packet.payload, Telemetry)
            and self.on_telemetry is not None
            and is_newer(packet.seq, self._last_telemetry)
        ):
            try:
                conservative_age_ms(
                    packet.source_time_ms,
                    time.time_ns() // 1_000_000,
                    max_age_ms=self.connection.max_control_age_ms,
                    clock_skew_ms=self.connection.max_clock_skew_ms,
                )
            except ValueError:
                return
            self._last_telemetry = packet.seq
            self.on_telemetry(packet)

    def _receive_vehicle_status(self, packet):
        if (
            packet.epoch != self.connection.status_stream
            or self.on_vehicle_status is None
            or not is_newer(packet.seq, self._last_vehicle_status)
        ):
            return
        budget = min(self.connection.max_control_age_ms,
                     self.settings.control.command_timeout_ms)
        try:
            age = conservative_age_ms(
                packet.source_time_ms, time.time_ns() // 1_000_000,
                max_age_ms=budget,
                clock_skew_ms=self.connection.max_clock_skew_ms,
            )
        except ValueError:
            return
        self._last_vehicle_status = packet.seq
        # Carry the remaining original budget across Qt's queued delivery.
        self.on_vehicle_status(packet, time.monotonic() + (budget - age) / 1000)

    async def _release(self):
        epoch, self.epoch = self.epoch, None
        self.armed = False
        self._lease_video = None
        if (
            epoch is not None
            and self.connection is not None
            and self.connection.protocol.rpc_ready
        ):
            try:
                await self.connection.protocol.request(
                    "release",
                    {"epoch": str(epoch)},
                    timeout_s=min(0.3, self.settings.network.register_timeout_s),
                )
            except (ConnectionError, TimeoutError, RpcError):
                pass  # Closing the connection and vehicle watchdog also revoke control.

    def _sample_window(self, sample, now):
        # Use USB capture time even when processing or queue delivery was delayed.
        for token, received in reversed(self._windows.items()):
            if (
                received < sample.sampled_at
                and now - received < self.connection.max_control_age_ms / 1000
            ):
                return token
        return None

    async def _send_session(self):
        protocol = self.connection.protocol
        while protocol.rpc_ready:
            permit = self.video_gate.snapshot()
            if self._picture_ready(permit):
                if not self._fresh() or self.latest.sampled_at < permit.ready_at:
                    status = "waiting_input"
                elif self._neutral():
                    break
                else:
                    status = "waiting_neutral"
            else:
                status = "waiting_video"
            if self.state != status:
                self._status(status)
            await asyncio.sleep(0.02)
        if not protocol.rpc_ready:
            return
        grant = await protocol.request("acquire", {})
        try:
            epoch = UUID(grant["epoch"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise RpcError("invalid_lease") from None
        if (
            grant.get("session") != self.settings.session
            or grant.get("neutral_required") is not True
            or epoch.int == 0
            or epoch in self._revoked
        ):
            raise RpcError("invalid_lease")
        self.epoch, self.armed = epoch, False
        self._lease_video = (permit.revision, permit.source)
        if not self._lease_picture_valid():
            self._status("video_unavailable", "primary_video_unavailable")
            raise RpcError("primary_video_unavailable")
        if grant.get("vehicle_principal") != permit.source.principal:
            raise RpcError("video_vehicle_mismatch")
        self._windows.clear()
        self._last_window = None
        self._sent.clear()
        self._seq, self._last_ack, self._last_telemetry = 0, None, None
        self._last_execution = time.monotonic()
        period = 1 / self.settings.control.send_hz
        next_tick = time.monotonic()
        self._status("waiting_execution")
        while protocol.rpc_ready and self.epoch == epoch:
            if not self._lease_picture_valid():
                self.armed = False
                self._status("video_unavailable", "primary_video_unavailable")
                raise RpcError("primary_video_unavailable")
            now = time.monotonic()
            if not self._fresh():
                raise RpcError("input_expired")
            if not self.armed and not self._neutral():
                raise RpcError("neutral_required")
            if (
                now - self._last_execution
                >= self.settings.control.command_timeout_ms / 1000
            ):
                raise RpcError("execution_confirmation_expired")
            if now >= next_tick:
                sample = self.latest
                token = self._sample_window(sample, now)
                next_tick += (math.floor((now - next_tick) / period) + 1) * period
                if token is None:
                    await asyncio.sleep(max(0, next_tick - time.monotonic()))
                    continue
                packet = Packet(
                    self.settings.session,
                    epoch,
                    self._seq,
                    sample.timestamp_ms,
                    Control(sample.throttle, sample.brake, sample.steering, token),
                )
                self._sent[self._seq] = sample
                while len(self._sent) > 64:
                    self._sent.popitem(last=False)
                protocol.send_datagram(packet.encode())
                self._seq = (self._seq + 1) & 0xFFFFFFFF
                self._tx_log.write(
                    epoch,
                    "Command sent session=%s epoch=%s seq=%d throttle=%.3f brake=%.3f steering=%.3f",
                    packet.session,
                    epoch,
                    packet.seq,
                    sample.throttle,
                    sample.brake,
                    sample.steering,
                    all_zero=sample.throttle == sample.brake == sample.steering == 0,
                )
            await asyncio.sleep(max(0, next_tick - time.monotonic()))

    async def run(self):
        collector = asyncio.create_task(self._collect(), name="driving-input")
        try:
            while True:
                generation = self._generation = object()
                try:
                    self._report_connection(False, "connecting")
                    self._status("connecting")
                    async with registered_connection(
                        self.settings.network,
                        self.settings.credentials,
                        self.settings.control,
                        session=self.settings.session,
                        role="driving",
                        revision=CONTROL_REVISION,
                        event_handler=lambda method, params, g=generation: self._event(
                            g, method, params
                        ),
                        datagram_handler=lambda data, g=generation: self._datagram(
                            g, data
                        ),
                        on_disconnect=lambda protocol, g=generation: self._disconnected(
                            g, protocol
                        ),
                    ) as connection:
                        self.connection = connection
                        self._last_vehicle_status = None
                        self._report_connection(True, "registered")
                        try:
                            self._status("registered")
                            await self._send_session()
                        finally:
                            await self._release()
                except (ConnectionError, OSError, TimeoutError, RpcError) as error:
                    self._report_connection(
                        False, error.code if isinstance(error, RpcError) else "disconnected"
                    )
                    self._status(
                        "video_vehicle_mismatch"
                        if isinstance(error, RpcError)
                        and error.code == "video_vehicle_mismatch"
                        else "video_unavailable"
                        if isinstance(error, RpcError)
                        and error.code == "primary_video_unavailable"
                        else "error",
                        error.code
                        if isinstance(error, RpcError)
                        else type(error).__name__,
                    )
                    logger.warning(
                        "Driving connection ended (%s): %s",
                        type(error).__name__,
                        error.code
                        if isinstance(error, RpcError)
                        else type(error).__name__,
                    )
                finally:
                    self.connection = None
                    self.epoch = None
                    self.armed = False
                    if self._connection_status[0]:
                        self._report_connection(False, "disconnected")
                await asyncio.sleep(self.settings.network.reconnect_delay_s)
        finally:
            self._generation = None
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            self.latest = None
            self._report_connection(False, "disconnected")
            self._status("disconnected")

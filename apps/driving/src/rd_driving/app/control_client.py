"""Send fresh operator inputs on an authenticated, automatically paired data channel."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import replace
from uuid import uuid4

from rd_core.control import is_newer
from rd_core.logging import SampledControlLog, get_logger
from rd_core.samples import ControlInput
from rd_core.settings import DrivingSettings
from rd_core.wire import (
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Telemetry,
    VehicleStatusReply,
    VehicleStatusRequest,
    Window,
    conservative_age_ms,
    parse_control_route,
)
from rd_quic.client import registered_connection
from rd_quic.framing import RpcError

logger = get_logger(__name__)

# Pin the contract implemented by this application even if only the transport is upgraded.
DRIVING_CONTROL_REVISION = "direct-control/5"


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
    ):
        self.settings, self.samples = settings, samples
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
        self._route_revision = 0
        self._route_principal = None
        self._stop_deadline = None
        self._windows = OrderedDict()
        self._last_window = None
        self._sent = OrderedDict()
        self._last_ack = None
        self._last_telemetry = None
        self._last_vehicle_status = None
        self._feedback_requests = OrderedDict()
        self._feedback_request_seq = 0
        self._last_execution = None
        self._seq = 0
        self._tx_log = SampledControlLog(logger)
        self._ack_log = SampledControlLog(logger)
        self.state = "idle"
        self._connection_status = (False, "")
        self.control_requested = False
        self._fresh_after = 0.0
        self._needs_recovery_neutral = False
        self._target_principal = None
        self._restart_requested = False
        self._status_detail = ""
        self._control_changed = asyncio.Event()

    def _report_connection(self, online, detail=""):
        status = (online, detail)
        if status != self._connection_status:
            self._connection_status = status
            if self.on_connection_status is not None:
                self.on_connection_status(*status)

    def _status(self, state, detail=""):
        if (state, detail) == (self.state, self._status_detail):
            return
        logger.info("Driving control state=%s session=%s epoch=%s detail=%r",
                    state, self.settings.session, self.epoch, detail)
        self.state, self._status_detail = state, detail
        if self.on_status is not None:
            self.on_status(state, detail)

    async def _collect(self):
        while True:
            self.latest = await self.samples.get()

    def _fresh(self):
        return isinstance(self.latest, ControlInput) and self.latest.fresh(
            self.settings.control.input_timeout_ms / 1000
        )

    def request_start_control(self):
        if self.control_requested:
            return False
        self.control_requested = True
        self._target_principal = self._route_principal
        self._needs_recovery_neutral = False
        self._fresh_after = time.monotonic()
        self._status("starting" if self.connection else "waiting_channel")
        self._control_changed.set()
        return True

    def _forget_stream(self):
        self.epoch = self._route_principal = None
        self._windows.clear()
        self._sent.clear()
        self._last_window = self._last_ack = self._last_telemetry = None
        self._last_execution = None
        self._seq = 0
        self.armed = False
        self._fresh_after = time.monotonic()

    def _stop_stream(self):
        # A pairing event can precede registration. No command has gone out on
        # this transport yet, so retain the staged route for a future Start.
        if self.connection is None:
            return
        epoch = self.epoch
        self._forget_stream()
        if epoch is None or not self.connection.protocol.rpc_ready:
            return
        protocol = self.connection.protocol
        protocol.discard_pending_datagrams()
        try:
            protocol.notify("stop_control", {"epoch": str(epoch)})
            self._stop_deadline = time.monotonic() + self.settings.network.register_timeout_s
        except (ConnectionError, RpcError):
            protocol.close(error_code=0x105, reason_phrase="stop_failed")

    def request_stop_control(self):
        self._restart_requested = False
        if not self.control_requested:
            return False
        self.control_requested = self._needs_recovery_neutral = False
        self._target_principal = None
        self._stop_stream()
        self._status("idle")
        self._control_changed.set()
        return True

    def prepare_transport_restart(self):
        self._restart_requested = self.control_requested
        if self._sent:
            self._needs_recovery_neutral = True
        self._stop_stream()
        if self.control_requested:
            self._status("waiting_channel")

    def _disconnected(self, generation, protocol):
        if generation is self._generation:
            self._transport_lost()

    def _transport_lost(self):
        if self._sent:
            self._needs_recovery_neutral = True
        self._forget_stream()
        self._feedback_requests.clear()
        self._stop_deadline = None
        if self.control_requested:
            self._status("waiting_channel")
        self._report_connection(False, "disconnected")
        self._control_changed.set()

    def _event(self, generation, method, params):
        if generation is not self._generation:
            return
        if method != "control_route":
            raise RpcError("unexpected_control_event")
        try:
            revision, epoch, principal = parse_control_route(params)
        except ValueError:
            raise RpcError("invalid_control_route") from None
        if revision <= self._route_revision:
            return
        self._route_revision = revision
        self._stop_deadline = None
        if self._sent:
            self._needs_recovery_neutral = True
        self._forget_stream()
        self.epoch, self._route_principal = epoch, principal
        if self.control_requested and principal is not None:
            if self._target_principal not in (None, principal):
                self.control_requested = False
                self._target_principal = None
                self._status("vehicle_changed")
            else:
                self._target_principal = principal
        self._control_changed.set()

    def _datagram(self, generation, data):
        if generation is not self._generation or self.connection is None:
            return
        try:
            packet = Packet.decode(data)
        except (ValueError, ConnectionError):
            return
        if packet.session != self.settings.session:
            return
        if isinstance(packet.payload, VehicleStatusReply):
            self._receive_vehicle_status(packet)
            return
        if packet.epoch != self.epoch:
            return
        if isinstance(packet.payload, Window):
            if is_newer(packet.seq, self._last_window):
                self._last_window = packet.seq
                self._windows[packet.payload.token] = time.monotonic()
                while len(self._windows) > 32:
                    self._windows.popitem(last=False)
            return
        if not self.control_requested:
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
                self.request_stop_control()
                self._status("paused", "execution_failed")
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
            # Compatibility for measurement callers. Only an actual execution sets this flag.
            self.armed = True
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
        reply = packet.payload
        sent_at = self._feedback_requests.pop(reply.request_id, None)
        if sent_at is None:
            return
        # The request was sent before the vehicle measured sample_age_ms.
        # Charging the full round trip bounds the sample's age at delivery.
        deadline = sent_at + (self.settings.feedback.max_age_ms - reply.sample_age_ms) / 1000
        if time.monotonic() >= deadline:
            return
        self._last_vehicle_status = packet.seq
        self.on_vehicle_status(replace(packet, payload=reply.status), deadline)

    async def _request_feedback(self, connection):
        period = 1 / self.settings.feedback.request_hz
        budget = self.settings.feedback.max_age_ms / 1000
        while connection.protocol.rpc_ready:
            now = time.monotonic()
            while self._feedback_requests and (
                now >= next(iter(self._feedback_requests.values())) + budget
            ):
                self._feedback_requests.popitem(last=False)
            request_id = uuid4().bytes
            packet = Packet(
                self.settings.session, connection.status_stream,
                self._feedback_request_seq, 0, VehicleStatusRequest(request_id),
            )
            try:
                sent = connection.protocol.send_datagram(packet.encode(), best_effort=True)
            except ConnectionError:
                return
            if sent:
                self._feedback_requests[request_id] = now
                self._feedback_request_seq = (self._feedback_request_seq + 1) & 0xFFFFFFFF
            await asyncio.sleep(period)

    def _sample_window(self, sample, now):
        # Use USB capture time even when processing or queue delivery was delayed.
        for token, received in reversed(self._windows.items()):
            if (
                received < sample.sampled_at
                and now - received < self.connection.max_control_age_ms / 1000
            ):
                return token
        return None

    def _send_latest(self, now):
        if self.epoch is None:
            self._status("waiting_vehicle")
            return
        if self._sent and (
            not self._fresh()
            or now - self._last_execution >= self.settings.control.command_timeout_ms / 1000
        ):
            reason = "input_expired" if not self._fresh() else "execution_confirmation_expired"
            self._needs_recovery_neutral = True
            self._stop_stream()
            self._status("retrying", reason)
            return
        sample = self.latest
        if not self._fresh() or sample.sampled_at <= self._fresh_after:
            self._status("waiting_input")
            return
        neutral = sample.throttle == 0 and sample.steering == 0
        if self._needs_recovery_neutral and not neutral:
            # A held accelerator/steering value must not restart motion following an outage.
            if sample.brake <= 0:
                self._status("waiting_neutral")
                return
            # Forward the brake immediately without passing the held motion axes
            # or disarming the neutral requirement for subsequent motion.
            sample = replace(sample, throttle=0, steering=0)
        token = self._sample_window(sample, now)
        if token is None:
            if not self._sent:
                self._status("waiting_control_window")
            return
        # Keep the original ACK budget anchored to the first send, then the last
        # confirmed sample. Additional sends never extend this deadline on their own.
        if not self._sent:
            self._last_execution = now
        packet = Packet(self.settings.session, self.epoch, self._seq, sample.timestamp_ms,
                        Control(sample.throttle, sample.brake, sample.steering, token))
        self._sent[self._seq] = sample
        while len(self._sent) > 64:
            self._sent.popitem(last=False)
        self.connection.protocol.send_datagram(packet.encode())
        self._seq = (self._seq + 1) & 0xffffffff
        if neutral:
            self._needs_recovery_neutral = False
        self._status("active", self._route_principal)
        self._tx_log.write(
            packet.epoch,
            "Command sent session=%s epoch=%s seq=%d throttle=%.3f brake=%.3f steering=%.3f",
            packet.session, packet.epoch, packet.seq, sample.throttle, sample.brake,
            sample.steering, all_zero=sample.throttle == sample.brake == sample.steering == 0,
        )

    async def _run_registered(self):
        protocol = self.connection.protocol
        period = 1 / self.settings.control.send_hz
        while protocol.rpc_ready:
            now = time.monotonic()
            if self._stop_deadline is not None and now >= self._stop_deadline:
                protocol.close(error_code=0x105, reason_phrase="stop_unconfirmed")
                return
            if self.control_requested:
                self._send_latest(now)
            elif self._stop_deadline is None:
                self._control_changed.clear()
                await self._control_changed.wait()
                continue
            await asyncio.sleep(max(0, period - (time.monotonic() - now)))

    async def run(self):
        self._restart_requested = False
        collector = asyncio.create_task(self._collect(), name="driving-input")
        try:
            while True:
                generation = self._generation = object()
                self._route_revision = 0
                try:
                    self._report_connection(False, "connecting")
                    async with registered_connection(
                        self.settings.network,
                        self.settings.credentials,
                        self.settings.control,
                        session=self.settings.session,
                        role="driving",
                        revision=DRIVING_CONTROL_REVISION,
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
                        self._feedback_request_seq = 0
                        feedback = None
                        if self.on_vehicle_status is not None:
                            feedback = asyncio.create_task(
                                self._request_feedback(connection), name="driving-feedback"
                            )
                        try:
                            self._report_connection(True, "registered")
                            await self._run_registered()
                        finally:
                            if feedback is not None:
                                feedback.cancel()
                                await asyncio.gather(feedback, return_exceptions=True)
                            self._feedback_requests.clear()
                except (ConnectionError, OSError, TimeoutError, RpcError) as error:
                    if isinstance(error, RpcError) and error.code in {
                        "access_denied", "role_denied", "protocol_upgrade_required",
                    } and self.control_requested:
                        self.request_stop_control()
                        self._status("paused", error.code)
                    if isinstance(error, RpcError) and error.code == "driver_already_connected" and self.control_requested:
                        self._status("channel_busy")
                    self._report_connection(
                        False, error.code if isinstance(error, RpcError) else "disconnected"
                    )
                    logger.warning(
                        "Driving connection ended (%s): %s",
                        type(error).__name__,
                        error.code
                        if isinstance(error, RpcError)
                        else type(error).__name__,
                    )
                finally:
                    self._generation = None
                    self.connection = None
                    if self._connection_status[0] or self.epoch is not None:
                        self._transport_lost()
                await asyncio.sleep(self.settings.network.reconnect_delay_s)
        finally:
            self._generation = None
            if not self._restart_requested:
                self.request_stop_control()
            self._restart_requested = False
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            self.latest = None
            self._report_connection(False, "disconnected")

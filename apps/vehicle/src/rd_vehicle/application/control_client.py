"""Vehicle-side v2 lease adapter around the independent actuator supervisor."""

import asyncio
import secrets
import time
from collections import OrderedDict
from uuid import UUID

from rd_core.control import Command, ControlState, ControlSupervisor, is_newer
from rd_core.logging import SampledControlLog, get_logger
from rd_core.settings import VehicleSettings
from rd_core.wire import (
    CONTROL_REVISION,
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Window,
    conservative_age_ms,
)
from rd_quic.client import registered_connection
from rd_quic.framing import RpcError

from rd_vehicle.shutdown import ComponentCleanupError

logger = get_logger(__name__)


class VehicleControlClient:
    def __init__(
        self,
        settings: VehicleSettings,
        controller,
        *,
        on_status=None,
        independent_stop=None,
    ):
        self.settings, self.controller = settings, controller
        self.on_status, self.independent_stop = on_status, independent_stop
        self.supervisor = None
        self.connection = None
        self.epoch = None
        self._generation = None
        self._last_seq = None
        self._windows = OrderedDict()
        self._window_seq = 0
        self._feedback_seq = 0
        self._last_feedback_sample = None
        self._rx_log = SampledControlLog(logger)
        self._window_drop_log = SampledControlLog(logger)
        self._lease_deadline = 0
        self._lease_ttl_s = 0
        self._loop = None
        self.state = "disconnected"

    def _status(self, state, detail=""):
        if self.state != state:
            logger.info(
                "Vehicle control state=%s session=%s epoch=%s",
                state,
                self.settings.session,
                self.epoch,
            )
        self.state = state
        if self.on_status is not None:
            self.on_status(state, detail)

    def _retire(self, reason, *, notify=True):
        epoch, self.epoch = self.epoch, None
        self._windows.clear()
        if epoch is not None:
            self.supervisor.stop(reason, epoch=str(epoch))
            logger.info(
                "Vehicle lease ended session=%s epoch=%s reason=%s",
                self.settings.session,
                epoch,
                reason,
            )
            if (
                notify
                and self.connection is not None
                and self.connection.protocol.rpc_ready
            ):
                try:
                    self.connection.protocol.notify("release", {"epoch": str(epoch)})
                except (ConnectionError, RpcError):
                    self.connection.protocol.close(
                        error_code=0x105, reason_phrase="release_failed"
                    )
        self._status(
            "registered"
            if self.connection is not None and self.connection.protocol.rpc_ready
            else "disconnected",
            reason,
        )

    async def _request(self, generation, method, params):
        if generation is not self._generation or self.connection is None:
            raise RpcError("vehicle_not_ready")
        if method != "prepare_lease" or set(params) != {
            "session",
            "epoch",
            "lease_ttl_ms",
            "max_control_age_ms",
            "max_clock_skew_ms",
        }:
            raise RpcError("unsupported_method")
        if params["session"] != self.settings.session or self.epoch is not None:
            raise RpcError("vehicle_not_ready")
        try:
            epoch = UUID(params["epoch"])
        except (ValueError, TypeError, AttributeError):
            raise RpcError("invalid_epoch") from None
        if epoch.int == 0 or any(
            type(params[key]) is not int
            for key in ("lease_ttl_ms", "max_control_age_ms", "max_clock_skew_ms")
        ):
            raise RpcError("invalid_lease")
        if (
            not 0
            <= params["max_clock_skew_ms"]
            < params["max_control_age_ms"]
            < params["lease_ttl_ms"]
            or params["max_clock_skew_ms"] != self.connection.max_clock_skew_ms
        ):
            raise RpcError("invalid_lease_limits")
        try:
            self.supervisor.begin_session(str(epoch))
        except (RuntimeError, ValueError):
            raise RpcError("device_not_ready") from None
        self.epoch, self._last_seq = epoch, None
        self._lease_ttl_s = params["lease_ttl_ms"] / 1000
        self._lease_deadline = time.monotonic() + self._lease_ttl_s
        self._status("waiting_neutral")
        return {"ready": True, "epoch": str(epoch)}

    def _event(self, generation, method, params):
        if generation is not self._generation:
            return
        if method != "lease_revoked" or set(params) != {"epoch", "reason"}:
            raise RpcError("unexpected_lease_event")
        if str(self.epoch) == params["epoch"]:
            self._retire("lease revoked", notify=False)

    def _datagram(self, generation, data):
        if (
            generation is not self._generation
            or self.connection is None
            or self.epoch is None
        ):
            return
        try:
            packet = Packet.decode(data)
        except ValueError:
            return
        if (
            packet.session != self.settings.session
            or packet.epoch != self.epoch
            or not isinstance(packet.payload, Control)
            or not is_newer(packet.seq, self._last_seq)
        ):
            return
        deadline = self._windows.get(packet.payload.window)
        if deadline is None or time.monotonic() >= deadline:
            self._window_drop_log.write(
                packet.epoch,
                "Command dropped session=%s epoch=%s seq=%d reason=window_unavailable",
                packet.session,
                packet.epoch,
                packet.seq,
            )
            return
        if time.monotonic() >= self._lease_deadline:
            self._retire("lease expired before command")
            return
        command = Command(
            str(packet.epoch),
            packet.seq,
            packet.payload.throttle,
            packet.payload.brake,
            packet.payload.steering,
            packet.source_time_ms,
        )
        if self.supervisor.submit(command, deadline=deadline):
            self._last_seq = packet.seq
            self._lease_deadline = (
                deadline - self.connection.max_control_age_ms / 1000 + self._lease_ttl_s
            )
            self._rx_log.write(
                packet.epoch,
                "Command received and accepted session=%s epoch=%s seq=%d throttle=%.3f brake=%.3f steering=%.3f",
                packet.session,
                packet.epoch,
                packet.seq,
                command.throttle,
                command.brake,
                command.steering,
                all_zero=command.throttle == command.brake == command.steering == 0,
            )
        elif self.supervisor.state == ControlState.FAILSAFE:
            self._retire(self.supervisor.reason)

    def _post_result(self, command, executed, detail):
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._result, command, executed, detail)

    def _result(self, command, executed, detail):
        if (
            self.connection is None
            or str(self.epoch) != command.epoch
            or not self.connection.protocol.rpc_ready
        ):
            return
        status = (
            ExecutionStatus.EXECUTED
            if executed
            else ExecutionStatus.NO_CONTROLLER
            if self.controller is None
            else ExecutionStatus.REJECTED
        )
        packet = Packet(
            self.settings.session,
            self.epoch,
            command.seq,
            command.source_timestamp,
            Execution(status, time.time_ns() // 1_000_000),
        )
        try:
            self.connection.protocol.send_datagram(packet.encode())
        except (ConnectionError, ValueError):
            self._retire("execution result delivery failed")
            return
        if executed:
            self._status("active")
        else:
            logger.warning(
                "Vehicle execution failed session=%s epoch=%s seq=%d status=%s",
                packet.session,
                packet.epoch,
                packet.seq,
                status.name,
            )
            self._retire("execution failed")

    def _publish_feedback(self):
        source = getattr(self.controller, "feedback", None)
        if (source is None or self.connection is None
                or not self.connection.protocol.rpc_ready):
            return False
        sample = source.latest()
        if sample is None or sample.revision == self._last_feedback_sample:
            return False
        try:
            conservative_age_ms(
                sample.source_time_ms, time.time_ns() // 1_000_000,
                max_age_ms=min(self.settings.feedback.stale_timeout_ms,
                               self.connection.max_control_age_ms),
                clock_skew_ms=self.connection.max_clock_skew_ms,
            )
            packet = Packet(
                self.settings.session,
                self.connection.status_stream,
                self._feedback_seq,
                sample.source_time_ms,
                sample.status,
            )
            if not self.connection.protocol.send_datagram(packet.encode(), best_effort=True):
                return False
        except (ConnectionError, ValueError):
            return False
        self._last_feedback_sample = sample.revision
        self._feedback_seq = (self._feedback_seq + 1) & 0xFFFFFFFF
        return True

    async def _feedback_watch(self):
        while True:
            self._publish_feedback()
            await asyncio.sleep(1 / self.settings.feedback.send_hz)

    async def _watch(self):
        while True:
            if self.epoch is not None:
                if (
                    self.supervisor.state == ControlState.FAILSAFE
                    or time.monotonic() >= self._lease_deadline
                ):
                    self._retire("local control expired")
                else:
                    now = time.monotonic()
                    while self._windows and next(iter(self._windows.values())) <= now:
                        self._windows.popitem(last=False)
                    token = secrets.token_bytes(16)
                    self._windows[token] = (
                        now + self.connection.max_control_age_ms / 1000
                    )
                    packet = Packet(
                        self.settings.session,
                        self.epoch,
                        self._window_seq,
                        0,
                        Window(token),
                    )
                    self._window_seq = (self._window_seq + 1) & 0xFFFFFFFF
                    while len(self._windows) > 32:
                        self._windows.popitem(last=False)
                    self.connection.protocol.send_datagram(packet.encode())
            await asyncio.sleep(min(self.connection.max_control_age_ms / 4000, 0.05))

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self.supervisor = ControlSupervisor(
            self.controller,
            timeout_s=self.settings.control.command_timeout_ms / 1000,
            independent_stop=self.independent_stop,
            on_result=self._post_result,
        )
        self.supervisor.stop("awaiting authenticated lease")
        try:
            while True:
                generation = self._generation = object()

                def disconnected(protocol, g=generation):
                    if g is self._generation:
                        self._retire("control connection closed", notify=False)

                try:
                    self._status("connecting")
                    async with registered_connection(
                        self.settings.network,
                        self.settings.credentials,
                        self.settings.control,
                        session=self.settings.session,
                        role="vehicle",
                        revision=CONTROL_REVISION,
                        request_handler=lambda method, params, g=generation: (
                            self._request(g, method, params)
                        ),
                        event_handler=lambda method, params, g=generation: self._event(
                            g, method, params
                        ),
                        datagram_handler=lambda data, g=generation: self._datagram(
                            g, data
                        ),
                        on_disconnect=disconnected,
                    ) as connection:
                        self.connection = connection
                        self._feedback_seq, self._last_feedback_sample = 0, None
                        watch = asyncio.create_task(
                            self._watch(), name="vehicle-control-watch"
                        )
                        closed = asyncio.create_task(connection.protocol.wait_closed())
                        feedback = asyncio.create_task(
                            self._feedback_watch(), name="vehicle-feedback"
                        )
                        owned = (watch, closed, feedback)
                        try:
                            self._status("registered")
                            done, _ = await asyncio.wait(
                                owned, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in done:
                                task.result()
                        finally:
                            self._retire("control session ended")
                            for task in owned:
                                task.cancel()
                            await asyncio.gather(*owned, return_exceptions=True)
                except (ConnectionError, OSError, TimeoutError, RpcError) as error:
                    if (
                        isinstance(error, RpcError)
                        and error.code == "protocol_upgrade_required"
                    ):
                        logger.error(
                            "Control protocol revision mismatch; upgrade all applications together"
                        )
                    logger.warning(
                        "Vehicle connection ended (%s): %s",
                        type(error).__name__,
                        error.code
                        if isinstance(error, RpcError)
                        else type(error).__name__,
                    )
                    self._status(
                        "error",
                        error.code
                        if isinstance(error, RpcError)
                        else type(error).__name__,
                    )
                finally:
                    self.connection = None
                await asyncio.sleep(self.settings.network.reconnect_delay_s)
        finally:
            self._generation = None
            self._retire("vehicle control shutdown")
            self.supervisor.stop("vehicle control shutdown")
            clean = await asyncio.to_thread(self.supervisor.close)
            self._loop = None
            self._status("disconnected")
            if not clean:
                raise ComponentCleanupError("actuator worker remains blocked after shutdown")

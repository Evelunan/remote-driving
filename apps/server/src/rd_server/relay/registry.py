"""Authenticated peer bindings and direct, connection-scoped control routes."""

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from rd_core.auth import AuthorizationError, AuthProvider, Principal
from rd_core.control import is_newer
from rd_core.settings import RelaySettings
from rd_core.wire import (
    CONTROL_REVISION,
    MAX_VEHICLE_STATUS_AGE_MS,
    Control,
    Execution,
    Packet,
    StopStatus,
    Telemetry,
    VehicleStatusReply,
    VehicleStatusRequest,
    Window,
    conservative_age_ms,
)
from rd_quic.framing import RpcError


@dataclass
class Binding:
    principal: Principal
    session: int
    status_stream: UUID
    last_status_seq: int | None = None
    next_status_seq: int = 0
    status_requests: OrderedDict[bytes, float] = field(default_factory=OrderedDict)


@dataclass
class ControlRoute:
    session: int
    epoch: UUID
    driver: object
    vehicle: object
    last_control: int | None = None
    last_execution: int | None = None
    last_observations: dict = field(default_factory=dict)
    windows: OrderedDict = field(default_factory=OrderedDict)
    last_window: int | None = None
    samples: OrderedDict = field(default_factory=OrderedDict)


class Registry:
    def __init__(self, auth: AuthProvider, settings: RelaySettings):
        self.auth, self.settings = auth, settings
        self.bindings = {}
        self.vehicles = {}
        self.drivers = {}
        self.routes = {}
        self._revision = 0

    def register(self, peer, params):
        if params.get("revision") != CONTROL_REVISION:
            raise RpcError("protocol_upgrade_required")
        if (
            set(params) != {"token", "session", "role", "revision"}
            or not isinstance(params["role"], str)
            or type(params["session"]) is not int
        ):
            raise RpcError("invalid_registration")
        if peer in self.bindings:
            raise RpcError("already_registered")
        try:
            principal = self.auth.authenticate(params["token"])
            principal.check_session(params["session"], role=params["role"])
        except AuthorizationError:
            raise RpcError("access_denied") from None
        if principal.role not in ("driving", "vehicle"):
            raise RpcError("role_denied")
        self.sweep()
        session = params["session"]
        if principal.role == "vehicle":
            if session in self.vehicles:
                raise RpcError("vehicle_already_connected")
            self.vehicles[session] = peer
        else:
            if session in self.drivers:
                raise RpcError("driver_already_connected")
            self.drivers[session] = peer
        binding = self.bindings[peer] = Binding(principal, session, uuid4())
        self._refresh_route(session)
        if peer not in self.bindings:
            raise RpcError("connection_closed")
        return {
            "revision": CONTROL_REVISION,
            "principal": principal.id,
            "role": principal.role,
            "session": session,
            "server_time_ms": int(time.time() * 1000),
            "max_control_age_ms": self.settings.max_control_age_ms,
            "max_clock_skew_ms": self.settings.max_clock_skew_ms,
            "idle_timeout_s": self.settings.idle_timeout_s,
            "status_stream": str(binding.status_stream),
        }

    def _binding(self, peer, role=None):
        binding = self.bindings.get(peer)
        if binding is None:
            raise RpcError("registration_required")
        try:
            binding.principal.check_session(binding.session, role=role)
        except AuthorizationError:
            raise RpcError("access_denied") from None
        return binding

    def _refresh_route(self, session):
        previous = self.routes.pop(session, None)
        if previous is not None:
            for peer in (previous.driver, previous.vehicle):
                peer.discard_pending_datagrams()
        driver = self.drivers.get(session)
        vehicle = self.vehicles.get(session)
        self._revision += 1
        params = {"revision": self._revision, "epoch": None, "vehicle_principal": None}
        if driver is not None and vehicle is not None:
            route = ControlRoute(session, uuid4(), driver, vehicle)
            self.routes[session] = route
            params.update(epoch=str(route.epoch), vehicle_principal=self.bindings[vehicle].principal.id)
        # A route event may precede its registration reply or a previous event.
        # Receivers use the revision to ignore late events from older routes.
        for peer in (vehicle, driver):
            if peer is not None and peer in self.bindings:
                try:
                    peer.notify("control_route", params)
                except (ConnectionError, RpcError):
                    self.unregister(peer)
                    peer.close(error_code=0x103, reason_phrase="notification_failed")
                    return  # unregister notified the survivor with the newer revision.

    def stop_control(self, peer, params):
        binding = self._binding(peer)
        if set(params) != {"epoch"} or not isinstance(params["epoch"], str):
            raise RpcError("invalid_stop")
        route = self.routes.get(binding.session)
        if route is None or params["epoch"] != str(route.epoch):
            return {"stopped": False}
        # Epoch changes invalidate every queued command, window and receipt.
        self._refresh_route(binding.session)
        return {"stopped": True}

    def unregister(self, peer):
        binding = self.bindings.pop(peer, None)
        if binding is None:
            return
        peers = self.vehicles if binding.principal.role == "vehicle" else self.drivers
        del peers[binding.session]
        driver = self.bindings.get(self.drivers.get(binding.session))
        if driver is not None:
            driver.status_requests.clear()
        self._refresh_route(binding.session)

    def sweep(self):
        for peer, binding in list(self.bindings.items()):
            try:
                binding.principal.check_active()
            except AuthorizationError:
                self.unregister(peer)
                peer.close(error_code=0x103, reason_phrase="credentials_expired")

    def _route_status(self, binding, packet):
        request = isinstance(packet.payload, VehicleStatusRequest)
        role = "driving" if request else "vehicle"
        if (
            binding.principal.role != role
            or packet.session != binding.session
            or packet.epoch != binding.status_stream
            or not is_newer(packet.seq, binding.last_status_seq)
            or (request and packet.source_time_ms != 0)
        ):
            return False
        targets = self.vehicles if request else self.drivers
        target = targets.get(binding.session)
        if target is None:
            return False
        try:
            receiver = self._binding(target, "vehicle" if request else "driving")
            pending = binding.status_requests if request else receiver.status_requests
            request_id = packet.payload.request_id
            now = time.monotonic()
            while pending and next(iter(pending.values())) <= now:
                pending.popitem(last=False)
            if request:
                if request_id in pending:
                    return False
            elif pending.pop(request_id, None) is None:
                return False
            binding.last_status_seq = packet.seq
            report = Packet(
                binding.session, receiver.status_stream, receiver.next_status_seq,
                packet.source_time_ms, packet.payload,
            )
            if target.send_datagram(report.encode(), best_effort=True):
                receiver.next_status_seq = (receiver.next_status_seq + 1) & 0xFFFFFFFF
                if request:
                    # Keep correlations up to the largest supported feedback age.
                    pending[request_id] = now + MAX_VEHICLE_STATUS_AGE_MS / 1000
                    # The maximum supported 50 Hz over 10 seconds needs 500 entries.
                    if len(pending) > 512:
                        pending.popitem(last=False)
                return True
        except (ConnectionError, ValueError, RpcError):
            # Optional status delivery has no effect on control.
            pass
        return False

    def route(self, peer, data):
        try:
            binding = self._binding(peer)
            packet = Packet.decode(data)
        except (ValueError, RpcError):
            return False
        if isinstance(packet.payload, (VehicleStatusRequest, VehicleStatusReply)):
            return self._route_status(binding, packet)
        route = self.routes.get(binding.session)
        if route is None or packet.session != binding.session or packet.epoch != route.epoch:
            return False
        if isinstance(packet.payload, Control):
            if route.driver is not peer or not is_newer(packet.seq, route.last_control):
                return False
            issued = route.windows.get(packet.payload.window)
            if (
                issued is None
                or time.monotonic() >= issued + self.settings.max_control_age_ms / 1000
            ):
                return False
            route.last_control = packet.seq
            route.samples[packet.seq] = (packet.source_time_ms, issued)
            while len(route.samples) > 64:
                route.samples.popitem(last=False)
            target = route.vehicle
        else:
            if route.vehicle is not peer:
                return False
            if isinstance(packet.payload, Window):
                if (
                    not is_newer(packet.seq, route.last_window)
                    or packet.payload.token in route.windows
                ):
                    return False
                route.last_window = packet.seq
                route.windows[packet.payload.token] = time.monotonic()
                while len(route.windows) > 32:
                    route.windows.popitem(last=False)
            elif isinstance(packet.payload, Execution):
                sample = route.samples.get(packet.seq)
                if (
                    sample is None
                    or time.monotonic()
                    >= sample[1] + self.settings.max_control_age_ms / 1000
                    or sample[0] != packet.source_time_ms
                    or not is_newer(packet.seq, route.last_execution)
                ):
                    return False
                route.last_execution = packet.seq
            elif isinstance(packet.payload, (Telemetry, StopStatus)):
                try:
                    conservative_age_ms(
                        packet.source_time_ms,
                        int(time.time() * 1000),
                        max_age_ms=self.settings.max_control_age_ms,
                        clock_skew_ms=self.settings.max_clock_skew_ms,
                    )
                except ValueError:
                    return False
                if not is_newer(packet.seq, route.last_observations.get(packet.kind)):
                    return False
                route.last_observations[packet.kind] = packet.seq
            else:
                return False
            target = route.driver
        try:
            self._binding(target)
            return target.send_datagram(data)
        except (ConnectionError, ValueError, RpcError):
            # Retire a failed destination immediately. Its replacement must
            # register on a different transport before it can receive commands.
            self.unregister(target)
            target.close(error_code=0x103, reason_phrase="forward_failed")
            return False

    def close(self):
        for peer in list(self.bindings):
            self.unregister(peer)

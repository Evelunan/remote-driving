"""Authenticated bindings and exclusive, epoch-scoped control leases."""

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from rd_core.auth import AuthorizationError, AuthProvider, Principal
from rd_core.control import is_newer
from rd_core.settings import RelaySettings
from rd_core.wire import (
    CONTROL_REVISION,
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Window,
    conservative_age_ms,
)
from rd_quic.framing import RpcError


@dataclass(frozen=True)
class Binding:
    principal: Principal
    session: int


@dataclass
class Lease:
    session: int
    epoch: UUID
    driver: object
    vehicle: object
    deadline: float
    ready: bool = False
    armed: bool = False
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
        self.leases = {}

    def register(self, peer, params):
        if params.get("revision") != CONTROL_REVISION:
            raise RpcError("protocol_upgrade_required")
        if (
            set(params) != {"token", "session", "role", "revision"}
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
        self.bindings[peer] = Binding(principal, session)
        return {
            "revision": CONTROL_REVISION,
            "principal": principal.id,
            "role": principal.role,
            "session": session,
            "server_time_ms": int(time.time() * 1000),
            "max_control_age_ms": self.settings.max_control_age_ms,
            "max_clock_skew_ms": self.settings.max_clock_skew_ms,
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

    async def acquire(self, peer):
        self.sweep()
        binding = self._binding(peer, "driving")
        if binding.session in self.leases:
            raise RpcError("lease_busy")
        vehicle = self.vehicles.get(binding.session)
        if vehicle is None:
            raise RpcError("vehicle_unavailable")
        self._binding(vehicle, "vehicle")
        lease = Lease(
            binding.session,
            uuid4(),
            peer,
            vehicle,
            time.monotonic() + self.settings.register_timeout_s,
        )
        # Reserve before suspending: a concurrent applicant cannot win the same lease.
        self.leases[binding.session] = lease
        parameters = {
            "session": lease.session,
            "epoch": str(lease.epoch),
            "lease_ttl_ms": round(self.settings.lease_ttl_s * 1000),
            "max_control_age_ms": self.settings.max_control_age_ms,
            "max_clock_skew_ms": self.settings.max_clock_skew_ms,
        }
        try:
            result = await vehicle.request(
                "prepare_lease", parameters, timeout_s=self.settings.register_timeout_s
            )
            if (
                result != {"ready": True, "epoch": str(lease.epoch)}
                or type(result.get("ready")) is not bool
            ):
                raise RpcError("vehicle_not_ready")
            self.sweep()
            if self.leases.get(binding.session) is not lease:
                raise RpcError("lease_revoked")
            self._binding(peer, "driving")
            self._binding(vehicle, "vehicle")
            lease.ready = True
            lease.deadline = time.monotonic() + self.settings.lease_ttl_s
            return {
                **parameters,
                "neutral_required": True,
                "vehicle_principal": self._binding(vehicle, "vehicle").principal.id,
            }
        except BaseException:
            self.revoke(binding.session, "prepare_failed", epoch=lease.epoch)
            raise

    def _notify(self, peer, method, params):
        try:
            peer.notify(method, params)
        except (RpcError, ConnectionError):
            # Revocation is already committed before this notification. A
            # congested/disconnected peer must not keep its authority alive.
            peer.close(error_code=0x103, reason_phrase="notification_failed")

    def revoke(self, session, reason, *, epoch=None):
        lease = self.leases.get(session)
        if lease is None or epoch is not None and lease.epoch != epoch:
            return False
        del self.leases[session]
        for peer in (lease.vehicle, lease.driver):
            self._notify(
                peer, "lease_revoked", {"epoch": str(lease.epoch), "reason": reason}
            )
        return True

    def release(self, peer, params):
        binding = self._binding(peer)
        if set(params) != {"epoch"} or not isinstance(params["epoch"], str):
            raise RpcError("invalid_release")
        lease = self.leases.get(binding.session)
        if lease is None or str(lease.epoch) != params["epoch"]:
            return {"revoked": False}
        if peer is not lease.driver and peer is not lease.vehicle:
            raise RpcError("lease_owner_required")
        return {"revoked": self.revoke(binding.session, "released", epoch=lease.epoch)}

    def unregister(self, peer):
        binding = self.bindings.pop(peer, None)
        if binding is None:
            return
        if self.vehicles.get(binding.session) is peer:
            del self.vehicles[binding.session]
        lease = self.leases.get(binding.session)
        if lease is not None and (lease.driver is peer or lease.vehicle is peer):
            self.revoke(binding.session, "connection_closed", epoch=lease.epoch)

    def sweep(self):
        for peer, binding in list(self.bindings.items()):
            try:
                binding.principal.check_active()
            except AuthorizationError:
                self.unregister(peer)
                peer.close(error_code=0x103, reason_phrase="credentials_expired")
        now = time.monotonic()
        for session, lease in list(self.leases.items()):
            if now >= lease.deadline:
                self.revoke(session, "lease_expired", epoch=lease.epoch)

    def route(self, peer, data):
        try:
            binding = self._binding(peer)
            packet = Packet.decode(data)
        except (ValueError, RpcError):
            return False
        lease = self.leases.get(binding.session)
        if lease is not None and time.monotonic() >= lease.deadline:
            self.revoke(binding.session, "lease_expired", epoch=lease.epoch)
            return False
        if (
            lease is None
            or not lease.ready
            or packet.session != binding.session
            or packet.epoch != lease.epoch
        ):
            return False
        if isinstance(packet.payload, Control):
            if lease.driver is not peer or not is_newer(packet.seq, lease.last_control):
                return False
            neutral = packet.payload.throttle == 0 and packet.payload.steering == 0
            if not lease.armed and not neutral:
                return False
            issued = lease.windows.get(packet.payload.window)
            if (
                issued is None
                or time.monotonic() >= issued + self.settings.max_control_age_ms / 1000
            ):
                return False
            lease.last_control = packet.seq
            lease.deadline = issued + self.settings.lease_ttl_s
            lease.samples[packet.seq] = (packet.source_time_ms, neutral, issued)
            while len(lease.samples) > 64:
                lease.samples.popitem(last=False)
            target = lease.vehicle
        else:
            if lease.vehicle is not peer:
                return False
            if isinstance(packet.payload, Window):
                if (
                    not is_newer(packet.seq, lease.last_window)
                    or packet.payload.token in lease.windows
                ):
                    return False
                lease.last_window = packet.seq
                lease.windows[packet.payload.token] = time.monotonic()
                while len(lease.windows) > 32:
                    lease.windows.popitem(last=False)
            elif isinstance(packet.payload, Execution):
                sample = lease.samples.get(packet.seq)
                if (
                    sample is None
                    or time.monotonic()
                    >= sample[2] + self.settings.max_control_age_ms / 1000
                    or sample[0] != packet.source_time_ms
                    or not is_newer(packet.seq, lease.last_execution)
                ):
                    return False
                lease.last_execution = packet.seq
                if sample[1] and packet.payload.status == ExecutionStatus.EXECUTED:
                    lease.armed = True
                if packet.payload.status != ExecutionStatus.EXECUTED:
                    self.revoke(binding.session, "execution_failed", epoch=lease.epoch)
                    return False
            else:
                try:
                    conservative_age_ms(
                        packet.source_time_ms,
                        int(time.time() * 1000),
                        max_age_ms=self.settings.max_control_age_ms,
                        clock_skew_ms=self.settings.max_clock_skew_ms,
                    )
                except ValueError:
                    return False
                if not is_newer(packet.seq, lease.last_observations.get(packet.kind)):
                    return False
                lease.last_observations[packet.kind] = packet.seq
            target = lease.driver
        try:
            self._binding(target)
            target.send_datagram(data)
        except (ConnectionError, ValueError, RpcError):
            self.revoke(binding.session, "forward_failed", epoch=lease.epoch)
            return False
        return True

    def close(self):
        for session in list(self.leases):
            self.revoke(session, "server_shutdown")
        self.bindings.clear()
        self.vehicles.clear()

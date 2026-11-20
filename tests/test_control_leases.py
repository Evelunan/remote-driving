import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rd_core.auth import AuthProvider
from rd_core.settings import AuthSettings, PrincipalSettings, RelaySettings
from rd_core.wire import (
    CONTROL_REVISION,
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Telemetry,
    Window,
)
from rd_quic.framing import RpcError
from rd_server.relay.registry import Registry


class Peer:
    def __init__(self, registry):
        self.registry = registry
        self.datagrams, self.events = [], []
        self.closed = False
        self.prepare_gate = None
        self.preparations = []

    async def request(self, method, params, **kwargs):
        assert method == "prepare_lease"
        self.preparations.append(params)
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        return {"ready": True, "epoch": params["epoch"]}

    def send_datagram(self, data):
        self.datagrams.append(Packet.decode(data))

    def notify(self, method, params):
        if self.closed:
            raise ConnectionError
        self.events.append((method, params))

    def close(self, **kwargs):
        self.closed = True
        self.registry.unregister(self)


@pytest.fixture
def setup(monkeypatch):
    clock = SimpleNamespace(mono=1000.0, wall=2000000.0)
    monkeypatch.setattr(
        "rd_server.relay.registry.time",
        SimpleNamespace(monotonic=lambda: clock.mono, time=lambda: clock.wall),
    )
    records = [
        PrincipalSettings(name, role, name.upper(), (42,), ("video",))
        for name, role in (
            ("vehicle", "vehicle"),
            ("driver", "driving"),
            ("other", "driving"),
            ("viewer", "viewer"),
        )
    ]
    tokens = {record.token_env: record.id + "x" * 40 for record in records}
    registry = Registry(
        AuthProvider(AuthSettings(tuple(records)), environ=tokens), RelaySettings()
    )
    peers = {record.id: Peer(registry) for record in records}
    for record in records[:3]:
        registry.register(
            peers[record.id],
            {
                "revision": CONTROL_REVISION,
                "token": tokens[record.token_env],
                "role": record.role,
                "session": 42,
            },
        )
    return registry, peers, clock, tokens


def control(lease, clock, seq=0, throttle=0, *, age_ms=0):
    return Packet(
        42,
        lease.epoch,
        seq,
        int(clock.wall * 1000) - age_ms,
        Control(throttle, 0, 0, bytes(16)),
    )


def execution(packet, status=ExecutionStatus.EXECUTED):
    return replace(packet, payload=Execution(status, packet.source_time_ms + 1))


def test_registration_checks_identity_role_scope_and_duplicate_vehicle(setup):
    registry, peers, _, tokens = setup
    for params in (
        {
            "revision": CONTROL_REVISION,
            "token": tokens["DRIVER"],
            "role": "vehicle",
            "session": 42,
        },
        {
            "revision": CONTROL_REVISION,
            "token": tokens["DRIVER"],
            "role": "driving",
            "session": 99,
        },
        {
            "revision": CONTROL_REVISION,
            "token": "invalid",
            "role": "driving",
            "session": 42,
        },
        {
            "revision": CONTROL_REVISION,
            "token": tokens["DRIVER"],
            "role": "driving",
            "session": True,
        },
    ):
        with pytest.raises(RpcError):
            registry.register(Peer(registry), params)
    with pytest.raises(RpcError, match="role_denied"):
        registry.register(
            peers["viewer"],
            {
                "revision": CONTROL_REVISION,
                "token": tokens["VIEWER"],
                "role": "viewer",
                "session": 42,
            },
        )
    with pytest.raises(RpcError, match="vehicle_already_connected"):
        registry.register(
            Peer(registry),
            {
                "revision": CONTROL_REVISION,
                "token": tokens["VEHICLE"],
                "role": "vehicle",
                "session": 42,
            },
        )
    assert registry.vehicles[42] is peers["vehicle"]
    assert peers["viewer"] not in registry.bindings


@pytest.mark.asyncio
async def test_exclusive_reservation_and_late_prepare_cannot_revoke_new_owner(setup):
    registry, peers, _, _ = setup
    gate = peers["vehicle"].prepare_gate = asyncio.Event()
    pending = asyncio.create_task(registry.acquire(peers["driver"]))
    await asyncio.sleep(0)
    old_epoch = registry.leases[42].epoch
    with pytest.raises(RpcError, match="lease_busy"):
        await registry.acquire(peers["other"])
    registry.unregister(peers["driver"])
    peers["vehicle"].prepare_gate = None
    new = await registry.acquire(peers["other"])
    gate.set()
    with pytest.raises(RpcError, match="lease_revoked"):
        await pending
    assert new["epoch"] == str(registry.leases[42].epoch) != str(old_epoch)
    registry.unregister(peers["driver"])
    assert registry.leases[42].driver is peers["other"]


@pytest.mark.asyncio
async def test_cancelled_prepare_revokes_reservation_and_notifies_vehicle(setup):
    registry, peers, _, _ = setup
    peers["vehicle"].prepare_gate = asyncio.Event()
    pending = asyncio.create_task(registry.acquire(peers["driver"]))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not registry.leases
    assert peers["vehicle"].events[-1][1]["reason"] == "prepare_failed"


@pytest.mark.asyncio
async def test_neutral_execution_gates_motion_and_binding_controls_both_directions(
    setup,
):
    registry, peers, clock, _ = setup
    await registry.acquire(peers["driver"])
    lease = registry.leases[42]
    assert registry.route(
        peers["vehicle"], Packet(42, lease.epoch, 0, 0, Window(bytes(16))).encode()
    )
    packet = control(lease, clock)
    assert not registry.route(
        peers["driver"], control(lease, clock, throttle=0.5).encode()
    )
    assert not registry.route(peers["other"], packet.encode())
    assert not registry.route(peers["vehicle"], packet.encode())
    assert not registry.route(peers["driver"], replace(packet, session=99).encode())
    assert registry.route(peers["driver"], packet.encode())
    assert not registry.route(
        peers["driver"], control(lease, clock, seq=1, throttle=0.5).encode()
    )
    assert not registry.route(peers["driver"], execution(packet).encode())
    assert registry.route(peers["vehicle"], execution(packet).encode())
    assert lease.armed
    assert registry.route(
        peers["driver"], control(lease, clock, seq=1, throttle=0.5).encode()
    )
    assert peers["vehicle"].datagrams[-1].payload.throttle == 0.5
    assert peers["driver"].datagrams[-1].payload.status == ExecutionStatus.EXECUTED


@pytest.mark.asyncio
async def test_duplicates_stale_samples_and_invalid_acks_do_not_extend_lease(setup):
    registry, peers, clock, _ = setup
    await registry.acquire(peers["driver"])
    lease = registry.leases[42]
    assert registry.route(
        peers["vehicle"], Packet(42, lease.epoch, 0, 0, Window(bytes(16))).encode()
    )
    packet = control(lease, clock, seq=0xFFFFFFFF)
    assert registry.route(peers["driver"], packet.encode())
    assert registry.route(peers["vehicle"], execution(packet).encode())
    packet = control(lease, clock, seq=0, throttle=0.5)
    assert registry.route(peers["driver"], packet.encode())
    deadline = lease.deadline
    for invalid in (
        packet,
        replace(packet, seq=0x80000000),
        replace(packet, seq=1, payload=replace(packet.payload, window=b"x" * 16)),
    ):
        assert not registry.route(peers["driver"], invalid.encode())
    for invalid in (
        replace(execution(packet), seq=99),
        replace(execution(packet), source_time_ms=packet.source_time_ms - 1),
    ):
        assert not registry.route(peers["vehicle"], invalid.encode())
    measurement = replace(packet, payload=Telemetry(1.0, 0.5))
    assert registry.route(peers["vehicle"], measurement.encode())
    assert not registry.route(peers["vehicle"], measurement.encode())
    assert lease.deadline == deadline
    clock.mono += registry.settings.max_control_age_ms / 1000
    assert not registry.route(peers["driver"], replace(packet, seq=1).encode())
    assert lease.deadline == deadline
    clock.mono = deadline
    registry.sweep()
    assert not registry.leases
    assert peers["vehicle"].events[-1][1]["reason"] == "lease_expired"


@pytest.mark.asyncio
async def test_release_checks_owner_and_epoch_and_execution_failure_revokes(setup):
    registry, peers, clock, _ = setup
    old = await registry.acquire(peers["driver"])
    with pytest.raises(RpcError, match="lease_owner_required"):
        registry.release(peers["other"], {"epoch": old["epoch"]})
    assert registry.release(peers["driver"], {"epoch": old["epoch"]}) == {
        "revoked": True
    }
    new = await registry.acquire(peers["driver"])
    assert old["epoch"] != new["epoch"]
    assert registry.release(peers["driver"], {"epoch": old["epoch"]}) == {
        "revoked": False
    }
    lease = registry.leases[42]
    assert registry.route(
        peers["vehicle"], Packet(42, lease.epoch, 0, 0, Window(bytes(16))).encode()
    )
    packet = control(lease, clock)
    assert registry.route(peers["driver"], packet.encode())
    assert not registry.route(
        peers["vehicle"], execution(packet, ExecutionStatus.REJECTED).encode()
    )
    assert not registry.leases
    assert peers["driver"].events[-1][1]["reason"] == "execution_failed"


@pytest.mark.asyncio
async def test_credential_expiry_and_vehicle_disconnect_revoke_authority(setup):
    registry, peers, _, _ = setup
    await registry.acquire(peers["driver"])
    binding = registry.bindings[peers["driver"]]
    registry.bindings[peers["driver"]] = replace(
        binding, principal=replace(binding.principal, expires_at=time.time() - 1)
    )
    registry.sweep()
    assert peers["driver"].closed and not registry.leases
    await registry.acquire(peers["other"])
    registry.unregister(peers["vehicle"])
    assert not registry.leases and not registry.vehicles
    with pytest.raises(RpcError, match="vehicle_unavailable"):
        await registry.acquire(peers["other"])


@pytest.mark.asyncio
async def test_packet_after_deadline_cannot_resurrect_lease_before_maintenance(setup):
    registry, peers, clock, _ = setup
    await registry.acquire(peers["driver"])
    lease = registry.leases[42]
    assert registry.route(
        peers["vehicle"], Packet(42, lease.epoch, 0, 0, Window(bytes(16))).encode()
    )
    clock.mono = lease.deadline
    assert not registry.route(peers["driver"], control(lease, clock).encode())
    assert not registry.leases


@pytest.mark.asyncio
async def test_expired_destination_is_not_forwarded_to_before_maintenance(setup):
    registry, peers, clock, _ = setup
    await registry.acquire(peers["driver"])
    lease = registry.leases[42]
    assert registry.route(
        peers["vehicle"], Packet(42, lease.epoch, 0, 0, Window(bytes(16))).encode()
    )
    binding = registry.bindings[peers["vehicle"]]
    registry.bindings[peers["vehicle"]] = replace(
        binding, principal=replace(binding.principal, expires_at=time.time() - 1)
    )
    assert not registry.route(peers["driver"], control(lease, clock).encode())
    assert not peers["vehicle"].datagrams and not registry.leases

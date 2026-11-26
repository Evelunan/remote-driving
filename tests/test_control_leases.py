import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

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
    VehicleStatus,
    Window,
)
from rd_quic.framing import RpcError
from rd_server.relay.registry import Registry


class Peer:
    def __init__(self, registry):
        self.registry = registry
        self.datagrams, self.events = [], []
        self.closed = False
        self.discarded = 0
        self.datagram_error = None
        self.prepare_gate = None
        self.preparations = []

    async def request(self, method, params, **kwargs):
        assert method == "prepare_lease"
        self.preparations.append(params)
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        return {"ready": True, "epoch": params["epoch"]}

    def send_datagram(self, data, *, best_effort=False):
        if self.datagram_error is not None:
            raise self.datagram_error
        self.datagrams.append(Packet.decode(data))
        return True

    def notify(self, method, params):
        if self.closed:
            raise ConnectionError
        self.events.append((method, params))

    def discard_pending_datagrams(self):
        self.discarded += 1

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
        PrincipalSettings(name, role, name.upper(), (session,), ("video",))
        for name, role, session in (
            ("vehicle", "vehicle", 42),
            ("driver", "driving", 42),
            ("other", "driving", 42),
            ("outside", "driving", 99),
            ("viewer", "viewer", 42),
        )
    ]
    tokens = {record.token_env: record.id + "x" * 40 for record in records}
    registry = Registry(
        AuthProvider(AuthSettings(tuple(records)), environ=tokens), RelaySettings()
    )
    peers = {record.id: Peer(registry) for record in records}
    for record in records[:-1]:
        registry.register(
            peers[record.id],
            {
                "revision": CONTROL_REVISION,
                "token": tokens[record.token_env],
                "role": record.role,
                "session": record.sessions[0],
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


@pytest.mark.parametrize(
    "revision", ["vehicle-window/1", "video-bound/1", "vehicle-status/1", "vehicle-status/2"]
)
def test_old_revision_requires_coordinated_upgrade(setup, revision):
    registry, _, _, tokens = setup
    with pytest.raises(RpcError, match="protocol_upgrade_required"):
        registry.register(Peer(registry), {
            "revision": revision, "token": tokens["DRIVER"],
            "role": "driving", "session": 42,
        })


@pytest.mark.asyncio
async def test_chassis_feedback_reaches_driver_without_renewing_control(setup, monkeypatch):
    from rd_core.settings import VehicleSettings
    from rd_vehicle.application.control_client import VehicleControlClient
    from rd_vehicle.infrastructure.controller.vehicle_feedback import FeedbackSample

    registry, peers, clock, _ = setup
    clock_api = SimpleNamespace(time_ns=lambda: int(clock.wall * 1_000_000_000))
    monkeypatch.setattr("rd_vehicle.application.control_client.time", clock_api)
    status = VehicleStatus(3.5, -12.5, 3.0, None, 2, True, 7, False)
    source = SimpleNamespace(sample=FeedbackSample(status, int(clock.wall * 1000), clock.mono, 1))
    source.latest = lambda: source.sample
    vehicle = VehicleControlClient(VehicleSettings(session=42), SimpleNamespace(feedback=source))
    vehicle_peer = peers["vehicle"]

    def deliver(data, *, best_effort):
        assert best_effort
        return registry.route(vehicle_peer, data)

    vehicle.connection = SimpleNamespace(
        protocol=SimpleNamespace(rpc_ready=True, send_datagram=deliver),
        status_stream=registry.bindings[vehicle_peer].status_stream,
        max_control_age_ms=300,
        max_clock_skew_ms=50,
    )
    for name in ("driver", "other"):
        assert not peers[name].datagrams
    assert vehicle._publish_feedback()
    assert not registry.leases
    for name in ("driver", "other"):
        receiver = peers[name]
        assert receiver.datagrams == [Packet(
            42, registry.bindings[receiver].status_stream, 0,
            source.sample.source_time_ms, status,
        )]
    assert not peers["outside"].datagrams
    assert not peers["viewer"].datagrams
    assert not vehicle._publish_feedback()

    await registry.acquire(peers["driver"])
    lease = registry.leases[42]
    deadline = lease.deadline
    clock.wall += 0.1
    source.sample = replace(source.sample, source_time_ms=int(clock.wall*1000), revision=2)
    assert vehicle._publish_feedback()
    assert lease.deadline == deadline
    assert registry.release(peers["driver"], {"epoch": str(lease.epoch)}) == {"revoked": True}
    assert not vehicle._publish_feedback()
    clock.wall += 0.1
    source.sample = replace(source.sample, source_time_ms=int(clock.wall*1000), revision=3)
    assert vehicle._publish_feedback()
    assert [packet.seq for packet in peers["driver"].datagrams] == [0, 1, 2]
    assert [packet.seq for packet in peers["other"].datagrams] == [0, 1, 2]
    clock.wall += 0.3
    source.sample = replace(source.sample, revision=4)
    assert not vehicle._publish_feedback()


@pytest.mark.asyncio
async def test_status_route_rejects_stale_or_unbound_sources_and_preserves_observers(setup):
    registry, peers, clock, tokens = setup
    vehicle, driver, other = (peers[name] for name in ("vehicle", "driver", "other"))
    await registry.acquire(driver)
    lease = registry.leases[42]
    timestamp = int(clock.wall * 1000)
    packet = Packet(42, registry.bindings[vehicle].status_stream, 0, timestamp, VehicleStatus(2))
    for sender, invalid in (
        (driver, replace(packet, epoch=registry.bindings[driver].status_stream)),
        (peers["viewer"], packet),
        (vehicle, replace(packet, epoch=lease.epoch)),
        (vehicle, replace(packet, session=99)),
        (vehicle, replace(packet, source_time_ms=timestamp - 300)),
        (vehicle, replace(packet, source_time_ms=timestamp + 100)),
    ):
        assert not registry.route(sender, invalid.encode())
    driver.datagram_error = ConnectionError()
    assert registry.route(vehicle, packet.encode())
    assert registry.leases[42] is lease
    assert other.datagrams[-1].seq == 0
    assert not driver.datagrams
    assert not registry.route(vehicle, packet.encode())

    driver.datagram_error = None
    driver_binding = registry.bindings[driver]
    driver_binding.principal = replace(driver_binding.principal, expires_at=1)
    assert registry.route(vehicle, replace(packet, seq=1).encode())
    assert not driver.datagrams
    assert other.datagrams[-1].seq == 1

    registry.unregister(vehicle)
    replacement = Peer(registry)
    result = registry.register(replacement, {
        "token": tokens["VEHICLE"], "role": "vehicle", "session": 42,
        "revision": CONTROL_REVISION,
    })
    assert not registry.route(vehicle, replace(packet, seq=2).encode())
    assert not registry.route(replacement, replace(packet, seq=2).encode())
    assert registry.route(replacement, replace(packet, epoch=UUID(result["status_stream"])).encode())
    observed = other.datagrams[-1]
    assert observed.epoch == registry.bindings[other].status_stream
    assert observed.seq == 2 and observed.source_time_ms == timestamp


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
    assert peers["vehicle"].discarded == 0
    assert registry.release(peers["driver"], {"epoch": old["epoch"]}) == {
        "revoked": True
    }
    assert peers["vehicle"].discarded == 1
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
    assert peers["vehicle"].discarded == 2
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

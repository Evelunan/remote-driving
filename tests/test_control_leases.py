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
    VehicleStatusReply,
    VehicleStatusRequest,
    Window,
)
from rd_quic.framing import RpcError
from rd_server.relay.registry import Registry


class Peer:
    def __init__(self, registry):
        self.registry = registry
        self.datagrams, self.events = [], []
        self.datagram_priorities = []
        self.closed = False
        self.discarded = 0
        self.datagram_error = None

    def send_datagram(self, data, *, best_effort=False):
        if self.datagram_error is not None:
            raise self.datagram_error
        self.datagrams.append(Packet.decode(data))
        self.datagram_priorities.append(best_effort)
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
    for record in (records[0], records[1], records[3]):
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


def control(route, clock, seq=0, throttle=0, *, age_ms=0):
    return Packet(
        42,
        route.epoch,
        seq,
        int(clock.wall * 1000) - age_ms,
        Control(throttle, 0, 0, bytes(16)),
    )


def execution(packet, status=ExecutionStatus.EXECUTED):
    return replace(packet, payload=Execution(status, packet.source_time_ms + 1))


@pytest.mark.parametrize(
    "revision", [
        "vehicle-window/1", "video-bound/1", "vehicle-status/1", "vehicle-status/2",
        "vehicle-status/3", "direct-control/4",
    ],
)
def test_old_revision_requires_coordinated_upgrade(setup, revision):
    registry, _, _, tokens = setup
    with pytest.raises(RpcError, match="protocol_upgrade_required"):
        registry.register(Peer(registry), {
            "revision": revision, "token": tokens["DRIVER"],
            "role": "driving", "session": 42,
        })


def test_chassis_feedback_reaches_driver_before_during_and_after_control(setup, monkeypatch):
    from rd_core.settings import DrivingSettings, VehicleSettings
    from rd_driving.app.control_client import DrivingControlClient
    from rd_vehicle.application.control_client import VehicleControlClient
    from rd_vehicle.infrastructure.controller.vehicle_feedback import FeedbackSample

    registry, peers, clock, _ = setup
    vehicle_clock = SimpleNamespace(monotonic=lambda: clock.mono + 500)
    monkeypatch.setattr("rd_vehicle.application.control_client.time", vehicle_clock)
    monkeypatch.setattr("rd_driving.app.control_client.time", SimpleNamespace(monotonic=lambda: clock.mono))
    status = VehicleStatus(3.5, -12.5, 3.0, None, 2, True, 7, False)
    timestamp = int(clock.wall * 1000) + 20_000
    source = SimpleNamespace(sample=FeedbackSample(status, timestamp, vehicle_clock.monotonic(), 1, 12))
    source.latest = lambda: source.sample
    vehicle = VehicleControlClient(VehicleSettings(session=42), SimpleNamespace(feedback=source))
    vehicle_peer, driver_peer = (peers[name] for name in ("vehicle", "driver"))
    vehicle_stream = registry.bindings[vehicle_peer].status_stream
    driver_stream = registry.bindings[driver_peer].status_stream
    displayed = []
    driver = DrivingControlClient(
        DrivingSettings(session=42), None,
        on_vehicle_status=lambda packet, deadline: displayed.append((packet, deadline)),
    )
    driver.connection = SimpleNamespace(status_stream=driver_stream)

    def deliver(data, *, best_effort):
        assert best_effort
        return registry.route(vehicle_peer, data)

    vehicle.connection = SimpleNamespace(
        protocol=SimpleNamespace(rpc_ready=True, send_datagram=deliver),
        status_stream=vehicle_stream,
    )

    def request_feedback(seq):
        request_id = (seq + 1).to_bytes(16, "big")
        driver._feedback_requests[request_id] = clock.mono
        request = Packet(42, driver_stream, seq, 0, VehicleStatusRequest(request_id))
        assert registry.route(driver_peer, request.encode())
        forwarded = vehicle_peer.datagrams[-1]
        assert forwarded == replace(request, epoch=vehicle_stream)
        assert vehicle_peer.datagram_priorities[-1]
        vehicle._datagram(vehicle._generation, forwarded.encode())
        assert vehicle._pending_feedback == request_id
        return request_id

    def receive_feedback(request_id, seq):
        assert vehicle._publish_feedback(request_id)
        forwarded = driver_peer.datagrams[-1]
        assert forwarded == Packet(
            42, driver_stream, seq, source.sample.source_time_ms,
            VehicleStatusReply(request_id, 12, status),
        )
        assert driver_peer.datagram_priorities[-1]
        sent_at = driver._feedback_requests[request_id]
        clock.mono += 0.125
        driver._datagram(driver._generation, forwarded.encode())
        assert displayed[-1] == (replace(forwarded, payload=status), pytest.approx(sent_at + 0.288))

    assert not driver_peer.datagrams
    request_id = request_feedback(0)
    receive_feedback(request_id, 0)
    assert not vehicle._publish_feedback(request_id)

    route = registry.routes[42]
    assert registry.route(vehicle_peer, Packet(42, route.epoch, 0, 0, Window(bytes(16))).encode())
    assert registry.route(driver_peer, control(route, clock, throttle=0.25).encode())
    source.sample = replace(
        source.sample, source_time_ms=timestamp + 125, received_at=vehicle_clock.monotonic(), revision=2,
    )
    receive_feedback(request_feedback(1), 1)
    assert registry.routes[42] is route
    request_id = request_feedback(2)
    assert registry.stop_control(driver_peer, {"epoch": str(route.epoch)}) == {"stopped": True}
    source.sample = replace(
        source.sample, source_time_ms=timestamp + 250, received_at=vehicle_clock.monotonic(), revision=3,
    )
    receive_feedback(request_id, 2)
    assert [packet.seq for packet, _ in displayed] == [0, 1, 2]
    assert all(not peers[name].datagrams for name in ("other", "outside", "viewer"))
    clock.mono += 0.3
    source.sample = replace(source.sample, revision=4)
    assert not vehicle._publish_feedback(request_feedback(3))


def test_status_route_only_accepts_current_matched_requests_and_replies(setup):
    registry, peers, clock, _ = setup
    vehicle, driver, other = (peers[name] for name in ("vehicle", "driver", "other"))
    route = registry.routes[42]
    driver_stream = registry.bindings[driver].status_stream
    vehicle_stream = registry.bindings[vehicle].status_stream
    request_id = b"a" * 16
    request = Packet(42, driver_stream, 0, 0, VehicleStatusRequest(request_id))
    reply = Packet(42, vehicle_stream, 0, int(clock.wall * 1000), VehicleStatusReply(request_id, 0, VehicleStatus(2)))
    for sender, invalid in (
        (other, request),
        (peers["outside"], replace(request, epoch=registry.bindings[peers["outside"]].status_stream)),
        (driver, replace(request, session=99)),
        (driver, replace(request, epoch=route.epoch)),
        (vehicle, replace(request, epoch=vehicle_stream)),
        (driver, replace(reply, epoch=driver_stream)),
        (vehicle, reply),
    ):
        assert not registry.route(sender, invalid.encode())
    vehicle.datagram_error = ConnectionError()
    assert not registry.route(driver, request.encode())
    assert not registry.route(vehicle, reply.encode())
    vehicle.datagram_error = None

    request_id = b"b" * 16
    request = replace(request, seq=1, payload=VehicleStatusRequest(request_id))
    reply = replace(reply, seq=1, payload=replace(reply.payload, request_id=request_id))
    assert registry.route(driver, request.encode())
    assert not registry.route(driver, request.encode())
    assert not registry.route(vehicle, replace(reply, session=99).encode())
    assert not registry.route(vehicle, replace(reply, epoch=route.epoch).encode())
    assert registry.route(vehicle, reply.encode())
    assert not registry.route(vehicle, replace(reply, seq=2).encode())

    request_id = b"c" * 16
    assert registry.route(driver, replace(request, seq=2, payload=VehicleStatusRequest(request_id)).encode())
    driver.datagram_error = ConnectionError()
    assert not registry.route(vehicle, replace(reply, seq=3, payload=replace(reply.payload, request_id=request_id)).encode())
    assert registry.routes[42] is route
    assert driver.datagrams == [replace(reply, epoch=driver_stream, seq=0)]
    assert not other.datagrams


@pytest.mark.parametrize("disconnected", ["driver", "vehicle"])
def test_vehicle_status_request_does_not_survive_a_peer_reconnect(setup, disconnected):
    registry, peers, _, tokens = setup
    vehicle, driver = (peers[name] for name in ("vehicle", "driver"))
    request_id = b"r" * 16
    request = Packet(42, registry.bindings[driver].status_stream, 0, 0, VehicleStatusRequest(request_id))
    assert registry.route(driver, request.encode())

    original = peers[disconnected]
    role = registry.bindings[original].principal.role
    registry.unregister(original)
    replacement = Peer(registry)
    result = registry.register(replacement, {
        "token": tokens[disconnected.upper()], "role": role, "session": 42,
        "revision": CONTROL_REVISION,
    })
    assert UUID(result["status_stream"]) == registry.bindings[replacement].status_stream
    if disconnected == "vehicle":
        vehicle = replacement
    else:
        driver = replacement

    reply = Packet(42, registry.bindings[vehicle].status_stream, 1, 100, VehicleStatusReply(request_id, 0, VehicleStatus(2)))
    assert not registry.route(vehicle, reply.encode())
    request_id = b"n" * 16
    request = Packet(42, registry.bindings[driver].status_stream, 1, 0, VehicleStatusRequest(request_id))
    assert registry.route(driver, request.encode())
    reply = replace(reply, seq=2, payload=replace(reply.payload, request_id=request_id))
    assert registry.route(vehicle, reply.encode())
    assert driver.datagrams == [replace(reply, epoch=request.epoch, seq=0)]


def test_driver_ignores_expired_unknown_and_out_of_order_vehicle_status_replies(monkeypatch):
    from rd_core.settings import DrivingSettings
    from rd_driving.app.control_client import DrivingControlClient

    now = 100.0
    monkeypatch.setattr("rd_driving.app.control_client.time", SimpleNamespace(monotonic=lambda: now))
    displayed = []
    stream = UUID(int=1234)
    driver = DrivingControlClient(
        DrivingSettings(session=42), None,
        on_vehicle_status=lambda packet, deadline: displayed.append((packet, deadline)),
    )
    driver.connection = SimpleNamespace(status_stream=stream)

    def receive(seq, token, sample_age, sent_at=None):
        request_id = token * 16
        if sent_at is not None:
            driver._feedback_requests[request_id] = sent_at
        reply = Packet(42, stream, seq, 10**12, VehicleStatusReply(request_id, sample_age, VehicleStatus(2)))
        driver._datagram(driver._generation, reply.encode())
        return reply

    receive(8, b"x", 20)
    receive(7, b"e", 110, now - 0.2)
    assert not displayed
    accepted = receive(5, b"f", 20, now - 0.125)
    assert displayed == [(replace(accepted, payload=VehicleStatus(2)), pytest.approx(now + 0.155))]
    receive(6, b"f", 20)
    receive(4, b"l", 20, now - 0.125)
    assert len(displayed) == 1

    driver._feedback_requests[b"d" * 16] = now - 0.1
    driver._transport_lost()
    receive(6, b"d", 20)
    assert len(displayed) == 1


def test_registration_checks_identity_role_scope_and_exclusive_connections(setup):
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
    with pytest.raises(RpcError, match="driver_already_connected"):
        registry.register(peers["other"], {
            "revision": CONTROL_REVISION, "token": tokens["OTHER"], "role": "driving", "session": 42,
        })
    assert registry.drivers[42] is peers["driver"]
    assert registry.vehicles[42] is peers["vehicle"]
    assert peers["viewer"] not in registry.bindings


def test_disconnect_and_late_stop_cannot_change_replacement_route(setup):
    registry, peers, _, tokens = setup
    driver, vehicle = peers["driver"], peers["vehicle"]
    first = registry.routes[42]
    assert driver.events[-1] == vehicle.events[-1]
    method, initial = driver.events[-1]
    assert method == "control_route" and initial["epoch"] == str(first.epoch)
    assert initial["vehicle_principal"] == "vehicle"

    registry.unregister(driver)
    assert 42 not in registry.routes
    offline = vehicle.events[-1][1]
    assert offline["revision"] > initial["revision"]
    assert offline["epoch"] is None and offline["vehicle_principal"] is None
    assert vehicle.discarded == driver.discarded == 1

    registry.register(peers["other"], {
        "token": tokens["OTHER"], "role": "driving", "session": 42, "revision": CONTROL_REVISION,
    })
    replacement = registry.routes[42]
    assert replacement.driver is peers["other"] and replacement.epoch != first.epoch
    assert vehicle.events[-1][1]["revision"] > offline["revision"]
    assert registry.stop_control(peers["other"], {"epoch": str(first.epoch)}) == {"stopped": False}
    with pytest.raises(RpcError, match="registration_required"):
        registry.stop_control(driver, {"epoch": str(replacement.epoch)})
    registry.unregister(driver)
    assert registry.routes[42] is replacement


def test_notification_failure_does_not_leave_a_usable_route(setup):
    registry, peers, clock, _ = setup
    driver, vehicle = peers["driver"], peers["vehicle"]
    old = registry.routes[42]
    vehicle.closed = True  # Cannot deliver a rotation to the vehicle.
    assert registry.stop_control(driver, {"epoch": str(old.epoch)}) == {"stopped": True}
    assert 42 not in registry.routes and vehicle not in registry.bindings
    latest = max((params for _, params in driver.events), key=lambda p: p["revision"])
    assert latest["epoch"] is None
    assert not registry.route(driver, control(old, clock).encode())


def test_first_brake_and_following_motion_do_not_wait_for_execution_ack(setup):
    registry, peers, clock, _ = setup
    route = registry.routes[42]
    driver, vehicle = peers["driver"], peers["vehicle"]
    assert registry.route(vehicle, Packet(42, route.epoch, 0, 0, Window(bytes(16))).encode())
    first = control(route, clock)
    first = replace(first, payload=replace(first.payload, brake=1))
    assert not registry.route(peers["other"], first.encode())
    assert not registry.route(peers["outside"], first.encode())
    assert not registry.route(vehicle, first.encode())
    assert not registry.route(driver, replace(first, session=99).encode())
    assert registry.route(driver, first.encode())
    assert registry.route(driver, control(route, clock, seq=1, throttle=0.5).encode())
    assert vehicle.datagrams[0] == first
    assert vehicle.datagrams[1].payload.throttle == 0.5
    assert not registry.route(driver, execution(first).encode())
    assert registry.route(vehicle, execution(first).encode())
    assert driver.datagrams[-1] == execution(first)


def test_sequences_timestamps_and_window_expiry_remain_independent_of_connection(setup):
    registry, peers, clock, _ = setup
    route = registry.routes[42]
    driver, vehicle = peers["driver"], peers["vehicle"]
    assert registry.route(vehicle, Packet(42, route.epoch, 0, 0, Window(bytes(16))).encode())
    packet = control(route, clock, seq=0xFFFFFFFF, throttle=0.5)
    assert registry.route(driver, packet.encode())
    assert registry.route(vehicle, execution(packet).encode())
    packet = control(route, clock, seq=0, throttle=0.5)
    assert registry.route(driver, packet.encode())
    for invalid in (
        packet, replace(packet, seq=0x80000000),
        replace(packet, seq=1, payload=replace(packet.payload, window=b"x" * 16)),
    ):
        assert not registry.route(driver, invalid.encode())
    for invalid in (
        replace(execution(packet), seq=99),
        replace(execution(packet), source_time_ms=packet.source_time_ms - 1),
    ):
        assert not registry.route(vehicle, invalid.encode())
    measurement = replace(packet, payload=Telemetry(1.0, 0.5))
    assert registry.route(vehicle, measurement.encode())
    assert not registry.route(vehicle, measurement.encode())
    clock.mono += registry.settings.max_control_age_ms / 1000
    assert not registry.route(driver, replace(packet, seq=1).encode())
    assert not registry.route(vehicle, execution(packet).encode())
    clock.mono += 60
    registry.sweep()
    assert registry.routes[42] is route
    assert registry.route(vehicle, Packet(42, route.epoch, 1, 0, Window(b"n" * 16)).encode())
    assert registry.route(driver, replace(packet, seq=1, payload=Control(0, 1, 0, b"n" * 16)).encode())


def test_stop_discards_old_packets_and_rejects_other_sessions(setup):
    registry, peers, clock, _ = setup
    old = registry.routes[42]
    driver, vehicle = peers["driver"], peers["vehicle"]
    assert registry.stop_control(peers["outside"], {"epoch": str(old.epoch)}) == {"stopped": False}
    assert registry.route(vehicle, Packet(42, old.epoch, 0, 0, Window(bytes(16))).encode())
    assert registry.route(driver, control(old, clock).encode())
    assert registry.route(vehicle, execution(control(old, clock), ExecutionStatus.REJECTED).encode())
    assert driver.datagrams[-1].payload.status is ExecutionStatus.REJECTED
    assert registry.stop_control(driver, {"epoch": str(old.epoch)}) == {"stopped": True}
    current = registry.routes[42]
    assert current.epoch != old.epoch
    assert driver.discarded == vehicle.discarded == 1
    assert not registry.route(driver, control(old, clock, seq=1).encode())
    assert not registry.route(driver, control(current, clock, seq=1).encode())
    assert not registry.route(vehicle, execution(control(old, clock)).encode())
    assert registry.stop_control(vehicle, {"epoch": str(old.epoch)}) == {"stopped": False}
    assert registry.routes[42] is current


def test_expired_identity_disconnects_and_invalidates_route(setup):
    registry, peers, _, _ = setup
    driver, vehicle = peers["driver"], peers["vehicle"]
    binding = registry.bindings[driver]
    binding.principal = replace(binding.principal, expires_at=time.time() - 1)
    registry.sweep()
    assert driver.closed and not registry.routes and 42 not in registry.drivers
    assert registry.drivers[99] is peers["outside"]
    assert vehicle.events[-1][1]["epoch"] is None
    registry.unregister(vehicle)
    assert not registry.vehicles


def test_expired_destination_is_not_forwarded_to_before_maintenance(setup):
    registry, peers, clock, _ = setup
    route = registry.routes[42]
    driver, vehicle = peers["driver"], peers["vehicle"]
    assert registry.route(vehicle, Packet(42, route.epoch, 0, 0, Window(bytes(16))).encode())
    binding = registry.bindings[vehicle]
    binding.principal = replace(binding.principal, expires_at=time.time() - 1)
    assert not registry.route(driver, control(route, clock).encode())
    assert not vehicle.datagrams and not registry.routes and vehicle.closed

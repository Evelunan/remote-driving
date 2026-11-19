import asyncio
import json
import logging
import ssl
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import pytest
from rd_core.async_compat import async_timeout
from rd_core.auth import Principal
from rd_server.main import create_runtime, main, parse_args
from rd_server.signaling.services.room_manager import RoomManager
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError


class Socket:
    def __init__(self):
        self.messages = []
        self.closed = None

    async def send(self, data):
        self.messages.append(json.loads(data))
        await asyncio.sleep(0)

    async def close(self, code, reason):
        self.closed = code


@pytest.mark.asyncio
async def test_publisher_source_requires_session_scope_is_server_owned_and_cannot_change():
    manager = RoomManager(3)
    vehicle = await manager.register_peer(
        Socket(),
        Principal("real-vehicle", "vehicle", frozenset({42, 43}), frozenset({"room"})),
    )
    viewer = await manager.register_peer(
        Socket(), Principal("viewer", "viewer", frozenset(), frozenset({"room"}))
    )
    await manager.join_room(viewer, "room")
    for session in (None, True, "42"):
        assert not await manager.join_room(vehicle, "room", session=session)
        assert vehicle.websocket.messages[-1]["message"] == "vehicle_session_required"
    await manager.handle_message(
        vehicle,
        json.dumps(
            {
                "type": "join",
                "room": "room",
                "session": 42,
                "source": {"principal": "forged", "session": 999},
            }
        ),
    )
    assert viewer.websocket.messages[-1]["source"] == {
        "principal": "real-vehicle",
        "session": 42,
    }
    await manager.join_room(viewer, "room")
    assert viewer.websocket.messages[-1]["peer_sources"] == {
        vehicle.id: {"principal": "real-vehicle", "session": 42}
    }
    assert not await manager.join_room(vehicle, "room", session=43)
    assert vehicle.websocket.messages[-1]["message"] == "publication_session_changed"
    await manager.handle_message(vehicle, '{"type":"join","room":"room","session":999}')
    assert vehicle.websocket.closed == 1008


@pytest.mark.asyncio
async def test_room_capacity_is_atomic_and_rejoin_is_idempotent():
    manager = RoomManager(2)
    peers = [await manager.register_peer(Socket()) for _ in range(3)]
    await manager.join_room(peers[0], "room")
    accepted = await asyncio.gather(
        *(manager.join_room(peer, "room") for peer in peers[1:])
    )
    assert sorted(accepted) == [False, True]
    assert manager.rooms["room"].peer_count == 2
    await manager.join_room(peers[0], "room")
    assert manager.rooms["room"].peer_count == 2
    assert sum(msg["type"] == "new_peer" for msg in peers[0].websocket.messages) == 1
    await asyncio.gather(*(manager.unregister_peer(peer) for peer in peers))
    assert not manager.rooms and not manager.peers


@pytest.mark.asyncio
async def test_room_scopes_source_spoofing_and_directed_routing():
    manager = RoomManager(4)
    peers = [
        await manager.register_peer(
            Socket(),
            Principal(
                str(index),
                "vehicle" if index == 0 else "viewer",
                frozenset({42}),
                frozenset({"allowed"}),
            ),
        )
        for index in range(3)
    ]
    for peer in peers:
        await manager.handle_message(
            peer, '{"type":"join","room":"allowed","session":42}'
        )
    sender, target, bystander = peers
    await manager.handle_message(
        sender,
        json.dumps({"type": "offer", "from": "spoof", "to": target.id, "sdp": "test"}),
    )
    assert target.websocket.messages[-1]["from"] == sender.id
    counts = [len(peer.websocket.messages) for peer in (target, bystander)]
    for value in ("outside-room", None, [], sender.id):
        await manager.handle_message(sender, json.dumps({"type": "offer", "to": value}))
        assert sender.websocket.messages[-1]["message"] == "invalid_target"
    assert [len(peer.websocket.messages) for peer in (target, bystander)] == counts
    await manager.handle_message(sender, '{"type":"join","room":"denied"}')
    assert sender.websocket.closed == 1008
    assert "denied" not in manager.rooms
    for value in ("[]", "null", '"string"', '{"type":[]}', "{"):
        await manager.handle_message(target, value)
        assert target.websocket.messages[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_expired_identity_cannot_send_or_receive():
    manager = RoomManager()
    expired = Principal(
        "expired", "viewer", frozenset(), frozenset({"room"}), time.time() - 1
    )
    peer = await manager.register_peer(Socket(), expired)
    await manager.handle_message(peer, '{"type":"join","room":"room"}')
    assert peer.websocket.closed == 1008 and not manager.rooms
    assert not await peer.send({"type": "offer"})
    assert not peer.websocket.messages


@pytest.mark.asyncio
async def test_room_has_one_publisher_and_viewers_cannot_signal_each_other():
    manager = RoomManager(5)
    peers = []
    for index, role in enumerate(("vehicle", "vehicle", "driving", "viewer")):
        peers.append(
            await manager.register_peer(
                Socket(),
                Principal(str(index), role, frozenset({42}), frozenset({"room"})),
            )
        )
    first, second, driver, viewer = peers
    assert await manager.join_room(first, "room", session=42)
    assert not await manager.join_room(second, "room", session=42)
    assert second.websocket.messages[-1]["message"] == "publisher_already_present"
    assert await manager.join_room(driver, "room")
    assert await manager.join_room(viewer, "room")
    before = len(viewer.websocket.messages)
    await manager.handle_message(
        driver, json.dumps({"type": "candidate", "to": viewer.id, "candidate": None})
    )
    assert driver.websocket.messages[-1]["message"] == "media_role_denied"
    assert len(viewer.websocket.messages) == before
    await manager.handle_message(first, json.dumps({"type": "offer", "sdp": "test"}))
    assert viewer.websocket.messages[-1]["type"] == "offer"
    assert driver.websocket.messages[-1]["type"] == "offer"
    await manager.unregister_peer(first)
    assert await manager.join_room(second, "room", session=42)


@pytest.fixture
def authenticated_args(tmp_path, local_tls, monkeypatch):
    cert, key = local_tls
    monkeypatch.setenv("TEST_OPERATOR", "operator-token-" + "a" * 32)
    monkeypatch.setenv("TEST_VIEWER", "viewer-token-" + "b" * 32)
    monkeypatch.setenv("TEST_VEHICLE", "vehicle-token-" + "c" * 32)
    path = tmp_path / "server.yaml"
    path.write_text(f"""environment: production
services: [signaling]
logging:
  level: DEBUG
signaling:
  port: 0
  max_peers_per_room: 3
  max_message_bytes: 4096
  ping_interval_s: 7
  ping_timeout_s: 8
  tls:
    cert: {cert.name}
    key: {key.name}
auth:
  principals:
    - id: operator
      role: driving
      token_env: TEST_OPERATOR
      sessions: [42]
      rooms: [allowed]
    - id: vehicle
      role: vehicle
      token_env: TEST_VEHICLE
      sessions: [42]
      rooms: [allowed]
    - id: viewer
      role: viewer
      token_env: TEST_VIEWER
      rooms: [allowed]
""")
    return ["--config", str(path)]


@pytest.mark.asyncio
async def test_actual_wss_authentication_forwarding_expiry_and_cleanup(
    authenticated_args, local_tls, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    runtime = create_runtime(parse_args(authenticated_args))
    service = runtime.services["signaling"]
    assert (
        service.config.max_peers_per_room == 3
        and service.config.max_message_bytes == 4096
    )
    async with async_timeout(10), AsyncExitStack() as clients:
        await runtime.start()
        try:
            port = service._server.sockets[0].getsockname()[1]
            context = ssl.create_default_context(cafile=str(local_tls[0]))

            async def client(token=None):
                headers = {"Authorization": "Bearer " + token} if token else None
                return await clients.enter_async_context(
                    connect(
                        f"wss://127.0.0.1:{port}",
                        ssl=context,
                        additional_headers=headers,
                        logger=logging.getLogger("test.ws.client"),
                    )
                )

            # Test client logs aren't part of server log protection.
            logging.getLogger("test.ws.client").setLevel(logging.WARNING)
            for token in (None, "invalid" * 8):
                denied = await client(token)
                with pytest.raises(ConnectionClosedError):
                    await denied.recv()
                assert denied.close_code == 1008
            assert not service.manager.peers
            operator = await client("operator-token-" + "a" * 32)
            viewer = await client("viewer-token-" + "b" * 32)
            await operator.send('{"type":"join","room":"allowed"}')
            op_id = json.loads(await operator.recv())["id"]
            await viewer.send('{"type":"join","room":"allowed"}')
            view_id = json.loads(await viewer.recv())["id"]
            assert json.loads(await operator.recv())["type"] == "new_peer"
            await operator.send(
                json.dumps(
                    {"type": "offer", "from": "forged", "to": view_id, "sdp": "example"}
                )
            )
            assert json.loads(await operator.recv())["message"] == "media_role_denied"
            vehicle = await client("vehicle-token-" + "c" * 32)
            await vehicle.send('{"type":"join","room":"allowed","session":42}')
            vehicle_id = json.loads(await vehicle.recv())["id"]
            assert json.loads(await operator.recv())["role"] == "vehicle"
            assert json.loads(await viewer.recv())["role"] == "vehicle"
            await vehicle.send(
                json.dumps(
                    {"type": "offer", "from": op_id, "to": view_id, "sdp": "example"}
                )
            )
            assert json.loads(await viewer.recv())["from"] == vehicle_id
            await viewer.send('{"type":"join","room":"forbidden"}')
            with pytest.raises(ConnectionClosedError):
                await viewer.recv()
            assert viewer.close_code == 1008
            assert json.loads(await operator.recv())["type"] == "peer_left"
            # An idle peer must close on expiry, without waiting for a message.
            path = authenticated_args[1]
            from pathlib import Path

            config = Path(path)
            expiry = (datetime.now(timezone.utc) + timedelta(milliseconds=350)).isoformat()
            config.write_text(
                config.read_text().replace(
                    "id: viewer", f"expires_at: '{expiry}'\n      id: viewer"
                )
            )
            refreshed = create_runtime(parse_args(authenticated_args))
            service.auth = refreshed.services["signaling"].auth
            idle = await client("viewer-token-" + "b" * 32)
            with pytest.raises(ConnectionClosedError):
                await idle.recv()
            assert idle.close_code == 1008
        finally:
            await runtime.stop()
        assert not service.manager.peers and not service.manager.rooms
    assert not [
        task for task in asyncio.all_tasks() if task.get_name() == "signaling-expiry"
    ]
    assert "operator-token-" not in caplog.text and "viewer-token-" not in caplog.text


def test_check_config_and_dump_do_not_start_services(
    authenticated_args, monkeypatch, capsys
):
    from rd_server.signaling.server import SignalingServer

    # The CLI configures process-wide logging. Keep this in-process listener
    # test from replacing pytest's capture streams with a short-lived stream.
    monkeypatch.setattr(
        "rd_server.main.configure_logging", lambda *args, **kwargs: None
    )

    async def forbidden_start(self):
        raise AssertionError("must not listen in check/dump mode")

    monkeypatch.setattr(SignalingServer, "start", forbidden_start)
    assert main([*authenticated_args, "--check-config"]) == 0
    assert "no listeners started" in capsys.readouterr().out
    monkeypatch.delenv("TEST_OPERATOR")
    assert main([*authenticated_args, "--dump-config"]) == 0
    dumped = json.loads(capsys.readouterr().out)
    assert dumped["sources"]["signaling.port"] == "file"
    assert dumped["config"]["auth"]["principals"][0]["token_env"] == "TEST_OPERATOR"
    assert main([*authenticated_args, "--check-config"]) == 2

import asyncio
import json
import logging
import ssl
from dataclasses import replace

import pytest
from rd_core.configuration import ConfigError
from rd_core.settings import ClientNetworkSettings, CredentialSettings, IceSettings
from rd_webrtc.signaling import (
    SignalingClient,
    SignalingError,
    check_media_config,
    ice_servers,
)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


@pytest.mark.asyncio
async def test_shared_wss_client_join_role_routing_reconnect_and_token_log_redaction(
    runtime_factory, local_tls, caplog
):
    caplog.set_level(logging.DEBUG)
    build, tokens = runtime_factory
    runtime = build(("signaling",))
    await runtime.start()
    service = runtime.services["signaling"]
    port = service._server.sockets[0].getsockname()[1]
    network = ClientNetworkSettings(
        signaling_url=f"wss://127.0.0.1:{port}", signaling_ca_file=local_tls[0]
    )
    vehicle = SignalingClient(network, CredentialSettings("TEST_VEHICLE"))
    driver = SignalingClient(network, CredentialSettings("TEST_DRIVER"))
    try:
        check_media_config(network, vehicle.credentials)
        await vehicle.connect()
        vehicle_id = (await vehicle.join("video"))["id"]
        await driver.connect()
        joined = await driver.join("video")
        assert joined["peer_roles"] == {vehicle_id: "vehicle"}
        driver_id = joined["id"]
        assert (await vehicle.recv())["role"] == "driving"
        await driver.send({"type": "offer", "to": vehicle_id, "sdp": "forged"})
        with pytest.raises(SignalingError, match="rejected"):
            await driver.recv()
        await vehicle.send(
            {"type": "offer", "to": driver_id, "from": "spoof", "sdp": "test"}
        )
        assert (await driver.recv())["from"] == vehicle_id
        await driver.close()
        await driver.close()
        assert (await vehicle.recv())["type"] == "peer_left"
        await driver.connect()
        assert (await driver.join("video"))["id"] != driver_id
    finally:
        await asyncio.gather(driver.close(), vehicle.close())
        await runtime.stop()
    assert not service.manager.peers
    assert all(token not in caplog.text for token in tokens.values())


@pytest.mark.asyncio
async def test_tls_and_room_denials_are_real_failures(runtime_factory, local_tls):
    build, _ = runtime_factory
    runtime = build(("signaling",))
    await runtime.start()
    port = runtime.services["signaling"]._server.sockets[0].getsockname()[1]
    network = ClientNetworkSettings(signaling_url=f"wss://127.0.0.1:{port}")
    untrusted = SignalingClient(network, CredentialSettings("TEST_DRIVER"))
    denied = SignalingClient(
        replace(network, signaling_ca_file=local_tls[0]),
        CredentialSettings("TEST_DRIVER"),
    )
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            await untrusted.connect()
        await denied.connect()
        with pytest.raises(ConnectionClosed):
            await denied.join("unassigned-room")
    finally:
        await untrusted.close()
        await denied.close()
        await runtime.stop()


@pytest.mark.asyncio
async def test_redirect_never_forwards_bearer_token(monkeypatch):
    monkeypatch.setenv("TEST_MEDIA", "secret-" + "s" * 40)
    reached = []

    async def target(ws):
        reached.append(ws.request.headers.get("Authorization"))

    async def redirect(connection, request):
        response = connection.respond(302, "redirect")
        response.headers["Location"] = target_url
        return response

    async with serve(target, "127.0.0.1", 0) as second:
        target_url = f"ws://127.0.0.1:{second.sockets[0].getsockname()[1]}"
        async with serve(target, "127.0.0.1", 0, process_request=redirect) as first:
            url = f"ws://127.0.0.1:{first.sockets[0].getsockname()[1]}"
            client = SignalingClient(
                ClientNetworkSettings(signaling_url=url),
                CredentialSettings("TEST_MEDIA"),
            )
            try:
                with pytest.raises(SignalingError, match="redirects"):
                    await client.connect()
            finally:
                await client.close()
    assert not reached


@pytest.mark.asyncio
async def test_signaling_disconnect_exits_listener_and_payload_limits(monkeypatch):
    monkeypatch.setenv("TEST_MEDIA", "secret-" + "s" * 40)

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "joined", "id": "id", "peers": []}))
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        network = ClientNetworkSettings(
            signaling_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            signaling_max_message_bytes=128,
        )
        client = SignalingClient(network, CredentialSettings("TEST_MEDIA"))
        try:
            await client.connect()
            with pytest.raises(SignalingError, match="exceeds"):
                await client.send({"type": "offer", "sdp": "x" * 256})
            await client.join("room")
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(client.listen(), 1)
        finally:
            await client.close()


def test_media_config_resolves_env_only_and_rejects_nonlocal_plaintext(monkeypatch):
    monkeypatch.setenv("TEST_MEDIA", "secret-" + "s" * 40)
    monkeypatch.setenv("TEST_TURN_USER", "test-user")
    monkeypatch.setenv("TEST_TURN_PASS", "test-pass")
    settings = (
        IceSettings(("turn:localhost:3478",), "TEST_TURN_USER", "TEST_TURN_PASS"),
    )
    servers = ice_servers(settings)
    assert servers[0].username == "test-user" and servers[0].credential == "test-pass"
    with pytest.raises(ConfigError, match="WSS"):
        check_media_config(
            ClientNetworkSettings(signaling_url="ws://example.org"),
            CredentialSettings("TEST_MEDIA"),
        )
    monkeypatch.delenv("TEST_TURN_PASS")
    with pytest.raises(ConfigError, match="TEST_TURN_PASS"):
        ice_servers(settings)

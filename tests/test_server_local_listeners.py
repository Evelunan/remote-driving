import json
import ssl
from contextlib import AsyncExitStack

import pytest
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from rd_core.async_compat import async_timeout
from rd_server.main import create_runtime, parse_args
from websockets.asyncio.client import connect as ws_connect



@pytest.mark.asyncio
@pytest.mark.parametrize("services", ["signaling", "relay", "signaling,relay"])
async def test_actual_local_listeners_handshake_and_cleanup(local_tls, services):
    certificate, key = local_tls
    args = parse_args(
        [
            "--services",
            services,
            "--allow-legacy-development",
            "--signaling-port",
            "0",
            "--relay-port",
            "0",
            "--cert",
            str(certificate),
            "--key",
            str(key),
        ]
    )
    runtime = create_runtime(args)
    async with async_timeout(10), AsyncExitStack() as clients:
        await runtime.start()
        try:
            assert runtime.ready
            if "signaling" in runtime.services:
                service = runtime.services["signaling"]
                port = service._server.sockets[0].getsockname()[1]
                ws = await clients.enter_async_context(
                    ws_connect(
                        f"wss://127.0.0.1:{port}",
                        ssl=ssl.create_default_context(cafile=str(certificate)),
                    )
                )
                await ws.send(json.dumps({"type": "join", "room": "local-test"}))
                assert json.loads(await ws.recv())["type"] == "joined"
            if "relay" in runtime.services:
                relay = runtime.services["relay"]
                port = relay.listener._transport.get_extra_info("sockname")[1]
                configuration = QuicConfiguration(
                    is_client=True,
                    server_name="localhost",
                    cafile=str(certificate),
                    max_datagram_frame_size=65536,
                )
                client = await clients.enter_async_context(
                    connect("127.0.0.1", port, configuration=configuration)
                )
                await client.wait_connected()
                assert relay.protocols
        finally:
            await runtime.stop()
        assert not runtime.ready
        if "signaling" in runtime.services:
            assert not runtime.services["signaling"].manager.peers
            assert not runtime.services["signaling"].manager.rooms
        if "relay" in runtime.services:
            assert not runtime.services["relay"].protocols
            assert not runtime.services["relay"].manager._sessions


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--services", "relay"],
        ["--allow-legacy-development", "--host", "0.0.0.0"],
        ["--allow-legacy-development", "--services", "unknown"],
    ],
)
def test_legacy_listener_cannot_be_accidentally_exposed(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)

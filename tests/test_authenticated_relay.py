import asyncio
import json
import ssl
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from uuid import UUID

import pytest
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from rd_core.async_compat import async_timeout
from rd_core.wire import (
    ALPN,
    CONTROL_REVISION,
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    Window,
)
from rd_quic.framing import RpcError
from rd_quic.protocol import RpcProtocol
from websockets.asyncio.client import connect as ws_connect


def client_config(cert, alpn=ALPN):
    return QuicConfiguration(
        is_client=True,
        alpn_protocols=[alpn],
        server_name="localhost",
        cafile=str(cert),
        max_datagram_frame_size=1200,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("services", [("relay",), ("signaling", "relay")])
async def test_real_authenticated_registration_exclusive_lease_and_recovery(
    runtime_factory, local_tls, services
):
    build, tokens = runtime_factory
    runtime = build(services)
    relay = runtime.services["relay"]
    async with async_timeout(10), AsyncExitStack() as clients:
        await runtime.start()
        try:
            port = relay.listener._transport.get_extra_info("sockname")[1]

            async def client(name=None, role=None):
                peer = await clients.enter_async_context(
                    connect(
                        "127.0.0.1",
                        port,
                        configuration=client_config(local_tls[0]),
                        create_protocol=RpcProtocol,
                    )
                )
                if name is not None:
                    role = role or ("driving" if name in ("driver", "other") else name)
                    result = await peer.request(
                        "register",
                        {
                            "revision": CONTROL_REVISION,
                            "token": tokens[name],
                            "role": role,
                            "session": 42,
                        },
                    )
                    assert result["principal"] == name
                return peer

            unauthorized = await client()
            with pytest.raises(RpcError, match="registration_required"):
                await unauthorized.request("acquire", {})
            with pytest.raises(RpcError, match="access_denied"):
                await unauthorized.request(
                    "register",
                    {
                        "revision": CONTROL_REVISION,
                        "token": "x" * 40,
                        "role": "driving",
                        "session": 42,
                    },
                )
            # A valid old token must not make an unmodified old client compatible.
            with pytest.raises(RpcError, match="protocol_upgrade_required"):
                await unauthorized.request(
                    "register",
                    {"token": tokens["driver"], "role": "driving", "session": 42},
                )
            with pytest.raises(RpcError, match="protocol_upgrade_required"):
                await unauthorized.request(
                    "register",
                    {
                        "token": tokens["driver"],
                        "role": "driving",
                        "session": 42,
                        "revision": "video-bound/1",
                    },
                )
            viewer = await client()
            with pytest.raises(RpcError, match="role_denied"):
                await viewer.request(
                    "register",
                    {
                        "revision": CONTROL_REVISION,
                        "token": tokens["viewer"],
                        "role": "viewer",
                        "session": 42,
                    },
                )
            if "signaling" in services:
                signaling = runtime.services["signaling"]
                ws_port = signaling._server.sockets[0].getsockname()[1]
                ws = await clients.enter_async_context(
                    ws_connect(
                        f"wss://127.0.0.1:{ws_port}",
                        ssl=ssl.create_default_context(cafile=str(local_tls[0])),
                        additional_headers={
                            "Authorization": "Bearer " + tokens["viewer"]
                        },
                    )
                )
                await ws.send('{"type":"join","room":"video"}')
                assert json.loads(await ws.recv())["type"] == "joined"
            vehicle, driver, other = (
                await client("vehicle"),
                await client("driver"),
                await client("other"),
            )
            prepare_started, prepare_gate = asyncio.Event(), asyncio.Event()
            vehicle_events, vehicle_data, driver_data = (
                asyncio.Queue(),
                asyncio.Queue(),
                asyncio.Queue(),
            )

            async def prepare(method, params):
                assert method == "prepare_lease"
                prepare_started.set()
                await prepare_gate.wait()
                return {"ready": True, "epoch": params["epoch"]}

            vehicle.request_handler = prepare
            vehicle.event_handler = lambda method, params: vehicle_events.put_nowait(
                (method, params)
            )
            vehicle.datagram_handler = vehicle_data.put_nowait
            driver.datagram_handler = driver_data.put_nowait
            pending = asyncio.create_task(driver.request("acquire", {}))
            await prepare_started.wait()
            assert not pending.done()
            with pytest.raises(RpcError, match="lease_busy"):
                await other.request("acquire", {})
            prepare_gate.set()
            grant = await pending
            assert grant["neutral_required"] is True
            assert grant["vehicle_principal"] == "vehicle"
            token = bytes(range(16))
            window = Packet(42, UUID(grant["epoch"]), 0, 0, Window(token))
            vehicle.send_datagram(window.encode())
            assert Packet.decode(await asyncio.wait_for(driver_data.get(), 1)) == window
            neutral = Packet(
                42,
                UUID(grant["epoch"]),
                0,
                int(time.time() * 1000),
                Control(0, 0, 0, token),
            )
            driver.send_datagram(neutral.encode())
            assert (
                Packet.decode(await asyncio.wait_for(vehicle_data.get(), 1)) == neutral
            )
            result = replace(
                neutral,
                payload=Execution(ExecutionStatus.EXECUTED, int(time.time() * 1000)),
            )
            vehicle.send_datagram(result.encode())
            assert Packet.decode(await asyncio.wait_for(driver_data.get(), 1)) == result
            motion = replace(
                neutral,
                seq=1,
                source_time_ms=int(time.time() * 1000),
                payload=Control(0.5, 0, 0, token),
            )
            driver.send_datagram(motion.encode())
            assert (
                Packet.decode(await asyncio.wait_for(vehicle_data.get(), 1)) == motion
            )
            with pytest.raises(RpcError, match="lease_owner_required"):
                await other.request("release", {"epoch": grant["epoch"]})
            driver.close()
            method, revoked = await asyncio.wait_for(vehicle_events.get(), 1)
            assert method == "lease_revoked" and revoked["epoch"] == grant["epoch"]
            recovered = await other.request("acquire", {})
            assert (
                recovered["epoch"] != grant["epoch"] and recovered["neutral_required"]
            )
            # A stale release cannot revoke the new generation even on the new owner.
            assert await other.request("release", {"epoch": grant["epoch"]}) == {
                "revoked": False
            }
            assert relay.registry.leases[42].ready
        finally:
            await runtime.stop()
        assert not runtime.ready and not relay.protocols
        assert not relay.registry.leases and not relay.registry.bindings
        assert not relay.registration_deadlines
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"rd-rpc-handler", "relay-maintenance"}
    ]


@pytest.mark.asyncio
async def test_registration_deadline_and_incompatible_alpn(runtime_factory, local_tls):
    build, _ = runtime_factory
    runtime = build(register_timeout_s=0.15)
    relay = runtime.services["relay"]
    await runtime.start()
    try:
        port = relay.listener._transport.get_extra_info("sockname")[1]
        async with connect(
            "127.0.0.1",
            port,
            configuration=client_config(local_tls[0]),
            create_protocol=RpcProtocol,
        ) as client:
            # Diagnostic RPC isn't authentication and cannot renew the deadline.
            assert "server_time_ms" in await client.request("clock", {})
            await asyncio.wait_for(client.wait_closed(), 1)
        with pytest.raises(ConnectionError):
            async with connect(
                "127.0.0.1",
                port,
                configuration=client_config(local_tls[0], "rd-control/1"),
                create_protocol=RpcProtocol,
            ):
                pytest.fail("incompatible protocol must not connect")
    finally:
        await runtime.stop()
    assert not relay.registry.bindings and not relay.registration_deadlines


@pytest.mark.asyncio
async def test_maintenance_failure_stops_joint_server(runtime_factory, monkeypatch):
    build, _ = runtime_factory
    runtime = build(("signaling", "relay"))

    def fail():
        raise RuntimeError("simulated registry failure")

    monkeypatch.setattr(runtime.services["relay"].registry, "sweep", fail)
    with pytest.raises(RuntimeError, match="simulated registry failure"):
        await asyncio.wait_for(runtime.run(asyncio.Event()), 2)
    assert not runtime.ready
    assert not runtime.services["signaling"]._server.sockets
    assert runtime.services["relay"].listener is None


@pytest.mark.asyncio
async def test_actual_relay_bind_failure_rolls_back_started_signaling(runtime_factory):
    build, _ = runtime_factory
    occupied, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    try:
        port = occupied.get_extra_info("sockname")[1]
        runtime = build(("signaling", "relay"), port=port)
        with pytest.raises(OSError):
            await runtime.start()
        assert not runtime.ready
        assert not runtime.services["signaling"]._server.sockets
        assert runtime.services["relay"].listener is None
        await runtime.stop()
    finally:
        occupied.close()
        await asyncio.sleep(0)

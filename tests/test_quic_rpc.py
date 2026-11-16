import asyncio
import struct
from contextlib import asynccontextmanager

import pytest
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import HandshakeCompleted, StreamDataReceived
from rd_core.wire import ALPN
from rd_quic.framing import MessageBuffer, RpcError, encode_message
from rd_quic.protocol import RpcProtocol
from rd_server.relay.service import Listener

MESSAGE = {"type": "request", "method": "register", "params": {"session": 42}}


def test_framing_accepts_arbitrary_fragmentation_and_waits_for_fin():
    encoded = encode_message(MESSAGE, 1024)
    for split in range(len(encoded)):
        buffer = MessageBuffer(1024)
        assert buffer.feed(encoded[:split], end_stream=False) is None
        assert buffer.feed(encoded[split:], end_stream=True) == {**MESSAGE, "v": 2}
    buffer = MessageBuffer(1024)
    assert buffer.feed(encoded, end_stream=False) is None
    assert buffer.feed(b"", end_stream=True)["params"] == {"session": 42}


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"v":1}',
        b'{"v":true}',
        b'{"v":2,"type":[]}',
        b'{"v":2,"v":2}',
        b'{"v":2,"type":"request","method":"register","params":{"x":NaN}}',
        b'{"v":2,"type":"request","method":"register","params":{"x":1e309}}',
        b'{"v":2,"type":"response","result":[]}',
        b'{"v":2,"type":"response","error":"secret with spaces"}',
        b"\xff",
    ],
)
def test_framing_rejects_invalid_and_ambiguous_json(body):
    with pytest.raises(RpcError):
        MessageBuffer(1024).feed(struct.pack("!I", len(body)) + body, end_stream=True)


def test_framing_rejects_oversize_before_receiving_body_and_extra_messages():
    with pytest.raises(RpcError):
        MessageBuffer(16).feed(struct.pack("!I", 17), end_stream=False)
    encoded = encode_message(MESSAGE, 1024)
    for data in (encoded[:-1], encoded + encoded, b"", b"\0\0\0\0"):
        with pytest.raises(RpcError):
            MessageBuffer(1024).feed(data, end_stream=True)


@asynccontextmanager
async def local_pair(local_tls, **server_options):
    cert, key = local_tls
    configuration = QuicConfiguration(
        is_client=False, alpn_protocols=[ALPN], max_datagram_frame_size=1200
    )
    configuration.load_cert_chain(cert, key)
    protocols = []

    def factory(*args, **kwargs):
        protocol = RpcProtocol(*args, **server_options, **kwargs)
        protocols.append(protocol)
        return protocol

    transport, listener = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: Listener(
            configuration=configuration, create_protocol=factory, retry=True
        ),
        local_addr=("127.0.0.1", 0),
    )
    port = transport.get_extra_info("sockname")[1]
    client_configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
        server_name="localhost",
        cafile=str(cert),
        max_datagram_frame_size=1200,
    )
    try:
        async with (
            asyncio.timeout(5),
            connect(
                "127.0.0.1",
                port,
                configuration=client_configuration,
                create_protocol=RpcProtocol,
            ) as client,
        ):
            yield client, protocols
    finally:
        await asyncio.gather(*(protocol.shutdown() for protocol in protocols))
        listener.close()
        await listener.closed.wait()
        await asyncio.sleep(0)
        for protocol in protocols:
            assert (
                not protocol._buffers and not protocol._tasks and not protocol._pending
            )


@pytest.mark.asyncio
async def test_real_reliable_rpc_both_directions_events_datagrams_and_errors(local_tls):
    events, datagrams = [], []

    async def server_request(method, params):
        if method == "deny":
            raise RpcError("access_denied")
        return {"registered": params["session"]}

    async with local_pair(
        local_tls,
        request_handler=server_request,
        event_handler=lambda method, params: events.append((method, params)),
        datagram_handler=datagrams.append,
    ) as (client, servers):
        assert client.rpc_ready
        assert await client.request("register", {"session": 42}) == {"registered": 42}
        with pytest.raises(RpcError, match="access_denied"):
            await client.request("deny", {})

        async def prepare(method, params):
            assert method == "prepare_lease"
            return {"ready": params["epoch"]}

        client.request_handler = prepare
        assert await servers[0].request("prepare_lease", {"epoch": "test"}) == {
            "ready": "test"
        }
        client.notify("release", {"epoch": "test"})
        client.send_datagram(b"latest-input")
        async with asyncio.timeout(1):
            while not events or not datagrams:
                await asyncio.sleep(0.001)
        assert events == [("release", {"epoch": "test"})]
        assert datagrams == [b"latest-input"]
        assert not client._stream_readers and not servers[0]._stream_readers


@pytest.mark.asyncio
async def test_rpc_timeout_cancels_remote_handler_and_old_result_cannot_complete_next(
    local_tls,
):
    cancelled = asyncio.Event()

    async def handler(method, params):
        if method == "slow":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        return {"name": method}

    async with local_pair(local_tls, request_handler=handler) as (client, _servers):
        with pytest.raises(TimeoutError):
            await client.request("slow", {}, timeout_s=0.03)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert await client.request("next", {}) == {"name": "next"}
        assert not client._pending and not client._buffers


@pytest.mark.asyncio
async def test_connection_close_cancels_pending_requests_and_handler_tasks(local_tls):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(method, params):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with local_pair(local_tls, request_handler=handler) as (client, servers):
        request = asyncio.create_task(client.request("pending", {}))
        await asyncio.wait_for(started.wait(), 1)
        servers[0].close()
        with pytest.raises(ConnectionError):
            await request
        await asyncio.wait_for(cancelled.wait(), 1)
    assert not [
        task for task in asyncio.all_tasks() if task.get_name() == "rd-rpc-handler"
    ]


@pytest.mark.asyncio
async def test_rpc_receive_deadline_and_invalid_alpn_fail_closed(local_tls):
    async with local_pair(local_tls, rpc_timeout_s=0.05) as (client, servers):
        stream = client._quic.get_next_available_stream_id()
        client._quic.send_stream_data(stream, struct.pack("!I", 100), end_stream=False)
        client.transmit()
        await asyncio.wait_for(client.wait_closed(), 1)
        assert not servers[0]._buffers
    protocol = RpcProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True))
    )
    protocol.transmit = lambda: None
    protocol.quic_event_received(
        HandshakeCompleted(
            alpn_protocol="legacy", early_data_accepted=False, session_resumed=False
        )
    )
    assert not protocol.rpc_ready and protocol._rpc_closed


@pytest.mark.asyncio
async def test_congested_datagram_and_notification_queues_are_bounded():
    protocol = RpcProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True)), max_streams=2
    )
    protocol.rpc_ready = True
    protocol.transmit = lambda: (
        None
    )  # Simulate transport unable to flush queued output.
    for value in range(1000):
        protocol.send_datagram(str(value).encode())
    assert list(protocol._quic._datagrams_pending) == [b"999"]
    for _ in range(8):
        protocol.notify("event", {})
    with pytest.raises(RpcError, match="backpressure"):
        protocol.notify("event", {})
    assert len(protocol._quic._streams) == 8
    protocol.close()


@pytest.mark.asyncio
async def test_early_data_and_partial_stream_flood_are_rejected():
    protocol = RpcProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True)), max_streams=2
    )
    protocol.transmit = lambda: None
    protocol.quic_event_received(
        StreamDataReceived(data=b"x", end_stream=False, stream_id=1)
    )
    assert protocol._rpc_closed and not protocol._buffers
    protocol = RpcProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True)), max_streams=2
    )
    protocol.transmit = lambda: None
    protocol.rpc_ready = True
    for stream in (1, 5, 9):
        protocol.quic_event_received(
            StreamDataReceived(data=b"\0", end_stream=False, stream_id=stream)
        )
    assert (
        protocol._rpc_closed
        and not protocol._buffers
        and not protocol._receive_deadlines
    )


@pytest.mark.asyncio
async def test_local_encoding_failure_does_not_reset_an_uncreated_stream(local_tls):
    async def handler(method, params):
        return {"ok": True}

    async with local_pair(local_tls, request_handler=handler) as (client, _servers):
        with pytest.raises(RpcError, match="message_too_large"):
            await client.request("large", {"value": "x" * 20000})
        assert not client._pending
        assert await client.request("small", {}) == {"ok": True}


@pytest.mark.asyncio
async def test_forced_shutdown_releases_connect_context_and_handshake_waiter():
    protocol = RpcProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True))
    )
    protocol.transmit = lambda: None
    waiter = asyncio.create_task(protocol.wait_connected())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await protocol.shutdown(timeout_s=0.01)
    await asyncio.wait_for(protocol.wait_closed(), 0.1)
    assert protocol.transport_closed and protocol._connected_waiter is None

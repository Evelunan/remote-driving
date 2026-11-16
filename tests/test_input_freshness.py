from rd_core.samples import ControlInput


def test_snapshot_freshness_does_not_change_when_reused():
    sample = ControlInput(0.5, 0, 0, sampled_at=10, timestamp_ms=1000)
    assert sample.fresh(0.2, now=10.1)
    assert not sample.fresh(0.2, now=10.3)
    assert not sample.fresh(0.2, now=9.9)


def test_processing_preserves_capture_time_and_accepts_short_axis_report(legacy_python):
    legacy_python("driving", """
        import asyncio
        from rd_core.samples import RawInput
        from factories.control_factory import ControlFactory
        async def check():
            q = ControlFactory.create_message_queue()
            p = ControlFactory.create_data_processor(q)
            await p.process(RawInput(bytes([192, 0, 0, 128, 128]), 1.25, 123))
            value = await q.get()
            assert value.throttle == value.brake == value.steering == 0
            assert value.sampled_at == 1.25 and value.timestamp_ms == 123
            assert value.valid
            await p.process(RawInput(None, 1.5, 150))
            value = await q.get()
            assert not value.valid and value.sampled_at == 1.5
        asyncio.run(check())
    """)


def test_sender_reuses_timestamp_but_stops_when_sampling_ceases(legacy_python):
    legacy_python("driving", """
        import asyncio
        import time
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from rd_core.samples import ControlInput
        from infrastructure.communication.latest_value_queue import LatestValueQueue
        from infrastructure.protocol.driving_protocol import DrivingProtocol
        from infrastructure.protocol.codec import ControlCodec as C
        async def check():
            queue = LatestValueQueue()
            packet_time = time.time_ns() // 1000000
            await queue.put(ControlInput(0.5, 0, 0, time.monotonic(), packet_time))
            p = DrivingProtocol(42, queue, QuicConnection(configuration=QuicConfiguration(is_client=True)), input_timeout_s=0.08, send_hz=100)
            sent, closed = [], []
            p._send_datagram = lambda data: sent.append((time.monotonic(), C.decode(data)))
            p.close = lambda **kwargs: closed.append(kwargs)
            p._business_connected = True
            p.start()
            await asyncio.wait_for(p._business_task, 1)
            control = [m for _, m in sent if m['type'] == C.MessageType.CONTROL]
            assert len(control) >= 2
            assert all(m['timestamp'] == packet_time & 0xffffffff for m in control)
            assert sent[-1][1]['type'] == C.MessageType.STOP
            assert len(closed) == 1 and not p._business_connected
            count = len(sent)
            await asyncio.sleep(0.03)
            assert len(sent) == count
            await p.shutdown()
        asyncio.run(check())
    """)


def test_expired_or_invalid_input_never_emits_motion(legacy_python):
    legacy_python("driving", """
        import asyncio
        import time
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from rd_core.samples import ControlInput
        from infrastructure.communication.latest_value_queue import LatestValueQueue
        from infrastructure.protocol.driving_protocol import DrivingProtocol
        from infrastructure.protocol.codec import ControlCodec as C
        async def check():
            for valid, age in [(True, 1), (False, 0)]:
                queue = LatestValueQueue()
                await queue.put(ControlInput(1, 0, 1, time.monotonic() - age, 123, valid))
                p = DrivingProtocol(42, queue, QuicConnection(configuration=QuicConfiguration(is_client=True)))
                sent = []
                p._send_datagram = lambda data: sent.append(C.decode(data))
                p.close = lambda **kwargs: None
                p._business_connected = True
                p.start()
                await asyncio.wait_for(p._business_task, 1)
                assert [m['type'] for m in sent] == [C.MessageType.HELLO_DRIVING, C.MessageType.STOP]
                await p.shutdown()
        asyncio.run(check())
    """)

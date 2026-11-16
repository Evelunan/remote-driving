def test_vehicle_datagrams_use_supervisor_and_ack_after_device_execution(legacy_python):
    legacy_python(
        "vehicle-unit",
        """
        import asyncio
        import threading
        import time
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted
        from rd_core.control import ControlState, ControlSupervisor
        from rd_vehicle.infrastructure.protocol.vehicle_protocol import VehicleProtocol
        from rd_vehicle.infrastructure.protocol.codec import ControlCodec

        async def check():
            entered, release = threading.Event(), threading.Event()
            commands, stops, outbound = [], [], []
            class Device:
                def send_command(self, *values):
                    if values[0]:
                        entered.set()
                        assert release.wait(2)
                    commands.append(values)
                def request_safe_stop(self, reason): stops.append(reason)
                def close(self): pass
            loop = asyncio.get_running_loop()
            def result(*args): loop.call_soon_threadsafe(p.execution_result, *args)
            s = ControlSupervisor(Device(), timeout_s=0.2, sequence_bits=16, on_result=result)
            p = VehicleProtocol(42, None, QuicConnection(configuration=QuicConfiguration(is_client=True)), supervisor=s)
            p._send_datagram = lambda data: outbound.append(ControlCodec.decode(data))
            p.quic_event_received(HandshakeCompleted(None, False, False))
            p.start()
            async def until(predicate):
                async with asyncio.timeout(1):
                    while not predicate(): await asyncio.sleep(0.002)
            def send(seq, throttle=0, session=42, timestamp=None, type_=ControlCodec.MessageType.CONTROL):
                p._handle_datagram(ControlCodec.encode(type_, session, seq=seq,
                    throttle=throttle, timestamp=timestamp))
            def acks():
                return [m for m in outbound if m['type'] == ControlCodec.MessageType.STATE]
            try:
                send(0, 1)  # First input must be neutral.
                send(1, session=43)
                send(2, timestamp=((time.time_ns() // 1000000) - 1000) & 0xffffffff)
                await asyncio.sleep(0.01)
                assert not commands and not acks()
                send(3)
                await until(lambda: acks())
                assert commands == [(0.0, 0.0, 0.0)]
                assert [m['seq'] for m in acks()] == [3]
                send(4, 0.5)
                await until(entered.is_set)
                assert [m['seq'] for m in acks()] == [3]
                send(5, type_=ControlCodec.MessageType.STOP)
                assert s.state == ControlState.FAILSAFE
                assert s.reason == 'driving input unavailable'
                p.quic_event_received(ConnectionTerminated(0, None, 'test disconnect'))
                release.set()
                await until(lambda: len(stops) >= 2)
                await asyncio.sleep(0.01)
                assert [m['seq'] for m in acks()] == [3]
                assert not p._business_connected
            finally:
                release.set()
                await p.shutdown()
                assert await asyncio.to_thread(s.close)
        asyncio.run(check())
    """,
    )

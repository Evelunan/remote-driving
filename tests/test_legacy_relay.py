"""Exercise actual relay/session code without opening network sockets."""


def test_only_bound_sender_can_forward_to_opposite_role(legacy_python):
    legacy_python("servers/quic", """
        import asyncio
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from services.codec import ControlCodec as C
        from services.relay_protocol import RelayProtocol
        from services.session_manager import SessionManager

        async def check():
            manager = SessionManager()
            def peer():
                p = RelayProtocol(QuicConnection(configuration=QuicConfiguration(is_client=True)))
                p.session_manager = manager
                p.received = []
                p._send_datagram = p.received.append
                return p
            driver, car, stranger = peer(), peer(), peer()
            driver._handle_datagram(C.encode(C.MessageType.HELLO_DRIVING, 42))
            car._handle_datagram(C.encode(C.MessageType.HELLO_CAR, 42))
            control = C.encode(C.MessageType.CONTROL, 42, throttle=0.5)
            state = C.encode(C.MessageType.STATE, 42, throttle=0.5)
            driver._handle_datagram(control)
            car._handle_datagram(state)
            assert car.received == [control], car.received
            assert driver.received == [state], driver.received
            stranger._handle_datagram(control)
            car._handle_datagram(control)
            driver._handle_datagram(C.encode(C.MessageType.CONTROL, 99))
            assert car.received == [control]
            assert driver.received == [state]
            stop = C.encode(C.MessageType.STOP, 42)
            stranger._handle_datagram(stop)
            car._handle_datagram(stop)
            assert car.received == [control]
            driver._handle_datagram(stop)
            assert car.received == [control, stop]
        asyncio.run(check())
    """)


def test_binding_cannot_be_overwritten_or_moved(legacy_python):
    legacy_python("servers/quic", """
        from services.session_manager import SessionManager
        m = SessionManager()
        original, competitor = object(), object()
        assert m.bind_peer('0', 'driving', original)
        assert m.bind_peer('0', 'driving', original)
        assert not m.bind_peer('0', 'driving', competitor)
        assert not m.bind_peer('1', 'driving', original)
        assert not m.bind_peer('0', 'car', original)
        assert not m.bind_peer('0', 'unknown', competitor)
        assert m.get_peer('0', 'driving') is original
        assert m.get_peer('1', 'driving') is None
        assert m.unbind(original) == '0'
        assert m.bind_peer('0', 'driving', competitor)
        m.unbind(original)
        assert m.get_peer('0', 'driving') is competitor
        m.unbind(competitor)
        assert not m._sessions and not m._conn_to_session
    """)


def test_quic_termination_releases_binding_and_heartbeat(legacy_python):
    legacy_python("servers/quic", """
        import asyncio
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.events import ConnectionTerminated
        from services.relay_protocol import RelayProtocol
        from services.session_manager import SessionManager

        async def check():
            p = RelayProtocol(QuicConnection(configuration=QuicConfiguration(is_client=True)))
            p.session_manager = SessionManager()
            p.session_manager.bind_peer('42', 'car', p)
            p._running = True
            task = asyncio.create_task(asyncio.sleep(60))
            p._ping_task = task
            event = ConnectionTerminated(error_code=0, frame_type=None, reason_phrase='test')
            p.quic_event_received(event)
            await asyncio.sleep(0)
            assert task.cancelled()
            assert not p._running
            assert not p.session_manager.has_active_session(p)
            p.quic_event_received(event)
        asyncio.run(check())
    """)


def test_bad_datagram_does_not_forward_or_replace_binding(legacy_python):
    legacy_python("servers/quic", """
        import asyncio
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.events import DatagramFrameReceived
        from services.relay_protocol import RelayProtocol
        from services.session_manager import SessionManager
        async def check():
            p = RelayProtocol(QuicConnection(configuration=QuicConfiguration(is_client=True)))
            p.session_manager = SessionManager()
            for data in (b'', b'\\x01\\x00\\x2a', b'\\x02\\x00\\x2aextra'):
                p.quic_event_received(DatagramFrameReceived(data=data))
            assert not p.session_manager.has_active_session(p)
        asyncio.run(check())
    """)

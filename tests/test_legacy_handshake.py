import pytest


@pytest.mark.parametrize("application,module,constructor", [
    ("driving", "driving_protocol", "DrivingProtocol(42, None, quic)"),
    ("vehicle-unit", "vehicle_protocol", "VehicleProtocol(42, None, quic)"),
])
def test_transport_creation_does_not_complete_tls_handshake(
    legacy_python, application, module, constructor,
):
    package = {"driving": "rd_driving", "vehicle-unit": "rd_vehicle"}[application]
    legacy_python(application, f"""
        import asyncio
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.events import HandshakeCompleted
        from {package}.infrastructure.protocol.{module} import *

        async def check():
            quic = QuicConnection(configuration=QuicConfiguration(is_client=True))
            p = {constructor}
            p.connection_made(object())
            waiting = asyncio.create_task(p.wait_connected())
            await asyncio.sleep(0)
            assert not waiting.done(), 'UDP transport is not TLS readiness'
            quic._events.append(HandshakeCompleted(
                alpn_protocol=None, early_data_accepted=False, session_resumed=False,
            ))
            p._process_events()
            await asyncio.wait_for(waiting, timeout=0.5)
            assert p._connected
            assert p._business_connected
        asyncio.run(check())
    """)

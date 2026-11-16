import asyncio
import ipaddress
import json
import ssl
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import pytest
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from rd_server.main import create_runtime, parse_args
from websockets.asyncio.client import connect as ws_connect


@pytest.fixture
def local_tls(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


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
    async with asyncio.timeout(10), AsyncExitStack() as clients:
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

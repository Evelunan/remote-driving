"""Isolate Qt/ROS process state; all imports resolve installed named packages."""

import ipaddress
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from rd_server.main import create_runtime, parse_args

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def legacy_python(tmp_path):
    def run(application, source):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            check=False,
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    return run


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


@pytest.fixture
def runtime_factory(tmp_path, local_tls, monkeypatch):
    cert, key = local_tls
    tokens = {
        "vehicle": "vehicle-" + "a" * 40,
        "driver": "driver-" + "b" * 40,
        "other": "other-" + "c" * 40,
        "viewer": "viewer-" + "d" * 40,
    }
    records = []
    for name, token in tokens.items():
        monkeypatch.setenv("TEST_" + name.upper(), token)
        role = "driving" if name in ("driver", "other") else name
        records.append(
            {
                "id": name,
                "role": role,
                "token_env": "TEST_" + name.upper(),
                "sessions": [42],
                "rooms": ["video"],
            }
        )

    def build(services=("relay",), **relay_options):
        config = {
            "environment": "production",
            "services": list(services),
            "auth": {"principals": records},
            "relay": {
                "host": "127.0.0.1",
                "port": 0,
                "tls": {"cert": str(cert), "key": str(key)},
                **relay_options,
            },
            "signaling": {
                "host": "127.0.0.1",
                "port": 0,
                "tls": {"cert": str(cert), "key": str(key)},
            },
        }
        path = tmp_path / "authenticated-server.yaml"
        path.write_text(yaml.safe_dump(config))
        return create_runtime(parse_args(["--config", str(path)]))

    return build, tokens

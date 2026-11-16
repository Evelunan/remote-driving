"""Run with a clean installed vehicle interpreter, never with real devices.

Exercises the console entry, spawn imports, single/dual component modes and
SIGTERM cleanup. Temporary TLS and random credentials remain local. No server
is needed: reserved local ports cannot belong to another service.
"""

import asyncio
import json
import os
import re
import secrets
import signal
import socket
import sys
import tempfile
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = directory / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path  # No TLS server; private key doesn't need to be stored.


async def check_mode(config, components, env):
    binary = Path(sys.executable).with_name("rd-vehicle")
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--config",
        str(config),
        "--components",
        ",".join(components),
        cwd=config.parent,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    pids, messages = {}, deque(maxlen=200)

    async def read():
        while line := await process.stdout.readline():
            decoded = line.decode(errors="replace")
            messages.append(decoded)
            match = re.search(
                r"Vehicle (control|media) event loop responding \(pid=(\d+)\)", decoded
            )
            if match:
                pids[match[1]] = int(match[2])

    reader = asyncio.create_task(read())
    try:
        async with asyncio.timeout(12):
            while set(pids) != set(components):
                if process.returncode is not None:
                    raise RuntimeError("vehicle CLI exited before component heartbeat")
                if reader.done():
                    reader.result()
                    raise RuntimeError("vehicle CLI output closed before heartbeat")
                await asyncio.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(9):
            result = await process.wait()
            await reader
        if result != 0:
            raise RuntimeError(f"vehicle CLI shutdown failed ({result})")
        assert len(set(pids.values())) == len(components)
        for pid in pids.values():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise RuntimeError("vehicle CLI left a child process running")
        assert not any(env["RD_SMOKE_TOKEN"] in line for line in messages)
        return {
            "components": list(components),
            "pids": pids,
            "exitcode": result,
            "children_reaped": True,
        }
    except Exception:
        # Preserve bounded child diagnostics before temporary environments are
        # removed; otherwise an intermittent failure leaves only an exit code.
        detail = "".join(messages).replace(env["RD_SMOKE_TOKEN"], "<redacted>")
        print(f"Vehicle smoke failed ({','.join(components)}):\n{detail}", file=sys.stderr)
        raise
    finally:
        # This script owns a new process group; clean it up even on test failure.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def main():
    if os.name != "posix":
        raise SystemExit("this signal/process-group smoke test requires POSIX")
    with (
        tempfile.TemporaryDirectory(prefix="rd-installed-processes-") as temp,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        socket.socket() as tcp,
    ):
        directory = Path(temp)
        udp.bind(("127.0.0.1", 0))
        tcp.bind(("127.0.0.1", 0))  # Reserved, not listening.
        ca = certificate(directory)
        config = directory / "vehicle.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "controller": "mock",
                    "credentials": {"token_env": "RD_SMOKE_TOKEN"},
                    "network": {
                        "quic_host": "127.0.0.1",
                        "quic_port": udp.getsockname()[1],
                        "quic_ca_file": str(ca),
                        "quic_server_name": "localhost",
                        "signaling_url": f"wss://127.0.0.1:{tcp.getsockname()[1]}",
                        "signaling_ca_file": str(ca),
                        "reconnect_delay_s": 0.2,
                    },
                    "cameras": [
                        {
                            "name": "front",
                            "source": "synthetic",
                            "width": 96,
                            "height": 64,
                            "fps": 20,
                        }
                    ],
                }
            )
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RD_") and key != "PYTHONPATH"
        }
        env["RD_SMOKE_TOKEN"] = secrets.token_urlsafe(32)
        reports = []
        for components in (("control", "media"), ("control",), ("media",)):
            reports.append(await check_mode(config, components, env))
        print(json.dumps({"python": sys.executable, "results": reports}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

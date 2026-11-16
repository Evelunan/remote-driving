"""Worker for verify_wheels.py; run only inside its disposable role environment."""

import argparse
import asyncio
import importlib.metadata
import importlib.util
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import zipfile
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
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def check_origin(role, wheels):
    expected = {"rd-core", "rd-quic", f"rd-{role}"}
    if role != "server":
        expected.add("rd-webrtc")
    for other in {"driving", "vehicle", "server"} - {role}:
        assert importlib.util.find_spec("rd_" + other) is None, (
            f"unexpected {other} app"
        )
    if role == "server":
        for module in ("rd_webrtc", "cv2", "PyQt6", "rclpy", "av"):
            assert importlib.util.find_spec(module) is None, module
    versions, checked = {}, 0
    for package in expected:
        distribution = importlib.metadata.distribution(package)
        versions[package] = distribution.version
        direct = json.loads(distribution.read_text("direct_url.json"))
        assert not direct.get("dir_info", {}).get("editable"), package
        assert direct["url"].endswith(".whl"), package
        assert Path(distribution.locate_file("")).is_relative_to(Path(sys.prefix))
        (wheel,) = wheels.glob(package.replace("-", "_") + "-*.whl")
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith(".py"):
                    assert Path(
                        distribution.locate_file(name)
                    ).read_bytes() == archive.read(name), name
                    checked += 1
    return {
        "packages": versions,
        "installed_python_files_match_wheels": checked,
        "other_apps_absent": True,
    }


async def check_server(config, cert):
    from rd_core.settings import (
        ClientNetworkSettings,
        ControlSettings,
        CredentialSettings,
    )
    from rd_core.wire import CONTROL_REVISION
    from rd_quic.client import registered_connection
    from rd_quic.framing import RpcError
    from rd_server.main import create_runtime, parse_args
    from websockets.asyncio.client import connect

    results = []
    for services in (("signaling", "relay"), ("signaling",), ("relay",)):
        runtime = create_runtime(
            parse_args(["--config", str(config), "--services", ",".join(services)])
        )
        addresses = []
        try:
            await runtime.start()
            assert runtime.ready and set(runtime.services) == set(services)
            if "signaling" in services:
                service = runtime.services["signaling"]
                address = service._server.sockets[0].getsockname()
                addresses.append((socket.SOCK_STREAM, address))
                async with connect(
                    f"wss://127.0.0.1:{address[1]}",
                    ssl=ssl.create_default_context(cafile=cert),
                    additional_headers={
                        "Authorization": "Bearer " + os.environ["RD_VIEWER_TOKEN"]
                    },
                    proxy=None,
                    open_timeout=3,
                    close_timeout=1,
                    logger=logging.Logger(  # noqa: LOG001 - isolated transport logger cannot expose headers
                        "rd.acceptance.transport", logging.CRITICAL + 1
                    ),
                ) as ws:
                    await ws.send('{"type":"join","room":"1234"}')
                    assert json.loads(await ws.recv())["type"] == "joined"
            if "relay" in services:
                service = runtime.services["relay"]
                address = service.listener._transport.get_extra_info("sockname")
                addresses.append((socket.SOCK_DGRAM, address))
                network = ClientNetworkSettings(
                    quic_host="127.0.0.1",
                    quic_port=address[1],
                    quic_ca_file=cert,
                    quic_server_name="localhost",
                )
                async with registered_connection(
                    network,
                    CredentialSettings(token_env="RD_OPERATOR_TOKEN"),
                    ControlSettings(),
                    session=1234,
                    role="driving",
                    revision=CONTROL_REVISION,
                ) as connection:
                    try:
                        await connection.protocol.request("acquire", {})
                    except RpcError as error:
                        assert str(error) == "vehicle_unavailable"
                    else:
                        raise AssertionError("lease granted without a vehicle")
        finally:
            await runtime.stop()
        assert not runtime.ready
        for kind, address in addresses:
            with socket.socket(socket.AF_INET, kind) as probe:
                if kind == socket.SOCK_STREAM:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(address)
        results.append(
            {
                "services": list(services),
                "authenticated_probes": True,
                "ports_released": True,
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", choices=("server", "vehicle", "driving"), required=True
    )
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check_origin(args.role, args.wheels)
    with tempfile.TemporaryDirectory(prefix="rd-installed-local-") as temporary:
        directory = Path(temporary)
        cert, key = certificate(directory)
        tokens = {
            name: secrets.token_urlsafe(32)
            for name in ("RD_OPERATOR_TOKEN", "RD_VEHICLE_TOKEN", "RD_VIEWER_TOKEN")
        }
        os.environ.update(tokens)
        os.environ["RD_RUNTIME_DIR"] = str(directory / "runtime")
        config_name = (
            "vehicle.mock.yaml" if args.role == "vehicle" else f"{args.role}.yaml"
        )
        config = yaml.safe_load(
            (Path(__file__).parent / "configs" / config_name).read_text()
        )
        if args.role == "server":
            for service in ("signaling", "relay"):
                config[service].update(
                    {
                        "host": "127.0.0.1",
                        "port": 0,
                        "tls": {"cert": str(cert), "key": str(key)},
                    }
                )
        else:
            config["network"].update(
                {"quic_ca_file": str(cert), "signaling_ca_file": str(cert)}
            )
        config_path = directory / config_name
        config_path.write_text(yaml.safe_dump(config))

        def run(command, timeout=20):
            result = subprocess.run(
                command,
                cwd=directory,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            output = result.stdout + result.stderr
            assert not any(token in output for token in tokens.values()), (
                "credential in output"
            )
            assert result.returncode == 0, output[-8000:]
            return result.stdout

        binary = str(Path(sys.executable).with_name("rd-" + args.role))
        run([binary, "--help"])
        loaded = json.loads(
            run([binary, "--config", str(config_path), "--dump-config"])
        )
        assert loaded["config"]["environment"] == config["environment"]
        run([binary, "--config", str(config_path), "--check-config"])
        run(
            [
                sys.executable,
                "-c",
                f"import sys, rd_{args.role}; assert not {{'cv2', 'PyQt6', 'rclpy'}} & set(sys.modules)",
            ]
        )
        report["cli_help_dump_check_and_lazy_imports"] = True
        if args.role == "server":
            for service in ("signaling", "relay"):
                run(
                    [
                        binary,
                        "--config",
                        str(config_path),
                        "--services",
                        service,
                        "--check-config",
                    ]
                )

            async def timed_checks():
                async with asyncio.timeout(30):
                    return await check_server(config_path, cert)

            report["server_runtime"] = asyncio.run(timed_checks())
        elif args.role == "vehicle":
            for component in ("control", "media"):
                run(
                    [
                        binary,
                        "--config",
                        str(config_path),
                        "--components",
                        component,
                        "--check-config",
                    ]
                )
            report["vehicle_cli"] = json.loads(
                run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("smoke_vehicle_processes.py")),
                    ],
                    timeout=100,
                )
            )
        else:
            run(
                [
                    sys.executable,
                    "-c",
                    """from PyQt6.QtWidgets import QApplication
from rd_driving.main_window import MainWindow
app = QApplication([])
window = MainWindow()
window.show()
window.close()
app.processEvents()
app.processEvents()
assert window._closing_ready
""",
                ]
            )
            report["qt_offscreen_open_close"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()

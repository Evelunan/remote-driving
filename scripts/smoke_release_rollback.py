"""Exercise real installed releases A -> B -> A using loopback and mock devices."""

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from check_installed import certificate
from manage_release import COMPATIBILITY, write_json

from rd_core.async_compat import async_timeout

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "scripts/manage_release.py"
ROLES = ("server", "vehicle", "driving")


def operation(action, root, env, *options, fail=False):
    result = subprocess.run(
        [sys.executable, str(MANAGER), action, "--root", str(root), *map(str, options)],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if (result.returncode != 0) != fail:
        raise RuntimeError(
            f"{action}: unexpected exit {result.returncode}: {result.stderr[-2000:]}"
        )
    return result


async def live_server(network, token):
    from rd_core.settings import (
        ClientNetworkSettings,
        ControlSettings,
        CredentialSettings,
    )
    from rd_core.wire import CONTROL_REVISION
    from rd_quic.client import registered_connection
    from rd_quic.framing import RpcError
    from websockets.asyncio.client import connect

    async with connect(
        network["signaling_url"],
        ssl=ssl.create_default_context(cafile=network["signaling_ca_file"]),
        additional_headers={"Authorization": "Bearer " + token},
        proxy=None,
        open_timeout=1,
        close_timeout=1,
        logger=logging.Logger("rd.release.probe", logging.CRITICAL + 1),  # noqa: LOG001 - no credential-bearing transport logs
    ) as ws:
        await ws.send('{"type":"join","room":"1234"}')
        async with async_timeout(3):
            assert json.loads(await ws.recv())["type"] == "joined"
    async with registered_connection(
        ClientNetworkSettings(
            **{
                **network,
                "quic_ca_file": Path(network["quic_ca_file"]),
                "signaling_ca_file": Path(network["signaling_ca_file"]),
            }
        ),
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
            raise RuntimeError("unexpected control authority without a vehicle")


def children(pid):
    path = Path(f"/proc/{pid}/task/{pid}/children")
    return [int(value) for value in path.read_text().split()] if path.exists() else []


def running(pid):
    return Path(f"/proc/{pid}").exists()


async def exercise(root, role, release_id, env, work, network):
    log_path = work / f"{role}-{release_id}-{time.monotonic_ns()}.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(  # noqa: ASYNC220 - local process launch; waits run in a worker
            [sys.executable, str(MANAGER), "run", "--root", str(root)],
            env=env,
            cwd=work,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pids = []
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"{role} did not become ready")
                if role == "server":
                    try:
                        await live_server(network, env["RD_OPERATOR_TOKEN"])
                        break
                    except (ConnectionError, OSError):
                        pass  # Startup polling only; authentication failures are fatal.
                elif role == "vehicle":
                    text = log_path.read_text()
                    if all(
                        f"Vehicle {component} event loop responding" in text
                        for component in ("control", "media")
                    ):
                        pids = children(process.pid)
                        if len(pids) < 2:
                            raise RuntimeError("vehicle components were not spawned")
                        break
                else:
                    # Installed GUI's event loop is checked separately below;
                    # run's actual exec/lock path must remain live as well.
                    if time.monotonic() > deadline - 18:
                        break
                await asyncio.sleep(0.05)
            blocked = operation(
                "activate",
                root,
                env,
                "--release",
                release_id,
                "--expect-current",
                release_id,
                fail=True,
            )
            assert "still running" in blocked.stderr
            duplicate = operation("run", root, env, fail=True)
            assert "still running" in duplicate.stderr
            process.send_signal(signal.SIGTERM)
            await asyncio.to_thread(process.wait, timeout=12)
            # Historical driving releases exited directly on SIGTERM. Preserve
            # that fact in rollback reports; current wheels additionally face a
            # strict zero-exit CLI signal check in verify_wheels.py.
            expected = {-signal.SIGTERM, 0} if role == "driving" else {0}
            if process.returncode not in expected:
                raise RuntimeError(f"{role} shutdown returned {process.returncode}")
            assert not any(running(pid) for pid in pids)
            assert all(
                token not in log_path.read_text()
                for token in (
                    env["RD_OPERATOR_TOKEN"],
                    env["RD_VEHICLE_TOKEN"],
                )
            )
            if role == "server":
                for kind, port in (
                    (
                        socket.SOCK_STREAM,
                        int(network["signaling_url"].rsplit(":", 1)[1]),
                    ),
                    (socket.SOCK_DGRAM, network["quic_port"]),
                ):
                    with socket.socket(socket.AF_INET, kind) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind(("127.0.0.1", port))
            return {
                "started": True,
                "exitcode": process.returncode,
                "graceful_exit": process.returncode == 0,
                "selection_while_running_rejected": True,
                "duplicate_start_rejected": True,
                "children_reaped": True,
                "authenticated_protocols": role == "server",
            }
        except BaseException:
            text = log_path.read_text()[-6000:]
            for key in ("RD_OPERATOR_TOKEN", "RD_VEHICLE_TOKEN"):
                text = text.replace(env[key], "<redacted>")
            print(text, file=sys.stderr)
            raise
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.to_thread(process.wait, timeout=5)


async def main(args):
    report = {
        "passed": False,
        "scope": "Linux loopback, installed wheels, mock vehicle and Qt offscreen; no systemd activation or physical devices",
        "compatibility": COMPATIBILITY,
        "releases": {},
        "phases": [],
        "physical_stop_verified": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RD_") and key != "PYTHONPATH"
    }
    env.update(
        {
            "RD_OPERATOR_TOKEN": secrets.token_urlsafe(32),
            "RD_VEHICLE_TOKEN": secrets.token_urlsafe(32),
            "QT_QPA_PLATFORM": "offscreen",
        }
    )
    # The driver's probe resolves this reference in this process as well.
    os.environ["RD_OPERATOR_TOKEN"] = env["RD_OPERATOR_TOKEN"]
    with tempfile.TemporaryDirectory(prefix="rd-release-rollback-") as temporary:
        work = Path(temporary)
        env["RD_RUNTIME_DIR"] = str(work / "runtime")
        cert, key = certificate(work)
        with (
            socket.socket() as tcp,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        ):
            tcp.bind(("127.0.0.1", 0))
            udp.bind(("127.0.0.1", 0))
            tcp_port, udp_port = tcp.getsockname()[1], udp.getsockname()[1]
        network = {
            "signaling_url": f"wss://127.0.0.1:{tcp_port}",
            "signaling_ca_file": str(cert),
            "quic_host": "127.0.0.1",
            "quic_port": udp_port,
            "quic_ca_file": str(cert),
            "quic_server_name": "localhost",
        }
        for version in ("old", "new"):
            records = {}
            for role in ROLES:
                config = {"environment": "production"}
                if role == "server":
                    config.update(
                        {
                            "signaling": {
                                "host": "127.0.0.1",
                                "port": tcp_port,
                                "tls": {"cert": str(cert), "key": str(key)},
                                "max_peers_per_room": 2 if version == "old" else 3,
                            },
                            "relay": {
                                "host": "127.0.0.1",
                                "port": udp_port,
                                "tls": {"cert": str(cert), "key": str(key)},
                            },
                            "auth": {
                                "principals": [
                                    {
                                        "id": name,
                                        "role": kind,
                                        "token_env": token,
                                        "rooms": ["1234"],
                                        "sessions": [1234],
                                    }
                                    for name, kind, token in (
                                        ("vehicle", "vehicle", "RD_VEHICLE_TOKEN"),
                                        ("operator", "driving", "RD_OPERATOR_TOKEN"),
                                    )
                                ]
                            },
                        }
                    )
                else:
                    config.update(
                        {
                            "network": network,
                            "credentials": {
                                "token_env": "RD_VEHICLE_TOKEN"
                                if role == "vehicle"
                                else "RD_OPERATOR_TOKEN"
                            },
                        }
                    )
                    if role == "vehicle":
                        config.update(
                            {
                                "controller": "mock",
                                "motion": {
                                    "max_linear_mps": 5.0 if version == "old" else 4.0
                                },
                                "cameras": [
                                    {
                                        "name": "front",
                                        "source": "synthetic",
                                        "width": 160,
                                        "height": 120,
                                        "fps": 20,
                                    }
                                ],
                            }
                        )
                    else:
                        config["ui"] = {"width": 1200 if version == "old" else 1100}
                config_path = work / f"{role}-{version}.json"
                write_json(config_path, config)
                root = work / role
                print(f"preparing {version}/{role}", flush=True)
                operation(
                    "prepare",
                    root,
                    env,
                    "--role",
                    role,
                    "--release",
                    version,
                    "--source",
                    getattr(args, version + "_source"),
                    "--wheels",
                    getattr(args, version + "_wheels"),
                    "--acceptance",
                    getattr(args, version + "_acceptance"),
                    "--config",
                    config_path,
                    *(["--offline"] if args.offline else []),
                )
                manifest = json.loads(
                    (root / "releases" / version / "manifest.json").read_text()
                )
                records[role] = {
                    "wheels": manifest["wheels"],
                    "lock_sha256": manifest["lock_sha256"],
                    "config_sha256": manifest["files"]["config.json"]["sha256"],
                    "python": manifest["interpreter"]["version"],
                }
            report["releases"][version] = records
            write_json(args.output, report)
        for index, version in enumerate(("old", "new", "old")):
            phase = {"release": version, "roles": {}}
            for role in ROLES:
                root = work / role
                operation(
                    "activate",
                    root,
                    env,
                    "--release",
                    version,
                    "--expect-current",
                    "none" if index == 0 else ("old" if index == 1 else "new"),
                )
                selected = root / "current"
                config = json.loads((selected / "config.json").read_text())
                if role == "server":
                    assert config["signaling"]["max_peers_per_room"] == (
                        2 if version == "old" else 3
                    )
                elif role == "vehicle":
                    assert config["motion"]["max_linear_mps"] == (
                        5.0 if version == "old" else 4.0
                    )
                else:
                    assert config["ui"]["width"] == (1200 if version == "old" else 1100)
                    script = """from PyQt6.QtCore import QTimer
from rd_driving.cli import main
from PyQt6.QtWidgets import QApplication
original = QApplication.exec
def limited(self):
    QTimer.singleShot(300, self.closeAllWindows)
    return original()
QApplication.exec = limited
raise SystemExit(main())
"""
                    gui = await asyncio.to_thread(
                        subprocess.run,
                        [
                            selected / "venv/bin/python",
                            "-c",
                            script,
                            "--config",
                            selected / "config.json",
                        ],
                        cwd=work,
                        env=env,
                        check=False,
                        timeout=10,
                        capture_output=True,
                        text=True,
                    )
                    if gui.returncode:
                        detail = gui.stderr[-2000:]
                        for secret in (
                            env["RD_OPERATOR_TOKEN"],
                            env["RD_VEHICLE_TOKEN"],
                        ):
                            detail = detail.replace(secret, "<redacted>")
                        raise RuntimeError("installed GUI check failed: " + detail)
                phase["roles"][role] = await exercise(
                    root, role, version, env, work, network
                )
                phase["roles"][role]["paired_config_restored"] = True
            report["phases"].append(phase)
            write_json(args.output, report)
        report["passed"] = True
        write_json(args.output, report)
        print("three-role release upgrade and rollback passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for prefix in ("old", "new"):
        parser.add_argument("--" + prefix + "-source", type=Path, default=ROOT)
        parser.add_argument("--" + prefix + "-wheels", type=Path, required=True)
        parser.add_argument("--" + prefix + "-acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    for name, value in vars(arguments).items():
        if isinstance(value, Path):
            setattr(arguments, name, value.resolve())
    asyncio.run(main(arguments))

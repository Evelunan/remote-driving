"""Private loopback coturn fixture; never installs or configures a host service."""

import asyncio
import hashlib
import importlib.metadata
import os
import secrets
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


class LocalTurn:
    def __init__(self, directory, executable, library_directory=None):
        self.directory = Path(directory)
        self.executable = Path(executable).resolve(strict=True)
        self.env = os.environ.copy()
        if library_directory:
            self.env["LD_LIBRARY_PATH"] = str(
                Path(library_directory).resolve(strict=True)
            )
        self.username = "rd-" + secrets.token_hex(12)
        self.password = secrets.token_urlsafe(32)
        self.process = None
        self.exits = []
        self.previous = {}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.config = self.directory / "turnserver.conf"
        self.config.write_text(
            "\n".join(
                (
                    "listening-ip=127.0.0.1",
                    "relay-ip=127.0.0.1",
                    f"listening-port={self.port}",
                    "relay-threads=0",
                    "no-cli",
                    "no-tcp",
                    "no-tls",
                    "no-dtls",
                    "no-tcp-relay",
                    "no-rfc5780",
                    "no-auth-pings",
                    "allow-loopback-peers",
                    "no-multicast-peers",
                    "lt-cred-mech",
                    "realm=rd-local-validation",
                    "denied-peer-ip=0.0.0.0-255.255.255.255",
                    "allowed-peer-ip=127.0.0.1",
                    f"user={self.username}:{self.password}",
                    f"cli-password={secrets.token_urlsafe(32)}",
                    f"userdb={self.directory / 'turn.sqlite'}",
                    f"pidfile={self.directory / 'turn.pid'}",
                    "log-file=stdout",
                    "simple-log",
                )
            )
            + "\n"
        )
        self.config.chmod(0o600)
        for name, value in (
            ("RD_LOCAL_TURN_USER", self.username),
            ("RD_LOCAL_TURN_PASSWORD", self.password),
        ):
            self.previous[name] = os.environ.get(name)
            os.environ[name] = value

    @property
    def ice_settings(self):
        from rd_core.settings import IceSettings

        return IceSettings(
            (f"turn:127.0.0.1:{self.port}?transport=udp",),
            "RD_LOCAL_TURN_USER",
            "RD_LOCAL_TURN_PASSWORD",
        )

    async def start(self):
        from aioice import stun

        if self.process is not None:
            raise RuntimeError("TURN fixture is already running")
        self.process = subprocess.Popen(  # noqa: ASYNC220 - own the child before the first cancellable await
            [str(self.executable), "-c", str(self.config)],
            cwd=self.directory,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Binding probe is only readiness; real authenticated allocations and
        # nominated relay pairs are checked by the media scenario itself.
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            probe.setblocking(False)
            async with asyncio.timeout(5):
                while True:
                    if self.process.poll() is not None:
                        raise RuntimeError(
                            f"coturn startup exited ({self.process.returncode})"
                        )
                    request = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
                    await loop.sock_sendto(
                        probe, bytes(request), ("127.0.0.1", self.port)
                    )
                    try:
                        data, source = await asyncio.wait_for(
                            loop.sock_recvfrom(probe, 4096), 0.1
                        )
                        response = stun.parse_message(data)
                        if (
                            source == ("127.0.0.1", self.port)
                            and response.transaction_id == request.transaction_id
                            and response.message_class == stun.Class.RESPONSE
                        ):
                            return
                    except (TimeoutError, ValueError):
                        pass

    async def stop(self, *, crash=False):
        process = self.process
        if process is None:
            return
        forced = False
        if process.poll() is None:
            (process.kill if crash else process.terminate)()
        try:
            async with asyncio.timeout(3):
                while process.poll() is None:
                    await asyncio.sleep(0.02)
        except TimeoutError:
            forced = True
            process.kill()
        process.wait(timeout=2)
        self.exits.append(
            {
                "pid": process.pid,
                "exitcode": process.returncode,
                "intentional_crash": crash,
                "forced_cleanup": forced,
            }
        )
        self.process = None
        if forced:
            raise RuntimeError("coturn fixture required forced cleanup")

    def force_relay(self, *, consent_interval=None):
        import aioice.ice
        import aiortc.rtcicetransport as ice

        if (
            importlib.metadata.version("aiortc") != "1.14.0"
            or importlib.metadata.version("aioice") != "0.10.2"
        ):
            raise RuntimeError(
                "review relay-only diagnostic hooks for these library versions"
            )
        original = ice.Connection

        def connection(*args, **kwargs):
            if kwargs.get("turn_server") != ("127.0.0.1", self.port):
                raise RuntimeError("TURN test requires the private fixture address")
            kwargs["transport_policy"] = aioice.ice.TransportPolicy.RELAY
            return original(*args, **kwargs)

        stack = ExitStack()
        stack.enter_context(patch.object(ice, "Connection", connection))
        # Remove host transports as well as host candidates, preventing inbound
        # checks from silently creating a peer-reflexive direct path.
        stack.enter_context(
            patch.object(aioice.ice, "get_host_addresses", lambda **kwargs: [])
        )
        if consent_interval is not None:
            stack.enter_context(
                patch.object(aioice.ice, "CONSENT_INTERVAL", consent_interval)
            )
        return stack

    def assert_paths(self, paths):
        if not paths or any(
            path["local_type"] != "relay" or path["remote_type"] != "relay"
            for path in paths
        ):
            raise RuntimeError("TURN test did not nominate relay-to-relay paths")

    def report(self):
        return {
            "version": subprocess.check_output(
                [str(self.executable), "--version"], env=self.env, text=True, timeout=3
            ).strip(),
            "binary_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
            "address": ["127.0.0.1", self.port],
            "exits": list(self.exits),
            "running": self.process is not None,
        }

    async def close(self):
        try:
            await self.stop()
        finally:
            for name, value in self.previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

"""Spawned vehicle components with bounded, timestamp-only health channels.

The control process owns the actuator and its independent watchdog. No image,
command or secret is transported through the parent's health channel. Closing
the parent's write side requests cleanup; parent death also delivers EOF.
"""

import asyncio
import math
import multiprocessing
import signal
import socket
import struct
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from rd_core.async_compat import BaseExceptionGroup
from rd_core.logging import configure_logging, get_logger

from rd_vehicle.shutdown import stop_on_signals

logger = get_logger(__name__)
_HEARTBEAT = struct.Struct("!d")


class ProcessFailure(RuntimeError):
    pass


def _log_component_failure(component, exception):
    """Expose grouped/chained failure locations without device or token payloads."""
    seen = set()

    def visit(exc):
        if id(exc) in seen:
            return
        seen.add(id(exc))
        locations = []
        trace = exc.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            locations.append(
                f"{Path(code.co_filename).name}:{trace.tb_lineno} ({code.co_name})"
            )
            trace = trace.tb_next
        logger.error(
            "Vehicle %s failure detail: %s at %s",
            component,
            type(exc).__name__,
            " -> ".join(locations) or "unknown",
        )
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                visit(child)
        if exc.__cause__ is not None:
            visit(exc.__cause__)
        elif exc.__context__ is not None and not exc.__suppress_context__:
            visit(exc.__context__)

    visit(exception)


async def _component_lifetime(settings, channel, runner, capture_health=None):
    from rd_vehicle.main import run

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        if not channel.recv(1, socket.MSG_PEEK):
            return
    except BlockingIOError:
        pass
    signals = ExitStack()
    signals.enter_context(stop_on_signals(stop, numbers=(signal.SIGTERM,)))

    async def parent_lifetime():
        try:
            # Parent sends no data. EOF, malformed input or a broken socket all
            # mean the supervising parent can no longer authorize this process.
            await loop.sock_recv(channel, 1)
        finally:
            stop.set()

    heartbeat_ready = asyncio.Event()
    if settings.components != ("media",) or runner is not None:
        heartbeat_ready.set()

    async def heartbeat():
        await heartbeat_ready.wait()
        pending = b""
        while not stop.is_set():
            if not pending:
                pending = _HEARTBEAT.pack(time.monotonic())
            try:
                sent = channel.send(pending)
                if sent == 0:
                    stop.set()
                    return
                pending = pending[sent:]
            except BlockingIOError:
                pass  # At most one partial heartbeat; never queue old samples.
            except OSError:
                stop.set()
                return
            await asyncio.sleep(settings.processes.heartbeat_interval_s)

    owned = [asyncio.create_task(parent_lifetime()), asyncio.create_task(heartbeat())]
    try:
        if runner is None:

            def fault():
                # Report the component fault before potentially slow device cleanup.
                try:
                    channel.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            await run(
                settings,
                stop,
                on_fault=fault,
                on_media_started=heartbeat_ready.set,
                capture_health=capture_health,
            )
        else:
            await runner(settings, stop)
    finally:
        for task in owned:
            task.cancel()
        try:
            await asyncio.gather(*owned, return_exceptions=True)
        finally:
            signals.close()


def _component_process(settings, channel, runner, capture_health=None):
    # A console Ctrl+C is broadcast to the process group on Windows. The parent
    # owns that request and delivers EOF after entering its shutdown path.
    # Keep children alive through asyncio.run() cleanup, including repeat Ctrl+C.
    previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        channel.setblocking(False)
        directory = settings.logging.directory
        if directory is not None:
            directory = directory / settings.components[0]
        configure_logging(settings.logging.level, directory=directory)
        asyncio.run(_component_lifetime(settings, channel, runner, capture_health))
    except Exception as exc:  # noqa: BLE001 - process boundary; never log device payloads
        logger.error(
            "Vehicle %s failed (%s)", settings.components[0], type(exc).__name__
        )
        _log_component_failure(settings.components[0], exc)
        raise SystemExit(1) from None
    finally:
        channel.close()
        signal.signal(signal.SIGINT, previous_sigint)


@dataclass
class _Child:
    component: str
    process: object
    channel: socket.socket
    started_at: float
    heartbeat_at: float = 0.0
    pending: bytes = b""

    def read_health(self):
        # Bound each poll even if a faulty child floods the pipe.
        for _ in range(4):
            try:
                data = self.channel.recv(4096)
            except BlockingIOError:
                return
            if not data:
                raise ProcessFailure(f"{self.component} health channel closed")
            data = self.pending + data
            complete = len(data) - len(data) % _HEARTBEAT.size
            now = time.monotonic()
            for (stamp,) in _HEARTBEAT.iter_unpack(data[:complete]):
                if (
                    not math.isfinite(stamp)
                    or not self.started_at <= stamp <= now
                    or stamp < self.heartbeat_at
                ):
                    raise ProcessFailure(f"{self.component} invalid heartbeat")
                self.heartbeat_at = stamp
            self.pending = data[complete:]


class VehicleProcesses:
    """Single-use supervisor. Failure closes all components; no automatic re-arm."""

    def __init__(self, settings, *, runner=None):
        self.settings = settings
        self._runner = runner  # Importable simulation runner; never a CLI option.
        self.children = []
        self._capture_health = None
        self.outcomes = []
        self._used = False

    def _start(self):
        from rd_vehicle.main import active_components

        context = multiprocessing.get_context("spawn")
        from rd_vehicle.application.capture_health import CaptureHealth

        components = active_components(self.settings)
        # Spawned children open the shared semaphore after start() returns.
        if "media" in components:
            self._capture_health = CaptureHealth(context)
        for component in components:
            parent, child = socket.socketpair()
            parent.setblocking(False)
            started = time.monotonic()
            process = context.Process(
                target=_component_process,
                args=(
                    replace(self.settings, components=(component,)),
                    child,
                    self._runner,
                    self._capture_health,
                ),
                name=f"rd-vehicle-{component}",
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                process.close()
                raise
            finally:
                child.close()
            self.children.append(_Child(component, process, parent, started))
            logger.info("Vehicle %s process started (pid=%s)", component, process.pid)

    async def _wait_exit(self, timeout):
        deadline = time.monotonic() + timeout
        while any(child.process.is_alive() for child in self.children):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.02)

    async def _close(self):
        forced = set()
        for child in self.children:
            try:
                child.channel.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        await self._wait_exit(self.settings.processes.shutdown_timeout_s)
        for child in self.children:
            if child.process.is_alive():
                forced.add(child.component)
                logger.error(
                    "Vehicle %s cleanup timed out; physical stop is unconfirmed",
                    child.component,
                )
                child.process.terminate()
        await self._wait_exit(self.settings.processes.kill_timeout_s)
        for child in self.children:
            if child.process.is_alive():
                child.process.kill()
        await self._wait_exit(self.settings.processes.kill_timeout_s)
        unreaped = []
        for child in self.children:
            child.channel.close()
            process = child.process
            self.outcomes.append(
                {
                    "component": child.component,
                    "pid": process.pid,
                    "exitcode": process.exitcode,
                    "forced": child.component in forced,
                }
            )
            if process.is_alive():
                unreaped.append(child.component)
            else:
                process.join(timeout=0)
                process.close()
        self.children.clear()
        if forced or unreaped or any(item["exitcode"] != 0 for item in self.outcomes):
            raise ProcessFailure(
                "vehicle component failed or cleanup was forced/incomplete"
            )

    async def run(self, stop=None, *, install_signals=False):
        from rd_vehicle.main import check_config

        if self._used:
            raise RuntimeError("vehicle process supervisor is single-use")
        self._used = True
        check_config(self.settings)
        stop = stop or asyncio.Event()
        signals = ExitStack()
        if install_signals:
            signals.enter_context(stop_on_signals(stop))
        try:
            if stop.is_set():
                return
            self._start()
            while not stop.is_set():
                for child in self.children:
                    if not child.process.is_alive():
                        raise ProcessFailure(f"{child.component} exited unexpectedly")
                    previous = child.heartbeat_at
                    child.read_health()
                    now = time.monotonic()
                    if not previous and child.heartbeat_at:
                        logger.info(
                            "Vehicle %s event loop responding (pid=%s)",
                            child.component,
                            child.process.pid,
                        )
                    if not child.heartbeat_at:
                        if (
                            now - child.started_at
                            > self.settings.processes.startup_timeout_s
                        ):
                            raise ProcessFailure(f"{child.component} startup timed out")
                        continue
                    # Queued heartbeats retain their original monotonic time.
                    if (
                        now - child.heartbeat_at
                        > self.settings.processes.heartbeat_timeout_s
                    ):
                        raise ProcessFailure(
                            f"{child.component} event-loop heartbeat expired"
                        )
                await asyncio.sleep(
                    min(0.05, self.settings.processes.heartbeat_interval_s)
                )
        except ProcessFailure as exc:
            logger.error("Vehicle process group failed: %s", exc)
            raise
        finally:
            # Repeated cancellation must not abandon live children or skip stop.
            cleanup = asyncio.create_task(self._close())
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            finally:
                signals.close()

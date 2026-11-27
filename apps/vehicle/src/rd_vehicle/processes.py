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
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from rd_core.async_compat import BaseExceptionGroup, Runner
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


async def _component_lifetime(settings, channel, runner):
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


def _component_process(settings, channel, runner):
    # A console Ctrl+C is broadcast to the process group on Windows. The parent
    # owns that request and delivers EOF after entering its shutdown path.
    # Keep children alive through event-loop cleanup, including repeat Ctrl+C.
    # This is the spawned process entry point. Keep ignoring broadcasts through
    # multiprocessing finalization, even after its health socket has closed.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        channel.setblocking(False)
        directory = settings.logging.directory
        if directory is not None:
            directory = directory / settings.components[0]
        configure_logging(settings.logging.level, directory=directory)
        factory = None
        if sys.platform == "win32" and settings.components == ("media",):
            # Proactor can leave ICE waiting for a UDP close callback.
            factory = asyncio.SelectorEventLoop
        with Runner(loop_factory=factory) as event_loop:
            event_loop.run(_component_lifetime(settings, channel, runner))
    except Exception as exc:  # noqa: BLE001 - process boundary; never log device payloads
        logger.error(
            "Vehicle %s failed (%s)", settings.components[0], type(exc).__name__
        )
        _log_component_failure(settings.components[0], exc)
        raise SystemExit(1) from None
    finally:
        channel.close()


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
    """Supervise each component independently and reap all children on shutdown."""

    def __init__(self, settings, *, runner=None):
        self.settings = settings
        self._runner = runner  # Importable simulation runner; never a CLI option.
        self.children = []
        self.outcomes = []
        self._used = False

    def _start(self, component):
        context = multiprocessing.get_context("spawn")
        parent, channel = socket.socketpair()
        parent.setblocking(False)
        started = time.monotonic()
        process = context.Process(
            target=_component_process,
            args=(
                replace(self.settings, components=(component,)),
                channel,
                self._runner,
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
            channel.close()
        child = _Child(component, process, parent, started)
        self.children.append(child)
        logger.info("Vehicle %s process started (pid=%s)", component, process.pid)
        return child

    @staticmethod
    async def _wait_exit(process, timeout):
        deadline = time.monotonic() + timeout
        while process.is_alive():
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.02)

    @staticmethod
    def _request_exit(child):
        try:
            child.channel.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    async def _close_child(self, child):
        process = child.process
        self._request_exit(child)
        await self._wait_exit(process, self.settings.processes.shutdown_timeout_s)
        forced = process.is_alive()
        if forced:
            logger.error(
                "Vehicle %s cleanup timed out (pid=%s)", child.component, process.pid
            )
            if child.component == "control":
                logger.error("Physical stop is unconfirmed")
            process.terminate()
            await self._wait_exit(process, self.settings.processes.kill_timeout_s)
        if process.is_alive():
            process.kill()
            await self._wait_exit(process, self.settings.processes.kill_timeout_s)
        child.channel.close()
        if process.is_alive():
            # Keep ownership. Never start a replacement alongside a surviving process.
            raise ProcessFailure(f"{child.component} could not be reaped")
        process.join(timeout=0)
        outcome = {
            "component": child.component,
            "pid": process.pid,
            "exitcode": process.exitcode,
            "forced": forced,
        }
        if outcome["exitcode"] != 0:
            logger.error(
                "Vehicle %s exited with code %s (pid=%s)",
                child.component,
                outcome["exitcode"],
                outcome["pid"],
            )
            if child.component == "control" and not forced:
                logger.error("Physical stop is unconfirmed")
        process.close()
        self.children.remove(child)
        self.outcomes.append(outcome)
        # Repeated failures of a disconnected component must not grow memory forever.
        del self.outcomes[:-64]
        return not forced and outcome["exitcode"] == 0

    async def _close(self):
        results = await asyncio.gather(
            *(self._close_child(child) for child in self.children),
            return_exceptions=True,
        )
        if not all(result is True for result in results):
            raise ProcessFailure(
                "vehicle component failed or cleanup was forced/incomplete"
            )

    def _check(self, child):
        if not child.process.is_alive():
            raise ProcessFailure(f"{child.component} exited unexpectedly")
        previous = child.heartbeat_at
        child.read_health()
        if not previous and child.heartbeat_at:
            logger.info(
                "Vehicle %s event loop responding (pid=%s)",
                child.component,
                child.process.pid,
            )
        now = time.monotonic()
        if not child.heartbeat_at:
            if now - child.started_at > self.settings.processes.startup_timeout_s:
                raise ProcessFailure(f"{child.component} startup timed out")
        elif now - child.heartbeat_at > self.settings.processes.heartbeat_timeout_s:
            raise ProcessFailure(f"{child.component} event-loop heartbeat expired")

    async def _supervise(self, component, stop):
        while not stop.is_set():
            child = None
            try:
                child = self._start(component)
                while not stop.is_set():
                    self._check(child)
                    await asyncio.sleep(
                        min(0.05, self.settings.processes.heartbeat_interval_s)
                    )
                return
            except Exception as exc:  # noqa: BLE001 - isolate spawn and health-check failures
                logger.error("Vehicle %s fault (%s)", component, type(exc).__name__)
                if isinstance(exc, ProcessFailure):
                    logger.error("%s", exc)
            while child is not None and not stop.is_set():
                try:
                    await self._close_child(child)
                    break
                except (OSError, ProcessFailure):
                    logger.exception(
                        "Vehicle %s still needs cleanup; restart suspended", component
                    )
                    # A process stuck in a driver may exit late. Resume only once reaped.
                    while child.process.is_alive() and not stop.is_set():
                        await asyncio.sleep(0.1)
            # Repeated startup faults must not create a process-spawning busy loop.
            try:
                await asyncio.wait_for(
                    stop.wait(), max(0.25, self.settings.network.reconnect_delay_s)
                )
            except asyncio.TimeoutError:
                pass

    async def run(self, stop=None, *, install_signals=False):
        from rd_vehicle.main import active_components

        if self._used:
            raise RuntimeError("vehicle process supervisor is single-use")
        self._used = True
        self.settings.validate()
        stop = stop or asyncio.Event()
        signals = ExitStack()
        if install_signals:
            signals.enter_context(stop_on_signals(stop))
        watchers = [
            asyncio.create_task(self._supervise(component, stop))
            for component in active_components(self.settings)
        ]
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                [stopped, *watchers], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            # Repeated cancellation must not abandon live children or skip stop.
            stop.set()
            for child in self.children:
                self._request_exit(child)

            async def finish():
                await asyncio.gather(stopped, *watchers, return_exceptions=True)
                await self._close()

            cleanup = asyncio.create_task(finish())
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            finally:
                signals.close()

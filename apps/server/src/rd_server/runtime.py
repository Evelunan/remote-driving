"""Transactional startup for callers and independent listener lifetimes for the CLI."""

import asyncio
from collections.abc import Mapping
from typing import Protocol

from rd_core.async_compat import ExceptionGroup
from rd_core.logging import get_logger

logger = get_logger(__name__)


class Service(Protocol):
    async def start(self): ...
    async def stop(self): ...
    async def wait_closed(self): ...


class ServerRuntime:
    def __init__(self, services: Mapping[str, Service]):
        if not services:
            raise ValueError("at least one service is required")
        self.services = dict(services)
        self._listening = set()
        self._started: list[Service] = []
        self._run_stop = None
        self._run_finished = None

    @property
    def ready(self):
        return len(self._listening) == len(self.services)

    async def start(self):
        if self._started or self._run_stop is not None:
            raise RuntimeError("runtime already started")
        try:
            for name, service in self.services.items():
                self._started.append(service)
                await service.start()
                self._listening.add(name)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self._run_stop is not None:
            self._run_stop.set()
            await self._run_finished.wait()
            return
        self._listening.clear()
        errors = []
        while self._started:
            service = self._started.pop()
            try:
                await service.stop()
            except Exception as error:  # noqa: BLE001 - clean up every started service
                errors.append(error)
        if errors:
            raise ExceptionGroup("server shutdown failed", errors)

    async def _serve(self, name, service, stop):
        while not stop.is_set():
            try:
                await service.start()
                self._listening.add(name)
                await service.wait_closed()
                raise RuntimeError(f"{name} listener stopped unexpectedly")
            except Exception as error:
                if len(self.services) == 1:
                    raise
                logger.error("Server %s failed (%s), retrying", name, type(error).__name__)
            finally:
                self._listening.discard(name)
                # Finish releasing the old listener, including leases and peers,
                # even if shutdown arrives while fault cleanup is in progress.
                cleanup = asyncio.create_task(service.stop())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                except Exception:
                    if len(self.services) > 1 and not stop.is_set():
                        logger.error("Server %s cleanup failed; restart disabled", name)
                        await stop.wait()
                    raise
            await asyncio.sleep(1.0)

    async def run(self, stop: asyncio.Event):
        if self._started or self._run_stop is not None:
            raise RuntimeError("runtime already started")
        self._run_stop, self._run_finished = stop, asyncio.Event()
        requested = asyncio.create_task(stop.wait())
        monitors = [
            asyncio.create_task(self._serve(name, service, stop), name=f"server-{name}")
            for name, service in self.services.items()
        ]
        try:
            done, _ = await asyncio.wait(
                [requested, *monitors], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done - {requested}:
                task.result()  # Preserve the failing module's cause after cleanup.
        finally:
            for task in [requested, *monitors]:
                task.cancel()
            results = await asyncio.gather(requested, *monitors, return_exceptions=True)
            self._run_finished.set()
            self._run_stop = self._run_finished = None
            for result in results:
                if isinstance(result, Exception):
                    raise result

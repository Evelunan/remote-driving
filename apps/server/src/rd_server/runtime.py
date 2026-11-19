"""One process, independently enabled services, transactional startup."""

import asyncio
from collections.abc import Mapping
from typing import Protocol

from rd_core.async_compat import ExceptionGroup


class Service(Protocol):
    async def start(self): ...
    async def stop(self): ...
    async def wait_closed(self): ...


class ServerRuntime:
    def __init__(self, services: Mapping[str, Service]):
        if not services:
            raise ValueError("at least one service is required")
        self.services = dict(services)
        self.ready = False
        self._started: list[Service] = []

    async def start(self):
        if self._started:
            raise RuntimeError("runtime already started")
        try:
            for service in self.services.values():
                self._started.append(service)
                await service.start()
        except BaseException:
            await self.stop()
            raise
        self.ready = True

    async def stop(self):
        self.ready = False
        errors = []
        while self._started:
            service = self._started.pop()
            try:
                await service.stop()
            except Exception as error:  # noqa: BLE001 - clean up every started service
                errors.append(error)
        if errors:
            raise ExceptionGroup("server shutdown failed", errors)

    async def run(self, stop: asyncio.Event):
        await self.start()
        requested = asyncio.create_task(stop.wait())
        monitors = [
            asyncio.create_task(service.wait_closed())
            for service in self.services.values()
        ]
        try:
            done, _ = await asyncio.wait(
                [requested, *monitors], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done - {requested}:
                task.result()  # Preserve the failing module's cause after cleanup.
            if requested not in done:
                raise RuntimeError("a required listener stopped unexpectedly")
        finally:
            for task in [requested, *monitors]:
                task.cancel()
            await asyncio.gather(requested, *monitors, return_exceptions=True)
            await self.stop()

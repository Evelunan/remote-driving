"""Structured concurrency APIs shared by the supported Python versions."""

import sys

if sys.version_info >= (3, 11):
    from asyncio import TaskGroup, timeout as async_timeout
    from builtins import BaseExceptionGroup, ExceptionGroup
else:
    import asyncio
    from contextlib import asynccontextmanager

    from async_timeout import timeout
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup
    from taskgroup import TaskGroup

    @asynccontextmanager
    async def async_timeout(delay):
        deadline = timeout(delay)
        try:
            async with deadline:
                yield
        except asyncio.TimeoutError as exc:
            # Python 3.11 unified asyncio.TimeoutError and built-in TimeoutError.
            if deadline.expired:
                raise TimeoutError from exc
            raise

__all__ = ["BaseExceptionGroup", "ExceptionGroup", "TaskGroup", "async_timeout"]

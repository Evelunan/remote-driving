"""Portable, idempotent signal requests for asyncio-owned vehicle lifetimes."""

import asyncio
import signal
import threading
from contextlib import contextmanager


@contextmanager
def stop_on_signals(stop, *, numbers=(signal.SIGINT, signal.SIGTERM)):
    """Keep handlers active through cleanup and restore the caller's handlers."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    loop = asyncio.get_running_loop()
    previous = {}
    pending = False
    timer = None

    def requested(_number, _frame):
        nonlocal pending
        # Signal dispatch can interrupt arbitrary Python code. Only set a flag;
        # the event-loop callback owns Event/Future operations and cleanup.
        pending = True

    def dispatch():
        nonlocal timer
        if pending:
            stop.set()
        if not stop.is_set():
            timer = loop.call_later(0.05, dispatch)

    try:
        for number in numbers:
            previous[number] = signal.signal(number, requested)
        timer = loop.call_later(0.05, dispatch)
        yield
    finally:
        if timer is not None:
            timer.cancel()
        for number, handler in previous.items():
            signal.signal(number, handler)

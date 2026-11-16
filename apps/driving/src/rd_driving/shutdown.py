"""Deliver process shutdown signals through the window's asynchronous close path."""

import signal
from contextlib import contextmanager

from PyQt6.QtCore import QTimer


@contextmanager
def shutdown_signals(window):
    pending = False

    def requested(_number, _frame):
        nonlocal pending
        # Python may dispatch this handler in the middle of a Qt callback. Do
        # not enter QWidget or change worker ownership from that call stack.
        pending = True

    def dispatch():
        nonlocal pending
        if pending:
            pending = False
            window.close()

    previous = {}
    timer = QTimer(window)
    timer.setInterval(50)
    timer.timeout.connect(dispatch)
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, requested)
        # Enter Python regularly even on the otherwise idle login screen;
        # CPython cannot dispatch pending handlers while Qt stays in native code.
        timer.start()
        yield
    finally:
        timer.stop()
        for number, handler in previous.items():
            signal.signal(number, handler)

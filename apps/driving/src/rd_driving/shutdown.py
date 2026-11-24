"""Handle desktop process shutdown signals and bound final cleanup."""

import faulthandler
import os
import signal
import sys
from contextlib import ExitStack, contextmanager

from PyQt6.QtCore import QTimer

SHUTDOWN_GRACE_S = 5.0


@contextmanager
def shutdown_deadline():
    """Arm the native watchdog after the window has revoked control."""
    with ExitStack() as resources:
        armed = False

        def arm():
            nonlocal armed
            if armed:
                return
            try:
                fd = sys.stderr.fileno()
            except (AttributeError, OSError, ValueError):
                fd = -1
            if fd < 0:
                fd = resources.enter_context(open(os.devnull, "wb")).fileno()
            # A native worker or Qt teardown can stop servicing Python timers.
            # The process watchdog can still dump the stacks and exit with status 1.
            faulthandler.dump_traceback_later(SHUTDOWN_GRACE_S, file=fd, exit=True)
            armed = True

        try:
            yield arm
        finally:
            if armed:
                faulthandler.cancel_dump_traceback_later()


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

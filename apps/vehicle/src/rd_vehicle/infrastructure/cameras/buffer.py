"""Atomic capture metadata and bounded notifications across a camera thread."""

import threading
import time

from rd_webrtc.video import CapturedImage


class CaptureBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._sequence = 0
        self._latest = None
        self._on_change = None

    def set_on_change(self, callback):
        with self._lock:
            self._on_change = callback

    def publish_owned(self, pixels, *, captured_at=None):
        """Caller transfers ownership; it must never reuse this image storage."""
        captured_at = time.monotonic() if captured_at is None else captured_at
        pixels.flags.writeable = False
        readonly = pixels.view()
        readonly.flags.writeable = False
        with self._lock:
            self._sequence += 1
            self._latest = CapturedImage(self._sequence, captured_at, readonly)
            callback = self._on_change
        if callback is not None:
            callback()

    def invalidate(self):
        with self._lock:
            self._latest = None
            callback = self._on_change
        if callback is not None:
            callback()

    def snapshot(self):
        with self._lock:
            return self._latest

    @property
    def sequence(self):
        with self._lock:
            return self._sequence

"""Local picture freshness and authenticated media provenance, without media imports."""

import math
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoSource:
    principal: str
    session: int
    peer_id: str

    def __post_init__(self):
        if (
            not isinstance(self.principal, str)
            or not 1 <= len(self.principal) <= 128
            or type(self.session) is not int
            or not 0 <= self.session <= 65535
            or not isinstance(self.peer_id, str)
            or not 1 <= len(self.peer_id) <= 128
        ):
            raise ValueError("invalid authenticated video source")

    @classmethod
    def from_message(cls, peer_id, data):
        if not isinstance(data, dict) or set(data) != {"principal", "session"}:
            raise ValueError("authenticated video source is required")
        return cls(data["principal"], data["session"], peer_id)


@dataclass(frozen=True)
class VideoPermit:
    ready: bool
    revision: int
    ready_at: float
    source: VideoSource | None
    reason: str


class VideoGate:
    """Qt paints publish observations; the control thread checks without Qt.

    Duplicate paints cannot renew freshness. Every outage increments a revision,
    even if recovery finishes between two control polls. Local timestamps only:
    received_at is not a remote capture time or an end-to-end latency estimate.
    """

    def __init__(self, *, stale_frame_ms=500, recovery_ms=500):
        if (
            not math.isfinite(stale_frame_ms)
            or stale_frame_ms <= 0
            or not math.isfinite(recovery_ms)
            or recovery_ms < 0
        ):
            raise ValueError("invalid video freshness policy")
        self.max_age_s, self.recovery_s = stale_frame_ms / 1000, recovery_ms / 1000
        self._lock = threading.Lock()
        self._revision = 0
        self._source = None
        self._sequence = -1
        self._received_at = None
        self._since = None
        self._ready_at = 0.0
        self._reason = "waiting_picture"

    def _invalidate(self, reason):
        self._revision += 1
        self._received_at = self._since = None
        self._ready_at = 0.0
        self._reason = reason

    def invalidate(self, reason="picture_lost", *, source=None):
        with self._lock:
            if source is None or source == self._source:
                self._invalidate(reason)

    def _expire(self, now):
        if (
            self._received_at is not None
            and not 0 <= now - self._received_at < self.max_age_s
        ):
            self._invalidate("picture_expired")

    def observe(self, sequence, received_at, source, *, now=None):
        now = time.monotonic() if now is None else now
        if (
            type(sequence) is not int
            or sequence < 0
            or not isinstance(source, VideoSource)
            or not math.isfinite(received_at)
            or not math.isfinite(now)
            or not 0 <= now - received_at < self.max_age_s
        ):
            return False
        with self._lock:
            self._expire(now)
            if source != self._source:
                self._invalidate("source_changed")
                self._source, self._sequence = source, -1
            if sequence <= self._sequence or (
                self._received_at is not None and received_at <= self._received_at
            ):
                return False
            self._sequence, self._received_at = sequence, received_at
            if self._since is None:
                self._since = now
            if not self._ready_at and now - self._since >= self.recovery_s:
                self._ready_at = now
            self._reason = "ready" if self._ready_at else "recovering_picture"
            return True

    def snapshot(self, *, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            return VideoPermit(
                bool(self._ready_at),
                self._revision,
                self._ready_at,
                self._source,
                self._reason,
            )

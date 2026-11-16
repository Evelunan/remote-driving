"""Owned immutable frame snapshots; polling never copies image data."""

import math
import threading
import time
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Frame:
    id: int = 0
    frame: np.ndarray | None = None
    received_at: float = 0
    generation: int = 0

    def fresh(self, max_age_s: float, *, now: float | None = None) -> bool:
        age = (time.monotonic() if now is None else now) - self.received_at
        return self.frame is not None and 0 <= age < max_age_s


class FrameBuffer:
    """One latest frame per view, copied once on publication.

    The producer retains ownership of its input. Readers retain a readonly
    snapshot and must make an explicit copy if they need to change its pixels.
    Generation tokens prevent late callbacks from reviving a closed session.
    """

    def __init__(self, positions=("front", "back", "left", "right")):
        self.positions = tuple(positions)
        if not self.positions or len(set(self.positions)) != len(self.positions):
            raise ValueError("positions must be nonempty and unique")
        self._frames = {name: Frame() for name in self.positions}
        self._generation = 0
        self._lock = threading.Lock()

    def begin_session(self):
        with self._lock:
            self._generation += 1
            self._frames = {
                name: Frame(id=frame.id + 1, generation=self._generation)
                for name, frame in self._frames.items()
            }
            return self._generation

    def clear(self):
        self.begin_session()

    def put(self, track_id, frame, *, generation=None, received_at=None):
        if track_id not in self.positions:
            raise ValueError("unknown camera view")
        with self._lock:
            generation = self._generation if generation is None else generation
            if generation != self._generation:
                return False
        if (
            not isinstance(frame, np.ndarray)
            or frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
            or not all(frame.shape)
        ):
            raise ValueError("expected a nonempty uint8 BGR image")
        received_at = time.monotonic() if received_at is None else received_at
        if not math.isfinite(received_at):
            raise ValueError("invalid receive timestamp")
        # The expensive operation happens outside the metadata lock.
        owned = frame.copy(order="C")
        owned.flags.writeable = False
        readonly = owned.view()
        readonly.flags.writeable = False
        with self._lock:
            if generation != self._generation:
                return False
            previous = self._frames[track_id]
            self._frames[track_id] = Frame(
                previous.id + 1, readonly, received_at, generation
            )
            return True

    def get(self, position):
        with self._lock:
            return self._frames.get(position)

    def get_if_new(self, position, after_id):
        with self._lock:
            frame = self._frames.get(position)
            return frame if frame is not None and frame.id != after_id else None

    @property
    def all_frames(self):
        with self._lock:
            return tuple(self._frames[name] for name in self.positions)

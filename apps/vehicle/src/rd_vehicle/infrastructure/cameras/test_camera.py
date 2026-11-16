"""Deterministic moving image for no-hardware media validation."""

import zlib

import numpy as np

from .thread_camera import ThreadCamera


class TestCamera(ThreadCamera):
    __test__ = False

    def __init__(self, width=640, height=480, fps=30, name="TestCamera", *, seed=42):
        super().__init__(width, height, fps, name)
        self._seed = seed + zlib.crc32(name.encode())
        self._rng = np.random.default_rng(self._seed)
        self._base_color = self._rng.integers(20, 180, 3, dtype=np.uint8)
        self.diagnostics = {
            "backend": "synthetic",
            "seed": self._seed,
            "width": width,
            "height": height,
            "target_fps": fps,
        }

    def _read(self):
        frame = np.empty((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = self._base_color
        sequence = self.get_frame_count() + 1
        x, y = (sequence * 7) % self.width, (sequence * 3) % self.height
        frame[y : min(y + 60, self.height), x : min(x + 60, self.width)] = 255
        # Encode the source sequence in pixels for repeatable comparisons.
        for bit in range(min(32, self.width)):
            frame[: min(8, self.height), bit] = 255 if sequence & (1 << bit) else 0
        return frame

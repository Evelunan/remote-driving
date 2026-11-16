"""Latest captured image shared by independently paced WebRTC subscriptions."""

import asyncio
import math
import time
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame


@dataclass(frozen=True)
class CapturedImage:
    sequence: int
    captured_at: float
    pixels: np.ndarray


class FrameSource:
    """Event-loop owned, one image regardless of subscriber count.

    Publication transfers a readonly image reference. Its owner must not mutate
    the backing storage. Each subscriber creates its own AV frame because codecs
    may mutate AV frame metadata independently (for example keyframe requests).
    """

    def __init__(self, name: str, fps: float, max_age_s: float = 0.5):
        if not math.isfinite(fps) or not 0 < fps <= 240:
            raise ValueError("invalid video FPS")
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("invalid capture age limit")
        self.name, self.fps, self.max_age_s = name, fps, max_age_s
        self.latest: CapturedImage | None = None
        self.closed = False
        self._last_sequence = 0
        self._last_capture = -math.inf
        self._subscribers: set[VideoFrameTrack] = set()

    @property
    def subscriber_count(self):
        return len(self._subscribers)

    def publish(self, image: CapturedImage) -> bool:
        if self.closed or image.sequence <= self._last_sequence:
            return False
        pixels = image.pixels
        if (
            type(image.sequence) is not int
            or not math.isfinite(image.captured_at)
            or image.captured_at < self._last_capture
            or image.captured_at > time.monotonic()
            or not isinstance(pixels, np.ndarray)
            or pixels.dtype != np.uint8
            or pixels.ndim != 3
            or pixels.shape[2] != 3
            or not all(pixels.shape)
            or pixels.flags.writeable
        ):
            raise ValueError(
                "expected monotonic capture metadata and readonly BGR image"
            )
        self._last_sequence, self._last_capture = image.sequence, image.captured_at
        self.latest = image
        self._wake()
        return True

    def invalidate(self):
        self.latest = None
        self._wake()

    def _wake(self):
        for track in self._subscribers:
            track._changed.set()

    def subscribe(self):
        if self.closed:
            raise MediaStreamError("capture source is closed")
        track = VideoFrameTrack(self)
        self._subscribers.add(track)
        return track

    def close(self):
        self.closed = True
        self.latest = None
        for track in tuple(self._subscribers):
            track.stop()


class VideoFrameTrack(VideoStreamTrack):
    def __init__(self, source: FrameSource):
        super().__init__()
        self._id = source.name
        self.source = source
        self._changed = asyncio.Event()
        self._last_sequence = 0
        self._next_send = 0.0
        self._capture_origin = None
        self._last_pts = -1

    def _fresh_image(self, now):
        image = self.source.latest
        return (
            image
            if (
                image is not None
                and image.sequence > self._last_sequence
                and 0 <= now - image.captured_at < self.source.max_age_s
            )
            else None
        )

    def _assert_live(self):
        if self.readyState != "live" or self.source.closed:
            raise MediaStreamError("video subscription ended")

    async def wait_ready(self):
        """Wait without owning an encoder slot or retaining a frame."""
        while True:
            self._assert_live()
            self._changed.clear()
            now = time.monotonic()
            if self._fresh_image(now) is not None:
                if now >= self._next_send:
                    return
                try:
                    async with asyncio.timeout(self._next_send - now):
                        await self._changed.wait()
                except TimeoutError:
                    pass
            else:
                await self._changed.wait()

    def recv_if_ready(self):
        """Select the latest frame after obtaining a slot, without blocking it."""
        self._assert_live()
        now = time.monotonic()
        image = self._fresh_image(now)
        if image is None or now < self._next_send:
            return None
        frame = VideoFrame.from_ndarray(image.pixels, format="bgr24")
        completed = time.monotonic()
        if completed - image.captured_at >= self.source.max_age_s:
            return None
        if self._capture_origin is None:
            self._capture_origin = image.captured_at
        pts = max(
            self._last_pts + 1,
            round((image.captured_at - self._capture_origin) * 90000),
        )
        frame.pts, frame.time_base = pts, Fraction(1, 90000)
        self._last_pts, self._last_sequence = pts, image.sequence
        self.last_captured_at = image.captured_at
        # Conversion and timer lateness consume this slot; skip missed slots.
        deadline = self._next_send or now
        slots = math.floor((completed - deadline) * self.source.fps) + 1
        self._next_send = deadline + slots / self.source.fps
        if self._next_send <= completed:
            self._next_send += 1 / self.source.fps
        return frame

    async def recv(self):
        while True:
            await self.wait_ready()
            frame = self.recv_if_ready()
            if frame is not None:
                return frame

    def stop(self):
        self.source._subscribers.discard(self)
        self._changed.set()
        super().stop()

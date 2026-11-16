"""Opt-in aiortc 1.14 / PyAV 16.1 sender budgeting, isolated per instance.

aiortc has no public sender encoder/executor configuration. Only this adapter
accesses sender state. RTP, RTCP, packetization and codec setup stay upstream;
no module globals or event loops are replaced.
"""

import asyncio
import importlib.metadata
import time
from concurrent.futures import ThreadPoolExecutor
from types import MethodType

from aiortc.codecs import get_encoder
from aiortc.codecs.vpx import Vp8Encoder
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcrtpsender import RTCEncodedFrame
from rd_core.configuration import ConfigError

from .video import VideoFrameTrack


def check_encoding_runtime():
    for package, expected in (("aiortc", "1.14.0"), ("av", "16.1.0")):
        if importlib.metadata.version(package) != expected:
            raise ConfigError(f"encoding budgets require reviewed {package} {expected}")


class _ThreadLimitedCodec:
    """Delegate codec setup to aiortc while capping its selected thread count."""

    __slots__ = ("_inner", "_limit")

    def __init__(self, context, limit):
        object.__setattr__(self, "_inner", context)
        object.__setattr__(self, "_limit", limit)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name == "thread_count":
            value = min(value, self._limit) if value > 0 else self._limit
        setattr(self._inner, name, value)


class BudgetedVp8Encoder(Vp8Encoder):
    def __init__(self, policy):
        self.policy = policy
        super().__init__()
        self.target_bitrate = policy.target_bitrate_bps

    @property
    def target_bitrate(self):
        return self._Vp8Encoder__target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate):
        if type(bitrate) is not int:
            raise ValueError("encoder target bitrate must be an integer")
        # aiortc also reads this value for bufsize, so all rate-control settings
        # use the same bound when REMB changes the target or recreates the codec.
        self._Vp8Encoder__target_bitrate = max(
            self.policy.min_bitrate_bps, min(bitrate, self.policy.max_bitrate_bps)
        )

    @property
    def codec(self):
        return self._context

    @codec.setter
    def codec(self, context):
        self._context = (
            _ThreadLimitedCodec(context, self.policy.max_threads)
            if context is not None and self.policy.max_threads is not None
            else context
        )


def create_encoder(codec, policy=None):
    if policy is None:
        return get_encoder(codec)
    if codec.mimeType.lower() != "video/vp8":
        raise ConfigError("per-view encoding budgets currently require VP8")
    return BudgetedVp8Encoder(policy)


class EncodingPool:
    """One bounded worker pool for every peer/view of a media publisher."""

    def __init__(self, workers):
        check_encoding_runtime()
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("encoding workers must be an integer in 1..32")
        self.workers = workers
        self._slots = asyncio.Semaphore(workers)
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rd-encode"
        )
        self._jobs = set()
        self._closed = False
        self._close_task = None
        self.completed = self.expired = 0

    async def next_frame(self, sender, codec, policy):
        track = sender.track
        if self._closed or not isinstance(track, VideoFrameTrack):
            raise MediaStreamError("budgeted media source is unavailable")
        # Frozen/slow cameras never occupy a worker slot. Once a slot is free,
        # re-read the latest capture, so waiters don't queue old AV frames.
        await track.wait_ready()
        async with self._slots:
            if self._closed:
                raise MediaStreamError("encoding pool closed")
            frame = track.recv_if_ready()
            if frame is None or not sender._enabled:
                return None
            captured_at = track.last_captured_at
            if sender._RTCRtpSender__encoder is None:
                sender._RTCRtpSender__encoder = create_encoder(codec, policy)
            encoder = sender._RTCRtpSender__encoder
            force_keyframe = sender._RTCRtpSender__force_keyframe
            sender._RTCRtpSender__force_keyframe = False
            job = asyncio.get_running_loop().run_in_executor(
                self._executor, encoder.encode, frame, force_keyframe
            )
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)
            try:
                payloads, timestamp = await asyncio.shield(job)
            except asyncio.CancelledError:
                # A running native call cannot be cancelled. Retain its slot and
                # frame until it finishes; peer.close must not race codec release.
                while not job.done():
                    try:
                        await asyncio.shield(job)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001 - cancellation wins after the owned call finishes
                        break
                # Retrieve the exception even when cancellation takes precedence.
                if not job.cancelled():
                    job.exception()
                raise
            self.completed += 1
            if time.monotonic() - captured_at >= track.source.max_age_s:
                self.expired += 1
                # The codec may now reference the unsent frame. Next output must
                # be a keyframe, otherwise dropping it breaks decoder prediction.
                sender._RTCRtpSender__force_keyframe = True
                return None
            return RTCEncodedFrame(payloads, timestamp, None) if payloads else None

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)


def bind_sender(sender, pool, policy):
    """Bind before negotiation; return an undo callback for the peer owner."""
    check_encoding_runtime()
    if (
        not isinstance(sender.track, VideoFrameTrack)
        or any(
            not hasattr(sender, name)
            for name in (
                "_RTCRtpSender__encoder",
                "_RTCRtpSender__force_keyframe",
                "_RTCRtpSender__started",
                "_enabled",
                "_next_encoded_frame",
            )
        )
        or sender._RTCRtpSender__started
        or "_next_encoded_frame" in vars(sender)
    ):
        raise ConfigError("unsupported or already active budgeted sender")

    async def next_frame(instance, codec):
        return await pool.next_frame(instance, codec, policy)

    sender._next_encoded_frame = MethodType(next_frame, sender)

    def unbind():
        del sender._next_encoded_frame

    return unbind

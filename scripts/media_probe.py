"""Bounded instrumentation for the local media benchmark, never imported by apps."""

import binascii
import math
import struct
import threading
import time
from collections import OrderedDict
from contextlib import ExitStack
from itertools import pairwise
from unittest.mock import patch

import numpy as np

VIEWS = ("front", "back", "left", "right")
CELL = 16
MARK_SIZE = CELL * 8


def stamp(pixels, view, sequence):
    """64 robust black/white cells: magic, view, uint32 sequence, CRC16."""
    payload = struct.pack("!BBI", 0xA7, VIEWS.index(view), sequence)
    payload += struct.pack("!H", binascii.crc_hqx(payload, 0xFFFF))
    bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8)).reshape(8, 8)
    pixels[:MARK_SIZE, :MARK_SIZE] = (
        np.repeat(np.repeat(bits, CELL, axis=0), CELL, axis=1)[..., None] * 255
    )


def identify(pixels):
    if pixels.shape[0] < MARK_SIZE or pixels.shape[1] < MARK_SIZE:
        raise ValueError("image too small for watermark")
    # Sample cell interiors; codec ringing at sharp boundaries isn't a bit.
    cells = pixels[:MARK_SIZE, :MARK_SIZE].reshape(8, CELL, 8, CELL, 3)
    bits = (
        cells[:, CELL // 4 : CELL * 3 // 4, :, CELL // 4 : CELL * 3 // 4].mean(
            axis=(1, 3, 4)
        )
        > 127
    )
    payload = np.packbits(bits.reshape(64)).tobytes()
    magic, view, sequence, checksum = struct.unpack("!BBIH", payload)
    if (
        magic != 0xA7
        or view >= len(VIEWS)
        or binascii.crc_hqx(payload[:6], 0xFFFF) != checksum
    ):
        raise ValueError("invalid media watermark")
    return VIEWS[view], sequence


def distribution(values):
    values = np.asarray(values, dtype=float)
    if not len(values):
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    if not np.isfinite(values).all():
        raise ValueError("non-finite measurement")
    return {
        "count": len(values),
        **{f"p{p}": float(np.percentile(values, p)) for p in (50, 95, 99)},
        "max": float(values.max()),
    }


def rates(timestamps, start, end):
    """Include empty seconds and the correctly normalized final partial second."""
    if not end > start:
        raise ValueError("invalid observation window")
    counts = [0] * math.ceil(end - start)
    for timestamp in timestamps:
        if start <= timestamp < end:
            counts[min(int(timestamp - start), len(counts) - 1)] += 1
    return {
        "count": sum(counts),
        "fps": sum(counts) / (end - start),
        "per_second_fps": [n / min(1, end - start - i) for i, n in enumerate(counts)],
    }


class Probe:
    def __init__(self, limit=60000):
        self.lock = threading.RLock()
        self.limit = limit
        self.frames = {}
        self.encoder_frames = {}
        self.decoded_frames = {}
        self.deliveries = {}
        self.codecs = []
        self.series = {}
        self.errors = {}
        self.high_water = 0

    def error(self, name):
        with self.lock:
            self.errors[name] = self.errors.get(name, 0) + 1

    def mark(self, key, stage, timestamp=None):
        with self.lock:
            if key not in self.frames:
                if len(self.frames) >= self.limit:
                    self.error("record_limit_exceeded")
                    return
                self.frames[key] = {}
                self.high_water = max(self.high_water, len(self.frames))
            self.frames[key].setdefault(
                stage, time.monotonic() if timestamp is None else timestamp
            )

    def sample(self, name, value, timestamp=None):
        with self.lock:
            series = self.series.setdefault(name, [])
            if len(series) >= self.limit:
                self.error("sample_limit_exceeded")
                return
            series.append((time.monotonic() if timestamp is None else timestamp, value))

    def window(self, views, start, end):
        with self.lock:
            records = [(key, dict(value)) for key, value in self.frames.items()]
            series = {key: list(value) for key, value in self.series.items()}
        pairs = {
            "capture_to_encoder_input": ("capture", "encoder_input"),
            "encoder_queue": ("encoder_input", "encode_start"),
            "encode_including_color_conversion": ("encode_start", "encode_end"),
            "encode_end_to_decode_start": ("encode_end", "decode_start"),
            "decode": ("decode_start", "decode_end"),
            "decode_to_track_recv": ("decode_end", "track_recv"),
            "receive_bgr_conversion": ("track_recv", "callback"),
            "watermark_probe": ("callback", "identified"),
            "cache_publication": ("identified", "cached"),
            "cache_to_render": ("cached", "render_start"),
            "qt_scale_pixmap": ("render_start", "render_end"),
            "render_to_paint": ("render_end", "paint"),
            "capture_to_receive": ("capture", "callback"),
            "capture_to_qt_paint": ("capture", "paint"),
        }
        result = {}
        for view in views:
            frames = [data for key, data in records if key[0] == view]
            latency = {}
            for name, (before, after) in pairs.items():
                samples = [
                    (data[after] - data[before]) * 1000
                    for data in frames
                    if before in data and after in data and start <= data[after] < end
                ]
                if any(value < 0 for value in samples):
                    self.error("negative_stage_duration")
                latency[name] = distribution(samples)
            paints = sorted(
                data["paint"]
                for data in frames
                if "paint" in data and start <= data["paint"] < end
            )
            # Include leading/trailing intervals, so a total freeze has a gap of
            # the whole window rather than disappearing from the report.
            boundaries = [start, *paints, end]
            gaps = [(b - a) * 1000 for a, b in pairwise(boundaries)]
            result[view] = {
                "rates": {
                    stage: rates([d[stage] for d in frames if stage in d], start, end)
                    for stage in (
                        "capture",
                        "encoder_input",
                        "encode_end",
                        "decode_end",
                        "callback",
                        "paint",
                    )
                },
                "latency_ms": latency,
                "presentation_gap_ms": distribution(gaps),
                "gaps_over_500ms": sum(gap >= 500 for gap in gaps),
            }
        return {
            "seconds": end - start,
            "views": result,
            "samples": {
                name: distribution(
                    [value for timestamp, value in samples if start <= timestamp < end]
                )
                for name, samples in series.items()
            },
        }

    def hooks(self):
        """Version-sensitive hooks fail closed; all are restored on exit."""
        import importlib.metadata

        import aioice.ice
        import aiortc.rtcrtpreceiver as rx
        import aiortc.rtcrtpsender as tx
        from rd_webrtc.video import VideoFrameTrack

        if importlib.metadata.version("aiortc") != "1.14.0":
            raise RuntimeError("review benchmark hooks for this aiortc version")
        stack = ExitStack()
        original_track = VideoFrameTrack.recv
        original_remote = rx.RemoteStreamTrack.recv
        encoder_factory, decoder_factory = tx.get_encoder, rx.get_decoder
        probe = self

        async def track_recv(track):
            frame = await original_track(track)
            key = (track.source.name, track._last_sequence)
            probe.mark(key, "encoder_input")
            with probe.lock:
                probe.encoder_frames[id(frame)] = key
            return frame

        class Encoder:
            def __init__(self, codec):
                self.inner = encoder_factory(codec)
                self.info = {
                    "mime_type": codec.mimeType,
                    "clock_rate": codec.clockRate,
                    "implementation": type(self.inner).__name__,
                }
                with probe.lock:
                    probe.codecs.append(self.info)

            @property
            def target_bitrate(self):
                return self.inner.target_bitrate

            @target_bitrate.setter
            def target_bitrate(self, value):
                self.inner.target_bitrate = value

            def encode(self, frame, *args):
                with probe.lock:
                    key = probe.encoder_frames.pop(id(frame), None)
                if key is None:
                    raise RuntimeError("missing encoder frame association")
                self.info["view"] = key[0]
                probe.mark(key, "encode_start")
                result = self.inner.encode(frame, *args)
                if result[0]:
                    probe.mark(key, "encode_end")
                context = getattr(self.inner, "codec", None)
                if context is not None:
                    self.info.update(
                        {
                            "codec": context.name,
                            "thread_count": context.thread_count,
                            "pixel_format": context.pix_fmt,
                        }
                    )
                return result

            def pack(self, packet):
                raise RuntimeError("this benchmark requires raw camera frames")

        class Decoder:
            def __init__(self, codec):
                self.inner = decoder_factory(codec)

            def decode(self, encoded):
                started = time.monotonic()
                frames = self.inner.decode(encoded)
                ended = time.monotonic()
                with probe.lock:
                    for frame in frames:
                        probe.decoded_frames[id(frame)] = (started, ended)
                return frames

        async def remote_recv(track):
            frame = await original_remote(track)
            with probe.lock:
                decoded = probe.decoded_frames.pop(id(frame), None)
                if decoded is None:
                    raise RuntimeError("missing decoder frame association")
                probe.deliveries[track.id] = (*decoded, time.monotonic())
            return frame

        stack.enter_context(
            patch.object(
                aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
            )
        )
        stack.enter_context(patch.object(VideoFrameTrack, "recv", track_recv))
        stack.enter_context(patch.object(tx, "get_encoder", Encoder))
        stack.enter_context(patch.object(rx, "get_decoder", Decoder))
        stack.enter_context(patch.object(rx.RemoteStreamTrack, "recv", remote_recv))
        return stack


def measured_cache(probe):
    from rd_driving.infrastructure.video.frame_buffer import FrameBuffer

    class Cache(FrameBuffer):
        def __init__(self):
            super().__init__()
            self.associations = OrderedDict()
            self.probe_lock = threading.RLock()
            self.last_sequence = {}

        def put(self, name, pixels, *, source):
            callback_at = time.monotonic()
            try:
                key = identify(pixels)
            except ValueError:
                probe.error("watermark_crc_or_magic")
                return False
            if key[0] != name:
                probe.error("watermark_view_mismatch")
                return False
            if key[1] <= self.last_sequence.get(name, 0):
                probe.error("watermark_sequence_not_new")
                return False
            self.last_sequence[name] = key[1]
            probe.mark(key, "callback", callback_at)
            probe.mark(key, "identified")
            with probe.lock:
                probe.frames[key]["source_peer_id"] = source.peer_id
                decoded = probe.deliveries.pop(name, None)
            if decoded is None:
                raise RuntimeError("missing track delivery")
            for stage, timestamp in zip(
                ("decode_start", "decode_end", "track_recv"), decoded
            ):
                probe.mark(key, stage, timestamp)
            with self.probe_lock:
                result = super().put(
                    name, pixels, source=source, received_at=callback_at
                )
                self.associations[name, super().get(name).id] = key
                while len(self.associations) > 1024:
                    self.associations.popitem(last=False)
                probe.mark(key, "cached")
                return result

        def get(self, name):
            with self.probe_lock:
                return super().get(name)

    return Cache()


def observe_display(widget, cache, probe):
    for name, display in widget._displays.items():
        original = display.update_frame

        def update(
            frame,
            frame_id,
            *,
            on_presented=None,
            name=name,
            display=display,
            original=original,
        ):
            if frame_id == display._last_frame_id:
                return original(frame, frame_id, on_presented=on_presented)
            with cache.probe_lock:
                key = cache.associations.get((name, frame_id))
            if key is None:
                raise RuntimeError("missing displayed frame association")
            probe.mark(key, "render_start")

            def presented():
                probe.mark(key, "paint")
                if on_presented is not None:
                    on_presented()

            original(frame, frame_id, on_presented=presented)
            probe.mark(key, "render_end")

        display.update_frame = update

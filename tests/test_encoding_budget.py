import asyncio
import threading
import time
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest
from aiortc.codecs import get_capabilities
from aiortc.codecs.vpx import Vp8Decoder, VpxPayloadDescriptor
from aiortc.jitterbuffer import JitterFrame
from av import VideoFrame
from rd_core.configuration import ConfigError, load_config
from rd_core.settings import CameraEncodingSettings, VehicleSettings
from rd_webrtc import encoding
from rd_webrtc.peer import PeerConnectionManager
from rd_webrtc.video import CapturedImage, FrameSource

VP8 = next(
    codec for codec in get_capabilities("video").codecs if codec.mimeType == "video/VP8"
)


def picture(width=800, height=480, pts=0):
    pixels = np.random.default_rng(42).integers(
        0, 256, (height, width, 3), dtype=np.uint8
    )
    frame = VideoFrame.from_ndarray(pixels, format="bgr24")
    frame.pts, frame.time_base = pts, Fraction(1, 90000)
    return frame


def publish(source, sequence, value=0):
    pixels = np.full((48, 64, 3), value, dtype=np.uint8)
    pixels.flags.writeable = False
    source.publish(CapturedImage(sequence, time.monotonic(), pixels))


def test_budget_reaches_real_codec_and_survives_rate_and_size_recreation():
    policy = CameraEncodingSettings(2_000_000, 100_000, 3_000_000, 1)
    encoder = encoding.create_encoder(VP8, policy)
    assert encoder.target_bitrate == 2_000_000  # Above upstream's hard-coded ceiling.
    for index, (target, expected, width) in enumerate(
        ((9_000_000, 3_000_000, 800), (1, 100_000, 800), (2_000_000, 2_000_000, 960))
    ):
        encoder.target_bitrate = target
        payloads, timestamp = encoder.encode(
            picture(width=width, pts=index * 3000), True
        )
        assert encoder.target_bitrate == encoder.codec.bit_rate == expected
        assert encoder.codec.thread_count == 1
        assert encoder.codec.width == width
        data = b"".join(VpxPayloadDescriptor.parse(payload)[1] for payload in payloads)
        decoded = Vp8Decoder().decode(JitterFrame(data, timestamp))
        assert len(decoded) == 1 and decoded[0].width == width


def test_strict_budget_configuration_and_independent_views():
    value = load_config(
        VehicleSettings,
        "vehicle",
        environ={},
        overrides={
            "encoder_workers": 2,
            "cameras": [
                {
                    "name": "front",
                    "encoding": {"target_bitrate_bps": 1_000_000, "max_threads": 1},
                },
                {
                    "name": "back",
                    "encoding": {
                        "target_bitrate_bps": 300_000,
                        "max_bitrate_bps": 500_000,
                    },
                },
            ],
        },
    ).value
    assert value.encoder_workers == 2
    assert value.cameras[0].encoding.target_bitrate_bps == 1_000_000
    assert value.cameras[1].encoding.max_bitrate_bps == 500_000
    assert value.cameras[1].encoding.max_threads is None
    first, second = (
        encoding.create_encoder(VP8, camera.encoding) for camera in value.cameras
    )
    first.target_bitrate = 9_000_000
    assert second.target_bitrate == 300_000
    for overrides in (
        {"encoder_workers": True},
        {"encoder_workers": 0},
        {"encoder_workers": 33},
        {"cameras": [{"encoding": {}}]},
        {"encoder_workers": 1, "cameras": [{"encoding": {"max_threads": 0}}]},
        {"encoder_workers": 1, "cameras": [{"encoding": {"min_bitrate_bps": 999_999}}]},
        {"encoder_workers": 1, "cameras": [{"encoding": {"target_bitrate_bps": True}}]},
    ):
        with pytest.raises(ConfigError):
            load_config(VehicleSettings, "vehicle", environ={}, overrides=overrides)


def test_unsupported_version_or_codec_is_rejected(monkeypatch):
    with pytest.raises(ConfigError, match="VP8"):
        encoding.create_encoder(
            SimpleNamespace(mimeType="video/H264"), CameraEncodingSettings()
        )
    monkeypatch.setattr(encoding.importlib.metadata, "version", lambda name: "unknown")
    with pytest.raises(ConfigError, match="reviewed"):
        encoding.EncodingPool(1)


@pytest.mark.asyncio
async def test_frozen_track_does_not_hold_a_worker_and_waiter_selects_latest(
    monkeypatch,
):
    pool = encoding.EncodingPool(1)
    peer = PeerConnectionManager([])
    sources = [FrameSource(name, 240) for name in ("front", "back", "left")]
    senders = [
        peer.add_track(source.subscribe(), encoding_pool=pool) for source in sources
    ]
    entered, release = threading.Event(), threading.Event()
    encoded = []

    class SlowEncoder:
        def encode(self, frame, force=False):
            if not entered.is_set():
                entered.set()
                release.wait(3)
            encoded.append(int(frame.to_ndarray(format="bgr24")[0, 0, 0]))
            return [b"payload"], frame.pts

    monkeypatch.setattr(encoding, "create_encoder", lambda *args: SlowEncoder())
    jobs = []
    try:
        # No image on front: it must not take the only encoding worker.
        jobs.append(asyncio.create_task(senders[0]._next_encoded_frame(VP8)))
        publish(sources[1], 1, 10)
        jobs.append(asyncio.create_task(senders[1]._next_encoded_frame(VP8)))
        assert await asyncio.to_thread(entered.wait, 1)
        publish(sources[2], 1, 20)
        jobs.append(asyncio.create_task(senders[2]._next_encoded_frame(VP8)))
        await asyncio.sleep(0.01)
        publish(sources[2], 2, 30)
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs[1:]), 2)
        assert encoded == [10, 30]
        assert not jobs[0].done()
    finally:
        release.set()
        for source in sources:
            source.close()
        await asyncio.gather(*jobs, return_exceptions=True)
        await peer.close()
        await pool.close()
    assert not any(t.name.startswith("rd-encode") for t in threading.enumerate())


@pytest.mark.asyncio
async def test_cancellation_keeps_slot_until_native_call_exits_and_close_joins(
    monkeypatch,
):
    pool = encoding.EncodingPool(1)
    peer = PeerConnectionManager([])
    source = FrameSource("front", 30)
    sender = peer.add_track(source.subscribe(), encoding_pool=pool)
    entered, release = threading.Event(), threading.Event()

    class SlowEncoder:
        def encode(self, frame, force=False):
            entered.set()
            release.wait(3)
            return [b"payload"], frame.pts

    monkeypatch.setattr(encoding, "create_encoder", lambda *args: SlowEncoder())
    job = closing = None
    try:
        publish(source, 1)
        job = asyncio.create_task(sender._next_encoded_frame(VP8))
        assert await asyncio.to_thread(entered.wait, 1)
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        closing = asyncio.create_task(pool.close())
        await asyncio.sleep(0.02)
        assert not job.done() and not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        await asyncio.wait_for(closing, 1)
        assert not any(t.name.startswith("rd-encode") for t in threading.enumerate())
    finally:
        release.set()
        source.close()
        await asyncio.gather(*(t for t in (job, closing) if t), return_exceptions=True)
        await peer.close()
        await pool.close()


@pytest.mark.asyncio
async def test_expired_encode_is_not_sent_and_next_frame_is_a_keyframe(monkeypatch):
    pool = encoding.EncodingPool(1)
    peer = PeerConnectionManager([])
    source = FrameSource("front", 240, max_age_s=0.02)
    sender = peer.add_track(source.subscribe(), encoding_pool=pool)
    calls = []

    class Encoder:
        def encode(self, frame, force=False):
            calls.append(force)
            if len(calls) == 1:
                time.sleep(0.04)
            return [b"payload"], frame.pts

    monkeypatch.setattr(encoding, "create_encoder", lambda *args: Encoder())
    try:
        publish(source, 1)
        assert await sender._next_encoded_frame(VP8) is None
        publish(source, 2)
        assert await sender._next_encoded_frame(VP8) is not None
        assert calls == [False, True]
        assert pool.expired == 1
    finally:
        source.close()
        await peer.close()
        await pool.close()

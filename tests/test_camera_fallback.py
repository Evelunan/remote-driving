import asyncio
import time

import numpy as np
import pytest
from rd_core.async_compat import async_timeout
from rd_core.configuration import ConfigError, load_config
from rd_core.settings import (
    CameraEncodingSettings,
    CameraSettings,
    ClientNetworkSettings,
    CredentialSettings,
    VehicleSettings,
    VideoSafetySettings,
)
from rd_vehicle.application.multi_camera_manager import MultiCameraManager
from rd_vehicle.infrastructure.cameras.buffer import CaptureBuffer
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.streams.track_manager import TrackManager
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
from rd_vehicle.main import _configure_cameras, _create_camera


def test_fallback_configuration_keeps_explicit_sources_and_accepts_missing_views():
    settings = load_config(
        VehicleSettings,
        "vehicle",
        environ={},
        overrides={
            "camera_fallback_source": "front",
            "video.primary_view": "right",
            "cameras": [
                {"name": "front", "source": 0},
                {"name": "back", "source": "synthetic"},
                {"name": "left"},
            ],
        },
    ).value
    assert [camera.source for camera in settings.cameras] == [0, "synthetic", None]
    assert settings.video.primary_view == "right"


@pytest.mark.parametrize(
    "source,cameras",
    [
        ("unknown", [{"name": "front", "source": 0}]),
        ("back", [{"name": "front", "source": 0}]),
        ("front", [{"name": "front"}]),
        ("front", [{"name": "front", "source": None}]),
    ],
)
def test_fallback_requires_a_configured_source(source, cameras):
    with pytest.raises(ConfigError, match="camera_fallback_source"):
        load_config(
            VehicleSettings,
            "vehicle",
            environ={},
            overrides={"camera_fallback_source": source, "cameras": cameras},
        )


class ManualCamera:
    def __init__(self, settings):
        self.camera_name = settings.name
        self.width, self.height, self.fps = (
            settings.width,
            settings.height,
            settings.fps,
        )
        self.buffer = CaptureBuffer()
        self.failure = None
        self.opened = self.closed = 0

    def set_on_frame(self, callback):
        self.buffer.set_on_change(callback)

    def get_snapshot(self):
        return self.buffer.snapshot()

    def start_capture(self):
        self.opened += 1
        self.buffer.publish_owned(np.full((self.height, self.width, 3), 50, np.uint8))

    def stop_capture(self):
        self.closed += 1
        self.buffer.invalidate()


class Publisher:
    def __init__(self):
        self.tracks = TrackManager()
        self.policies = {}

    def register_video_track(self, name, fps, max_age_s, *, encoding=None):
        self.tracks.register(name, fps, max_age_s)
        self.policies[name] = encoding

    async def push_frame(self, image, name):
        self.tracks.publish(name, image)

    def invalidate_frame(self, name):
        self.tracks.invalidate(name)


async def until(predicate):
    async with async_timeout(1):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_only_missing_sources_share_capture_and_invalidation():
    settings = VehicleSettings(
        camera_fallback_source="front",
        cameras=(
            CameraSettings("front", 0, 96, 64, 20),
            CameraSettings("back", "/dev/independent-camera", 32, 24, 10),
            CameraSettings("left"),
        ),
    )
    publisher = Publisher()
    manager = MultiCameraManager(publisher)
    created = {}

    def factory(config):
        camera = created[config.name] = ManualCamera(config)
        return camera

    _configure_cameras(manager, settings, factory)
    assert set(created) == {"front", "back"}
    assert set(publisher.tracks.sources) == {"front", "back", "left", "right"}
    assert publisher.tracks.sources["left"].fps == 20
    tracks = publisher.tracks.create_tracks("viewer")
    try:
        await manager.start()
        await until(
            lambda: all(s.latest is not None for s in publisher.tracks.sources.values())
        )
        sources = publisher.tracks.sources
        original = created["front"].get_snapshot()
        assert (
            sources["front"].latest
            is sources["left"].latest
            is sources["right"].latest
            is original
        )
        assert sources["back"].latest is not original
        assert not original.pixels.flags.writeable
        frames = await asyncio.wait_for(
            asyncio.gather(*(tracks[name].recv() for name in tracks)), 2
        )
        assert [(frame.width, frame.height) for frame in frames] == [
            (96, 64),
            (32, 24),
            (96, 64),
            (96, 64),
        ]
        assert len({id(frame) for frame in frames}) == 4
        # An invalid source must clear all copies without clearing independent input.
        created["front"].buffer.invalidate()
        await until(
            lambda: all(
                sources[name].latest is None for name in ("front", "left", "right")
            )
        )
        assert sources["back"].latest is not None
        stamp = time.monotonic()
        created["front"].buffer.publish_owned(
            np.zeros((64, 96, 3), np.uint8), captured_at=stamp
        )
        await until(lambda: sources["left"].latest is not None)
        assert sources["right"].latest.captured_at == stamp
        assert sources["right"].latest.sequence == original.sequence + 1
    finally:
        await manager.stop()
        await manager.stop()
        publisher.tracks.close()
    assert all(camera.opened == camera.closed == 1 for camera in created.values())
    assert all(source.latest is None for source in publisher.tracks.sources.values())


@pytest.mark.asyncio
async def test_copied_primary_expires_with_original_capture():
    settings = VehicleSettings(
        camera_fallback_source="front",
        cameras=(CameraSettings("front", "synthetic", 96, 64, 30),),
        video=VideoSafetySettings(primary_view="left", stale_frame_ms=100),
    )
    publisher = Publisher()
    manager = MultiCameraManager(publisher, video_policy=settings.video)
    _configure_cameras(manager, settings, ManualCamera)
    try:
        await manager.start()
        views = tuple(publisher.tracks.sources.values())
        await until(lambda: all(view.latest is not None for view in views))
        await until(lambda: all(view.latest is None for view in views))
        await until(lambda: all(view.latest is not None for view in views))
        assert manager.cameras[0].opened == 2
    finally:
        await manager.stop()
        publisher.tracks.close()


@pytest.mark.asyncio
async def test_configured_camera_failure_is_not_replaced_by_fallback():
    settings = VehicleSettings(
        camera_fallback_source="front",
        cameras=(CameraSettings("front", 0), CameraSettings("back", 2)),
    )
    publisher = Publisher()
    manager = MultiCameraManager(publisher)
    _configure_cameras(manager, settings, ManualCamera)
    try:
        await manager.start()
        await until(lambda: publisher.tracks.sources["back"].latest is not None)
        back = next(
            camera for camera in manager.cameras if camera.camera_name == "back"
        )
        back.failure = RuntimeError("device disconnected")
        back.buffer.invalidate()
        with pytest.raises(RuntimeError, match="capture worker failed: back"):
            await asyncio.wait_for(manager.wait(), 1)
        assert publisher.tracks.sources["back"].latest is None
    finally:
        await manager.stop()
        publisher.tracks.close()


def test_disabled_fallback_preserves_implicit_synthetic_camera():
    settings = VehicleSettings(cameras=(CameraSettings("front"),))
    publisher = Publisher()
    manager = MultiCameraManager(publisher)
    _configure_cameras(manager, settings, _create_camera)
    assert len(manager.cameras) == 1
    assert isinstance(manager.cameras[0], TestCamera)
    assert set(publisher.tracks.sources) == {"front"}
    publisher.tracks.close()


def test_copy_encoding_policy_is_per_view_and_inherits_for_omitted_views():
    source_policy = CameraEncodingSettings(target_bitrate_bps=600_000)
    left_policy = CameraEncodingSettings(target_bitrate_bps=400_000)
    settings = VehicleSettings(
        camera_fallback_source="front",
        encoder_workers=2,
        cameras=(
            CameraSettings("front", "synthetic", encoding=source_policy),
            CameraSettings("left", encoding=left_policy),
        ),
    )
    settings.validate()
    publisher = Publisher()
    manager = MultiCameraManager(publisher)
    _configure_cameras(manager, settings, ManualCamera)
    assert publisher.policies == {
        "front": source_policy,
        "back": source_policy,
        "left": left_policy,
        "right": source_policy,
    }
    publisher.tracks.close()


@pytest.mark.asyncio
async def test_fallback_four_views_arrive_over_real_webrtc(monkeypatch):
    import aioice.ice
    from aiortc import RTCConfiguration, RTCPeerConnection
    from aiortc.mediastreams import MediaStreamError
    from rd_webrtc.video_timing import ReceiverTiming

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )
    settings = VehicleSettings(
        camera_fallback_source="front",
        cameras=(CameraSettings("front", "synthetic", 96, 64, 20),),
    )
    streamer = WebRTCStreamer(
        "room", ClientNetworkSettings(), CredentialSettings(), session=1234
    )
    manager = MultiCameraManager(streamer)
    _configure_cameras(manager, settings, _create_camera)
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    clock = ReceiverTiming()
    receiver.on("datachannel", clock.attach)
    tasks, received = [], {}
    timed = set()

    async def consume(track):
        rtp = next(r for r in receiver.getReceivers() if r.track is track)
        try:
            while True:
                frame = await track.recv()
                received[track.id] = (frame.width, frame.height)
                captured_at = clock.capture_time(rtp, frame.pts)
                if captured_at is not None and 0 <= time.monotonic() - captured_at < 1:
                    timed.add(track.id)
        except MediaStreamError:
            pass

    @receiver.on("track")
    def on_track(track):
        tasks.append(asyncio.create_task(consume(track)))

    try:
        await manager.start()
        peer = await streamer._create_peer("viewer")
        async with async_timeout(5):
            await receiver.setRemoteDescription(await peer.create_offer())
            await receiver.setLocalDescription(await receiver.createAnswer())
            answer = receiver.localDescription
            await peer.set_answer(answer.sdp, answer.type)
            while len(timed) < 4:
                await asyncio.sleep(0.01)
        assert received == {
            name: (96, 64) for name in ("front", "back", "left", "right")
        }
        assert len(manager.cameras) == 1
    finally:
        await manager.stop()
        await streamer.close()
        await receiver.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert manager.cameras[0]._thread is None

import asyncio
import time
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame
from av.video.reformatter import ColorRange
from rd_core.async_compat import ExceptionGroup, async_timeout
from rd_core.settings import CameraSettings, ClientNetworkSettings, CredentialSettings
from rd_vehicle.application.multi_camera_manager import (
    MultiCameraManager,
    _FrameNotification,
)
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.cameras.thread_camera import ThreadCamera
from rd_vehicle.infrastructure.cameras.usb_camera import USBCamera
from rd_vehicle.infrastructure.streams.track_manager import TrackManager
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
from rd_vehicle.main import _create_camera
from rd_webrtc.video import CapturedImage, FrameSource


def captured(sequence, value=0, *, at=None):
    pixels = np.full((24, 32, 3), value, dtype=np.uint8)
    pixels.flags.writeable = False
    return CapturedImage(sequence, time.monotonic() if at is None else at, pixels)


@pytest.mark.asyncio
@pytest.mark.parametrize("decoded_format", ["yuvj422p", "yuv420p"])
async def test_pyav_yuv_capture_preserves_limited_range_and_independent_subscribers(
    monkeypatch, decoded_format,
):
    from rd_vehicle.infrastructure.cameras import pyav_camera

    black = VideoFrame(32, 24, decoded_format)
    white = VideoFrame(32, 24, decoded_format)
    black.color_range = white.color_range = ColorRange.JPEG
    for frame, luma in ((black, 0), (white, 255)):
        for index, plane in enumerate(frame.planes):
            plane.update(bytes([128 if index else luma]) * plane.buffer_size)

    class Capture:
        streams = SimpleNamespace(video=[SimpleNamespace(index=0, average_rate=30)])

        def decode(self, video):
            assert video == 0
            return iter((black, white))

        def close(self):
            pass

    monkeypatch.setattr(pyav_camera.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pyav_camera.av, "open", lambda *a, **kw: Capture())
    camera = _create_camera(
        CameraSettings(
            source="/dev/video0", capture_backend="pyav", capture_pixel_format="yuv420p"
        )
    )
    camera._open()
    source = FrameSource("front", fps=120)
    try:
        camera.buffer.publish_owned(camera._read())
        captured_black = camera.get_snapshot()
        assert captured_black.pixel_format == "yuv420p"
        assert not captured_black.pixels.flags.writeable
        assert np.all(captured_black.pixels[:24] == 16)
        assert np.all(captured_black.pixels[24:] == 128)
        assert np.all(camera.get_latest_frame() == 0)

        camera.buffer.publish_owned(camera._read())
        captured_white = camera.get_snapshot()
        assert np.all(captured_white.pixels[:24] == 235)
        assert np.all(captured_black.pixels[:24] == 16)
        assert source.publish(captured_white)
        a = await source.subscribe().recv()
        b = await source.subscribe().recv()
        assert (a.format.name, a.width, a.height) == ("yuv420p", 32, 24)
        assert a.color_range == ColorRange.MPEG
        a.pts = 123
        a.planes[0].update(bytes(a.planes[0].buffer_size))
        assert b.pts == 0
        assert np.all(b.to_ndarray()[:24] == 235)
        assert np.all(captured_white.pixels[:24] == 235)
    finally:
        camera._close()
        source.close()


@pytest.mark.asyncio
async def test_subscribers_are_independent_and_close_wakes_recv():
    manager = TrackManager()
    manager.register("front", fps=120)
    a = manager.create_tracks("a")["front"]
    manager.publish("front", captured(1, 10))
    frame_a = await a.recv()
    b = manager.create_tracks("b")["front"]
    frame_b = await b.recv()
    assert a.readyState == b.readyState == "live"
    assert frame_a is not frame_b
    frame_a.pts = 123456
    assert frame_b.pts == 0
    assert frame_a.time_base == Fraction(1, 90000)
    manager.create_tracks("b")
    assert b.readyState == "ended" and a.readyState == "live"
    manager.publish("front", captured(2, 20))
    assert np.all((await a.recv()).to_ndarray(format="bgr24") == 20)
    waiting = asyncio.create_task(a.recv())
    await asyncio.sleep(0)
    manager.close_tracks("b")
    assert manager.sources["front"].subscriber_count == 1
    manager.close()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(waiting, 0.2)
    assert manager.sources["front"].subscriber_count == 0


@pytest.mark.asyncio
async def test_absent_duplicate_and_expired_capture_never_generate_new_frames():
    source = FrameSource("front", 120, max_age_s=0.1)
    track = source.subscribe()
    try:
        for image in (None, captured(1, at=time.monotonic() - 1)):
            if image is not None:
                source.publish(image)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(track.recv(), 0.025)
        source.publish(captured(2, 7))
        assert np.all((await track.recv()).to_ndarray(format="bgr24") == 7)
        assert not source.publish(captured(2, 8))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(track.recv(), 0.025)
        source.publish(captured(3, 9))
        assert np.all((await track.recv()).to_ndarray(format="bgr24") == 9)
    finally:
        source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fps", [10, 60])
async def test_rate_uses_camera_fps_and_pts_uses_capture_time(fps):
    source = FrameSource("front", fps)
    track = source.subscribe()
    first = captured(1, 1)
    source.publish(first)
    try:
        assert (await track.recv()).pts == 0
        started = time.monotonic()
        second = captured(2, 2)
        source.publish(second)
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        # A slow subscriber receives the latest image, not an accumulating queue.
        for sequence in range(3, 203):
            latest = captured(sequence, sequence)
            source.publish(latest)
        frame = await asyncio.wait_for(pending, 1)
        assert time.monotonic() - started >= 1 / fps - 0.003
        assert np.all(frame.to_ndarray(format="bgr24") == 202)
        assert frame.pts == max(
            1, round((latest.captured_at - first.captured_at) * 90000)
        )
        assert source.latest is latest
        source.publish(captured(203))
        blocked = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        track.stop()
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(blocked, 0.2)
    finally:
        source.close()


@pytest.mark.asyncio
async def test_conversion_time_is_inside_frame_period_and_late_work_does_not_catch_up(
    monkeypatch,
):
    import rd_webrtc.video as module

    clock = SimpleNamespace(now=10.0)

    class Conversion:
        @staticmethod
        def from_ndarray(pixels, **kwargs):
            clock.now += 0.04  # 40 ms of work within a 100 ms frame period.
            return SimpleNamespace()

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "VideoFrame", Conversion)
    source = FrameSource("front", 10)
    track = source.subscribe()
    pending = None
    try:
        source.publish(captured(1, at=clock.now))
        assert (await track.recv()).pts == 0
        clock.now = 10.101  # Timer woke up 1 ms late.
        source.publish(captured(2, at=clock.now))
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        assert pending.done(), "frame conversion must not add another 40 ms of pacing"
        assert pending.result().pts == 9090

        clock.now = 10.2
        source.publish(captured(3, at=clock.now))
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        assert pending.done(), "timer wake-up delay must not accumulate every frame"
        assert pending.result().pts == 18000

        # A long scheduling interruption selects one latest frame, then starts
        # a new interval instead of emitting missed historical slots.
        clock.now = 11.0
        source.publish(captured(4, at=clock.now))
        assert (await track.recv()).pts == 90000
        source.publish(captured(5, at=clock.now))
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        assert not pending.done()
        clock.now = 11.1
        source.publish(captured(6, at=clock.now))
        await asyncio.sleep(0)
        assert pending.done() and pending.result().pts == 99000
    finally:
        source.close()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_conversion_overrun_skips_slots_and_waits_before_next_latest_frame(
    monkeypatch,
):
    import rd_webrtc.video as module

    clock = SimpleNamespace(now=10.0)

    class SlowConversion:
        @staticmethod
        def from_ndarray(pixels, **kwargs):
            clock.now += 0.24
            return SimpleNamespace()

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "VideoFrame", SlowConversion)
    source = FrameSource("front", 10)
    track = source.subscribe()
    pending = None
    try:
        source.publish(captured(1, at=clock.now))
        assert (await track.recv()).pts == 0
        source.publish(captured(2, at=clock.now))
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(0)
        assert not pending.done(), (
            "an overrun must not emit missed frame slots immediately"
        )
        clock.now = 10.3
        source.publish(captured(3, at=clock.now))
        await asyncio.sleep(0)
        assert pending.done() and pending.result().pts == 27000
    finally:
        source.close()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


class LocalPublisher:
    def __init__(self):
        self.tracks = TrackManager()
        self.count = 0

    def register_video_track(self, name, fps, max_age_s=0.5):
        self.tracks.register(name, fps)

    async def push_frame(self, image, name):
        self.count += 1
        return self.tracks.publish(name, image)

    def invalidate_frame(self, name):
        self.tracks.invalidate(name)


@pytest.mark.parametrize(
    ("level", "sample_levels"), [("INFO", ["INFO"]), ("DEBUG", ["INFO", "DEBUG"])]
)
def test_capture_pacing_absorbs_timer_overshoot(monkeypatch, caplog, level, sample_levels):
    from types import SimpleNamespace

    from rd_vehicle.infrastructure.cameras import thread_camera

    elapsed = 0.0
    waits = 0
    camera = TestCamera(32, 24, 60, "front")
    pixels = np.zeros((24, 32, 3), np.uint8)

    def read():
        nonlocal elapsed
        elapsed += 0.002
        return pixels

    def wait(delay):
        nonlocal elapsed, waits
        assert delay >= 0
        elapsed += delay + 0.004
        waits += 1

    clock = SimpleNamespace(
        perf_counter=lambda: elapsed, monotonic=lambda: int(elapsed * 64) / 64
    )
    monkeypatch.setattr(thread_camera, "time", clock)
    camera._read = read
    camera._stop = SimpleNamespace(is_set=lambda: waits >= 1260, wait=wait)
    with caplog.at_level(level, logger=thread_camera.__name__):
        camera._run()
    assert camera.failure is None and camera.get_frame_count() == 1260
    assert elapsed == pytest.approx(21.004)
    samples = [r for r in caplog.records if "read_fps=" in r.msg]
    assert [r.levelname for r in samples] == sample_levels
    for sample in samples:
        _, fps, frames, seconds, target, read_percent = sample.args
        assert seconds >= 10 and frames / seconds == fps
        assert fps == pytest.approx(target, abs=0.15)
        assert read_percent == pytest.approx(100 * frames * 0.002 / seconds)


@pytest.mark.asyncio
async def test_two_reads_in_one_clock_tick_do_not_reopen_or_extend_primary_deadline():
    from rd_core.settings import VideoSafetySettings

    publisher = LocalPublisher()
    health = []
    camera = TestCamera(32, 24, 60, "front")
    manager = MultiCameraManager(
        publisher, video_policy=VideoSafetySettings(stale_frame_ms=150),
        on_capture_health=health.append,
    )
    manager.add_camera(camera)
    stamp = time.monotonic()
    image = captured(1, at=stamp)
    camera.get_snapshot = lambda: image
    notice = _FrameNotification(asyncio.get_running_loop())
    manager._running = True
    worker = asyncio.create_task(manager._isolated_worker(camera, notice, True, False))
    try:
        async with async_timeout(1):
            while publisher.count < 1:
                await asyncio.sleep(0)
            image = captured(2, at=stamp)
            notice.event.set()
            while publisher.count < 2 and not health:
                await asyncio.sleep(0)
            assert publisher.count == 2 and not health
            while not health:
                image = captured(image.sequence + 1, at=stamp)
                notice.event.set()
                await asyncio.sleep(0.01)
            assert health == [False]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        publisher.tracks.close()


@pytest.mark.asyncio
async def test_camera_manager_stop_freeze_resume_and_no_orphan_workers():
    for _ in range(3):
        publisher = LocalPublisher()
        manager = MultiCameraManager(publisher)
        front = TestCamera(width=64, height=48, fps=30, name="front")
        back = TestCamera(width=64, height=48, fps=60, name="back")
        manager.add_camera(front)
        manager.add_camera(back)
        tracks = publisher.tracks.create_tracks("viewer")
        try:
            await manager.start()
            assert (await asyncio.wait_for(tracks["front"].recv(), 1)).width == 64
            assert (await asyncio.wait_for(tracks["back"].recv(), 1)).height == 48
            await asyncio.to_thread(front.stop_capture)
            await asyncio.sleep(0.02)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(tracks["front"].recv(), 0.08)
            await asyncio.wait_for(tracks["back"].recv(), 1)
            await asyncio.to_thread(front.start_capture)
            await asyncio.wait_for(tracks["front"].recv(), 1)
        finally:
            await manager.stop()
            await manager.stop()
            publisher.tracks.close()
        assert all(task.done() for task in manager._workers + manager._starting)
        assert front._thread is None and back._thread is None
        count = publisher.count
        await asyncio.sleep(0.02)
        assert publisher.count == count


@pytest.mark.asyncio
async def test_camera_start_failure_and_cancellation_roll_back_threads():
    class FailedCamera(ThreadCamera):
        def _open(self):
            raise RuntimeError("fake camera open failure")

    publisher = LocalPublisher()
    manager = MultiCameraManager(publisher)
    good = TestCamera(width=32, height=24, name="front")
    bad = FailedCamera(32, 24, 30, "back")
    manager.add_camera(good)
    manager.add_camera(bad)
    with pytest.raises(ExceptionGroup, match="startup failed"):
        await manager.start()
    assert good._thread is None and bad._thread is None
    assert not manager._workers
    publisher.tracks.close()

    class SlowOpen(TestCamera):
        def _open(self):
            time.sleep(0.05)

    publisher = LocalPublisher()
    manager = MultiCameraManager(publisher)
    slow = SlowOpen(width=32, height=24, name="front")
    manager.add_camera(slow)
    task = asyncio.create_task(manager.start())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow._thread is None
    publisher.tracks.close()


@pytest.mark.asyncio
async def test_usb_startup_logs_each_failed_device(monkeypatch, caplog):
    from rd_vehicle.infrastructure.cameras import usb_camera

    class Capture:
        def __init__(self, source, backend):
            self.source = source

        def isOpened(self):
            return self.source < 2

        def getBackendName(self):
            return "DSHOW"

        def set(self, key, value):
            return True

        def get(self, key):
            return 30

        def grab(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(usb_camera.cv2, "VideoCapture", Capture)
    monkeypatch.setattr(usb_camera.platform, "system", lambda: "Windows")
    caplog.set_level("INFO")
    publisher = LocalPublisher()
    manager = MultiCameraManager(publisher)
    for source, name in enumerate(("front", "back", "left", "right")):
        manager.add_camera(USBCamera(source, name))
    with pytest.raises(ExceptionGroup, match="startup failed"):
        await manager.start()
    publisher.tracks.close()
    for source, name in enumerate(("front", "back", "left", "right")):
        assert f"Camera {name} opening: source={source} backend=DSHOW" in caplog.text
        if source < 2:
            assert f"Camera {name} opened: source={source}" in caplog.text
        else:
            assert (
                f"Camera {name} open failed: source={source} backend=DSHOW isOpened=False"
                in caplog.text
            )


def test_empty_reads_warn_once_until_frames_arrive(caplog):
    class DelayedFrames(TestCamera):
        reads = 0

        def _read(self):
            self.reads += 1
            return None if self.reads <= 3 else super()._read()

    caplog.set_level("INFO")
    camera = DelayedFrames(width=32, height=24, fps=120, name="back")
    camera.start_capture()
    try:
        deadline = time.monotonic() + 1
        while camera.get_frame_count() < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert camera.get_frame_count() >= 2
    finally:
        camera.stop_capture()
    assert caplog.messages.count("Camera back returned no frame") == 1
    assert caplog.messages.count("Camera back receiving frames") == 1


def test_notifications_coalesce_without_retaining_images():
    queued = []
    loop = SimpleNamespace(call_soon_threadsafe=queued.append)
    notification = _FrameNotification(loop)
    for _ in range(10000):
        notification.notify()
    assert len(queued) == 1
    queued.pop()()
    assert notification.event.is_set()
    notification.notify()
    assert len(queued) == 1
    notification.close()
    queued.pop()()
    notification.notify()
    assert not queued


@pytest.mark.parametrize(
    "system, source, backend",
    [
        ("Windows", 0, "CAP_DSHOW"),
        ("Windows", "video.mp4", "CAP_ANY"),
        ("Linux", "/dev/v4l/by-id/example", "CAP_V4L2"),
        ("Linux", 0, "CAP_V4L2"),
        ("Darwin", 0, "CAP_ANY"),
    ],
)
def test_usb_handle_opens_at_start_and_closes_on_owner_thread(
    monkeypatch, system, source, backend
):
    import threading

    from rd_vehicle.infrastructure.cameras import usb_camera

    calls = []

    class Capture:
        def __init__(self, source, backend):
            self.format = 0
            calls.append(("open", threading.get_ident(), source, backend))

        def set(self, key, value):
            if key == usb_camera.cv2.CAP_PROP_FOURCC:
                self.format = value
            elif backend == "CAP_DSHOW":
                self.format = usb_camera.cv2.VideoWriter.fourcc(*"YUY2")
            return True

        def get(self, key):
            if key == usb_camera.cv2.CAP_PROP_FOURCC:
                return self.format
            return 60

        def getBackendName(self):
            return "mock"

        def isOpened(self):
            return True

        def grab(self):
            return True

        def retrieve(self):
            return True, np.zeros((24, 32, 3), dtype=np.uint8)

        def release(self):
            calls.append(("close", threading.get_ident(), None))

    monkeypatch.setattr(usb_camera.cv2, "VideoCapture", Capture)
    monkeypatch.setattr(usb_camera.platform, "system", lambda: system)
    camera = USBCamera(source, "front", width=64, height=48, fps=60)
    assert not calls
    camera.start_capture()
    deadline = time.monotonic() + 1
    while camera.get_snapshot() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    image = camera.get_snapshot()
    assert image.pixels.shape == (24, 32, 3)  # negotiated dimensions preserved
    assert not image.pixels.flags.writeable
    camera.stop_capture()
    assert [call[0] for call in calls] == ["open", "close"]
    assert calls[0][1] == calls[1][1] != threading.get_ident()
    assert calls[0][2] == source
    assert calls[0][3] == getattr(usb_camera.cv2, backend)
    assert camera.diagnostics["format"] == "MJPG"
    assert camera.get_snapshot() is None


def test_directshow_applies_mjpg_dimensions_after_setting_rate(monkeypatch):
    from unittest.mock import Mock

    from rd_vehicle.infrastructure.cameras import usb_camera

    cv = usb_camera.cv2
    camera = USBCamera(0, "front", width=1280, height=720, fps=60)
    capture = Mock()
    capture.get.return_value = cv.VideoWriter.fourcc(*"MJPG")
    monkeypatch.setattr(cv, "VideoCapture", lambda *args: capture)
    monkeypatch.setattr(usb_camera.platform, "system", lambda: "Windows")
    camera._open()
    try:
        assert [c.args for c in capture.set.call_args_list] == [
            (cv.CAP_PROP_BUFFERSIZE, 1),
            (cv.CAP_PROP_FPS, 60),
            (cv.CAP_PROP_FOURCC, cv.VideoWriter.fourcc(*"MJPG")),
            (cv.CAP_PROP_FRAME_WIDTH, 1280),
            (cv.CAP_PROP_FRAME_HEIGHT, 720),
        ]
    finally:
        camera._close()


@pytest.mark.asyncio
async def test_streamer_peer_replacement_and_stop_during_listen():
    config = ClientNetworkSettings(signaling_url="ws://127.0.0.1:1")
    streamer = WebRTCStreamer("room", config, CredentialSettings(), session=42)
    streamer.register_video_track("front", fps=60)
    first = await streamer._create_peer("a")
    old_a = streamer.track_manager.subscriptions["a"]["front"]
    second = await streamer._create_peer("b")
    track_b = streamer.track_manager.subscriptions["b"]["front"]
    await streamer._create_peer("a")
    assert old_a.readyState == "ended" and first.pc.connectionState == "closed"
    assert track_b.readyState == "live" and second.pc.connectionState != "closed"
    listening = asyncio.Event()

    async def listen():
        listening.set()
        await asyncio.Future()

    streamer.signaling.connect = AsyncMock()
    streamer.signaling.send = AsyncMock()
    streamer.signaling.join = AsyncMock(
        return_value={"id": "id", "peers": [], "peer_roles": {}}
    )
    streamer.signaling.listen = listen
    streamer.signaling.close = AsyncMock()
    task = asyncio.create_task(streamer.start())
    await asyncio.wait_for(listening.wait(), 1)
    await asyncio.wait_for(streamer.close(), 1)
    await streamer.close()
    assert task.done() and not streamer.peers
    assert track_b.readyState == "ended"
    assert streamer.signaling.connect.await_count == 1
    assert streamer.track_manager.sources["front"].closed
    with pytest.raises(RuntimeError, match="single-use"):
        await streamer.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_view", ["front", "back"])
async def test_isolated_camera_reopens_without_interrupting_healthy_track(broken_view):
    from rd_core.settings import VideoSafetySettings
    from rd_vehicle.application.capture_health import CaptureHealth

    fail = False

    class Intermittent(TestCamera):
        def _read(self):
            if fail:
                raise OSError("simulated camera outage")
            return super()._read()

    publisher = LocalPublisher()
    health = CaptureHealth()
    manager = MultiCameraManager(
        publisher, video_policy=VideoSafetySettings(), on_capture_health=health.update
    )
    healthy_view = "front" if broken_view == "back" else "back"
    manager.add_camera(Intermittent(64, 48, 30, broken_view))
    manager.add_camera(TestCamera(64, 48, 30, healthy_view))
    tracks = publisher.tracks.create_tracks("viewer")
    try:
        await manager.start()
        await asyncio.wait_for(tracks[broken_view].recv(), 2)
        await asyncio.wait_for(tracks[healthy_view].recv(), 2)
        revision = health.permit()
        assert revision is not None
        fail = True
        async with async_timeout(2):
            while health.permit() is not None:
                await asyncio.sleep(0.01)
        first = await asyncio.wait_for(tracks[healthy_view].recv(), 1)
        second = await asyncio.wait_for(tracks[healthy_view].recv(), 1)
        assert second.pts > first.pts
        fail = False
        await asyncio.wait_for(tracks[broken_view].recv(), 3)
        assert health.permit() is not None and health.permit() != revision
    finally:
        await manager.stop()
        publisher.tracks.close()
    assert health.permit() is None


@pytest.mark.asyncio
async def test_capture_failure_propagates_and_manager_closes_other_cameras():
    class Failing(TestCamera):
        def _read(self):
            if self.get_frame_count() >= 2:
                raise RuntimeError("simulated unplug failure")
            return super()._read()

    publisher = LocalPublisher()
    manager = MultiCameraManager(publisher)
    failed = Failing(width=32, height=24, fps=30, name="front")
    other = TestCamera(width=32, height=24, fps=30, name="back")
    manager.add_camera(failed)
    manager.add_camera(other)
    try:
        await manager.start()
        with pytest.raises(RuntimeError, match="capture worker failed"):
            await asyncio.wait_for(manager.wait(), 1)
    finally:
        await manager.stop()
        publisher.tracks.close()
    assert failed._thread is other._thread is None


@pytest.mark.asyncio
async def test_real_webrtc_two_receivers_share_capture_and_close_independently(
    monkeypatch,
):
    import aioice.ice
    from aiortc import RTCConfiguration, RTCPeerConnection

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )
    config = ClientNetworkSettings(signaling_url="ws://127.0.0.1:1")
    streamer = WebRTCStreamer("room", config, CredentialSettings(), session=42)
    manager = MultiCameraManager(streamer)
    camera = TestCamera(width=96, height=64, fps=30, name="front")
    manager.add_camera(camera)
    receivers, consumers, counts = {}, [], {"a": 0, "b": 0}

    async def consume(track, name):
        try:
            while True:
                frame = await track.recv()
                assert frame.width == 96 and frame.height == 64
                counts[name] += 1
        except MediaStreamError:
            pass

    async def connect(name):
        receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        receivers[name] = receiver

        @receiver.on("track")
        def on_track(track):
            assert track.id == "front"
            consumers.append(asyncio.create_task(consume(track, name)))

        sender = await streamer._create_peer(name)
        offer = await sender.create_offer()
        await receiver.setRemoteDescription(offer)
        await receiver.setLocalDescription(await receiver.createAnswer())
        answer = receiver.localDescription
        await sender.set_answer(answer.sdp, answer.type)

    async def until(predicate):
        async with async_timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    try:
        await manager.start()
        await connect("a")
        await until(lambda: counts["a"] >= 2)
        first_count = counts["a"]
        await connect("b")
        await until(lambda: counts["b"] >= 2 and counts["a"] > first_count)
        b_count = counts["b"]
        await streamer._on_peer_left({"id": "a"})
        await receivers["a"].close()
        await until(lambda: counts["b"] > b_count)
        assert streamer.track_manager.sources["front"].subscriber_count == 1
        a_count = counts["a"]
        await connect("a")
        await until(lambda: counts["a"] > a_count)
    finally:
        await manager.stop()
        await streamer.close()
        await asyncio.gather(*(peer.close() for peer in receivers.values()))
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
    assert camera._thread is None
    assert not streamer.peers and not streamer._retired

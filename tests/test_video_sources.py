import asyncio
import time
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from aiortc.mediastreams import MediaStreamError
from rd_vehicle.application.multi_camera_manager import (
    MultiCameraManager,
    _FrameNotification,
)
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.cameras.thread_camera import ThreadCamera
from rd_vehicle.infrastructure.cameras.usb_camera import USBCamera
from rd_vehicle.infrastructure.streams.track_manager import TrackManager
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
from rd_webrtc.video import CapturedImage, FrameSource


def captured(sequence, value=0, *, at=None):
    pixels = np.full((24, 32, 3), value, dtype=np.uint8)
    pixels.flags.writeable = False
    return CapturedImage(sequence, time.monotonic() if at is None else at, pixels)


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
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(track.recv(), 0.025)
        source.publish(captured(2, 7))
        assert np.all((await track.recv()).to_ndarray(format="bgr24") == 7)
        assert not source.publish(captured(2, 8))
        with pytest.raises(TimeoutError):
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


class LocalPublisher:
    def __init__(self):
        self.tracks = TrackManager()
        self.count = 0

    def register_video_track(self, name, fps):
        self.tracks.register(name, fps)

    async def push_frame(self, image, name):
        self.count += 1
        return self.tracks.publish(name, image)

    def invalidate_frame(self, name):
        self.tracks.invalidate(name)


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
            with pytest.raises(TimeoutError):
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


def test_usb_handle_opens_at_start_and_closes_on_owner_thread(monkeypatch):
    import threading

    from rd_vehicle.infrastructure.cameras import usb_camera

    calls = []

    class Capture:
        def __init__(self, source, backend):
            calls.append(("open", threading.get_ident(), source))

        def set(self, key, value):
            pass

        def get(self, key):
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
    camera = USBCamera("/dev/v4l/by-id/example", "front", width=64, height=48, fps=60)
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
    assert calls[0][2] == "/dev/v4l/by-id/example"
    assert camera.get_snapshot() is None


@pytest.mark.asyncio
async def test_streamer_peer_replacement_and_stop_during_listen():
    config = SimpleNamespace(
        signaling=SimpleNamespace(url="ws://127.0.0.1:1"), ice_servers=[]
    )
    streamer = WebRTCStreamer("room", config)
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
    config = SimpleNamespace(
        signaling=SimpleNamespace(url="ws://127.0.0.1:1"), ice_servers=[]
    )
    streamer = WebRTCStreamer("room", config)
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
        async with asyncio.timeout(3):
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
    assert not streamer.peers and not streamer._closing_peers

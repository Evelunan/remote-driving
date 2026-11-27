import asyncio
import time

import pytest
from rd_core.async_compat import async_timeout
from rd_core.settings import ClientNetworkSettings, CredentialSettings
from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
from rd_vehicle.application.multi_camera_manager import MultiCameraManager
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer


async def until(predicate, timeout=4):
    async with async_timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_missing_or_forged_publisher_metadata_cannot_become_a_video_source():
    receiver = WebRTCReceiver("room", ClientNetworkSettings(), CredentialSettings())
    for data in (
        {"peers": ["p"], "peer_roles": {"p": "vehicle"}},
        {
            "peers": ["p"],
            "peer_roles": {"p": "vehicle"},
            "peer_sources": {"p": {"principal": "v", "session": True}},
        },
    ):
        with pytest.raises(ValueError, match="video source"):
            await receiver._on_joined(data)
        assert not receiver._publishers
    with pytest.raises(PermissionError):
        await receiver._on_offer(
            {"from": "forged", "source": {"principal": "v", "session": 42}}
        )
    await receiver.close()


@pytest.mark.asyncio
async def test_repeated_decoded_pts_is_not_new_picture_evidence():
    import numpy as np
    from av import VideoFrame
    from rd_core.video_safety import VideoSource

    receiver = WebRTCReceiver("room", ClientNetworkSettings(), CredentialSettings())
    receiver._publishers["p"] = VideoSource("vehicle", 42, "p")
    peer = await receiver._create_peer("p")
    frames = []
    receiver.on_frame(lambda *args: frames.append(args))

    class Repeated:
        id = "front"
        stopped = False

        async def recv(self):
            frame = VideoFrame.from_ndarray(
                np.zeros((24, 32, 3), dtype=np.uint8), format="bgr24"
            )
            frame.pts = 0
            return frame

        def stop(self):
            self.stopped = True

    track = Repeated()
    try:
        with pytest.raises(ValueError, match="timestamp did not advance"):
            await receiver._recv_track("p", peer, track)
        assert len(frames) == 1 and track.stopped
    finally:
        await receiver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("budgeted", [False, True])
async def test_actual_authenticated_media_clients_reconnect_and_own_all_tasks(
    runtime_factory, local_tls, monkeypatch, budgeted
):
    import aioice.ice

    monkeypatch.setattr(
        aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
    )
    build, _ = runtime_factory
    runtime = build(("signaling",), signaling_options={"max_peers_per_room": 3})
    await runtime.start()
    service = runtime.services["signaling"]
    port = service._server.sockets[0].getsockname()[1]
    network = ClientNetworkSettings(
        signaling_url=f"wss://127.0.0.1:{port}",
        signaling_ca_file=local_tls[0],
        reconnect_delay_s=0.05,
    )
    publisher = WebRTCStreamer(
        "video",
        network,
        CredentialSettings("TEST_VEHICLE"),
        session=42,
        encoder_workers=1 if budgeted else None,
    )
    driver = WebRTCReceiver("video", network, CredentialSettings("TEST_DRIVER"))
    viewer = WebRTCReceiver("video", network, CredentialSettings("TEST_VIEWER"))
    manager = MultiCameraManager(publisher)
    camera = TestCamera(width=96, height=64, fps=30, name="front")
    manager.add_camera(camera)
    frames, statuses = {"driver": [], "viewer": []}, []
    timed_sources = set()

    def received(name, frame, source, captured_at):
        frames["driver"].append((name, frame.shape))
        if captured_at is not None:
            assert 0 <= time.monotonic() - captured_at < 1
            timed_sources.add(source.peer_id)

    driver.on_frame(received, include_capture_time=True)
    viewer.on_frame(
        lambda name, frame, source: frames["viewer"].append((name, frame.shape))
    )
    driver.set_status_callback(lambda code, text: statuses.append(code))
    tasks = []
    try:
        await manager.start()
        tasks = [
            asyncio.create_task(endpoint.start())
            for endpoint in (publisher, driver, viewer)
        ]
        await until(lambda: all(len(value) >= 3 for value in frames.values()))
        assert frames["driver"][0] == ("front", (64, 96, 3))
        assert (
            statuses.index("joined")
            < statuses.index("connected")
            < statuses.index("receiving")
        )
        old_id = publisher.my_id
        await until(lambda: old_id in timed_sources)
        old_peers = tuple(publisher.peers.values())
        before = {name: len(value) for name, value in frames.items()}
        await service.manager.peers[old_id].websocket.close(1012, "test disconnect")
        await until(lambda: publisher.my_id is not None and publisher.my_id != old_id)
        await until(lambda: publisher.my_id in timed_sources)
        await until(
            lambda: all(len(frames[name]) > count + 2 for name, count in before.items())
        )
        assert all(peer.pc.connectionState == "closed" for peer in old_peers)
        assert not publisher.track_manager.sources["front"].closed
        publisher_id, driver_id = publisher.my_id, driver.my_id
        viewer_peer = viewer.peers[publisher_id]
        source = publisher.track_manager.sources["front"]
        before = {name: len(value) for name, value in frames.items()}
        failed = driver.peers[publisher_id]
        await failed.pc.close()
        await until(lambda: driver.my_id is not None and driver.my_id != driver_id)
        await until(
            lambda: all(len(frames[name]) > count + 2 for name, count in before.items())
        )
        assert publisher.my_id == publisher_id
        assert viewer.peers[publisher_id] is viewer_peer
        assert viewer_peer.pc.connectionState == "connected"
        assert publisher.track_manager.sources["front"] is source and not source.closed
        assert driver.peers[publisher_id] is not failed
        assert failed.pc.connectionState == "closed"
        await driver.close()
        await until(lambda: len(service.manager.peers) == 2)
        viewer_count = len(frames["viewer"])
        await until(lambda: len(frames["viewer"]) > viewer_count + 2)
        assert not driver._peer_tasks and not driver.peers and not driver._track_ids
    finally:
        await manager.stop()
        await asyncio.gather(
            *(endpoint.close() for endpoint in (publisher, driver, viewer))
        )
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
    assert camera._thread is None
    if budgeted:
        assert publisher.encoding_pool.completed > 0
        assert publisher.encoding_pool._close_task.done()
    assert not service.manager.peers and not service.manager.rooms
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(("media-", "signaling-"))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", ["duplicate", "new_session", "new_peer", "close"])
async def test_peer_failure_recovery_cannot_interrupt_replacement_or_explicit_close(
    overlap, monkeypatch
):
    from unittest.mock import AsyncMock

    receiver = WebRTCReceiver("room", ClientNetworkSettings(), CredentialSettings())
    receiver.my_id = "old-session"
    started, release = asyncio.Event(), asyncio.Event()

    class Peer:
        async def close(self):
            started.set()
            await release.wait()

    peer = Peer()
    receiver.peers["vehicle"] = peer
    recover = AsyncMock()
    monkeypatch.setattr(receiver.signaling, "close", recover)
    receiver._schedule_cleanup("vehicle", peer)
    receiver._schedule_cleanup("vehicle", peer)
    await started.wait()
    if overlap == "new_session":
        receiver.my_id = "new-session"
    elif overlap == "new_peer":
        receiver.peers["vehicle"] = Peer()
    elif overlap == "close":
        receiver._closed = True
    release.set()
    await asyncio.gather(*tuple(receiver._cleanup_jobs))
    assert recover.await_count == (1 if overlap == "duplicate" else 0)
    await receiver.close()
    assert not receiver.peers and not receiver._retired


@pytest.mark.asyncio
async def test_disconnect_can_finish_when_inline_peer_removal_is_cancelled(monkeypatch):
    receiver = WebRTCReceiver("room", ClientNetworkSettings(), CredentialSettings())
    peer = await receiver._create_peer("vehicle")
    transceiver = peer.pc.addTransceiver("video")
    original_stop = transceiver.stop
    entered, release = asyncio.Event(), asyncio.Event()
    stops = 0

    async def finish_transceiver():
        nonlocal stops
        stops += 1
        entered.set()
        await release.wait()
        await original_stop()

    monkeypatch.setattr(transceiver, "stop", finish_transceiver)
    retiring = asyncio.create_task(receiver._on_peer_left({"id": "vehicle"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        retiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retiring
        assert not receiver.peers and receiver._retired == {peer}
    finally:
        release.set()

    # The session cancels inline signaling handlers before disconnecting.
    # Its disconnect must join the in-progress peer close.
    await asyncio.wait_for(receiver._disconnect(), timeout=2)
    assert peer.pc.connectionState == "closed"
    assert not receiver._retired and stops == 1


@pytest.mark.asyncio
async def test_late_decode_completion_cannot_publish_into_replacement_peer():
    import numpy as np
    from av import VideoFrame

    receiver = WebRTCReceiver("room", ClientNetworkSettings(), CredentialSettings())
    peer = await receiver._create_peer("vehicle")
    entered = asyncio.Event()
    frames = []
    receiver.on_frame(lambda *args: frames.append(args))

    class LateFrame:
        id = "front"
        stopped = False

        async def recv(self):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return VideoFrame.from_ndarray(
                    np.zeros((24, 32, 3), dtype=np.uint8), format="bgr24"
                )

        def stop(self):
            self.stopped = True

    track = LateFrame()
    receiver._spawn_peer_task(
        "vehicle", peer, receiver._recv_track("vehicle", peer, track)
    )
    await entered.wait()
    await receiver._create_peer("vehicle")
    assert not frames and track.stopped
    assert peer.pc.connectionState == "closed"
    await receiver.close()


@pytest.mark.asyncio
async def test_close_before_session_first_tick_still_releases_created_peers():
    publisher = WebRTCStreamer(
        "room", ClientNetworkSettings(), CredentialSettings(), session=42
    )
    publisher.register_video_track("front")
    peer = await publisher._create_peer("driver")
    starting = asyncio.create_task(publisher.start())
    await asyncio.sleep(0)
    await publisher.close()
    await asyncio.gather(starting, return_exceptions=True)
    assert peer.pc.connectionState == "closed"
    assert publisher.track_manager.sources["front"].closed


def test_qt_media_stop_is_nonblocking_and_not_connected_before_join(legacy_python):
    legacy_python(
        "driving",
        """
        import asyncio
        import time
        from PyQt6.QtCore import QCoreApplication
        from rd_driving.infrastructure.workers.video_reciver_thread import WebRTCReceiverThread
        app = QCoreApplication([])
        class Receiver:
            started = False
            closed = False
            def set_status_callback(self, callback):
                self.status = callback
            async def start(self):
                self.started = True
                self.status("connecting", "connecting")
                await asyncio.Future()
            async def close(self):
                await asyncio.sleep(0.15)
                self.closed = True
        receiver = Receiver()
        worker = WebRTCReceiverThread(receiver)
        connections = []
        worker.connection_changed.connect(connections.append)
        worker.start()
        deadline = time.monotonic() + 2
        while not receiver.started and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        started = time.monotonic()
        worker.request_stop()
        assert time.monotonic() - started < 0.1
        assert worker.isRunning()
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert not worker.isRunning() and receiver.closed
        assert True not in connections
    """,
    )


def test_window_disconnects_video_without_restarting_other_workers(legacy_python):
    legacy_python(
        "driving",
        """
        import os
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        import threading
        import time
        import numpy as np
        from PyQt6.QtCore import QThread, pyqtSignal, QTimer
        from PyQt6.QtWidgets import QApplication
        import rd_driving.main_window as module
        app = QApplication([])
        class Worker(QThread):
            connection_changed = pyqtSignal(bool)
            status_changed = pyqtSignal(str, str)
            error_occurred = pyqtSignal(str)
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.args = args
                self.stop = threading.Event()
            def run(self):
                self.stop.wait(3)
                time.sleep(0.15)
            def request_stop(self):
                self.stop.set()
        module.ControlSystemThread = Worker
        module.WebRTCReceiverThread = Worker
        window = module.MainWindow(session_id="42", room="video")
        window.show()
        assert window._receiver_thread is None
        panel = window._control_panel
        panel._action_buttons['video'].click()
        old_video, old_control = window._receiver_thread, window._control_thread
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(10)
        started = time.monotonic()
        panel._action_buttons['video'].click()
        assert time.monotonic() - started < 0.1
        assert window._receiver_thread is old_video
        deadline = time.monotonic() + 3
        while window._receiver_thread is old_video and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert not old_video.isRunning() and old_control.isRunning()
        assert window._receiver_thread is None
        assert len(ticks) >= 3
        old_video.args[0]._on_frame("front", np.ones((24, 32, 3), dtype=np.uint8), None)
        assert window.fb.get("front").frame is None
        assert panel._action_buttons['video'].text() == '连接视频'
        panel._action_buttons['video'].click()
        active = (window._receiver_thread, window._control_thread)
        assert active[0] is not old_video and active[1] is old_control
        window.close()
        while any(worker.isRunning() for worker in active) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        app.processEvents()
        assert all(not worker.isRunning() for worker in active)
        assert window._closing_ready
    """,
    )


def test_peer_recovery_requires_a_full_media_gap_and_persistently_late_primary():
    from rd_core.settings import VideoSafetySettings
    from rd_driving.infrastructure.video.webrtc_reciver import _MediaOutage

    monitor = _MediaOutage(VideoSafetySettings())
    assert not monitor.needs_reconnect("front", 9.95, 10.0)
    for i in range(1, 20):
        assert not monitor.needs_reconnect("left", None, 10 + i * 0.1)
    assert not monitor.needs_reconnect("front", 10.0, 12.0)
    assert not monitor.needs_reconnect("left", 13.0, 13.1)
    assert not monitor.needs_reconnect("front", None, 13.3)
    assert not monitor.needs_reconnect("front", 12.0, 13.4)
    assert monitor.needs_reconnect("front", 12.0, 13.7)
    assert not monitor.needs_reconnect("front", 13.75, 13.8)
    assert not monitor.needs_reconnect("front", 12.5, 14.0)

"""Own capture threads and one bounded new-frame notification per camera."""

import asyncio
import math
import threading
import time


class _FrameNotification:
    def __init__(self, loop):
        self.loop = loop
        self.event = asyncio.Event()
        self.lock = threading.Lock()
        self.pending = False
        self.closed = False

    def notify(self):
        with self.lock:
            if self.closed or self.pending:
                return
            self.pending = True
            self.loop.call_soon_threadsafe(self._deliver)

    def _deliver(self):
        with self.lock:
            self.pending = False
            if not self.closed:
                self.event.set()

    def close(self):
        with self.lock:
            self.closed = True


class MultiCameraManager:
    def __init__(self, streamer, *, video_policy=None, on_fault=None):
        self.cameras = []
        self.streamer = streamer
        self._running = False
        self._workers = []
        self._notifications = []
        self._starting = []
        self._stop_task = None
        self._video_policy = video_policy
        self._on_fault = on_fault

    def add_camera(self, camera):
        if self._running or self._stop_task is not None:
            raise RuntimeError("cannot add camera after start")
        if any(cam.camera_name == camera.camera_name for cam in self.cameras):
            raise ValueError("duplicate camera name")
        self.streamer.register_video_track(
            camera.camera_name,
            fps=camera.fps,
            max_age_s=self._video_policy.stale_frame_ms / 1000
            if self._video_policy
            else 0.5,
        )
        self.cameras.append(camera)

    async def start(self):
        if self._running or self._stop_task is not None:
            raise RuntimeError("camera manager is single-use")
        if not self.cameras:
            raise RuntimeError("at least one camera is required")
        self._running = True
        loop = asyncio.get_running_loop()
        for cam in self.cameras:
            notification = _FrameNotification(loop)
            self._notifications.append(notification)
            cam.set_on_frame(notification.notify)
        self._starting = [
            asyncio.create_task(asyncio.to_thread(cam.start_capture))
            for cam in self.cameras
        ]
        try:
            # Shield to_thread ownership: stop must join pending open operations.
            results = await asyncio.shield(
                asyncio.gather(*self._starting, return_exceptions=True)
            )
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                raise ExceptionGroup("camera startup failed", errors)
            if not self._running:
                return
            for cam, notification in zip(self.cameras, self._notifications):
                notification.event.set()
                self._workers.append(
                    asyncio.create_task(
                        self._camera_push_worker(cam, notification),
                        name="publish-" + cam.camera_name,
                    )
                )
            if self._video_policy is not None:
                self._workers.append(
                    asyncio.create_task(
                        self._watch_primary(), name="primary-capture-watch"
                    )
                )
        except BaseException as exc:
            if isinstance(exc, Exception) and self._on_fault is not None:
                self._on_fault()
            await self.stop()
            raise

    async def _watch_primary(self):
        camera = next(
            (
                cam
                for cam in self.cameras
                if cam.camera_name == self._video_policy.primary_view
            ),
            None,
        )
        if camera is None:
            raise RuntimeError("primary camera is missing")
        last_sequence, captured_at = 0, time.monotonic()
        timeout = self._video_policy.stale_frame_ms / 1000
        while self._running:
            image = camera.get_snapshot()
            now = time.monotonic()
            if image is not None and image.sequence > last_sequence:
                if (
                    not math.isfinite(image.captured_at)
                    or image.captured_at > now
                    or (last_sequence and image.captured_at <= captured_at)
                ):
                    raise RuntimeError("invalid primary capture timestamp")
                last_sequence, captured_at = image.sequence, image.captured_at
            if now - captured_at >= timeout:
                self.streamer.invalidate_frame(camera.camera_name)
                raise RuntimeError("primary capture expired")
            await asyncio.sleep(min(timeout / 4, 0.05))

    async def _camera_push_worker(self, camera, notification):
        last_sequence = 0
        while self._running:
            await notification.event.wait()
            notification.event.clear()
            image = camera.get_snapshot()
            if image is None:
                self.streamer.invalidate_frame(camera.camera_name)
                if camera.failure is not None:
                    raise RuntimeError(
                        "capture worker failed: " + camera.camera_name
                    ) from camera.failure
            elif image.sequence != last_sequence:
                await self.streamer.push_frame(image, camera.camera_name)
                last_sequence = image.sequence

    async def wait(self):
        if not self._workers:
            raise RuntimeError("camera manager has not started")
        # Propagate failures to the application supervisor, which owns stop().
        try:
            await asyncio.gather(*self._workers)
        except Exception:
            if self._on_fault is not None:
                self._on_fault()
            raise

    async def stop(self):
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_owned())
        await asyncio.shield(self._stop_task)

    async def _stop_owned(self):
        self._running = False
        for cam in self.cameras:
            cam.set_on_frame(None)
        for notification in self._notifications:
            notification.close()
        # Opening is bounded by each adapter. Do not close a handle while its
        # open/read owner is still using it.
        await asyncio.gather(*self._starting, return_exceptions=True)
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        results = await asyncio.gather(
            *(asyncio.to_thread(cam.stop_capture) for cam in self.cameras),
            return_exceptions=True,
        )
        for cam in self.cameras:
            self.streamer.invalidate_frame(cam.camera_name)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("camera cleanup failed", errors)

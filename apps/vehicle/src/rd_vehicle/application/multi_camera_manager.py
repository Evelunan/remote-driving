"""Own capture threads and one bounded new-frame notification per camera."""

import asyncio
import logging
import math
import threading
import time

from rd_core.async_compat import ExceptionGroup

logger = logging.getLogger(__name__)


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
    def __init__(
        self, streamer, *, video_policy=None, on_fault=None, on_capture_health=None
    ):
        self.cameras = []
        self.streamer = streamer
        self._views_frozen = False
        self._running = False
        self._workers = []
        self._notifications = []
        self._starting = []
        self._stop_task = None
        self._video_policy = video_policy
        self._on_fault = on_fault
        self._on_capture_health = on_capture_health
        self._failed = set()
        self._camera_by_view = {}
        self._views_by_camera = {}

    def add_camera(self, camera, *, encoding=None):
        self._register_view(camera.camera_name, camera, encoding)
        self.cameras.append(camera)

    def add_camera_copy(self, name, source, *, encoding=None):
        """Publish an existing capture as another view without opening a device."""
        if source not in self._camera_by_view:
            raise ValueError("copy source camera is missing")
        self._register_view(name, self._camera_by_view[source], encoding)

    def _register_view(self, name, camera, encoding):
        if self._views_frozen:
            raise RuntimeError("cannot add camera after start")
        if name in self._camera_by_view:
            raise ValueError("duplicate camera name")
        self.streamer.register_video_track(
            name,
            fps=camera.fps,
            max_age_s=self._video_policy.stale_frame_ms / 1000
            if self._video_policy
            else 0.5,
            **({"encoding": encoding} if encoding is not None else {}),
        )
        self._camera_by_view[name] = camera
        self._views_by_camera.setdefault(camera.camera_name, []).append(name)

    def _invalidate_camera(self, camera):
        for name in self._views_by_camera[camera.camera_name]:
            self.streamer.invalidate_frame(name)

    async def start(self):
        if self._running or (
            self._stop_task is not None and not self._stop_task.done()
        ):
            raise RuntimeError("previous camera capture has not stopped")
        if not self.cameras:
            raise RuntimeError("at least one camera is required")
        if self._stop_task is not None:
            self._stop_task.result()
            self._stop_task = None
        self._workers.clear()
        self._notifications.clear()
        self._failed.clear()
        self._views_frozen = True
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
            if errors and self._on_capture_health is None:
                raise ExceptionGroup("camera startup failed", errors)
            if not self._running:
                return
            primary = (
                self._camera_by_view[self._video_policy.primary_view]
                if self._video_policy
                else None
            )
            if self._on_capture_health is not None:
                self._failed.update(
                    cam
                    for cam, result in zip(self.cameras, results)
                    if isinstance(result, Exception) or cam is primary
                )
                self._on_capture_health(not self._failed)
            for cam, notification, result in zip(
                self.cameras, self._notifications, results
            ):
                notification.event.set()
                self._workers.append(
                    asyncio.create_task(
                        self._isolated_worker(
                            cam,
                            notification,
                            cam is primary,
                            isinstance(result, Exception),
                        )
                        if self._on_capture_health is not None
                        else self._camera_push_worker(cam, notification),
                        name="publish-" + cam.camera_name,
                    )
                )
            if self._video_policy is not None and self._on_capture_health is None:
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

    def _health(self, camera, healthy):
        if healthy == (camera not in self._failed):
            return
        if healthy:
            self._failed.remove(camera)
        else:
            self._failed.add(camera)
        self._on_capture_health(not self._failed)

    async def _reopen(self, camera):
        # Opening and releasing a device cannot overlap final shutdown.
        def reopen():
            camera.stop_capture()
            camera.start_capture()

        while self._running:
            await asyncio.sleep(0.5)
            operation = asyncio.create_task(asyncio.to_thread(reopen))
            try:
                await asyncio.shield(operation)
                return
            except asyncio.CancelledError:
                await asyncio.gather(operation, return_exceptions=True)
                raise
            except Exception as exc:  # noqa: BLE001 - retry isolated camera driver failures
                logger.warning(
                    "Camera %s reopen failed (%s)",
                    camera.camera_name,
                    type(exc).__name__,
                )

    async def _isolated_worker(self, camera, notification, primary, reopen):
        last_sequence = 0
        timeout = self._video_policy.stale_frame_ms / 1000
        last_capture = None
        deadline = time.monotonic() + timeout
        while self._running:
            if reopen:
                self._invalidate_camera(camera)
                self._health(camera, False)
                logger.warning("Camera %s unavailable, retrying", camera.camera_name)
                await self._reopen(camera)
                deadline = time.monotonic() + timeout
                last_capture = None
                reopen = False
            notification.event.clear()
            image = camera.get_snapshot()
            now = time.monotonic()
            if camera.failure is not None:
                reopen = True
                continue
            if image is None:
                self._invalidate_camera(camera)
            elif image.sequence > last_sequence:
                if not 0 <= now - image.captured_at < timeout or (
                    last_capture is not None and image.captured_at < last_capture
                ):
                    self._invalidate_camera(camera)
                    if primary:
                        reopen = True
                        continue
                    await notification.event.wait()
                    continue
                for name in self._views_by_camera[camera.camera_name]:
                    await self.streamer.push_frame(image, name)
                last_sequence, last_capture = image.sequence, image.captured_at
                # Consecutive reads may share a Windows clock tick. The sequence
                # identifies a new read; an equal timestamp cannot move the deadline.
                deadline = last_capture + timeout
                self._health(camera, True)
            if primary:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reopen = True
                    continue
                try:
                    await asyncio.wait_for(notification.event.wait(), remaining)
                except asyncio.TimeoutError:
                    reopen = True
            else:
                await notification.event.wait()

    async def _watch_primary(self):
        camera = self._camera_by_view.get(self._video_policy.primary_view)
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
                    or (last_sequence and image.captured_at < captured_at)
                ):
                    raise RuntimeError("invalid primary capture timestamp")
                last_sequence, captured_at = image.sequence, image.captured_at
            if now - captured_at >= timeout:
                self._invalidate_camera(camera)
                raise RuntimeError("primary capture expired")
            await asyncio.sleep(min(timeout / 4, 0.05))

    async def _camera_push_worker(self, camera, notification):
        last_sequence = 0
        while self._running:
            await notification.event.wait()
            notification.event.clear()
            image = camera.get_snapshot()
            if image is None:
                self._invalidate_camera(camera)
                if camera.failure is not None:
                    raise RuntimeError(
                        "capture worker failed: " + camera.camera_name
                    ) from camera.failure
            elif image.sequence != last_sequence:
                for name in self._views_by_camera[camera.camera_name]:
                    await self.streamer.push_frame(image, name)
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
        if self._on_capture_health is not None:
            self._on_capture_health(False)
        self._running = False
        if not self._views_frozen:
            return
        for cam in self.cameras:
            cam.set_on_frame(None)
            self._invalidate_camera(cam)
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
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("camera cleanup failed", errors)

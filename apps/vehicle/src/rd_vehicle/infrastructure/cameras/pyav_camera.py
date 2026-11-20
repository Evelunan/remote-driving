"""Low-latency Linux V4L2 capture through PyAV / FFmpeg."""

import logging
import platform

import av

from .thread_camera import ThreadCamera

logger = logging.getLogger(__name__)


class PyAVCamera(ThreadCamera):
    """Decode an MJPEG V4L2 source without OpenCV's slow capture path."""

    def __init__(
        self, camera_id: int | str, camera_name="", height=None, width=None, fps=None
    ):
        super().__init__(
            640 if width is None else width,
            480 if height is None else height,
            30 if fps is None else fps,
            camera_name,
        )
        self.camera_id = camera_id
        self.container = None
        self._frames = None
        self._pending = None

    @staticmethod
    def supports(camera_id):
        return platform.system() == "Linux" and (
            isinstance(camera_id, int)
            or isinstance(camera_id, str)
            and camera_id.startswith("/dev/")
        )

    def _source_path(self):
        if not self.supports(self.camera_id):
            raise ValueError("PyAV capture requires a Linux /dev video source")
        return (
            f"/dev/video{self.camera_id}"
            if isinstance(self.camera_id, int)
            else self.camera_id
        )

    def _open(self):
        source = self._source_path()
        logger.info(
            "Camera %s opening: source=%s backend=PyAV/FFmpeg-V4L2 "
            "requested=%sx%s@%s MJPG",
            self.camera_name,
            source,
            self.width,
            self.height,
            self.fps,
        )
        self.container = av.open(
            source,
            mode="r",
            format="video4linux2",
            options={
                "input_format": "mjpeg",
                "video_size": f"{self.width}x{self.height}",
                "framerate": f"{self.fps:g}",
            },
        )
        streams = self.container.streams.video
        if not streams:
            raise RuntimeError("configured source has no video stream")
        stream = streams[0]
        self._frames = self.container.decode(video=stream.index)
        try:
            first = next(self._frames)
        except StopIteration:
            raise RuntimeError("configured camera returned no frames") from None
        self._pending = first.to_ndarray(format="bgr24")
        rate = stream.average_rate or stream.base_rate
        self.diagnostics = {
            "backend": "PyAV/FFmpeg-V4L2",
            "width": first.width,
            "height": first.height,
            "reported_fps": float(rate) if rate is not None else None,
            "format": "MJPG",
            "decoded_format": first.format.name,
            "target_fps": self.fps,
        }
        logger.info(
            "Camera %s opened: source=%s negotiated=%s",
            self.camera_name,
            source,
            self.diagnostics,
        )

    def _read(self):
        if self._pending is not None:
            frame, self._pending = self._pending, None
            return frame
        try:
            frame = next(self._frames)
        except StopIteration:
            raise RuntimeError("camera stream ended") from None
        return frame.to_ndarray(format="bgr24")

    def _close(self):
        self._pending = None
        self._frames = None
        if self.container is not None:
            self.container.close()
            self.container = None

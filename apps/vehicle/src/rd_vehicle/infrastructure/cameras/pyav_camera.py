"""Low-latency Linux V4L2 capture through PyAV / FFmpeg."""

import logging
import platform

import av
import numpy as np
from av.video.reformatter import ColorRange, VideoReformatter

from .thread_camera import ThreadCamera

logger = logging.getLogger(__name__)

_FULL_RANGE = np.arange(256, dtype=np.uint16)
_LIMITED_LUMA = ((219 * _FULL_RANGE + 127) // 255 + 16).astype(np.uint8)
_LIMITED_CHROMA = ((224 * _FULL_RANGE + 127) // 255 + 16).astype(np.uint8)


class PyAVCamera(ThreadCamera):
    """Decode an MJPEG V4L2 source without OpenCV's slow capture path."""

    def __init__(
        self,
        camera_id: int | str,
        camera_name="",
        height=None,
        width=None,
        fps=None,
        *,
        pixel_format="bgr24",
    ):
        if pixel_format not in ("bgr24", "yuv420p"):
            raise ValueError("unsupported PyAV capture pixel format")
        super().__init__(
            640 if width is None else width,
            480 if height is None else height,
            30 if fps is None else fps,
            camera_name,
            pixel_format=pixel_format,
        )
        self.camera_id = camera_id
        self.pixel_format = pixel_format
        self.container = None
        self._frames = None
        self._pending = None
        self._reformatter = None

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
        if self.pixel_format == "yuv420p":
            self._reformatter = VideoReformatter()
        self._pending = self._capture_pixels(first)
        rate = stream.average_rate or stream.base_rate
        self.diagnostics = {
            "backend": "PyAV/FFmpeg-V4L2",
            "width": first.width,
            "height": first.height,
            "reported_fps": float(rate) if rate is not None else None,
            "format": "MJPG",
            "decoded_format": first.format.name,
            "output_pixel_format": self.pixel_format,
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
        return self._capture_pixels(frame)

    def _capture_pixels(self, frame):
        if self.pixel_format == "bgr24":
            return frame.to_ndarray(format="bgr24")
        if frame.width % 2 or frame.height % 2:
            raise ValueError("yuv420p capture requires even negotiated dimensions")
        # MJPEG normally uses full-range YUV. VP8 and the default H.264
        # encoder use limited-range YUV420P.
        color_range = frame.color_range or ColorRange.JPEG
        if frame.format.name == "yuv420p" and color_range == ColorRange.JPEG:
            # With the same pixel format PyAV 16.1 can leave the range unchanged.
            pixels = frame.to_ndarray()
            pixels[: frame.height] = _LIMITED_LUMA[pixels[: frame.height]]
            pixels[frame.height :] = _LIMITED_CHROMA[pixels[frame.height :]]
            return pixels
        return self._reformatter.reformat(
            frame,
            format="yuv420p",
            src_color_range=color_range,
            dst_color_range=ColorRange.MPEG,
        ).to_ndarray()

    def _close(self):
        self._pending = None
        self._frames = None
        self._reformatter = None
        if self.container is not None:
            self.container.close()
            self.container = None

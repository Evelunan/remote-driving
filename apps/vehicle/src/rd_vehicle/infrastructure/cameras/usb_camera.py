"""OpenCV camera adapter supporting numeric devices and stable device paths."""

import logging
import platform

from rd_webrtc.opencv import cv2

from .thread_camera import ThreadCamera

logger = logging.getLogger(__name__)


class USBCamera(ThreadCamera):
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
        self.cap = None

    def _open(self):
        # Opening occurs at start, after the full configuration is validated.
        backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
        # CAP_ANY may select MSMF, whose device initialization can exceed the
        # bounded open deadline on Windows. DirectShow supports numeric devices;
        # keep file/URL sources on automatic backend selection.
        if platform.system() == "Windows" and isinstance(self.camera_id, int):
            backend = cv2.CAP_DSHOW
        source = self.camera_id
        if isinstance(source, str) and not source.startswith("/dev/"):
            source = "<configured source>"
        backend_name = {cv2.CAP_DSHOW: "DSHOW", cv2.CAP_V4L2: "V4L2"}.get(
            backend, "ANY"
        )
        logger.info(
            "Camera %s opening: source=%s backend=%s requested=%sx%s@%s MJPG",
            self.camera_name,
            source,
            backend_name,
            self.width,
            self.height,
            self.fps,
        )
        self.cap = cv2.VideoCapture(self.camera_id, backend)
        if not self.cap.isOpened():
            logger.error(
                "Camera %s open failed: source=%s backend=%s isOpened=False",
                self.camera_name,
                source,
                backend_name,
            )
            raise RuntimeError("cannot open configured camera")
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        mjpg = cv2.VideoWriter.fourcc(*"MJPG")
        if backend == cv2.CAP_DSHOW:
            # OpenCV's DirectShow FPS setter can rebuild the graph without the
            # FourCC. Stage MJPG before the width/height pair commits the format.
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if backend != cv2.CAP_DSHOW:
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        elif self.cap.get(cv2.CAP_PROP_FOURCC) != mjpg:
            # Some drivers switch back to YUY2 on resize.
            self.cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        raw_code = (fourcc & 0xFFFFFFFF).to_bytes(4, "little")
        format_name = (
            raw_code.decode("ascii")
            if all(32 <= byte < 127 for byte in raw_code)
            else f"0x{fourcc & 0xFFFFFFFF:08X}"
        )
        if backend == cv2.CAP_DSHOW and fourcc != mjpg:
            logger.warning(
                "Camera %s requested MJPG but driver reports %s",
                self.camera_name,
                format_name,
            )
        self.diagnostics = {
            "backend": self.cap.getBackendName(),
            "width": self.cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            "height": self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "reported_fps": self.cap.get(cv2.CAP_PROP_FPS),
            "fourcc": fourcc,
            "format": format_name,
            "target_fps": self.fps,
        }
        logger.info(
            "Camera %s opened: source=%s negotiated=%s",
            self.camera_name,
            source,
            self.diagnostics,
        )

    def _read(self):
        if not self.cap.grab():
            return None
        ok, frame = self.cap.retrieve()
        # Preserve the negotiated dimensions instead of silently resizing them.
        return frame if ok else None

    def _close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

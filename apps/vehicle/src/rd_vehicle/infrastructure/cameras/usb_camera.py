"""OpenCV camera adapter supporting numeric devices and stable device paths."""

import platform

from rd_webrtc.opencv import cv2

from .thread_camera import ThreadCamera


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
        self.cap = cv2.VideoCapture(self.camera_id, backend)
        if not self.cap.isOpened():
            raise RuntimeError("cannot open configured camera")
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.diagnostics = {
            "backend": self.cap.getBackendName(),
            "width": self.cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            "height": self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "reported_fps": self.cap.get(cv2.CAP_PROP_FPS),
            "fourcc": int(self.cap.get(cv2.CAP_PROP_FOURCC)),
            "target_fps": self.fps,
        }

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

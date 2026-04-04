# video_track.py
import asyncio
import numpy as np
from typing import Optional
from av import VideoFrame
from aiortc import VideoStreamTrack


class VideoFrameTrack(VideoStreamTrack):
    def __init__(self, fps=30, track_id: str = None):
        super().__init__()
        self._fps = fps
        # 设置自定义 track ID，用于标识不同的摄像头
        if track_id:
            self._id = track_id
            
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_event = asyncio.Event()
        self._closed = False

    async def update_frame(self, frame: np.ndarray):
        if self._closed:
            return
        self._latest_frame = frame
        self._frame_event.set()

    async def recv(self):
        if self._closed:
            raise asyncio.CancelledError

        pts, time_base = await self.next_timestamp()

        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=1 / self._fps)
        except asyncio.TimeoutError:
            pass

        frame = self._latest_frame
        self._frame_event.clear()

        if frame is None:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)

        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def stop(self):
        if not self._closed:
            self._closed = True
            self._frame_event.set()
        super().stop()

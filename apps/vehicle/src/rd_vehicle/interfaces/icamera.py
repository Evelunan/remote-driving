# interfaces/icamera.py
import abc

import numpy as np


class ICamera(abc.ABC):
    @abc.abstractmethod
    def get_snapshot(self):
        """原子读取采集序号、主机采集完成时间和只读图像；不可修改像素。"""

    @abc.abstractmethod
    def set_on_frame(self, callback):
        """设置采集变化通知；允许从相机线程调用，None 取消通知。"""

    @property
    @abc.abstractmethod
    def failure(self):
        """采集线程异常；无异常时为 None。"""

    @abc.abstractmethod
    def start_capture(self):
        """启动摄像头采集"""

    @abc.abstractmethod
    def stop_capture(self):
        """停止摄像头采集"""

    @abc.abstractmethod
    def get_latest_frame(self) -> np.ndarray:
        """获取最新帧"""

    @abc.abstractmethod
    def get_frame_count(self) -> int:
        """获取帧计数器"""

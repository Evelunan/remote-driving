# interfaces/icamera.py
import abc
import numpy as np

class ICamera(abc.ABC):
    @abc.abstractmethod
    def start_capture(self):
        """启动摄像头采集"""
        pass
    
    @abc.abstractmethod
    def stop_capture(self):
        """停止摄像头采集"""
        pass
    
    @abc.abstractmethod
    def get_latest_frame(self) -> np.ndarray:
        """获取最新帧"""
        pass
    
    @abc.abstractmethod
    def get_frame_count(self) -> int:
        """获取帧计数器"""
        pass
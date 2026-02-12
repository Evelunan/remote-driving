from abc import ABC, abstractmethod
import numpy as np

class IStream(ABC):
    """流媒体编码器接口"""
    
    @abstractmethod
    async def stream_frame(self, frame: np.ndarray):
        """编码并推流一帧视频"""
        pass
    
    @abstractmethod
    async def close(self):
        """关闭编码器并释放资源"""
        pass
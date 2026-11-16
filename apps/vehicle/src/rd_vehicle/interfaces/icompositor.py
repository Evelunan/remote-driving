from abc import ABC, abstractmethod
import numpy as np

class IFrameCompositor(ABC):
    """视频帧合成器接口"""
    
    @abstractmethod
    def compose_frames(self, frames: list) -> np.ndarray:
        """合成多个视频帧为单一输出帧"""
        pass
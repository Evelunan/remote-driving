from abc import ABC, abstractmethod


class IController(ABC):
    @abstractmethod
    def send_command(self, throttle, brake, steering):
        """发送控制指令"""
        pass

    @abstractmethod
    def close(self):
        """关闭控制器连接"""
        pass

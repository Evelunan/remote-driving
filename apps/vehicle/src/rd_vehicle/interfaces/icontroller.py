from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class StopResult:
    """A delivered stop request does not prove that a chassis has stopped."""

    requested: bool
    confirmed: bool = False
    detail: str = "chassis feedback not connected"


class IController(ABC):
    @abstractmethod
    def send_command(self, throttle, brake, steering):
        """发送控制指令"""
        pass

    @abstractmethod
    def request_safe_stop(self, reason: str) -> StopResult:
        """Request the adapter's stop action; real brake/feedback hooks go here."""
        raise NotImplementedError

    @abstractmethod
    def close(self):
        """关闭控制器连接"""
        pass

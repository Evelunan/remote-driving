from interfaces.communication import IMessageQueue
from infrastructure.communication import AsyncQueue


class CommunicationFactory:
    @staticmethod
    def create_message_queue(max_size: int = 100) -> IMessageQueue:
        return AsyncQueue(max_size)

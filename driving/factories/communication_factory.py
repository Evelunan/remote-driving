from interfaces.communication import IMessageQueue
from infrastructure.communication import AsyncQueue, LatestValueQueue


class CommunicationFactory:
    @staticmethod
    def create_message_queue(max_size: int = 100) -> IMessageQueue:
        # return AsyncQueue(max_size)
        return LatestValueQueue()

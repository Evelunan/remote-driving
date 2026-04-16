from infrastructure.workers import ControlSystemThread
from infrastructure.communication import AsyncQueue
from infrastructure.protocol import ControlCodec
import asyncio

queue = AsyncQueue()



async def run_quic():
    control = ControlSystemThread(signal_bridge=None, session_id="1234")
    await control._run_quic(queue)
    stop_event = asyncio.Event()
    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(run_quic())

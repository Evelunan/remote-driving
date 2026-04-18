from infrastructure.workers import ControlSystemThread
from infrastructure.communication import AsyncQueue
from infrastructure.protocol import ControlCodec
import asyncio

queue = AsyncQueue()
queue.put(
    {
        "throttle": 0.1,
        "brake": 0,
        "steering": 0.2,
    }
)


async def run_quic():
    control = ControlSystemThread(signal_bridge=None, session_id="1234")
    await control._run_quic(queue)
    stop_event = asyncio.Event()
    await stop_event.wait()

# def test():
#     self.cap.grab()
#     ret, frame = self.cap.retrieve()
#     # frame直接修改帧，在帧后面追加timestamp 和frame_id
#     # 发送给接收端
#     # 切割分成视频帧和 timestamp 和frame_id
    
if __name__ == "__main__":
    asyncio.run(run_quic())


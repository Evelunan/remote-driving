import asyncio
from logger import get_logger
from signaling_server import SignalingServer

logger = get_logger("webrtc")

async def main():
    server = SignalingServer() 
    try:
        await server.start()
        await asyncio.Future()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()
    

if __name__ == "__main__":
    asyncio.run(main())
import asyncio
from logger import get_logger
from server import SignalingServer

logger = get_logger(__name__)


async def main():
    server = SignalingServer()
    try:
        await server.start()
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("信令服务器关闭 (用户终止)")
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

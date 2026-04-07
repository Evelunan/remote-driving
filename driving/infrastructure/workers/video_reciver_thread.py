import asyncio
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal

from infrastructure.video import  WebRTCReceiver
from logger import get_logger

logger = get_logger(__name__)


class WebRTCReceiverThread(QThread):
    """视频接收线程 - 修复版"""

    def __init__(self, receiver: WebRTCReceiver):
        super().__init__()
        self._receiver = receiver
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main_task: Optional[asyncio.Task] = None

    def run(self):
        # 1. 创建新的事件循环
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            # 2. 创建主任务，而不是直接 run_until_complete 阻塞
            # 这样可以让事件循环有机会处理其他任务（虽然这里主要是单任务）
            self._main_task = self._loop.create_task(self._async_main())

            # 3. 运行直到任务完成或被取消
            self._loop.run_until_complete(self._main_task)

        except asyncio.CancelledError:
            logger.info("线程任务被取消")
        except Exception as e:
            logger.error(f"线程发生异常: {e}")
        finally:
            # 4. 清理资源
            try:
                # 取消所有未完成的任务
                tasks = asyncio.all_tasks(self._loop)
                for task in tasks:
                    task.cancel()
                # 等待所有任务取消
                if tasks:
                    self._loop.run_until_complete(
                        asyncio.gather(*tasks, return_exceptions=True)
                    )

                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.close()
            except Exception:
                pass

    async def _async_main(self):
        """异步主入口"""
        try:
            await self._receiver.start()
            # start() 只是启动了监听任务，这里需要保持运行
            # 等待 receiver 内部的 _listen_task 结束，或者等待外部取消
            if self._receiver._listen_task:
                await self._receiver._listen_task
        except Exception as e:
            logger.error(f"接收器运行出错: {e}")

    def request_stop(self):
        """请求停止线程 - 线程安全"""
        logger.info("正在请求停止 WebRTC 接收器...")

        if self._loop and self._loop.is_running():
            # 使用 call_soon_threadsafe 在线程内部调度协程
            # 注意：这里不能直接 await，因为是跨线程调用

            # 方案 A: 创建一个停止任务并调度它
            def stop_receiver():
                # 创建停止任务
                task = self._loop.create_task(self._receiver.close())
                # 也可以把 task 存起来，但这通常不需要

            self._loop.call_soon_threadsafe(stop_receiver)

            # 方案 B: 也可以直接取消主任务
            if self._main_task:
                self._loop.call_soon_threadsafe(self._main_task.cancel)
        else:
            # 如果循环还没跑起来，直接调用 close
            # 注意：这需要在主线程 await，但在 QThread 里不好处理
            # 所以主要依赖上面的逻辑
            pass

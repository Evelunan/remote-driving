import asyncio
import time
from typing import Optional
from common.constants import ROOT_PATH
from PyQt6.QtCore import QThread
from aioquic.asyncio import connect

from interfaces import IMessageQueue
from .signal_bridge import SignalBridge
from logger import get_logger
from factories import ControlFactory
from config.loaders import get_quic_config, get_app_config
from infrastructure.protocol import DrivingProtocol, ControlCodec
from app import DrivingDatagram

logger = get_logger(__name__)


class USBConfig:
    """USB 设备配置"""

    VENDOR_ID = 0x2F24
    PRODUCT_ID = 0x0009
    ENDPOINT_IN = 0x81


class VehicleInfo:
    max_w = 5
    max_v = 5


class ControlSystemThread(QThread):
    """控制系统线程 - USB 设备 + 数据处理 + QUIC 通信"""

    def __init__(self, signal_bridge: Optional[SignalBridge], session_id: str):
        super().__init__()
        self._bridge = signal_bridge  # 现在可以是 None
        self._session_id = session_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._stopping = False

        self._raw_queue = ControlFactory.create_message_queue()
        self._processed_queue = ControlFactory.create_message_queue()

        self._state_queue = ControlFactory.create_message_queue()
        self._state_callback = None

    def run(self):
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            logger.info("控制系统任务被取消")
        except Exception as e:
            if not self._stopping:
                logger.exception(f"控制系统错误: {e}")
        finally:
            # 清理事件循环
            if self._loop:
                try:
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(self._loop)
                    for task in pending:
                        task.cancel()
                    # 等待所有任务取消完成
                    if pending:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )

                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:
                    pass
                finally:
                    self._loop.close()

    async def _main(self):
        tasks = [
            asyncio.create_task(self._run_usb(self._raw_queue)),
            asyncio.create_task(
                self._run_processor(self._raw_queue, self._processed_queue)
            ),
            asyncio.create_task(self._run_quic(self._processed_queue)),
            asyncio.create_task(self._run_state()),
            # asyncio.create_task(self.put_processed_data()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    # --- 辅助方法：安全发射信号 ---
    def _safe_emit(self, signal_name: str, *args):
        """
        如果 bridge 存在，则发射信号；否则忽略。
        signal_name: 信号方法名，如 "usb_status_changed"
        """
        if not self._stopping and self._bridge is not None:
            try:
                signal = getattr(self._bridge, signal_name)
                signal.emit(*args)
            except AttributeError:
                logger.warning(f"SignalBridge 缺少信号: {signal_name}")
            except Exception as e:
                logger.error(f"发射信号 {signal_name} 时出错: {e}")

    async def _run_usb(self, raw_queue: IMessageQueue):
        usb = ControlFactory.create_usb_device(
            USBConfig.VENDOR_ID, USBConfig.PRODUCT_ID
        )
        usb.set_data_callback(raw_queue.put)

        async def safe_sleep(delay: float):
            try:
                await asyncio.sleep(delay)
                return True
            except asyncio.CancelledError:
                return False

        while self._running:
            try:
                self._safe_emit("usb_status_changed", False, "连接中")
                await usb.connect()
                self._safe_emit("usb_status_changed", True, "在线")

                while self._running and usb.is_connected():
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except ConnectionError:
                self._safe_emit("usb_status_changed", False, "未连接")
                if not await safe_sleep(5):
                    break
            except Exception as e:
                logger.info(f"[USB ERROR] {e}")
                self._safe_emit("usb_status_changed", False, "错误")
                if not await safe_sleep(5):
                    break
            finally:
                try:
                    await usb.disconnect()
                except Exception as e:
                    logger.info(f"[USB DISCONNECT ERROR] {e}")

    async def _run_processor(
        self, raw_queue: IMessageQueue, processed_queue: IMessageQueue
    ):
        proc = ControlFactory.create_data_processor(processed_queue)
        while self._running:
            try:
                data = await asyncio.wait_for(raw_queue.get(), timeout=1.0)
                # 这里的业务逻辑保持不变，不再依赖 bridge 发射数据
                # 如果需要回调，可以通过 proc 的回调机制处理
                await proc.process(data)
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if not self._stopping:
                    logger.error(f"处理器错误: {e}")

    async def _run_quic(self, processed_queue: IMessageQueue):
        config = get_quic_config()
        while self._running:
            try:
                self._safe_emit("quic_status_changed", False, "连接中")

                async with connect(
                    config.host,
                    config.port,
                    configuration=config.to_aiortc_config(True),
                    create_protocol=lambda *a, **k: DrivingProtocol(
                        int(self._session_id), processed_queue, *a, **k
                    ),
                ) as protocol:
                    await protocol.wait_connected()
                    self._safe_emit("quic_status_changed", True, "在线")
                    protocol.set_state_callback(self._state_queue.put)
                    protocol.start()
                    await protocol.wait_closed()
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                self._safe_emit("quic_status_changed", False, "离线")
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                self._safe_emit("quic_status_changed", False, "错误")
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break

    async def _run_state(self):
        while self._running:
            try:
                message = await asyncio.wait_for(self._state_queue.get(), timeout=1.0)
                v, w, rtt = ControlCodec.process_state(
                    message, VehicleInfo.max_v, VehicleInfo.max_w
                )
                
                if get_app_config().debug_mode:
                    try:
                        log_file = ROOT_PATH / "logs" / "rtt_stats.csv"
                        with open(log_file, "a") as f:
                            f.write(f"{time.time()},{rtt}\n")
                    except Exception as e:
                        logger.error(f"记录 rtt 失败: {e}")

                if self._state_callback:
                    self._state_callback(v, w, rtt)
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                if self._state_callback:
                    self._state_callback(0, 0, 0)
            except Exception as e:
                if not self._stopping:
                    logger.error(f"错误: {e}")

    def request_stop(self):
        self._stopping = True
        self._running = False
        if self._loop and self._loop.is_running():
            # 安全地取消任务
            self._loop.call_soon_threadsafe(self._cancel_all_tasks)

    def _cancel_all_tasks(self):
        # 在事件循环线程内执行取消
        tasks = asyncio.all_tasks(self._loop)
        for task in tasks:
            task.cancel()

    async def put_processed_data(self):
        while self._running:
            await asyncio.sleep(0.05)
            await self._processed_queue.put(
                {
                    "throttle": 0.1,
                    "brake": 0,
                    "steering": 0.2,
                }
            )

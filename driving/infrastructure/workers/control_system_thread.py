import asyncio
from PyQt6.QtCore import QThread
from aioquic.asyncio import connect

from interfaces import IMessageQueue
from .signal_bridge import SignalBridge
from logger import get_logger
from factories import ControlFactory
from config.loaders import get_quic_config
from infrastructure.protocol import DrivingProtocol
from app import DrivingDatagram

logger = get_logger(__name__)


class USBConfig:
    """USB 设备配置"""

    # 方向盘设备
    VENDOR_ID = 0x2F24
    PRODUCT_ID = 0x0009
    ENDPOINT_IN = 0x81


class ControlSystemThread(QThread):
    """控制系统线程 - USB 设备 + 数据处理 + QUIC 通信"""

    def __init__(self, signal_bridge: SignalBridge, session_id: str):
        super().__init__()
        self._bridge = signal_bridge
        self._session_id = session_id
        self._loop = None
        self._running = False
        self._stopping = False

    def run(self):
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            logger.debug("控制系统任务被取消")
        except Exception as e:
            if not self._stopping:
                logger.error(f"控制系统错误: {e}")
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main(self):
        raw_queue = ControlFactory.create_message_queue()
        processed_queue = ControlFactory.create_message_queue()
        tasks = [
            asyncio.create_task(self._run_usb(raw_queue)),
            asyncio.create_task(self._run_processor(raw_queue, processed_queue)),
            asyncio.create_task(self._run_quic(processed_queue)),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    async def _run_usb(self, raw_queue: IMessageQueue):
        usb = ControlFactory.create_usb_device(
            USBConfig.VENDOR_ID, USBConfig.PRODUCT_ID
        )
        usb.set_data_callback(raw_queue.put)

        async def safe_sleep(delay: float):
            """可取消的 sleep"""
            try:
                await asyncio.sleep(delay)
                return True
            except asyncio.CancelledError:
                return False

        def emit_status(online: bool, msg: str):
            if not self._stopping:
                self._bridge.usb_status_changed.emit(online, msg)

        while self._running:
            try:
                emit_status(False, "连接中")
                await usb.connect()
                emit_status(True, "在线")
                while self._running and usb.is_connected():
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except ConnectionError:
                emit_status(False, "未连接")
                if not await safe_sleep(3):
                    break
            except Exception as e:
                # 建议记录日志，而不是吞掉异常
                logger.info(f"[USB ERROR] {e}")
                emit_status(False, "错误")
                if not await safe_sleep(3):
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
                # vals = calc.calculate(data)
                # if not self._stopping:
                #     self._bridge.control_data_updated.emit(
                #         vals["throttle"], vals["brake"], vals["steering"]
                #     )
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
                if not self._stopping:
                    self._bridge.quic_status_changed.emit(False, "连接中")
                async with connect(
                    config.host,
                    config.port,
                    configuration=config.to_aiortc_config(True),
                    create_protocol=lambda *a, **k: DrivingProtocol(
                        self._session_id, processed_queue, *a, **k
                    ),
                ) as protocol:
                    await protocol.wait_connected()
                    if not self._stopping:
                        self._bridge.quic_status_changed.emit(True, "在线")
                    protocol.start()
                    await protocol.wait_closed()
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                if not self._stopping:
                    self._bridge.quic_status_changed.emit(False, "离线")
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._stopping:
                    self._bridge.quic_status_changed.emit(False, "错误")
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break

    def request_stop(self):
        self._stopping = True
        self._running = False
        if self._loop and self._loop.is_running():
            for task in asyncio.all_tasks(self._loop):
                self._loop.call_soon_threadsafe(task.cancel)

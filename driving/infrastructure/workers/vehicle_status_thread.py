from PyQt6.QtCore import QThread, pyqtSignal
import asyncio

import aiohttp

from logger import get_logger

logger = get_logger(__name__)


class VehicleStatusThread(QThread):
    """
    车辆状态轮询线程 - 定时从后台 API 获取车辆状态

    使用车辆唯一标识 (vehicle_id) 查询车辆状态信息。

    API 请求示例:
        GET /api/vehicle/status?vehicle_id=CAR_001

    期望的响应格式:
        {
            "speed": 60,        # 车速 km/h
            "battery": 85,      # 电量 %
            "gear": "D",        # 档位
            "signal": "强"      # 信号强度
        }
    """

    status_updated = pyqtSignal(dict)  # 状态更新信号
    error_occurred = pyqtSignal(str)  # 错误信号

    def __init__(self, vehicle_id: str):
        """
        Args:
            vehicle_id: 车辆唯一标识，从登录接口返回的 login_data["vehicle_id"] 获取
        """
        super().__init__()
        self._vehicle_id = vehicle_id
        self._running = False
        self._loop = None
        self._stopping = False

    def run(self):
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_status())
        except asyncio.CancelledError:
            logger.debug("车辆状态轮询任务被取消")
        except Exception as e:
            if not self._stopping:
                logger.error(f"车辆状态线程错误: {e}")
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    async def _poll_status(self):
        """轮询获取车辆状态"""
        interval = 2  # 转换为秒

        try:
            async with aiohttp.ClientSession() as session:
                while self._running:
                    try:
                        url = "ServerConfig.get_vehicle_status_url()"
                        # 使用车辆唯一标识查询状态
                        params = {"vehicle_id": self._vehicle_id}

                        async with session.get(
                            url, params=params, timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if not self._stopping:
                                    self.status_updated.emit(data)
                            else:
                                logger.warning(f"获取车辆状态失败: HTTP {resp.status}")

                    except aiohttp.ClientError as e:
                        logger.debug(f"车辆状态请求失败: {e}")
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        logger.error(f"车辆状态错误: {e}")

                    try:
                        await asyncio.sleep(interval)
                    except asyncio.CancelledError:
                        return
        except asyncio.CancelledError:
            return

    def request_stop(self):
        self._stopping = True
        self._running = False
        if self._loop and self._loop.is_running():
            for task in asyncio.all_tasks(self._loop):
                self._loop.call_soon_threadsafe(task.cancel)

import cv2

from logger import get_logger
from infrastructure.workers import ControlSystemThread
from infrastructure.workers import WebRTCReceiverThread
from infrastructure.video import WebRTCReceiver, FrameBuffer, MultiCameraDisplay
from config.loaders import get_webrtc_config

logger = get_logger(__name__)


def main():
    # 1. 创建线程实例
    control = ControlSystemThread(session_id="1234", signal_bridge=None)

    config = get_webrtc_config()
    buffer = FrameBuffer()
    display = MultiCameraDisplay(width=1280, height=720)
    reciver = WebRTCReceiver("1234", config)
    reciver.on_frame(buffer.put)

    video = WebRTCReceiverThread(reciver)
    # 2. 启动线程
    # control.start()
    video.start()
    logger.info("控制系统线程已启动")

    try:
        # 3. 保持主线程运行，等待用户中断
        # 这里只是简单的死循环等待，实际项目中可能是 app.exec() 或其他逻辑
        while True:
            canvas = display.compose(buffer, "front_dominant")
            cv2.imshow("Multi-Camera View", canvas)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                logger.info("用户主动退出")
            control.wait(33)  # 等待1秒，防止死锁，同时检测线程状态

    except KeyboardInterrupt:
        logger.info("收到退出信号，正在关闭...")
    finally:
        # 4. 优雅关闭流程 (关键步骤)
        logger.info("正在停止控制系统...")

        # 调用你定义的请求停止方法 (设置标志位)
        control.request_stop()

        # 调用 Qt 线程的标准停止方法
        control.quit()

        # 【核心修复】：阻塞主线程，直到 control 线程彻底结束
        # 参数 5000 表示最多等待 5000ms (5秒)，防止死锁
        if control.wait(5000):
            logger.info("控制系统已安全退出")
        else:
            logger.warning("控制系统退出超时，强制终止")


if __name__ == "__main__":
    main()

# main.py
import cv2

from common.utils import *
from logger import get_logger

logger = get_logger(__name__)

vid = "0408"
pid = "5423"
index = get_camera_index(pid=pid, vid=vid)
logger.info(f"index = {index}")

cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)

if not cap.isOpened():
    logger.error("无法打开摄像头")
else:
    logger.info("摄像头打开成功")

    ret, frame = cap.read()
    if ret:
        logger.info("成功读取一帧图像")
        cv2.imshow("Test", frame)
        cv2.waitKey(0)
    else:
        logger.error("读取图像失败")

cap.release()
cv2.destroyAllWindows()

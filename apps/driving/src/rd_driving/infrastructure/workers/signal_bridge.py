from PyQt6.QtCore import QObject, pyqtSignal


class SignalBridge(QObject):
    """Qt 信号桥接器 - 用于线程间通信"""

    usb_status_changed = pyqtSignal(bool, str)
    quic_status_changed = pyqtSignal(bool, str)
    control_data_updated = pyqtSignal(float, float, float, float)
    control_confirmation_expired = pyqtSignal()
    vehicle_status_updated = pyqtSignal(dict)  # 车辆状态更新信号

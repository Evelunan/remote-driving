from PyQt6.QtCore import QObject, pyqtSignal


class SignalBridge(QObject):
    """Qt 信号桥接器 - 用于线程间通信"""

    usb_status_changed = pyqtSignal(bool, str)
    local_input_changed = pyqtSignal(object)
    quic_status_changed = pyqtSignal(bool, str)
    control_status_changed = pyqtSignal(str, str)
    control_target_changed = pyqtSignal(str)
    control_data_updated = pyqtSignal(dict)  # ACK plus original sample deadline
    control_confirmation_expired = pyqtSignal()
    vehicle_status_updated = pyqtSignal(dict)  # 车辆状态更新信号

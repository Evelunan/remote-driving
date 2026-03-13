import struct

# name        type      offset    note
# msg_type:   uint8     0         消息类型 (0-255)
# session_id: uint16    1-2       会话号 (0-65535)
# speed:      unint8    3         线速度原始值 (0-255) 128为中点， <128：前进   >128：后退
# steering:   uint8     4         角速度原始值 (0-255) 128为中点， <128：左转   >128：右转


# 格式定义:
# ! : 网络字节序 (大端)
# B : uint8 (1字节) -> msg_type
# H : uint16 (2字节) -> session_id
# B : uint8 (1字节) -> speed
# B : uint8 (1字节) -> steering


class ControlCodec:
    """
    控制协议编解码器
    """

    MSG_TYPE_CONTROL = 0x01
    MSG_TYPE_KEEPALIVE = 0x02

    # 中立值
    ZERO = 128

    _CONTROL_STRUCT = struct.Struct("!BHBB")
    _KEEPALIVE_STRUCT = struct.Struct("!BH")

    # =========================
    # validation
    # =========================
    @staticmethod
    def _validate_session(session_id: int) -> None:
        if not isinstance(session_id, int):
            raise TypeError("session_id must be int")
        if not (0 <= session_id <= 0xFFFF):
            raise ValueError("session_id must be 0-65535")

    @staticmethod
    def _validate_u8(value: int, name: str) -> None:
        if not isinstance(value, int):
            raise TypeError(f"{name} must be int")
        if not (0 <= value <= 255):
            raise ValueError(f"{name} must be 0-255")

    # =========================
    # normalize
    # =========================
    @staticmethod
    def normalize_to_u8(value: float) -> int:
        """
        [-1,1] -> [0,255]
        """
        if not isinstance(value, (float, int)):
            raise TypeError("value must be float")
        value = max(-1.0, min(1.0, float(value)))
        return int(round((value + 1.0) * 127.5))

    @staticmethod
    def u8_to_normalized(value: int) -> float:
        """
        [0,255] -> [-1,1]
        """
        if not (0 <= value <= 255):
            raise ValueError("value must be 0-255")
        return round(value / 127.5 - 1.0, 2)

    # =========================
    # encode
    # =========================
    @classmethod
    def encode_control(
        cls,
        session_id: int,
        speed: int,
        steering: int,
    ) -> bytes:
        cls._validate_session(session_id)
        cls._validate_u8(speed, "speed")
        cls._validate_u8(steering, "steering")
        return cls._CONTROL_STRUCT.pack(
            cls.MSG_TYPE_CONTROL,
            session_id,
            speed,
            steering,
        )

    @classmethod
    def encode_keepalive(cls, session_id: int) -> bytes:
        cls._validate_session(session_id)
        return cls._KEEPALIVE_STRUCT.pack(
            cls.MSG_TYPE_KEEPALIVE,
            session_id,
        )

    # =========================
    # decode entry
    # =========================
    @classmethod
    def decode(cls, data: bytes) -> dict:
        """
        统一解码接口
        """
        if len(data) < 1:
            raise ValueError("empty packet")
        msg_type = data[0]
        if msg_type == cls.MSG_TYPE_CONTROL:
            return cls.decode_control(data)
        elif msg_type == cls.MSG_TYPE_KEEPALIVE:
            return cls.decode_keepalive(data)
        else:
            raise ValueError(f"unknown msg_type {msg_type}")

    # =========================
    # decode control
    # =========================
    @classmethod
    def decode_control(cls, data: bytes) -> dict:
        if len(data) != cls._CONTROL_STRUCT.size:
            raise ValueError("invalid control packet size")
        msg_type, session_id, speed, steering = cls._CONTROL_STRUCT.unpack(data)
        return {
            "type": "control",
            "session_id": session_id,
            "speed": speed,
            "steering": steering,
            "speed_norm": cls.u8_to_normalized(speed),
            "steering_norm": cls.u8_to_normalized(steering),
        }

    # =========================
    # decode keepalive
    # =========================
    @classmethod
    def decode_keepalive(cls, data: bytes) -> dict:
        if len(data) != cls._KEEPALIVE_STRUCT.size:
            raise ValueError("invalid keepalive packet size")
        msg_type, session_id = cls._KEEPALIVE_STRUCT.unpack(data)
        return {
            "type": "keepalive",
            "session_id": session_id,
        }

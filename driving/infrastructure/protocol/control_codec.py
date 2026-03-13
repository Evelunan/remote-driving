import struct

# name        type          note
# msg_type:   uint8         消息类型 (0-255)
# session_id: uint16        会话号 (0-65535)
# speed:      unint8        线速度原始值 (0-255) 128为中点， <128：前进   >128：后退
# steering:   uint8         角速度原始值 (0-255) 128为中点， <128：左转   >128：右转

# 延迟抖动
# type session speed steering timestamp
# 1B   2B      1B    1B       4B


class ControlCodec:
    """
    远程驾驶控制协议编解码器
    """

    # =========================
    # message types
    # =========================
    MSG_HELLO_DRIVING = 0x01
    MSG_HELLO_CAR = 0x02
    MSG_CONTROL = 0x03
    MSG_STATE = 0x04

    ZERO = 128

    # =========================
    # struct layout
    # =========================
    _HEADER_STRUCT = struct.Struct("!BH")  # type session
    _MOTION_STRUCT = struct.Struct("!BHBB")  # type session speed steering

    # =========================
    # validation
    # =========================
    @staticmethod
    def _validate_session(session_id: int):
        if not isinstance(session_id, int):
            raise TypeError("session_id must be int")
        if not (0 <= session_id <= 0xFFFF):
            raise ValueError("session_id must be 0-65535")

    @staticmethod
    def _validate_u8(value: int, name: str):
        if not isinstance(value, int):
            raise TypeError(f"{name} must be int")
        if not (0 <= value <= 255):
            raise ValueError(f"{name} must be 0-255")

    # =========================
    # normalize
    # =========================
    @staticmethod
    def normalize_to_u8(value: float) -> int:
        value = max(-1.0, min(1.0, float(value)))
        return int(round((value + 1.0) * 127.5))

    @staticmethod
    def u8_to_normalized(value: int) -> float:
        return round(value / 127.5 - 1.0, 2)

    # =========================
    # encode entry
    # =========================
    @classmethod
    def encode(cls, msg_type, session_id, speed=None, steering=None):
        handler = cls._ENCODE_TABLE.get(msg_type)
        if not handler:
            raise ValueError(f"unknown msg_type {msg_type}")
        return handler(msg_type, session_id, speed, steering)

    # =========================
    # decode entry
    # =========================
    @classmethod
    def decode(cls, data: bytes):
        if not data:
            raise ValueError("empty packet")
        msg_type = data[0]
        entry = cls._DECODE_TABLE.get(msg_type)
        if not entry:
            raise ValueError(f"unknown msg_type {msg_type}")
        name, handler = entry
        result = handler(data)
        result["type"] = name
        return result

    # =========================
    # encode handlers
    # =========================
    @classmethod
    def encode_hello(cls, msg_type, session_id, *_):
        cls._validate_session(session_id)
        return cls._HEADER_STRUCT.pack(
            msg_type,
            session_id,
        )

    @classmethod
    def encode_motion(cls, msg_type, session_id, speed, steering):
        cls._validate_session(session_id)
        cls._validate_u8(speed, "speed")
        cls._validate_u8(steering, "steering")
        return cls._MOTION_STRUCT.pack(
            msg_type,
            session_id,
            speed,
            steering,
        )

    # =========================
    # decode handlers
    # =========================
    @classmethod
    def decode_hello(cls, data):
        if len(data) != cls._HEADER_STRUCT.size:
            raise ValueError("invalid hello packet size")
        _, session_id = cls._HEADER_STRUCT.unpack(data)
        return {"session_id": session_id}

    @classmethod
    def decode_motion(cls, data):
        if len(data) != cls._MOTION_STRUCT.size:
            raise ValueError("invalid motion packet size")
        _, session_id, speed, steering = cls._MOTION_STRUCT.unpack(data)
        return {
            "session_id": session_id,
            "speed": speed,
            "steering": steering,
            "speed_norm": cls.u8_to_normalized(speed),
            "steering_norm": cls.u8_to_normalized(steering),
        }


# =========================
# dispatch tables
# =========================

ControlCodec._ENCODE_TABLE = {
    ControlCodec.MSG_HELLO_DRIVING: ControlCodec.encode_hello,
    ControlCodec.MSG_HELLO_CAR: ControlCodec.encode_hello,
    ControlCodec.MSG_CONTROL: ControlCodec.encode_motion,
    ControlCodec.MSG_STATE: ControlCodec.encode_motion,
}

ControlCodec._DECODE_TABLE = {
    ControlCodec.MSG_HELLO_DRIVING: ("hello-driving", ControlCodec.decode_hello),
    ControlCodec.MSG_HELLO_CAR: ("hello-car", ControlCodec.decode_hello),
    ControlCodec.MSG_CONTROL: ("control", ControlCodec.decode_motion),
    ControlCodec.MSG_STATE: ("state", ControlCodec.decode_motion),
}

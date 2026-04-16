# type      1B      类型
# session   2B      会话号
# seq       2B      序列号
# timestamp 4B      时间戳
# throttle  1B      制动  [-1, 1]
# brake     1B      刹车  [0, 1]
# sterring  1B      转向  [-1, 1]

from enum import IntEnum
import struct
import time


class ControlCodec:

    class MessageType(IntEnum):
        HELLO_DRIVING = 0x01
        HELLO_CAR = 0x02
        CONTROL = 0x03
        STATE = 0x04
        HEARTBEAT = 0x05

    HEADER_STRUCT = struct.Struct("!BH")
    CONTROL_PACKET_STRUCT = struct.Struct("!BHHIbbb")

    _seq = 0  # 自增序列号

    @staticmethod
    def encode_normalized_to_int8(value: float) -> int:
        """
        将归一化值 [-1.0, 1.0] 编码为 int8 [-127, 127]
        """
        value = max(-1.0, min(1.0, float(value)))
        return int(value * 127)

    @staticmethod
    def decode_int8_to_normalized(value: int) -> float:
        """
        将 int8 [-127, 127] 解码为归一化值 [-1.0, 1.0]
        """
        return round(float(value) / 127.0, 2)

    # =========================
    # 编码
    # =========================

    @classmethod
    def encode(
        cls,
        type: int,
        session: int,
        throttle: float = 0,
        brake: float = 0,
        steering: float = 0,
        timestamp: int = None,
        seq: int = None,
    ) -> bytes:
        """
        编码控制包

        :param type: 包类型 (0~255)
        :param session: 会话号 (0-65536)
        :param throttle: [-1, 1]
        :param brake: [0, 1]
        :param steering: [-1, 1]
        :param timestamp: 可选（默认当前时间 ms）
        :param seq: 可选（默认自增）
        """

        # 自动时间戳（毫秒）
        if timestamp is None:
            timestamp = (time.time_ns() // 1_000_000) & 0xFFFFFFFF

        # 自动序列号
        if seq is None:
            cls._seq = (cls._seq + 1) & 0xFFFF
            seq = cls._seq

        return cls.CONTROL_PACKET_STRUCT.pack(
            type & 0xFF,
            session & 0xFFFF,
            seq & 0xFFFF,
            timestamp & 0xFFFFFFFF,
            cls.encode_normalized_to_int8(throttle),
            cls.encode_normalized_to_int8(brake),
            cls.encode_normalized_to_int8(steering),
        )

    # =========================
    # 解码
    # =========================

    @classmethod
    def decode(cls, data: bytes) -> dict:
        """
        解码控制包
        """

        if len(data) != cls.CONTROL_PACKET_STRUCT.size:
            raise ValueError(f"Invalid packet size: {len(data)}")

        type_, session, seq, ts, throttle_i, brake_i, steering_i = (
            cls.CONTROL_PACKET_STRUCT.unpack(data)
        )

        return {
            "type": type_,
            "session": session,
            "seq": seq,
            "timestamp": ts,
            "throttle": cls.decode_int8_to_normalized(throttle_i),
            "brake": cls.decode_int8_to_normalized(brake_i),
            "steering": cls.decode_int8_to_normalized(steering_i),
        }

    # =========================
    # 解码（仅头部）⭐服务器用
    # =========================

    @classmethod
    def decode_header(cls, data: bytes) -> dict:
        if len(data) < cls.HEADER_STRUCT.size:
            raise ValueError("Packet too short")

        type_, session = cls.HEADER_STRUCT.unpack_from(data)

        try:
            msg_type = cls.MessageType(type_)
        except ValueError:
            msg_type = type_

        return {
            "type": msg_type,
            "session": session,
        }

    @staticmethod
    def get_role(msg_type) -> str | None:
        if msg_type in (
            ControlCodec.MessageType.HELLO_CAR,
            ControlCodec.MessageType.STATE,
        ):
            return "car"
        if msg_type in (
            ControlCodec.MessageType.HELLO_DRIVING,
            ControlCodec.MessageType.CONTROL,
        ):
            return "driving"
        return None

    @staticmethod
    def is_newer(new_seq, old_seq) -> bool:
        MAX_SEQ = 0xFFFF
        HALF = 0x8000
        return ((new_seq - old_seq) & MAX_SEQ) < HALF
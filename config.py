# config.py
# 全局配置

# ======================
# 服务器与车辆配置
# ======================
SERVER_CONFIG = {
    "base_url": "http://81.71.49.243:8080",
    "register_path": "/api/vehicle/register",
}

VEHICLE_INFO = {
    "vehicle_id": "car_001",
    "vehicle_name": "远程驾驶车辆1号",
}

# 摄像头统一参数配置
CAMERA_CONFIG = {
    "width": 640,
    "height": 480,
    "fps": 30
}

# 推流配置


# WebRTC配置
WEBRTC_CONFIG = {
    "server_url": "ws://81.71.49.243:8765",
    "room": "2430",
    "ice_servers": {
        "stun": {
            "urls": [
                "stun:81.71.49.243:3478",
                "stun:stun1.l.google.com:19302"
            ]
        },
        "turn": {
            "urls": ["turn:81.71.49.243:3478"],
            "username": "ascte",
            "password": "ascte"
        }
    }
}

#  QUIC配置
QUIC_CONFIG = {
    "host": "81.71.49.243",
    "port": 4433,
    "session_id": "2430",
    "quic": {
        "verify_mode": False,
        "enable_datagrams": True,
        "max_datagram_frame_size": 65536,
        "idle_timeout": 10
    }
}

# 摄像头VID和PID
CAMERAS = {
    "camera_front": {
        "vid": "a111",
        "pid": "b111"
    },
    "camera_back": {
        "vid": "1bcf",
        "pid": "0005"
    },
    "camera_right": {
        "vid": "1bcf",
        "pid": "0003"
    },
    "camera_left": {
        "vid": "1bcf",
        "pid": "0004"
    }
}

# ======================
# 车辆运动参数
# ======================
VEHICLE_CONTROL = {
    "max_speed": 500,             # 最大线速度 (单位: mm/s)
    "max_angular_velocity": 2.0,  # 最大角速度 (单位: rad/s)
}


COMMUNICATION = {
    # 串口配置
    "serial": {
        "port": "/dev/ttyUSB0",
        "baudrate": 2000000,      # 2Mbps
        "timeout": 0.001,         # 超时时间 (秒)
        "write_timeout": 0.001    # 写超时时间 (秒)
    },


    # 指令协议
    "command_protocol": {
        "template": bytes([
            0xCD,  # 报头
            0x0A,  # 帧长度
            0x01,  # 功能位
            0x00,  # X轴方向 (占位符)
            0x00,  # 速度高8位 (占位符)
            0x00,  # 速度低8位 (占位符)
            0x00,  # Y轴方向
            0x00,  # Y速度高8位
            0x00,  # Y速度低8位
            0x00,  # 转向方向 (占位符)
            0x00,  # 角速度高8位 (占位符)
            0x00,  # 角速度低8位 (占位符)
            0x00   # 校验位 (占位符)
        ]),
    }
}

# ======================
# 视频编码配置
# ======================
VIDEO_ENCODER_CONFIG = {
    # 编码器优先级（从高到低）
    "encoder_priority": ["h264_nvenc", "h264_qsv", "libx264"],

    # 目标码率 (bps)
    "bitrate": 2_000_000,        # 2 Mbps

    # 关键帧间隔（GOP size）
    "gop_size": 30,              # 30帧 = 1秒一个关键帧（30fps）

    # 低延迟软编码参数 (libx264)
    "low_latency": {
        "profile": "baseline",   # baseline profile 最低延迟
        "preset": "ultrafast",   # 最快编码速度
        "tune": "zerolatency",   # 禁用编码缓冲
        "bframes": 0,            # 禁用 B 帧
        "rc_lookahead": 0,       # 禁用码率控制前瞻
    },

    # NVIDIA NVENC 硬件编码参数
    "nvenc": {
        "preset": "p1",          # 最快预设（p1-p7，p1最快）
        "tune": "ll",            # ll = low latency 低延迟模式
        "rc": "cbr",             # 恒定码率
        "zerolatency": True,     # 零延迟模式
    },

    # Intel Quick Sync Video 硬件编码参数
    "qsv": {
        "preset": "veryfast",    # 最快预设
        "async_depth": 1,        # 异步深度，1 = 最低延迟
    },
}

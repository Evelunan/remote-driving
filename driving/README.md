# Driving - 驾驶端

远程驾驶系统的驾驶端模块，运行在驾驶员的电脑上，负责采集方向盘输入并发送控制指令。

## 功能

- **USB 设备输入**: 从 USB 方向盘设备读取模拟输入信号
- **视频接收**: 通过 WebRTC 接收来自车辆端的实时视频流
- **控制指令传输**: 通过 QUIC 协议将控制指令发送到中继服务器

## 目录结构

```
driving/
├── main.py              # 程序入口
├── receiver.py          # 视频接收测试脚本
├── app/                 # 应用层
│   └── driving_datagram.py  # QUIC 数据报客户端
├── config/              # 配置文件
│   ├── server.yaml      # 服务器配置
│   ├── log.yaml         # 日志配置
│   └── loaders/         # 配置加载器
├── interfaces/          # 接口定义
├── infrastructure/      # 基础设施实现
│   ├── communication/   # 通信组件
│   │   └── control_states.py  # 控制状态管理
│   ├── device/          # 设备连接
│   │   └── usb_connector.py   # USB 设备连接器
│   ├── processing/      # 数据处理
│   │   ├── analog_calculator.py  # 模拟值计算器
│   │   └── data_processor.py     # 数据处理器
│   ├── protocol/        # 协议实现
│   │   ├── control_codec.py      # 控制指令编解码
│   │   └── driving_protocol.py   # 驾驶端协议
│   └── video/           # 视频接收
│       ├── webrtc_client.py      # WebRTC 客户端
│       └── video_receiver.py     # 视频接收器
├── factories/           # 工厂类
└── logger/              # 日志工具
```

## 控制协议

使用二进制编码传输控制指令：

| 字段 | 类型 | 说明 |
|------|------|------|
| msg_type | uint8 | 消息类型 |
| session_id | uint16 | 会话号 |
| speed | uint8 | 线速度 (128为中点) |
| steering | uint8 | 角速度 (128为中点) |

消息类型：
- `0x01` - HELLO_DRIVING
- `0x02` - HELLO_CAR
- `0x03` - CONTROL
- `0x04` - STATE
- `0x05` - HEARTBEAT

## 运行

```bash
# 启动驾驶端（USB 输入 + QUIC 控制传输）
python main.py

# 测试视频接收
python receiver.py --room <room_id> --server <signaling_server_url>
```

## 依赖

- Python 3.12+
- aiortc (WebRTC)
- aioquic (QUIC)
- pyusb (USB 设备)

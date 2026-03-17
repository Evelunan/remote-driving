# Vehicle-Unit - 车辆端

远程驾驶系统的车辆端模块，运行在车辆上，负责视频采集推送和接收控制指令。

## 功能

- **视频采集**: 从 USB 摄像头采集视频
- **视频合成**: 将多路摄像头画面合成为单路视频
- **视频推流**: 通过 WebRTC 将视频推送到驾驶端
- **控制接收**: 通过 QUIC 协议接收来自驾驶端的控制指令
- **车辆控制**: 通过串口将控制指令发送给车辆控制器

## 目录结构

```
vehicle-unit/
├── main.py              # 程序入口
├── sender.py            # 视频发送测试脚本
├── application/         # 应用层
│   ├── stream_app.py    # 多摄像头流应用
│   └── vehicle_datagram.py  # QUIC 数据报客户端
├── config/              # 配置文件
│   ├── server.yaml      # 服务器配置
│   ├── media.yaml       # 媒体配置
│   ├── vehicle.yaml     # 车辆配置
│   └── loaders/         # 配置加载器
├── interfaces/          # 接口定义
│   ├── icamera.py       # 摄像头接口
│   ├── icompositor.py   # 合成器接口
│   ├── istream.py       # 流接口
│   └── icontroller.py   # 控制器接口
├── infrastructure/      # 基础设施实现
│   ├── cameras/         # 摄像头
│   │   └── usb_camera.py    # USB 摄像头
│   ├── compositors/     # 视频合成
│   │   └── driving_compositor.py  # 驾驶画面合成器
│   ├── streams/         # 流推送
│   │   └── webrtc_streamer.py  # WebRTC 推流器
│   ├── controller/      # 控制器
│   │   └── serial_controller.py  # 串口控制器
│   └── protocol/        # 协议实现
│       ├── control_codec.py   # 控制指令编解码
│       └── vehicle_protocol.py # 车辆端协议
├── common/              # 公共工具
├── lib/                 # 外部库
│   ├── CvCameraIndex_x64.dll  # Windows 摄像头索引
│   └── libcamera_index.so     # Linux 摄像头索引
└── logger/              # 日志工具
```

## 控制协议

与驾驶端使用相同的二进制编码协议：

| 字段 | 类型 | 说明 |
|------|------|------|
| msg_type | uint8 | 消息类型 |
| session_id | uint16 | 会话号 |
| speed | uint8 | 线速度 (128为中点) |
| steering | uint8 | 角速度 (128为中点) |

## 运行

```bash
# 启动车辆端（视频推流 + 控制接收）
python main.py

# 测试视频发送
python sender.py --room <room_id> --server <signaling_server_url>
```

## 配置

**server.yaml** - 服务器连接配置:
- WebRTC 信令服务器地址
- STUN/TURN 服务器配置
- QUIC 服务器地址

**media.yaml** - 媒体配置:
- 摄像头设备列表
- 分辨率和帧率

**vehicle.yaml** - 车辆配置:
- 串口设备参数

## 依赖

- Python 3.12+
- aiortc (WebRTC)
- aioquic (QUIC)
- opencv-python (视频处理)
- pyserial (串口通信)

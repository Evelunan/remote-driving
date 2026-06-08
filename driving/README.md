# 驾驶端 (Driving Unit)

`driving` 是远程驾驶系统的驾驶端应用，提供 PyQt6 控制台、多路视频显示、驾驶外设读取、QUIC 控制指令发送和 WebRTC 视频接收能力。

## 功能定位

- 通过 WebRTC 接收车辆端推送的多路摄像头视频。
- 通过 QUIC Datagram 向车辆端发送低延迟控制指令。
- 读取 USB 驾驶外设数据，并转换为统一控制状态。
- 使用 PyQt6 展示登录页、视频网格、连接状态和控制状态。

## 项目结构

```text
driving/
├── app/                  # 驾驶端应用层报文定义
├── common/               # 公共常量
├── config/               # app、server、log 配置及加载器
├── factories/            # UI、视频、控制组件工厂
├── infrastructure/       # 通信、设备、协议、视频、工作线程等实现
├── interfaces/           # 抽象接口
├── logger/               # 日志模块
├── test/                 # FPS / RTT 数据与统计脚本
├── ui/                   # PyQt6 组件、主题和窗口控件
├── main_window.py        # GUI 主入口
├── receiver.py           # WebRTC 接收相关实验脚本
├── test.py               # QUIC 控制链路调试脚本
├── .python-version       # 独立部署时使用的 Python 版本，默认 3.12
└── pyproject.toml        # 子项目依赖
```

## 环境依赖

要求 **Python >= 3.12**。核心依赖包括：

- `pyqt6`：桌面 GUI。
- `aiortc`、`websockets`：WebRTC 视频接收与信令。
- `aioquic`：QUIC Datagram 控制链路。
- `opencv-python`、`numpy`：视频帧处理。
- `libusb1`：USB 驾驶外设接入。
- `pyopengl`、`pyopengl-accelerate`：可选 OpenGL 渲染组件。
- `pyyaml`：配置加载。

## 配置文件

- `config/app.yaml`：应用名称、版本、窗口尺寸、主题和刷新定时器。
- `config/server.yaml`：注册接口、WebRTC 信令地址、STUN / TURN、QUIC 中继地址。
- `config/log.yaml`：日志格式、级别与输出。

驾驶端需要在 `config/server.yaml` 中配置两个实际部署的服务地址：

| 服务 | 地址 |
| --- | --- |
| WebRTC 信令服务器 | `ws://<webrtc-signaling-host>:8765` |
| QUIC 中继服务器 | `<quic-relay-host>:4433` |

请按服务分别修改 `config/server.yaml` 中的 `webrtc.signaling.host` / `port` 和 `quic.host` / `port`。如果 WebRTC 需要公网穿透，再按部署环境配置 `webrtc.ice.stun` / `webrtc.ice.turn`。`regist` 配置目前未在驾驶端启动流程中使用。

## 启动方式

推荐在驾驶端实际部署目录使用 `uv`：

```powershell
cd driving
uv sync
uv run python main_window.py
```

正常启动后会进入登录页。开发调试时可以跳过登录：

```powershell
uv run python main_window.py --session 1234 --room 1234
```

参数说明：

- `--session`：QUIC 会话号，范围 `0-65535`，需要与车辆端一致。
- `--room`：WebRTC 房间号，需要与车辆端一致。

也可以进入子目录后使用 `pip`：

```powershell
cd driving
pip install .
python main_window.py --session 1234 --room 1234
```

## 调试与测试

统计已有 FPS / RTT 数据：

```powershell
cd driving
uv run python test/test_data_process.py
```

单独调试 QUIC 控制链路：

```powershell
cd driving
uv run python test.py
```

## 常见问题

- GUI 无法启动：确认当前环境支持桌面显示，并已安装 PyQt6。
- 一直等待车辆端：确认车辆端已启动，且 `room` 完全一致。
- QUIC 未连接：确认 QUIC 中继地址、端口、证书校验配置和防火墙规则。
- 无驾驶外设数据：确认 USB 设备权限、驱动和 `libusb1` 环境。

## 许可证

仓库当前未声明许可证。

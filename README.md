# 远程驾驶系统

本仓库是一个 Python 3.12 的远程驾驶系统工作区，包含驾驶端 GUI、车辆端采集与控制程序，以及两个中继服务：

- `driving`：驾驶端控制台，负责显示多路视频、读取驾驶外设并发送控制指令。
- `vehicle-unit`：车辆端程序，负责采集车载摄像头、推送 WebRTC 视频流，并接收 QUIC 控制指令。
- `servers/webrtc`：WebRTC 信令服务器，负责 SDP / ICE 信令交换。
- `servers/quic`：QUIC Datagram 中继服务器，负责驾驶端与车辆端之间的低延迟控制与状态数据转发。

## 项目结构

```text
my-driving/
├── driving/              # 驾驶端 GUI 与控制链路客户端
├── vehicle-unit/         # 车辆端视频采集、WebRTC 推流、底盘控制
├── servers/
│   ├── webrtc/           # WebSocket WebRTC 信令服务
│   └── quic/             # QUIC Datagram 中继服务
├── .python-version       # 本地 workspace 开发环境，默认 3.12
├── pyproject.toml        # uv workspace 配置
└── uv.lock               # 锁定的依赖版本
```

## 环境准备

根目录用于本地统一开发，使用 `uv` workspace 管理依赖，要求 **Python >= 3.12**。

```powershell
uv sync
```

四个子项目实际部署位置不同，因此每个子项目都保留独立的 `pyproject.toml` 和 `.python-version`：

- `driving/.python-version`：`3.12`
- `servers/webrtc/.python-version`：`3.12`
- `servers/quic/.python-version`：`3.12`
- `vehicle-unit/.python-version`：`system`，表示车辆端部署时跟随 ROS 小车的系统 Python。

每个子项目的 `pyproject.toml` 都配置了阿里云 PyPI 镜像源，独立部署时可以直接在子项目目录执行 `uv sync`。

如果不使用 `uv`，也可以进入各子项目目录后用 `pip install .` 安装对应依赖。驾驶端需要 GUI 环境，车辆端需要摄像头和可选串口 / ROS2 控制环境。

## 配置说明

本地联调前通常需要检查这些配置：

- `servers/webrtc/config/server.yaml`：WebRTC 信令服务监听地址，默认 `0.0.0.0:8765`。
- `servers/quic/config/server.yaml`：QUIC 中继服务监听地址，默认 `0.0.0.0:4433`。
- `driving/config/server.yaml`：驾驶端连接的信令服务器、QUIC 服务器、STUN / TURN 地址。
- `vehicle-unit/config/server.yaml`：车辆端连接的信令服务器、QUIC 服务器、STUN / TURN 地址。
- `vehicle-unit/config/media.yaml`：车辆端摄像头 VID / PID、分辨率和帧率。
- `vehicle-unit/config/vehicle.yaml`：底盘控制模式，默认 `controller: none`，可切换为 `serial` 或 `ros2`。

驾驶端和车辆端需要配置两个实际部署的服务地址：

| 服务 | 地址 |
| --- | --- |
| WebRTC 信令服务器 | `ws://<webrtc-signaling-host>:8765` |
| QUIC 中继服务器 | `<quic-relay-host>:4433` |

请分别修改 `driving/config/server.yaml` 与 `vehicle-unit/config/server.yaml` 中对应服务的 `host` / `port`。如果 WebRTC 需要公网穿透，再按部署环境配置 `webrtc.ice.stun` / `webrtc.ice.turn`。`regist` 配置目前未在启动流程中使用。

## 启动顺序

建议按下面顺序分别在多个终端中启动。

### 1. 启动 WebRTC 信令服务器

在 WebRTC 服务实际部署目录启动：

```powershell
cd servers/webrtc
uv sync
uv run python main.py
```

默认监听 `ws://0.0.0.0:8765`。

### 2. 启动 QUIC 中继服务器

在 QUIC 服务实际部署目录启动：

```powershell
cd servers/quic
uv sync
uv run python main.py
```

默认监听 `0.0.0.0:4433`。QUIC 需要 TLS 证书，仓库中已有 `servers/quic/certs/cert.pem` 与 `servers/quic/certs/key.pem`；如果需要重新生成测试证书，请参考 [servers/quic/README.md](servers/quic/README.md)。

### 3. 启动车辆端

普通测试模式或串口控制模式可以在车辆端部署目录使用 `uv` 启动：

```powershell
cd vehicle-unit
uv sync
uv run python main.py --session 1234
```

`--session` 是 QUIC 会话号，范围 `0-65535`。如果不传 `--room`，WebRTC 房间号默认与 `session` 相同。

```powershell
uv run python main.py --session 1234 --room car-1234
```

如果车辆端部署在 ROS2 小车上，并且 `vehicle-unit/config/vehicle.yaml` 中配置了 `controller: ros2`，需要使用 ROS 小车上启动 ROS 程序的 Python 环境运行，也就是 `/usr/bin/python`。此前测试环境是在 ROS 小车的 `remote_driver` 用户下。首次部署如需安装依赖，可先执行 `/usr/bin/python -m pip install .`。

```bash
cd vehicle-unit
/usr/bin/python main.py --session 1234
```

如需指定独立房间号：

```bash
/usr/bin/python main.py --session 1234 --room car-1234
```

### 4. 启动驾驶端

在驾驶端实际部署目录启动：

```powershell
cd driving
uv sync
uv run python main_window.py
```

正常启动会进入登录页。开发调试时可以带参数直接进入控制界面：

```powershell
uv run python main_window.py --session 1234 --room 1234
```

驾驶端 `--session` 需要与车辆端一致，`--room` 需要与车辆端 WebRTC 房间一致。

## 联调检查清单

- 信令服务器和 QUIC 中继服务器已启动，端口没有被防火墙拦截。
- 驾驶端与车辆端配置里的服务器地址一致。
- 车辆端 `vehicle-unit/config/media.yaml` 中的摄像头 VID / PID 与实际设备一致。
- 如果启用串口底盘控制，`vehicle-unit/config/vehicle.yaml` 中的 `controller`、串口路径和波特率正确。
- 本地测试自签名证书时，客户端 `quic.options.verify_mode` 可保持 `false`。

## 子项目文档

- [driving/README.md](driving/README.md)
- [vehicle-unit/README.md](vehicle-unit/README.md)
- [servers/webrtc/README.md](servers/webrtc/README.md)
- [servers/quic/README.md](servers/quic/README.md)

## 许可证

仓库当前未声明许可证。如需开源或对外交付，请补充 `LICENSE` 文件并在各子项目 README 中同步说明。

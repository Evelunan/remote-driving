# 车辆端 (Vehicle Unit)

`vehicle-unit` 是部署在真实车辆或仿真车辆上的端侧应用，负责采集多路摄像头画面、通过 WebRTC 推流给驾驶端，并通过 QUIC Datagram 接收控制指令后驱动车辆底盘。

## 功能定位

- 采集多路 USB 摄像头视频，并通过 WebRTC 推送给驾驶端。
- 连接 QUIC 中继服务器，接收指定 `session` 的控制指令。
- 支持测试模式、串口底盘控制和 ROS2 `cmd_vel` 控制入口。
- 使用异步协程与多进程方式隔离视频采集、推流和控制链路。

## 项目结构

```text
vehicle-unit/
├── application/          # 多摄像头管理与车辆端 QUIC 数据报应用逻辑
├── common/               # 公共工具和常量
├── config/               # media、server、vehicle、log 配置及加载器
├── infrastructure/
│   ├── cameras/          # USB 相机和测试相机实现
│   ├── compositors/      # 视频合成器
│   ├── controller/       # serial / ros2 底盘控制器
│   ├── protocol/         # 车辆端控制协议与编解码
│   └── streams/          # WebRTC 推流、信令客户端和视频轨道
├── interfaces/           # 抽象接口
├── lib/                  # 私有或平台相关依赖
├── logger/               # 日志模块
├── main.py               # 车辆端主入口
├── .python-version       # 独立部署时使用的 Python 选择，ROS 小车默认 system
└── pyproject.toml        # 子项目依赖
```

## 环境依赖

要求 **Python >= 3.10**。本地 workspace 开发通常使用 Python 3.12；ROS2 小车部署时使用系统 ROS Python，也就是 `/usr/bin/python`。

核心依赖包括：

- `aiortc`、`websockets`：WebRTC 推流与信令。
- `aioquic`：QUIC Datagram 控制指令接收。
- `opencv-python`、`numpy`：摄像头采集与帧处理。
- `pyserial`：串口底盘控制。
- `pyyaml`：配置加载。

在 Linux、树莓派或 Jetson 上运行时，需要确保当前用户有权访问 `/dev/video*` 和 `/dev/ttyUSB*` 等设备。

## 配置文件

- `config/server.yaml`：WebRTC 信令服务器、STUN / TURN、QUIC 中继服务器地址。
- `config/media.yaml`：摄像头名称、VID / PID、分辨率、帧率。
- `config/vehicle.yaml`：底盘控制器类型和运动控制参数。
- `config/log.yaml`：日志配置。

`config/vehicle.yaml` 中 `controller` 可选：

- `none`：测试模式，不下发底盘控制。
- `serial`：通过串口控制底盘。
- `ros2`：通过 ROS2 `cmd_vel` 控制入口。部署在 ROS 小车上时，需要使用启动 ROS 程序的 Python 环境运行车辆端。

车辆端需要在 `config/server.yaml` 中配置两个实际部署的服务地址：

| 服务 | 地址 |
| --- | --- |
| WebRTC 信令服务器 | `ws://<webrtc-signaling-host>:8765` |
| QUIC 中继服务器 | `<quic-relay-host>:4433` |

请按服务分别修改 `config/server.yaml` 中的 `webrtc.signaling.host` / `port` 和 `quic.host` / `port`。如果 WebRTC 需要公网穿透，再按部署环境配置 `webrtc.ice.stun` / `webrtc.ice.turn`。`regist` 配置目前未在车辆端启动流程中使用。

## 启动方式

普通测试模式或串口控制模式推荐在车辆端实际部署目录使用 `uv`：

```powershell
cd vehicle-unit
uv sync
uv run python main.py --session 1234
```

`--session` 是必填参数，范围 `0-65535`。默认情况下，WebRTC 房间号会使用相同的 `session` 值。

如果需要指定独立房间号：

```powershell
uv run python main.py --session 1234 --room car-1234
```

也可以进入子目录后使用 `pip`：

```powershell
cd vehicle-unit
pip install .
python main.py --session 1234
```

### ROS2 小车环境

如果车辆端在 ROS2 小车上运行，并且 `config/vehicle.yaml` 中配置了：

```yaml
controller: ros2
```

需要使用小车上启动 ROS 程序的 Python 环境，也就是 `/usr/bin/python`。此前测试环境是在 ROS 小车的 `remote_driver` 用户下。

首次部署如需安装依赖：

```bash
/usr/bin/python -m pip install .
```

```bash
cd vehicle-unit
/usr/bin/python main.py --session 1234
```

如果需要指定独立 WebRTC 房间号：

```bash
/usr/bin/python main.py --session 1234 --room car-1234
```

使用 ROS2 控制器时，请确保当前 shell 已具备对应 ROS2 环境，且 `/usr/bin/python` 能导入 ROS2 相关依赖。

## 与驾驶端配对

- 车辆端 `--session` 必须与驾驶端 `--session` 一致。
- 车辆端 `--room` 必须与驾驶端 `--room` 一致。
- 信令服务器和 QUIC 中继服务器需要先启动。

示例：

```powershell
# 车辆端机器
cd vehicle-unit
uv run python main.py --session 1234 --room 1234

# 驾驶端机器
cd driving
uv run python main_window.py --session 1234 --room 1234
```

## 常见问题

- 摄像头无法打开：检查 `config/media.yaml` 的 VID / PID、设备权限和摄像头占用情况。
- 串口无法打开：检查 `config/vehicle.yaml` 的串口路径、波特率和用户权限。
- 有控制指令但底盘不动：确认 `controller` 不是 `none`，并检查控制器实现与底盘协议。
- 视频不出现：确认 WebRTC 房间一致，信令服务器可达，STUN / TURN 配置可用。

## 许可证

仓库当前未声明许可证。

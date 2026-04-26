# 驾驶端 (Driving Unit)

基于 WebRTC 与 QUIC 协议的高性能远程驾驶系统。该项目旨在提供低延迟的多路视频传输以及可靠的控制信号下发机制，支持 USB 驾驶外设（方向盘、踏板等）接入，并具备完善的多线程异步处理架构。

## 🌟 核心特性

- **低延迟视频流**：基于 `aiortc` (WebRTC) 实现多路摄像头视频的实时传输。
- **低延迟控制信令**：基于 QUIC Datagram 扩展 (`aioquic`) 实现控制信号的高频、低延迟下发，有效避免 TCP 队头阻塞问题，同时结合 WebSocket 辅助连接握手。
- **多摄像机视图**：基于 PyQt6 自定义组件实现，其中使用 `multi_video_widget` 进行多视角的网格布局，由 `video_display` 负责接收并渲染实际的视频帧数据。
- **外设接入支持**：内置 `libusb1` 支持，读取真实驾驶端外设数据（如方向盘转向角、油门制动踏板等）。
- **异步与多线程架构**：控制系统与视频接收分离，保障高频实时交互的流畅性，避免主线程阻塞，且具备完善的优雅退出机制。
- **现代化 UI**：基于 PyQt6 构建的用户交互界面。

## 📂 项目结构

```text
driving/
├── app/                  # 应用层核心逻辑与数据报文定义 (如 driving_datagram)
├── common/               # 公共工具类与辅助函数
├── config/               # 配置文件目录 (日志配置 log.yaml、服务器配置 server.yaml、输入配置等)
├── factories/            # 各种工厂类，用于组件实例化
├── infrastructure/       # 基础设施层 (WebRTC 视频流、控制指令通信、USB 设备接入、工作线程等)
├── interfaces/           # 接口定义层，保障各模块解耦
├── logger/               # 日志记录模块，支持灵活的格式与输出
├── logs/                 # 运行时日志输出目录
├── ui/                   # 基于 PyQt6 的用户界面 (组件、主题、控件)
├── main.py               # 项目的主入口 (启动多线程与主控循环)
├── main_window.py        # 主界面入口文件
├── receiver.py           # 独立的 WebRTC 接收端测试/实现脚手架
└── pyproject.toml        # Python 项目配置及依赖清单
```

## 🛠️ 环境依赖

本项目要求 **Python >= 3.12**。
核心依赖如下：

- `aiohttp`, `aioquic`, `websockets` (网络通信)
- `aiortc` (WebRTC 协议栈)
- `opencv-python`, `numpy` (图像处理与矩阵运算)
- `pyqt6` (图形用户界面)
- `libusb1` (硬件设备通信)
- `pyopengl` (硬件加速与渲染)
- `pyyaml` (配置解析)

## 🚀 快速开始

### 1. 安装依赖

建议使用虚拟环境，通过 `pyproject.toml` 安装相关依赖，或直接使用 pip：

```bash
# 确保在 driving 目录下
pip install .
```

### 2. 运行主控程序

通过启动主程序，将自动拉起控制系统与视频流线程：

```bash
python main.py
```

如果你希望启动完整的 GUI 界面：

```bash
python main_window.py
```

### 3. 运行视频接收端 (调试/独立模块)

如果需要单独测试或运行 WebRTC 的接收端逻辑：

```bash
# 替换为指定的房间号
python receiver.py --room 1234
```

## 🔌 架构说明

系统主要分为两个核心线程：
1. **视频接收线程 (`WebRTCReceiverThread`)**：负责通过 WebRTC 建立点对点连接，并源源不断将解析到的帧推入 `FrameBuffer`，由主线程的 OpenCV 或 Qt 界面负责渲染。
2. **控制系统线程 (`ControlSystemThread`)**：负责轮询或监听本地 USB 驾驶外设数据，打包为统一的协议报文（Datagram），并发送至车辆端，具备防阻塞机制。

所有模块通过 `interfaces` 进行解耦，网络配置信息在 `config` 内集中管理。系统的退出采用了安全回收机制（Graceful Shutdown），保证各底层连接能够被正确关闭。

## 📝 许可证

[Add License Here]

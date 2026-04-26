# 车辆端 (Vehicle Unit)

这是远程驾驶系统部署在真实车辆（或仿真车）上的核心应用。它负责采集车载多路摄像头画面，通过 WebRTC 协议推流给驾驶端；同时通过 QUIC Datagram 接收极低延迟的控制指令（如方向盘、油门、刹车），并通过串口或 ROS2 将指令下发给车辆底盘执行。

## 🌟 核心特性

- **多路视频采集与推流**：借助 `opencv-python` 采集多路 USB 摄像头数据，结合 `aiortc` 实现基于 WebRTC 的实时视频流推送。
- **低延迟指令接收**：使用 `aioquic` 连接到 QUIC 中继服务器，以 Datagram 的形式接收无阻塞的高频控制信号。
- **灵活的底盘控制接口**：内置 `SerialController` 支持串口直接控制底盘，也预留了 ROS2 (`cmd_vel`) 的接入支持。
- **多进程与异步架构**：采集与网络推流采用多进程/异步协程混合架构，确保高帧率视频流与高频控制信号的吞吐不受互相阻塞影响。

## 📂 项目结构

```text
vehicle-unit/
├── application/          # 核心应用逻辑 (多摄像头管理、QUIC 数据报文处理端)
├── common/               # 公共工具和辅助函数
├── config/               # 配置文件 (摄像头参数、WebRTC与QUIC配置、硬件串口配置等)
├── infrastructure/       # 基础设施层
│   ├── cameras/          # USB 硬件相机或测试相机的驱动实现
│   ├── compositors/      # 画面合成器 (可选)
│   ├── controller/       # 车辆底盘控制器 (SerialController / ROS2 Controller)
│   ├── protocol/         # QUIC 通信协议实现
│   └── streams/          # WebRTC 推流器实现
├── interfaces/           # 各模块间的抽象接口 (依赖倒置)
├── lib/                  # 第三方库或私有依赖
├── logger/               # 日志记录模块
├── main.py               # 车辆端主入口 (启动视频采集与控制接收)
└── pyproject.toml        # 依赖清单
```

## 🛠️ 环境依赖

系统要求：**Python >= 3.12**，并确保操作系统支持对应的 USB 驱动或 ROS2 环境。

核心库：
- `aiortc`, `websockets`：WebRTC 信令与音视频推流。
- `aioquic`：QUIC 协议栈控制信号接收。
- `opencv-python`, `numpy`：图像采集与格式转换。
- `pyserial`：串口通信。
- `pyyaml`：配置解析。

## 🚀 快速开始

### 1. 安装依赖

车辆端环境推荐使用虚拟环境进行安装：

```bash
# 确保在 vehicle-unit 目录下
pip install .
```
> 如果你在 Linux (如树莓派/Jetson) 上运行，请确保当前用户有权访问 `/dev/video*` 和 `/dev/ttyUSB*` 设备。

### 2. 硬件与网络配置

- 请检查 `config/` 目录下的相关 YAML 配置文件，确保填入了正确的中继服务器 (QUIC) 和信令服务器 (WebRTC) 的 IP 及端口。
- 检查并修改摄像头相关的 `vid` 和 `pid` 参数，确保系统能够正确拉起对应的 USB 摄像头。

### 3. 运行车辆端程序

启动主程序，开始等待驾驶端的连接：

```bash
python main.py
```
> 此时程序会连接信令服务器与 QUIC 中继服务器。一旦远端驾驶端发起连接，视频流就会开始推送，底盘也会进入待命受控状态。

## 📝 许可证

[Add License Here]

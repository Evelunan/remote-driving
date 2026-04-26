# WebRTC Signaling Server (WebRTC 信令服务器)

轻量级、基于 WebSocket 的 WebRTC 信令服务器。该服务主要用于在远程驾驶系统中为“驾驶端”和“车辆端”提供早期的 WebRTC 握手机制（SDP 和 ICE 候选信息的交换），帮助双端建立点对点（P2P）的视频流连接。

## 🌟 核心特性

- **基于房间的隔离机制**：支持将多个连接划分到不同的房间 (`Room`) 中，每个房间内的节点（Peers）可互相广播信令，保障多车的独立控制互不干扰。
- **纯异步高性能**：基于 `websockets` 和 `asyncio` 构建，轻松处理高并发连接。
- **安全与配置化**：支持通过 `config/server.yaml` 灵活配置心跳间隔、最大消息大小、房间最大人数等安全与性能参数。
- **生命周期管理**：提供完整的上线/离线检测与优雅退出（Graceful Shutdown）功能，有效管理僵尸连接。

## 📂 项目结构

```text
webrtc/
├── common/               # 公用的异常或常量定义
├── config/               # 服务配置目录 (包含 server.yaml 及加载器)
├── logger/               # 独立日志系统
├── models/               # 数据模型定义 (如 Peer 状态模型等)
├── server/               # WebSocket 服务器核心逻辑
│   └── signaling_server.py # 负责端口监听、生命周期与连接分发
├── services/             # 业务逻辑服务层
│   └── room_manager.py   # 房间管理、消息解析分发中心
├── main.py               # 服务器启动入口
└── pyproject.toml        # 依赖与项目信息
```

## 🛠️ 环境依赖

系统要求：**Python >= 3.12**

核心库：
- `websockets`：提供底层 WebSocket 服务器支持。
- `pyyaml`：用于加载服务端配置。

## 🚀 快速开始

### 1. 安装依赖

推荐在虚拟环境中通过 `pyproject.toml` 安装依赖：

```bash
# 确保在 servers/webrtc 目录下
pip install .
```

### 2. 运行服务器

启动服务器（默认配置参数由 `config/server.yaml` 决定）：

```bash
python main.py
```

> 终端中将提示“信令服务器已启动: ws://[IP]:[PORT]”。此时客户端可以通过该地址进行 WebSocket 连接并发送 JSON 格式的信令消息。

## 🔌 信令交互简述

客户端连接后，基本交互流程如下：
1. **加入房间**：客户端发送 `{"type": "join", "room": "房间号"}` 报文，服务器将其注册到 `RoomManager` 对应的房间。
2. **SDP 交换**：客户端将生成的 WebRTC `offer` 或 `answer` 通过服务器转发给同房间内的目标节点。
3. **ICE 交换**：客户端将发现的网络打洞候选路径（ICE candidates）通过服务器进行互换，直到两端完成 P2P 建立。
4. **断开清理**：连接丢失或退出时，服务器自动清理房间内的失效节点。

## 📝 许可证

[Add License Here]

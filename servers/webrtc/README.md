# WebRTC 信令服务器

`servers/webrtc` 是远程驾驶系统的 WebRTC 信令服务，基于 WebSocket 转发驾驶端与车辆端之间的 SDP 和 ICE 消息，帮助双方建立点对点视频连接。

## 功能定位

- 按房间隔离驾驶端和车辆端连接。
- 转发 WebRTC offer、answer、ICE candidate 等信令消息。
- 管理 peer 生命周期，处理断开、清理和心跳。
- 通过 YAML 配置监听地址、房间人数、心跳和消息大小限制。

## 项目结构

```text
servers/webrtc/
├── common/               # 公共常量
├── config/               # server、log 配置及加载器
├── logger/               # 日志模块
├── models/               # Room、Peer 数据模型
├── server/               # WebSocket 服务器入口逻辑
├── services/             # 房间管理和消息分发
├── main.py               # 服务启动入口
├── .python-version       # 独立部署时使用的 Python 版本，默认 3.12
└── pyproject.toml        # 子项目依赖
```

## 环境依赖

要求 **Python >= 3.12**。核心依赖包括：

- `websockets`：WebSocket 服务端。
- `pyyaml`：配置加载。

## 配置文件

`config/server.yaml` 默认配置：

```yaml
host: "0.0.0.0"
port: 8765
max_peers_per_room: 2
ping_interval: 30.0
ping_timeout: 10.0
max_message_size: 1_048_576
```

`max_peers_per_room` 默认是 `2`，适合一辆车和一个驾驶端配对。如果需要观察端或多端接入，需要调整该值并确认业务逻辑支持。

## 启动方式

推荐在 WebRTC 服务实际部署目录使用 `uv`：

```powershell
cd servers/webrtc
uv sync
uv run python main.py
```

也可以进入子目录后使用 `pip`：

```powershell
cd servers/webrtc
pip install .
python main.py
```

启动后，客户端应连接到：

```text
ws://<webrtc-signaling-host>:8765
```

如果部署到其他机器，请同步修改驾驶端和车辆端 `config/server.yaml` 中的 `webrtc.signaling.host` / `port`。

## 信令流程

1. 驾驶端和车辆端连接 WebSocket 信令服务器。
2. 双方加入同一个房间。
3. 车辆端和驾驶端通过服务器交换 WebRTC SDP。
4. 双方继续交换 ICE candidate。
5. P2P 连接建立后，视频流由 WebRTC 通道传输，信令服务仅负责连接协调。

## 常见问题

- 客户端连不上：检查 `host` / `port`、防火墙和客户端配置。
- 一直等待对端：确认双方加入同一个房间，且 `max_peers_per_room` 足够。
- WebRTC 无法建立：信令服务只负责转发，仍需检查客户端 STUN / TURN 配置和网络可达性。

## 许可证

仓库当前未声明许可证。

# QUIC 中继服务器

`servers/quic` 是远程驾驶系统的 QUIC Datagram 中继服务，负责在驾驶端和车辆端之间按 `session` 转发低延迟控制指令与车辆状态数据。

## 功能定位

- 使用 `aioquic` 提供 QUIC Datagram 服务。
- 按 `session_id` 绑定驾驶端和车辆端。
- 转发控制指令、车辆状态和心跳报文。
- 使用 TLS 证书启动 QUIC 服务。

## 项目结构

```text
servers/quic/
├── certs/                # TLS 证书，默认读取 cert.pem 和 key.pem
├── common/               # 公共常量和路径
├── config/               # server、log 配置及加载器
├── logger/               # 日志模块
├── services/             # RelayProtocol、SessionManager、Codec
├── main.py               # 服务启动入口
├── .python-version       # 独立部署时使用的 Python 版本，默认 3.12
└── pyproject.toml        # 子项目依赖
```

## 环境依赖

要求 **Python >= 3.12**。核心依赖包括：

- `aioquic`：QUIC 协议栈。
- `pyyaml`：配置加载。

## 配置文件

`config/server.yaml` 默认配置：

```yaml
quic:
  host: "0.0.0.0"
  port: 4433
  enable_datagrams: true
  max_datagram_frame_size: 65536
  idle_timeout: 3600
```

驾驶端和车辆端都需要把各自 `config/server.yaml` 中的 `quic.host` 与 `quic.port` 指向该服务。

客户端连接地址应按实际部署环境填写，例如：

```text
<quic-relay-host>:4433
```

## TLS 证书

QUIC 必须使用 TLS。服务启动时会读取：

- `certs/cert.pem`
- `certs/key.pem`

仓库中已有一组证书文件。需要重新生成本地测试证书时，可以在 `servers/quic` 目录执行：

```powershell
New-Item -ItemType Directory -Force certs
openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem -out certs/cert.pem -days 365 -nodes
```

本地自签名证书联调时，客户端配置里的 `quic.options.verify_mode` 可保持 `false`。

## 启动方式

推荐在 QUIC 服务实际部署目录使用 `uv`：

```powershell
cd servers/quic
uv sync
uv run python main.py
```

也可以进入子目录后使用 `pip`：

```powershell
cd servers/quic
pip install .
python main.py
```

启动后默认监听：

```text
0.0.0.0:4433
```

## 报文流程

1. 车辆端以车辆身份连接 QUIC 中继，并发送自己的 `session_id`。
2. 驾驶端以驾驶端身份连接 QUIC 中继，并发送相同的 `session_id`。
3. 中继服务在 `SessionManager` 中完成配对。
4. 驾驶端发送控制指令，中继服务转发给车辆端。
5. 车辆端发送状态数据，中继服务转发给驾驶端。
6. 心跳报文用于维持连接和清理失效端点。

## 常见问题

- 服务启动失败：检查 `certs/cert.pem` 和 `certs/key.pem` 是否存在。
- 客户端连不上：检查 UDP 端口 `4433` 是否开放，客户端 `quic.host` 是否正确。
- 已连接但不转发：确认驾驶端和车辆端使用相同 `session_id`。
- 证书校验失败：本地自签名测试时确认客户端 `verify_mode: false`。

## 许可证

仓库当前未声明许可证。

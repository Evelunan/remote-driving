# QUIC Relay Server (QUIC 中继服务器)

专为远程驾驶系统设计的 QUIC Datagram 中继服务器，基于 `aioquic` 构建。该服务用于在“驾驶端（Driving）”与“车辆端（Car）”之间建立低延迟、高频的控制信号与状态数据传输通道。

## 🌟 核心特性

- **低延迟数据报中继**：利用 QUIC Datagram 扩展，丢弃了传统流式传输的可靠性限制，避免队头阻塞，实现极低延迟的控制信号透传。
- **灵活的会话管理**：基于 `Session ID` 动态绑定配对的驾驶端与车辆端点，提供双向消息路由 (`session_manager`)。
- **内建心跳检测**：中继协议 (`RelayProtocol`) 自带心跳保活机制，能自动检测并清理异常断开的连接。
- **安全加密机制**：强制要求使用 TLS 证书，保障车云/端云链路的数据安全。

## 📂 项目结构

```text
quic/
├── certs/                # TLS 证书目录 (需要放置 cert.pem 和 key.pem)
├── common/               # 公共常量和路径定义
├── config/               # 配置文件，如服务监听地址及 QUIC 参数 (server.yaml)
├── logger/               # 独立的日志记录模块
├── services/             # 核心服务逻辑实现
│   ├── relay_protocol.py # QUIC 数据报解析与处理中枢
│   ├── session_manager.py# 维护车辆端与驾驶端对等连接关系的管理器
│   └── codec.py          # 控制报文与网络数据间的编解码器
├── main.py               # 中继服务主启动入口
└── pyproject.toml        # 依赖与项目信息
```

## 🛠️ 环境依赖

系统要求：**Python >= 3.12**

核心库：
- `aioquic`：异步 QUIC 协议栈。
- `pyyaml`：配置文件解析。

## 🚀 快速开始

### 1. 准备 TLS 证书

QUIC 强制要求加密传输，因此在启动服务器前，你必须在 `certs` 目录下准备好对应的证书文件：

```bash
mkdir -p certs
# 生成用于本地测试的自签名证书
openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem -out certs/cert.pem -days 365 -nodes
```

### 2. 安装依赖

通过 `pyproject.toml` 安装依赖：

```bash
pip install .
```

### 3. 运行服务器

启动并监听配置好的端口（由 `config/server.yaml` 指定）：

```bash
python main.py
```
> 默认情况下，日志将提示“中继服务器正在监听 [主机IP]:[端口]”。

## 🔌 报文交互流程

中继服务器依靠 Datagram 的消息类型 (`msg_type`) 和身份 (`role`) 决定路由：
1. **握手与绑定 (HELLO)**：端点发送 `HELLO_CAR` 或 `HELLO_DRIVING` 报文以及对应的会话号，服务器将在 `SessionManager` 中进行登记。
2. **数据转发 (FORWARD)**：端点发送 `CONTROL` (控制指令) 或 `STATE` (车辆端状态)，服务器识别出对端绑定的 Protocol 后进行透传。
3. **心跳保活 (HEARTBEAT)**：定期进行 Ping 探测维持连接的活跃性。

## 📝 许可证

[Add License Here]

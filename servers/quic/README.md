### quic - QUIC 中继服务器

用于在驾驶端和车辆端之间转发控制指令，支持低延迟数据报传输。

```
quic/
├── main.py              # 程序入口
├── services/            # 服务层
│   ├── session_manager.py   # 会话管理
│   ├── relay_protocol.py    # 中继协议
│   └── control_codec.py     # 控制指令编解码
├── config/              # 配置
│   ├── server.yaml      # 服务器配置
│   └── log.yaml         # 日志配置
├── certs/               # TLS 证书
│   ├── cert.pem
│   └── key.pem
└── common/              # 公共常量
```

**配置项** (server.yaml):
- `host`: 监听地址
- `port`: 监听端口
- `enable_datagrams`: 启用数据报
- `max_datagram_frame_size`: 最大数据报大小
- `idle_timeout`: 空闲超时
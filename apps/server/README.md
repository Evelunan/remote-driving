# 统一服务端

`rd-server` 提供信令和 QUIC 中继两个模块，支持同进程启动或同一制品分别启动。视频媒体仍通过 WebRTC 直连或 TURN 传输，不经过此服务。

当前改造尚未完成：这里仍是旧协议的本机开发入口。可靠注册、身份授权、控制租约和新版协议正在后续阶段实现。

```bash
uv run --package rd-server rd-server --help
# 只启用本机信令
uv run --package rd-server rd-server --services signaling --allow-legacy-development
# 双模块（证书由部署者提供，仓库不包含私钥）
uv run --package rd-server rd-server --services signaling,relay --allow-legacy-development --cert /path/cert.pem --key /path/key.pem
# 单独中继使用 --services relay
```

有证书时信令使用 WSS；QUIC 始终需要 TLS。旧协议开发入口只允许 `127.0.0.1` 或 `::1`。需要客户端信任提供的证书，不能通过关闭验证来替代信任配置。

所有选中模块启动成功后运行时才标记就绪。任一模块启动失败会清理已启动模块；运行中监听器意外关闭会撤销就绪并关闭其他模块。测试覆盖单模块、双模块、部分失败回滚和真实本机 WSS/QUIC 握手与关闭。

构建与独立安装见[根 README](../../README.md)。完整配置、认证与生产部署尚未验收，勿将本机开发验证视为实车或公网部署验收。

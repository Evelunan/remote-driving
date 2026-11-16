# 统一服务端

`rd-server` 提供信令和 QUIC 中继两个模块，支持同进程启动或同一制品分别启动。视频媒体仍通过 WebRTC 直连或 TURN 传输，不经过此服务。

统一配置与认证信令已实现。当前 QUIC 仍是旧协议的本机开发入口；可靠注册、控制租约和两端认证客户端在后续批次实施。

```bash
uv run --package rd-server rd-server --help
# 本机旧客户端的单模块开发入口
uv run --package rd-server rd-server --config configs/server.local.yaml
# 认证 WSS 配置检查：需先提供配置引用的令牌环境变量和 TLS 文件
uv run --package rd-server rd-server --config configs/server.signaling.yaml --check-config
# 有效配置与字段来源，不解析秘密值
uv run --package rd-server rd-server --config configs/server.signaling.yaml --dump-config
# 旧协议双模块本机测试；单独中继可改为 --services relay
uv run --package rd-server rd-server --services signaling,relay --allow-legacy-development --cert /path/cert.pem --key /path/key.pem
```

服务端拒绝未知字段和无效范围，入口注入不可变配置。只启用一个模块时不要求另一个模块的证书。认证角色、room/session 范围与令牌环境变量在实例文件中声明；令牌和私钥不入库。配置规则、字段迁移与权限行为见[配置说明](../../docs/configuration.zh-CN.md)。

所有选中模块启动成功后运行时才标记就绪。任一模块启动失败会清理已启动模块；运行中监听器意外关闭会撤销就绪并关闭其他模块。测试覆盖单模块、双模块、部分失败回滚、真实本机 WSS/QUIC 握手、信令权限与关闭。

构建与独立安装见[根 README](../../README.md)。整套认证迁移和实车验收尚未完成。

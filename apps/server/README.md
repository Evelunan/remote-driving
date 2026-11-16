# 统一服务端

`rd-server` 提供信令和 QUIC 中继两个模块，支持同进程启动或同一制品分别启动。视频媒体仍通过 WebRTC 直连或 TURN 传输，不经过此服务。

统一配置、认证信令和 QUIC v2 可靠注册/独占租约已实现。驾驶端与车辆端仍在迁移新版客户端；旧协议只保留显式本机开发入口，不能与认证模式混用。

```bash
uv run --package rd-server rd-server --help
# 本机旧客户端的单模块开发入口
uv run --package rd-server rd-server --config configs/server.local.yaml
# 认证双模块配置检查：需先提供配置引用的令牌环境变量和 TLS 文件
uv run --package rd-server rd-server --config configs/server.yaml --check-config
# 有效配置与字段来源，不解析秘密值
uv run --package rd-server rd-server --config configs/server.yaml --dump-config
# 旧协议双模块本机测试；单独中继可改为 --services relay
uv run --package rd-server rd-server --services signaling,relay --allow-legacy-development --cert /path/cert.pem --key /path/key.pem
```

服务端拒绝未知字段和无效范围，入口注入不可变配置。只启用一个模块时不要求另一个模块的证书。认证角色、room/session 范围与令牌环境变量在实例文件中声明；令牌和私钥不入库。配置规则、字段迁移与权限行为见[配置说明](../../docs/configuration.zh-CN.md)。

所有选中模块启动成功后运行时才标记就绪。任一模块启动失败会清理已启动模块；运行中监听器意外关闭会撤销就绪并关闭其他模块。测试覆盖单模块、双模块、部分失败回滚、真实本机 WSS/QUIC 握手、信令/控制权限隔离、独占租约、代次恢复与关闭。

构建与独立安装见[根 README](../../README.md)。中继只在车端确认新代次准备完成后授予租约，并等待中立命令执行确认后放行运动目标。失联、超时、凭据过期和执行失败撤销控制权；这不代表真实底盘已停车。整套客户端迁移和实车验收尚未完成。

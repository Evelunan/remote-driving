# 统一服务端

`rd-server` 提供信令和 QUIC 中继两个模块，支持同进程启动或同一制品分别启动。视频媒体仍通过 WebRTC 直连或 TURN 传输，不经过此服务。

中继在 TLS 连接上校验端点凭据、角色与会话权限。同一会话各绑定一条驾驶连接与车辆连接，两者到齐后中继自动配对。

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

双模块运行时，信令或 QUIC 中继绑定失败、监听中断会触发该模块清理并在一秒后重试。信令重启期间，中继继续转发控制指令。中继重启期间，信令继续服务原视频房间，驾驶端与车端重新注册后自动配对。两者都在监听时，服务端才标记为就绪。单模块启动失败会向调用方报告。程序化的 `ServerRuntime.start()` 保留事务启动语义，供要求所有监听器同时就绪的调用方使用。

构建与独立安装见[根 README](../../README.md)。中继按连接、配对代次、序号和有效窗口检查控制指令，转发设备实际返回的执行回执。停止控制、断线或身份过期会使旧代次失效。

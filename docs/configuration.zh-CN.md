# 配置与身份授权

当前实施状态：统一服务端已接入下述加载机制；驾驶端和车辆端的不可变配置模型已建立，运行组件尚在迁移。认证信令已实现；QUIC 可靠注册、控制租约和客户端认证仍待后续批次，不应据此认为整套系统已可用于实车。

## 加载规则

优先级为：类型化默认值 → 实例 YAML → 当前角色环境变量 → 命令行。使用 `--config /absolute/path/server.yaml` 指定实例，也可设置 `RD_CONFIG`。相对路径统一以实例文件所在目录为基准；没有实例文件时以当前工作目录为基准。

环境变量采用 `RD_SERVER__SIGNALING__PORT=8766` 形式。`--set signaling.port=8767` 可重复指定不同字段。值按 YAML 类型解析，布尔值、整数、数值、数组必须匹配字段定义；数组整体替换，不拼接。`--host`、`--services`、`--cert`、`--key`、`--signaling-port`、`--relay-port` 是相应字段的便捷覆盖；不得同时用便捷参数和 `--set` 指定同一字段。

字段名和字段类型严格校验，拒绝未知字段、重复 YAML 键、非有限数值、无效范围、YAML 别名以及过深配置。未知字段即使属于未启用模块也会报错；仅启用 signaling 时无需填写 relay 的证书。配置加载不使用模块单例，多份实例互不污染；加载结果不可变，不支持运行中热更新关键控制参数。

```bash
# 不监听、不连接设备；检查当前入口支持的模式、令牌环境变量和 TLS 文件
rd-server --config /path/server.yaml --check-config
# 不解析秘密值，只输出有效配置及各字段来源
rd-server --config /path/server.yaml --dump-config
# 单模块运行，CLI 的端口值覆盖文件与环境变量
rd-server --config /path/server.yaml --services signaling --set signaling.port=8767
```

`--dump-config` 只进行配置模式校验，因此可在尚未注入令牌、尚未安装证书时使用；它不是可启动性验收。`--check-config` 在配置校验后创建监听器配置，但不启动监听器。未实现的认证 QUIC 模式会明确报错，不能自动退回旧协议。

## 身份与信令权限

服务端的 `auth.principals` 定义身份 ID、角色、允许访问的控制 session 和视频 room。令牌通过 `token_env` 引用环境变量，要求 32–4096 字符且每个身份使用不同令牌。应使用密码学随机令牌；仓库不保存令牌或私钥。内存中的认证表保存令牌 SHA-256 摘要，使用常量时间摘要比较。

客户端在 WebSocket 握手头中发送 `Authorization: Bearer <token>`。服务端在登记 peer 前验证令牌；无令牌、无效令牌和已过期令牌均关闭连接（1008）。加入房间前检查 room 权限，过期身份不能发送或接收信令；有 `expires_at` 的空闲连接也会自动关闭。身份过期配置使用带时区的 ISO 时间。变更或撤销令牌目前需要重启服务，现有连接将关闭，不提供静默热更新。

`viewer` 可以访问被授予的视频房间，但其角色不能用于取得驾驶控制权。房间成员关系不代表控制租约；控制授权由后续 relay 模块独立裁决。服务端总是根据连接填充消息 `from`；指定 `to` 时，目标必须是同房间其他成员，非法目标不会变成广播。

应用 DEBUG 日志不打开 WebSocket 传输层的报文/头部 DEBUG 输出，避免记录 Authorization。配置转储只显示环境变量引用；应用错误信息不包含令牌、SDP 或原始 YAML 行。

## 示例与迁移

- [server.local.yaml](../configs/server.local.yaml)：显式旧协议、本机 signaling；可配合当前未迁移客户端测试。
- [server.signaling.yaml](../configs/server.signaling.yaml)：认证 WSS 示例，需部署方提供证书和三个令牌环境变量。相对 TLS 路径在 `configs/tls/` 下解析。

旧信令模块内的 `server.yaml` 和缓存加载器已移除，入口统一为 `rd-server`。字段映射：`ping_interval` → `signaling.ping_interval_s`，`ping_timeout` → `signaling.ping_timeout_s`，`max_message_size` → `signaling.max_message_bytes`；host、port、max_peers_per_room 移入 signaling。

信令和中继可分别配置监听地址和 TLS 路径。生产信令必须使用 WSS；认证的非回环监听在任何环境都要求 TLS。未经认证的旧模式只能显式启用 `allow_legacy_development`，且仅允许 development 环境与回环地址。QUIC 旧入口仍需证书，`relay.idle_timeout_s`、`relay.max_datagram_bytes` 已传入 aioquic；其余新版租约字段尚未接入旧协议，不能当作已生效的安全策略。

回退须匹配对应版本的制品与配置；新文件不能直接交给旧入口。认证连接不降级为匿名连接。当前三应用的联动认证迁移尚未完成，后续发布文档会给出完整协同升级/回退步骤。

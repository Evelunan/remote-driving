# 配置与身份授权

当前实施状态：三个正式入口均使用不可变配置。认证 WSS/WebRTC、QUIC 租约和配对时钟诊断已有本机验证；驾驶 USB、UI、车辆运动限值和相机配置已实际注入并通过模拟联动验证。当前版本不作为实车部署完成态。

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

`--dump-config` 只进行配置模式校验，因此可在尚未注入令牌、尚未安装证书时使用；它不是可启动性验收。`--check-config` 在配置校验后创建监听器配置，但不启动监听器。认证 QUIC 模式仅接受 `rd-control/2`，不匹配的协议会被拒绝，不能自动退回旧协议。

## 身份与信令权限

服务端的 `auth.principals` 定义身份 ID、角色、允许访问的控制 session 和视频 room。令牌通过 `token_env` 引用环境变量，要求 32–4096 字符且每个身份使用不同令牌；同时用于信令时须使用不含空白的可见 ASCII 字符。应使用密码学随机令牌；仓库不保存令牌或私钥。内存中的认证表保存令牌 SHA-256 摘要，使用常量时间摘要比较。

客户端在 WebSocket 握手头中发送 `Authorization: Bearer <token>`。服务端在登记 peer 前验证令牌；无令牌、无效令牌和已过期令牌均关闭连接（1008）。加入房间前检查 room 权限，过期身份不能发送或接收信令；有 `expires_at` 的空闲连接也会自动关闭。身份过期配置使用带时区的 ISO 时间。变更或撤销令牌目前需要重启服务，现有连接将关闭，不提供静默热更新。

`vehicle` 才能发送 offer，`driving`/`viewer` 只能向车辆发送 answer；candidate 也只能在车辆与接收角色之间路由。每个房间最多一个车辆发布者，离开后其他已授权车辆才能加入。服务端在 joined/new_peer 中提供真实角色，接收端只接受本房间车辆的 offer。`viewer` 可以访问被授予的视频房间，但其角色不能用于取得驾驶控制权。房间成员关系不代表控制租约；控制授权由 relay 模块独立裁决。服务端总是根据连接填充消息 `from`；指定 `to` 时，目标必须是同房间其他成员，非法目标不会变成广播。

两端共用 `rd_webrtc.signaling.SignalingClient`：校验证书及主机名，非回环地址拒绝明文 WS；禁止重定向和隐式环境代理，避免令牌发送到其他目的地。TURN 用户名/口令从环境变量引用读取。加入房间必须收到确认，断线结束监听，不在旧连接上重试。`network.signaling_*` 控制消息大小、ping、发送及关闭超时，`register_timeout_s` 限定连接与加入房间时间，`reconnect_delay_s` 控制重连间隔。固定 websockets 16.0 并测试禁止重定向的适配点。

应用 DEBUG 日志不打开 WebSocket 传输层的报文/头部 DEBUG 输出，避免记录 Authorization。配置转储只显示环境变量引用；应用错误信息不包含令牌、SDP 或原始 YAML 行。

## 示例与迁移

- [server.yaml](../configs/server.yaml)：认证双模块示例；用 `--services relay` 或 `--services signaling` 可分别部署。
- [server.local.yaml](../configs/server.local.yaml)：显式旧协议、本机 signaling；仅供旧协议回归；新版媒体客户端不降级到匿名信令。
- [server.signaling.yaml](../configs/server.signaling.yaml)：认证 WSS 示例，需部署方提供证书和三个令牌环境变量。相对 TLS 路径在 `configs/tls/` 下解析。

旧信令模块内的 `server.yaml` 和缓存加载器已移除，入口统一为 `rd-server`。字段映射：`ping_interval` → `signaling.ping_interval_s`，`ping_timeout` → `signaling.ping_timeout_s`，`max_message_size` → `signaling.max_message_bytes`；host、port、max_peers_per_room 移入 signaling。

信令和中继可分别配置监听地址和 TLS 路径。生产信令必须使用 WSS；认证的非回环监听在任何环境都要求 TLS。未经认证的旧模式只能显式启用 `allow_legacy_development`，且仅允许 development 环境与回环地址。QUIC 旧入口仍需证书，且不提供认证租约。在认证 v2 中，`idle_timeout_s` 进入传输配置，`register_timeout_s` 限定注册/准备阶段，`lease_ttl_s` 控制授权过期，`max_control_age_ms` 与 `max_clock_skew_ms` 约束采样新鲜度，`max_datagram_bytes` 与 `max_rpc_bytes` 限定消息大小；这些租约字段不适用于旧协议。租约 TTL 必须大于控制采样年龄上限。

回退须匹配对应版本的制品与配置；新文件不能直接交给旧入口。认证连接不降级为匿名连接。三应用正式入口均使用认证 v2；后续发布文档会补充完整协同升级/回退演练。

## 驾驶端和车辆端

两端也支持 `--config`、`--set`、`--dump-config`、`--check-config`。环境变量分别以 `RD_DRIVING__`、`RD_VEHICLE__` 开头。`--session` 和 `--room` 独立覆盖；指定其中一个不会隐式修改另一个。GUI 的会话选择只覆盖本次连接资源 ID，保留其他配置；配置文件重新加载需要重启应用。

- [driving.yaml](../configs/driving.yaml)：驾驶端，USB VID/PID 与 UI 配置，`RD_OPERATOR_TOKEN`。
- [vehicle.yaml](../configs/vehicle.yaml)：ROS2 模板，真实相机稳定路径需替换，`RD_VEHICLE_TOKEN`。
- [vehicle.mock.yaml](../configs/vehicle.mock.yaml)：模拟控制器与四路合成相机；无需底盘/相机，仍需要真实的本地 TLS 和认证配置。

驾驶端 `--connect` 立即使用配置的 session/room，否则打开选择页面；同时提供两个便捷参数也会立即连接。车辆端 `--components control,media` 或单项选择模块；controller 为 none 时禁用控制、不注册、不生成 ACK，control-only + none 会在启动前拒绝。正式入口默认以父进程管理独立 control/media 子进程，单模块模式只启动所选子进程。详见[进程隔离](process-isolation.zh-CN.md)。

两端 `--dump-config` 不加载 Qt、OpenCV、ROS，也不解析秘密；`--check-config` 校验所选模块的本地凭据/证书，车辆选 ROS2 时检查 rclpy 导入，不创建 ROS 节点，不打开 USB/相机/串口，不连接网络。依赖导入通过不意味着真实设备可用。

旧配置不再隐式兼容，迁移时显式使用以下字段（禁止混用不同单位）：

| 原配置/行为 | 新配置/行为 |
| --- | --- |
| 包内多份 server.yaml | `network`，显式 WSS URL、QUIC host/port/server_name 与 CA 文件 |
| 固定 TURN 用户名/口令 | `network.ice_servers` 中的 username_env / credential_env 引用 |
| app.yaml 中 USB 常量 | `usb.vendor_id` / `usb.product_id` |
| UI 刷新常量与 debug | `ui.frame_interval_ms` / stats_interval_ms / debug；画面过期改为 `video.stale_frame_ms` |
| 车端模糊 cm/s、mm/s 最大速度 | `motion.max_linear_mps` / max_angular_radps，当前默认 5.0 / 2.0 |
| 控制超时秒数 | `control.command_timeout_ms`，当前默认 300；输入超时默认 200 ms |
| 串口全局配置 | `serial.port` / baud_rate / read_timeout_s / write_timeout_s / template_hex |
| VID/PID 推测相机索引、硬编码四路参数 | `cameras` 数组，各路 source / width / height / fps |

相机 name 仅接受当前四视角 front/back/left/right，不允许重复；source 可为 synthetic、非负设备索引或设备路径。推荐 Linux `/dev/v4l/by-id/…` 稳定路径。尺寸必须为正偶数，FPS 为 (0, 240]；各路实际硬件参数需检查协商日志。数组整体替换，未配置的视角显示无信号。

### 摄像头不足时复用画面

车辆端可设置 `camera_fallback_source: front`，将未配置输入的视角补齐为四路，适合单摄像头或双摄像头测试：

```yaml
camera_fallback_source: front
cameras:
  - name: front
    source: /dev/video0
    width: 640
    height: 480
    fps: 30
  - name: back
    source: /dev/video2
    width: 640
    height: 480
    fps: 30
  - name: left
    # 未写 source，复用 front；right 整项省略，也复用 front。
```

仅有一只摄像头时，只保留上述 front 条目即可。完整的本机测试配置见 [vehicle.single-camera.yaml](../configs/vehicle.single-camera.yaml)，使用模拟底盘和真实前摄像头。先确认 WSL 已出现可读取的视频设备，再按实际情况修改 `/dev/video0`，启动命令：

```bash
direnv exec . uv run --no-sync rd-vehicle --config configs/vehicle.single-camera.yaml
```

- 显式 `source` 优先，包括设备索引 `0` 和 `synthetic`；该视角独立采集。设备路径不存在、无权限或读取故障会按原流程报错，不会被替换为 front。
- 缺少 `source`、`source: null` 或整个视角未列出时，复用所选源。源视角本身必须存在且显式配置 `source`；可选择 front/back/left/right。
- 复用视角不重复打开设备、不新增采集线程，沿用源帧的分辨率、采集序号、时间和帧率。该视角的 width/height/fps 不单独生效；原采集帧失效时，复用视角一并失效。
- 每个视角仍使用独立 WebRTC 轨道并单独编码，并非只发送一路视频。列出视角时使用其 `encoding`，省略整个视角时继承源视角的 `encoding`。
- 默认 `camera_fallback_source: null` 关闭该功能；未列出的视角保持无信号，已列出但未写 source 的条目保留原有合成视频默认行为。修改配置后重启车辆端。旧版本不识别新字段，升级或回退需配套配置文件。

复制的辅助视角展示的是同一摄像头画面，仅用于测试多视角界面和传输，不提供车辆其他方向的真实视野。

串口默认读写超时均为 0.02 s，13 字节模板显式可配，速度编码上限由 uint16 表示范围限制；这部分只保留兼容适配，用户当前选择 ROS2。运动限值和超时均需实车验收，停车接口尚未接入物理确认。

主视角和控制授权使用统一 `video.primary_view` / `stale_frame_ms` / `recovery_ms`；不再接受 `ui.stale_frame_ms`。媒体发布身份/session 必须匹配控制租约的车辆，恢复须持续新帧与新的中立输入，详见 [画面与控制](video-safety.zh-CN.md)。三个应用须协同升级信令来源与 acquire 字段，缺少新字段时不会降级授权。

## 可选编码预算

`encoder_workers` 是车辆媒体进程内、所有视角和订阅者共享的编码工作线程数，范围 1–32；默认 `null` 保留 aiortc 原路径。配置后使用独立工作线程池，不占 asyncio 默认线程池的编码工作名额；这不是编解码及 Qt 全部原生线程数的上限，也不限制驾驶端解码器。

每个 `cameras` 条目可包含 `encoding`，缺省为 `null`。设定此字段必须同时启用 `encoder_workers`，目前按视角预算仅支持 VP8；该视角通过 SDP 仅提供 VP8/RTX，遇到不支持的对端不会静默使用忽略预算的其他 codec。未启用逐视角预算的轨道保留原有 codec 协商。

```yaml
encoder_workers: 2
cameras:
  - name: front
    source: synthetic
    width: 1080
    height: 720
    fps: 30
    encoding:
      target_bitrate_bps: 1000000
      min_bitrate_bps: 250000
      max_bitrate_bps: 1500000
      max_threads: 1
  - name: back
    source: synthetic
    width: 640
    height: 480
    fps: 30
    encoding:
      target_bitrate_bps: 500000
      min_bitrate_bps: 250000
      max_bitrate_bps: 600000
      max_threads: 1
```

上例用于合成视频实验，`cameras` 数组整体替换；需要四路时必须同时填写另两路。每路要求 `10000 <= min_bitrate_bps <= target_bitrate_bps <= max_bitrate_bps <= 50000000`，字段均为整数。REMB 反馈可改变目标值，但只能在配置范围内；底层编码器按原有超过 10% 的变化条件重新配置。目标和实际编码 bit_rate、实际 RTP payload 速率可用基准工具分别观测。关键帧、RTX、RTP/UDP/SRTP 等开销以及编码器的短期波动不受该目标字段严格封顶，不能据此直接承诺带宽硬限制。

`max_threads` 为每个 VP8 编码器的线程上限（1–16 或 `null`）；上限不会强制将低分辨率编码器提升到更多线程，`null` 使用 aiortc 原有选择。高码率、低线程上限或过少工作线程可能增加 CPU、延迟或掉帧，需按实际视角和订阅数量测量。仓库默认配置未采用实验参数。

等待编码名额时只保存轨道状态，名额可用后重新取最新且未过期的采集帧。冻结的相机不占名额。编码结束后若已超过采集年龄预算，则不发送该结果，并要求下一帧为关键帧，防止丢弃参考帧破坏解码。取消会等本次原生编码调用结束再回收，不能把取消 Future 当作已终止原生线程；原生调用永久阻塞仍由车端进程监督器超时处置。

适配集中于 `rd_webrtc.encoding`，只绑定当前 sender 实例，不替换库级全局函数。所用 aiortc 1.14.0 / PyAV 16.1.0 已固定，启用预算时会在 `--check-config` 和运行时验证版本；升级必须复验真实编码、重连、取消及安装门槛。

新增预算字段属于车辆应用配置。升级/回退时配置与对应车辆 wheel 一起切换；旧版车辆入口会拒绝新增字段，应恢复旧版完整配置，不能只回退二进制文件。性能与资源取舍见 [B21 测量](video-performance.zh-CN.md)。

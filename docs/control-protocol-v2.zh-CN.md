# 控制协议 v2

当前协议组合为 QUIC ALPN `rd-control/2`、Datagram 格式 v2 和注册修订 `direct-control/4`。三端须配套升级。

## 连接与可靠消息

TLS QUIC 的 ALPN 固定为 `rd-control/2`；完成握手后才处理业务消息，不接受 0-RTT 业务数据。证书与主机名验证由客户端配置完成。连接建立只代表传输就绪；应用还需按角色和会话认证注册。

每个 RPC 请求使用一个双向 stream，响应从同一 stream 返回。消息编码为 `uint32 big-endian body_length` + UTF-8 JSON，且必须接收到 FIN 才处理。JSON 对象包含 `v: 2`，拒绝重复键、非有限数、错误字段组合和超限消息。

- 请求：`{"v":2,"type":"request","method":"register","params":{...}}`。
- 成功响应：`{"v":2,"type":"response","result":{...}}`。
- 失败响应：`{"v":2,"type":"response","error":"access_denied"}`；错误码不携带令牌或原始负载。
- 单向事件：`{"v":2,"type":"event","method":"control_route","params":{...}}`，使用 unidirectional stream。

默认 RPC 上限 16 KiB、32 个并发请求/接收任务、5 秒组帧/处理超时。请求超时取消对应远端处理；stream ID 不复用，晚到响应不能完成新请求。连接关闭时取消并等待所属处理任务。传输层还限制等待 QUIC 确认的流数量，拥塞时拒绝继续排队。关键撤销通知遇到背压时，上层必须关闭目标连接并依赖车端 watchdog，不能无界重试。

## Datagram 固定格式

全部整数使用网络字节序。头部为 34 字节：

| 字段 | 字节数 | 含义 |
| --- | ---: | --- |
| magic | 2 | ASCII `RD` |
| version | 1 | 2 |
| kind | 1 | 下表消息类型 |
| session | 2 | 车辆控制会话编号，不能代替身份认证 |
| epoch | 16 | 服务端分配的非零 UUID。车辆状态使用连接的状态流 UUID，其余消息使用配对代次 UUID |
| seq | 4 | 当前代次内的 uint32 序号，严格判断更新与回绕 |
| source_time_ms | 8 | 原始采样时间，Unix 毫秒；重复发送缓存样本时不得刷新 |

| kind | 负载 | 语义 |
| --- | --- | --- |
| 1 CONTROL | 三个 float32 后接 16 字节窗口标记 | throttle/steering ∈ [-1,1]，brake ∈ [0,1] |
| 5 WINDOW | 16 字节随机标记 | 车端在当前配对代次内滚动签发；头部 seq 为窗口序号，source_time_ms 为 0 |
| 2 EXECUTION | uint8 status、uint64 completed_at_ms | 执行结果；status=1 表示设备 API 完成，2 拒绝、3 控制器未启用、4 已失效；不是实测车速/停车 |
| 3 TELEMETRY | float32 linear_mps、angular_radps | 来自传感器的真实测量；未接入传感器时不发送伪造值 |
| 4 STOP_STATUS | uint8 requested、confirmed | 两字段只允许 0/1；请求与物理确认分开，没有真实反馈时 confirmed=0 |
| 6 VEHICLE_STATUS | uint8 有效位掩码、四个 float32、四个 uint8 | 底盘车速 m/s、转向角 °、左右轮速 m/s、档位、自动驾驶使能、驾驶模式、刹车状态 |

`VEHICLE_STATUS` 的掩码 bit 0..7 对应上述八个字段。未置位表示字段未知，线上占位值必须为 0，解码后为 `None`；已置位的两个布尔字段只允许 0/1。NaN/Inf 不进入协议，不能用无效字段占位冒充实测零。档位和模式保留底盘原始 uint8 编码。该消息不表示停车确认，旧 `TELEMETRY` 的线速度/角速度语义保持不变。

速度字段名为 `speed_mps`、`left_wheel_mps` 和 `right_wheel_mps`，单位固定为 m/s。`vehicle-status/2` 修正了旧 `vehicle-status/1` 的 km/h 约定；虽然 Datagram 长度和字段顺序相同，两种语义不能混用，注册时严格校验修订。

解码时校验固定长度、版本、类型、代次与数值范围。控制 Datagram 无送达保证。拥塞期间仅保留最新控制值；同代次的控制窗口和执行回执各保留最新一条。车辆状态在队列空闲时发送。注册、代次变更和停止请求使用可靠流。

## 新鲜度与代次约束

车端在本机单调时钟上记录每个标记的截止时刻。窗口长度取中继的 max_control_age_ms 与车端 command_timeout_ms 的较小值。驾驶端只可选择在该次 USB 采样之前已收到的标记。多个窗口可以重叠，丢失一次签发后可用后续窗口恢复。中继按收到标记的本机时刻检查转发资格，车端以自己的原始截止时刻检查执行资格。

执行线程和 watchdog 共用窗口的原始截止时刻。ACK 按配对代次、序号和原始采样标识匹配，驾驶端还用本机采样至确认的耗时检查 ACK 新鲜度。控制路径无需跨机比较 Unix 时间。独立遥测和停车状态仍按原时间戳检查，max_clock_skew_ms 保留用于观测数据。

采样和传送所用的时间会扣减车端 watchdog 的剩余预算。每次连接换代都丢弃旧代次命令、执行回执和待发数据。执行回执用于确认设备接口调用；车辆是否运动或停下仍需真实反馈。

## 已实现的中继业务

三端与共享包须匹配注册修订 `direct-control/4`。旧租约协议（含 `vehicle-status/3`）会收到 `protocol_upgrade_required`。

注册 RPC 为 `register(token, role, session, revision)`。令牌确定身份，申报的角色必须与服务端配置相符。只有 driving/vehicle 角色可注册对应 session。车端绑定独占，冲突连接不能覆盖已有绑定，同一连接不能二次注册切换身份。注册响应包含连接专用的 `status_stream` 和中继的 `idle_timeout_s`。已注册客户端以双方配置中较短空闲超时的三分之一为间隔调用 `clock({})` 保活。

成功注册的 driving 连接自动订阅该 session 的 `VEHICLE_STATUS`。中继只接收当前已认证车辆连接、同 session、同状态流 UUID 且时间与序号有效的底盘状态。转发时写入每个订阅者的状态流 UUID 和独立序号，保留原始采样时间，即使车辆重连也可以继续按序接收。车辆状态回传独立于操作员的启停选择，状态转发失败只影响当前样本。接收端仍需检查状态流、采样时效及更新次序。

同一会话各保留一条已认证驾驶连接与车辆连接。双方到齐后，中继在可靠流上向双方发送 `control_route` 事件，携带 `revision`、`epoch` 与 `vehicle_principal`。缺少对端时，后两个字段均为空。端点只接受当前连接上更新的事件版本。配对期间，车端在后台滚动发布有效窗口。

点击开始后，驾驶端直接发送新采样。中继核对配对身份、序号和窗口，首条可以是制动、油门或转向。车端执行后返回的 ACK 须与该代次中已转发指令的序号、原始采样时间和时效相符。中继最多保留最近 64 个样本用于匹配。

驾驶端或车端可通过可靠消息 `stop_control({epoch})` 停止当前代次。中继清理待发命令；若双方仍在线，会自动分配新的代次。旧代次的停止请求不能打断新代次。断线或凭据失效立即取消原有配对。设备端独立监护控制超时和执行失败。

`clock({})` 返回服务器 Unix 毫秒，可用于客户端时钟诊断；它不授予权限，也不能延长注册期限。未完成注册的连接会在 register_timeout_s 后关闭。旧协议仅在显式回环开发模式下运行，与认证 v2 使用不同 ALPN 约束。

## 传输实现边界

`rd-core.wire` 不依赖 QUIC；`rd-quic` 提供唯一组帧、请求/响应、任务与关闭逻辑。aioquic 1.3 缺少公开的强制取消定时器和替换未发 Datagram 队列 API，因此两处操作集中在 `rd_quic.compat`，版本固定为 1.3.0；升级必须通过真实连接、拥塞与关闭测试。应用不应散布这些私有字段访问。

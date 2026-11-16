# 控制协议 v2

实施状态：B08 已完成唯一 Datagram codec 与可靠 RPC；B09 已接入认证中继、可靠注册、独占租约与代次恢复，并通过本机真实 QUIC 测试。驾驶端/车辆端集成随后实施；本文件不是整套控制安全验收报告。旧协议仍保留显式本机开发入口，不能自动降级。

## 连接与可靠消息

TLS QUIC 的 ALPN 固定为 `rd-control/2`；完成握手后才处理业务消息，不接受 0-RTT 业务数据。证书与主机名验证由客户端配置完成。连接建立只代表传输就绪；应用还需认证注册和控制授权。

每个 RPC 请求使用一个双向 stream，响应从同一 stream 返回。消息编码为 `uint32 big-endian body_length` + UTF-8 JSON，且必须接收到 FIN 才处理。JSON 对象包含 `v: 2`，拒绝重复键、非有限数、错误字段组合和超限消息。

- 请求：`{"v":2,"type":"request","method":"register","params":{...}}`。
- 成功响应：`{"v":2,"type":"response","result":{...}}`。
- 失败响应：`{"v":2,"type":"response","error":"access_denied"}`；错误码不携带令牌或原始负载。
- 单向事件：`{"v":2,"type":"event","method":"lease_revoked","params":{...}}`，使用 unidirectional stream。

默认 RPC 上限 16 KiB、32 个并发请求/接收任务、5 秒组帧/处理超时。请求超时取消对应远端处理；stream ID 不复用，晚到响应不能完成新请求。连接关闭时取消并等待所属处理任务。传输层还限制等待 QUIC 确认的流数量，拥塞时拒绝继续排队。关键撤销通知遇到背压时，上层必须关闭目标连接并依赖车端 watchdog，不能无界重试。

## Datagram 固定格式

全部整数使用网络字节序。头部为 34 字节：

| 字段 | 字节数 | 含义 |
| --- | ---: | --- |
| magic | 2 | ASCII `RD` |
| version | 1 | 2 |
| kind | 1 | 下表消息类型 |
| session | 2 | 车辆控制会话编号，不能代替身份认证 |
| epoch | 16 | 服务端分配的非零租约 UUID |
| seq | 4 | 当前代次内的 uint32 序号，严格判断更新与回绕 |
| source_time_ms | 8 | 原始采样时间，Unix 毫秒；重复发送缓存样本时不得刷新 |

| kind | 负载 | 语义 |
| --- | --- | --- |
| 1 CONTROL | float32 throttle、brake、steering | 归一化目标：throttle/steering ∈ [-1,1]，brake ∈ [0,1] |
| 2 EXECUTION | uint8 status、uint64 completed_at_ms | 执行结果；status=1 表示设备 API 完成，2 拒绝、3 控制器未启用、4 已失效；不是实测车速/停车 |
| 3 TELEMETRY | float32 linear_mps、angular_radps | 来自传感器的真实测量；未接入传感器时不发送伪造值 |
| 4 STOP_STATUS | uint8 requested、confirmed | 两字段只允许 0/1；请求与物理确认分开，没有真实反馈时 confirmed=0 |

精确匹配类型与长度，拒绝额外字节、未知版本、空 epoch、非法枚举、NaN/Inf 与越界值。固定向量和边界回归见 `tests/test_v2_wire.py`。Datagram 不保证送达，拥塞时同一连接只保留一个尚未发送的最新 Datagram；可靠注册、租约和停车请求不能依赖它保证送达。

## 新鲜度与代次约束

接收方用 `max(0, now_ms - source_time_ms + clock_skew_ms)` 作为保守年龄上界；过于超前的时间戳和达到最大年龄的样本被拒绝。这个上界依赖发送方/接收方时钟差不超过配置预算，部署必须同步时钟并验收误差。客户端时钟诊断与车端本地监护的 v2 集成将在后续批次接入；不能仅凭报文新增字段宣称已保证时限。

采样年龄会扣减车端本地单调时钟 watchdog 的剩余预算。心跳不刷新采样时间、序号不代替时间戳、执行 ACK 不代替传感器反馈。每次新的控制权授予需要新 epoch 和中立输入；旧代次命令、确认和关闭回调必须拒绝。服务端已执行代次/来源/序号/新鲜度检查和中立执行确认门控；车端状态机的 v2 集成仍待完成。

## 已实现的中继业务

注册 RPC 为 `register(token, role, session)`；令牌确定身份，申报的角色必须与服务端配置相符。只有 driving/vehicle 角色可注册控制 session，viewer 即使拥有视频房间权限也不能控制。车端绑定独占，冲突连接不能覆盖已有绑定；同一连接不能二次注册切换身份。

`acquire({})` 仅供已注册 driving 连接调用。服务端先预占 session，生成 UUID epoch，再可靠调用车端 `prepare_lease`，参数为 session、epoch、lease_ttl_ms、max_control_age_ms、max_clock_skew_ms。车端须返回 `{ready: true, epoch: <相同值>}`。准备失败、超时、连接关闭或申请取消都会撤销预占；旧准备响应不能撤销后来取得的新租约。

授予结果包含上述参数与 `neutral_required: true`。在车端返回中立命令的 EXECUTED 结果前，中继只放行 throttle=0、steering=0 的控制包；brake 可为合法制动值。驱动侧应保持新鲜中立采样直至获得执行确认，再允许运动目标。执行 ACK 必须对应当前租约实际转发过的 seq 和原始采样时间；中继最多保留 64 个最近样本用于校验。执行失败会撤销租约。

`release({epoch})` 可由当前驾驶者或车端发起，成功返回 `{revoked: true}`。旧 epoch 返回 false，其他驾驶者不能释放现有租约。服务端也支持可靠单向 release 事件用于尽快撤销；结束前需确认结果时使用 RPC。

撤销会向双方发送可靠 `lease_revoked(epoch, reason)` 事件。断线、租约期限、凭据过期、转发失败、执行失败和服务停止都可触发撤销。只有有效、更新且未过期的控制样本续期，续期扣减已有采样年龄；心跳、时钟探测、ACK、遥测与非法包均不续期。车端独立 watchdog 仍是必需项，服务端通知不能代替物理制动。

`clock({})` 返回服务器 Unix 毫秒，可用于客户端时钟诊断；它不授予权限，也不能延长注册期限。未完成注册的连接会在 register_timeout_s 后关闭。旧协议仅在显式回环开发模式下运行，与认证 v2 使用不同 ALPN 约束。

## 传输实现边界

`rd-core.wire` 不依赖 QUIC；`rd-quic` 提供唯一组帧、请求/响应、任务与关闭逻辑。aioquic 1.3 缺少公开的强制取消定时器和替换未发 Datagram 队列 API，因此两处操作集中在 `rd_quic.compat`，版本固定为 1.3.0；升级必须通过真实连接、拥塞与关闭测试。应用不应散布这些私有字段访问。

# 控制协议 v2

实施状态：B08 已完成唯一 Datagram codec 与可靠 RPC 传输基础，并通过本机真实 QUIC 测试。服务端租约和驾驶端/车辆端集成随后实施；本文件不是整套控制安全验收报告。旧协议仍保留显式本机开发入口，不能自动降级。

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

接收方用 `max(0, now_ms - source_time_ms + clock_skew_ms)` 作为保守年龄上界；过于超前的时间戳和达到最大年龄的样本被拒绝。这个上界依赖发送方/接收方时钟差不超过配置预算，部署必须同步时钟并验收误差。客户端时钟诊断、租约及车端本地监护将在集成批次接入；不能仅凭报文新增字段宣称已保证时限。

采样年龄会扣减车端本地单调时钟 watchdog 的剩余预算。心跳不刷新采样时间、序号不代替时间戳、执行 ACK 不代替传感器反馈。每次新的控制权授予需要新 epoch 和中立输入；旧代次命令、确认和关闭回调必须拒绝。这些规则由后续服务端与车端状态机共同落实。

## 传输实现边界

`rd-core.wire` 不依赖 QUIC；`rd-quic` 提供唯一组帧、请求/响应、任务与关闭逻辑。aioquic 1.3 缺少公开的强制取消定时器和替换未发 Datagram 队列 API，因此两处操作集中在 `rd_quic.compat`，版本固定为 1.3.0；升级必须通过真实连接、拥塞与关闭测试。应用不应散布这些私有字段访问。

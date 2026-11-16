# 异常网络与服务恢复验收

本记录使用真实 WSS、WebRTC、QUIC 和 Qt offscreen 绘制，摄像头为四路 320×240、30 FPS 合成源，底盘为 mock。驾驶输入以 100 Hz 采样，正式控制客户端按默认配置发送；所有身份、画面来源、授权代次、执行确认和画面新鲜度逻辑均使用应用实现。此工具不生成假的视频就绪事件，也不放宽应用安全阈值。

## 范围与复现

[validate_network.py](../scripts/validate_network.py)在一个测试进程内运行正式组件，Qt 位于主线程，服务器、媒体和控制组件位于后台 asyncio 线程，保留真实相机/设备/编码线程。车端正式分进程的性能和故障隔离另见[进程隔离](process-isolation.zh-CN.md)。这里没有模拟实车停车确认。

[network_shaper.py](../scripts/network_shaper.py)包装该测试事件循环创建的 UDP 端点，在真实 socket 发送前施加延迟、抖动、丢包、按字节串行化的带宽限制及黑洞。它覆盖实际 QUIC、STUN、DTLS、RTP/RTCP 数据包，不解密或伪造确认，不修改主机路由、网卡或内核配置，拒绝非回环目的地址。

每个端点的默认队列同时限制为 256 个包、256 KiB 和 500 ms，超过限制丢包并计数。进入故障时可以丢弃旧队列；恢复时保留仍在排队的旧包，由正式协议和授权逻辑处理晚到流量。停止端点时取消全部定时发送。随机种子固定为 42；编码和网络反馈会改变发包顺序，因此固定种子不代表不同运行的丢包位置完全相同。

```bash
for scenario in mild rpc-only control-blackhole media-blackhole clock-budget signaling-restart relay-restart server-restart; do
  .venv/bin/python scripts/validate_network.py --case "$scenario" --seconds 3 \
    --output "docs/validation/network-b26-${scenario}.json"
done
.venv/bin/python -m pytest -q tests/test_network_shaper.py tests/test_network_validation.py
```

`--seconds`控制轻度扰动的观察时长或故障恢复后的持续运行时长。轻度扰动要求至少 2 秒，以观察 RTP 丢包后的恢复；开发校准中使用 0.5 秒窗口曾无法观察到某个辅助视角恢复，因此没有将该短窗口算作通过。轻度扰动的持续授权和四路进展断言保留；正式记录使用 3 秒。

程序自行创建临时证书和随机角色令牌，报告只记录源代码哈希、环境、策略、统计和时间，不记录凭据或报文内容。已有输出先置为 `passed: false`，失败不会留下旧成功结论。

## B26 已执行场景

| 场景 | 注入方式和验收条件 |
| --- | --- |
| mild | 媒体端点延迟 5±3 ms、丢包概率 1%、上限 3 Mbit/s；控制端点延迟 2±1 ms、丢包概率 1%、上限 256 kbit/s。控制代次保持，四路继续绘制，实际收到执行确认 |
| rpc-only | 在 QUIC 加密前仅抑制应用 DATAGRAM，可靠 RPC 继续工作。零速后实际发起并收到车辆到中继的 clock RPC，证明连接存活不能维持旧运动目标 |
| control-blackhole | 丢弃全部控制 UDP，媒体继续。驾驶端撤权、车辆请求模拟零速；恢复网络并保持油门时不能自动恢复 |
| media-blackhole | 丢弃全部媒体 UDP，控制 UDP 保持正常。主画面失效导致撤权和模拟零速，车辆控制连接仍存活 |
| clock-budget | 控制 UDP 每跳增加 80 ms，观察正式 ClockGuard 的预算拒绝，不扩大默认预算；要求零速、撤权及恢复后重新回中 |
| signaling-restart | 信令与中继分开托管；停止信令，使用相同地址/端口创建新的信令实例。中继仍运行，恢复后发布身份改变 |
| relay-restart | 分开托管；停止中继并创建新监听器/注册表，信令和媒体保持运行，旧授权不继承 |
| server-restart | 共同托管；停止统一 runtime，再用相同端口创建新的信令与中继实例，重新认证、收流和授权 |

七个故障场景都先发送非零目标，再注入故障；撤权后继续保持油门，恢复链路并等待画面新鲜，额外保持 750 ms，确认没有自动授权。随后产生新鲜中立输入，必须取得不同的控制代次，最后再次发送非零目标。工具检查整个模拟设备执行历史，确保从首次零速到新中立授权之间没有短暂非零脉冲。退出要求相机/设备线程、媒体任务、UDP 端点及所有延迟发送均回收。

正式 B26 单次记录如下，时间单位为 ms。零速时间是从注入开始到 mock 接收零速目标，不是物理停车时间；恢复授权时间包含测试故意保持油门的 750 ms，因此不作为最短恢复延迟基准。

| 场景 | 故障→模拟零速 | 恢复→四路重新绘制 | 恢复→新中立授权 | 原始记录 |
| --- | ---: | ---: | ---: | --- |
| rpc-only | 243.20 | 20.82 | 818.87 | [JSON](validation/network-b26-rpc-only.json) |
| control-blackhole | 245.23 | 105.25 | 898.65 | [JSON](validation/network-b26-control-blackhole.json) |
| media-blackhole | 511.45 | 3702.61 | 4493.97 | [JSON](validation/network-b26-media-blackhole.json) |
| clock-budget | 481.08 | 20.56 | 2471.82 | [JSON](validation/network-b26-clock-budget.json) |
| signaling-restart | 14.75 | 628.31 | 1424.59 | [JSON](validation/network-b26-signaling-restart.json) |
| relay-restart | 1.45 | 138.83 | 923.43 | [JSON](validation/network-b26-relay-restart.json) |
| server-restart | 1.37 | 668.93 | 1448.62 | [JSON](validation/network-b26-server-restart.json) |

[轻度扰动记录](validation/network-b26-mild.json)中，约 3 秒内收到 142 个执行确认，采样到确认的中位数为 26.88 ms、最大值为 34.35 ms；媒体四路 RTP payload 约为 50.94 / 62.05 / 45.98 / 54.48 kbit/s，接收端窗口末尾 RTP jitter 约为 1.58–2.43 ms。原始报告保留所有端点包计数、实际被选中的 ICE 路径和可用的 RTCP 统计；统计缺失不被解释为零 RTT/零丢包。不同连接代次的 RTP ID 不直接相减，重启后不强行给出跨连接码率差值。

## 尚未覆盖的条件

- 所有实际 ICE 候选均为回环 host。尚未以此报告证明强制 TURN、跨 NAT、跨机器或真实无线网络。
- 轻度扰动的合成画面码率低于设定带宽上限。字节限速已通过真实 UDP 校准，但本批不能代替媒体负载超过可用带宽的饱和拥塞验收。
- UDP 延迟包含 STUN/DTLS/QUIC 握手及媒体数据。TCP/WSS 未做带宽/丢包整形；信令使用有序关闭/重新监听场景。
- 本批服务重启为新 runtime/监听器/注册表实例；关闭会通知客户端，不是进程 SIGKILL、断电或目标机 systemd 重启。
- 本机功能测试的时间分布不构成实时停车上界。目标网络时钟误差、相机曝光到物理显示、ROS2/真实底盘制动与停车确认仍待目标环境验收。
- B19 的偶发非零退出、B20 的长期原生内存滞留没有因本批短时正常退出而被标为解决。

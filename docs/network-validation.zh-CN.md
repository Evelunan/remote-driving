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

## B27 强制 TURN 与中继进程重启

TURN 是 WebRTC 的中继服务：两端无法直接连通时，视频经过 `车辆端 → TURN → 驾驶端`。它与交换连接信息的信令服务、传输控制指令的 QUIC 中继各自承担不同职责。这里使用真实 coturn 4.6.1 进程，重启会丢失旧的中继分配；仅保持信令在线不能重新建立这些分配。

新增 [local_turn.py](../scripts/local_turn.py) 在私有临时目录创建随机凭据和配置，仅监听、转发至 IPv4 回环地址。它不安装或启动系统服务。诊断适配将 aiortc/aioice 设置为只收集 relay 候选，同时移除 host 传输，防止 peer-reflexive 候选悄悄改走直连；报告要求实际选中的双方候选均为 `relay`。这些只用于测试的适配固定检查 aiortc 1.14.0 和 aioice 0.10.2，未改变应用的候选策略。

```bash
# 可执行文件已在 PATH 时无需设置；解包方式可额外提供依赖库目录。
export RD_TEST_TURNSERVER=/path/to/turnserver
export RD_TEST_TURN_LIBRARY_DIR=/path/to/extracted/usr/lib/x86_64-linux-gnu
for scenario in turn-relay turn-outage; do
  .venv/bin/python scripts/validate_network.py --case "$scenario" --seconds 3 \
    --output "artifacts/${scenario}.json"
done
RD_TEST_REQUIRE_TURN=1 .venv/bin/python -m pytest -q tests/test_network_validation.py -k turn
```

`turn-relay` 验证四路转发、持续授权和执行确认；`turn-outage` 在非零模拟目标期间向私有 coturn 发送 SIGKILL，等待撤权及模拟零速，再在同一端口启动新的 coturn。恢复后仍必须经过新鲜画面、回中和新代次授权；执行历史检查覆盖整个失效至重新授权区间。

[修复前记录](validation/turn-b27-before-failed.json)确认真实缺陷：模拟零速已请求，重启后等待 45 秒仍无画面，双方 peer 数量均为 0，但信令仍在线。旧实现只关闭失效 peer，未触发新的 offer。[修复后记录](validation/turn-b27-after.json)及[正常中继记录](validation/turn-b27-relay.json)保留实际路径、环境、源码指纹、包计数和子进程退出结果。

| 默认 ICE 时序下的单次记录 | 故障→模拟零速 | 重启→四路恢复 | 重启→新中立授权 |
| --- | ---: | ---: | ---: |
| 修复前 | 517.15 ms | 等待 45 秒超时 | 未恢复 |
| 修复后 | 498.28 ms | 32528.76 ms | 33303.26 ms |

表中重启耗时包括 coturn 就绪和 ICE 故障检测，授权耗时还包括测试故意保持油门的 750 ms。这是本机单次功能记录，不是时间上界或实车停车测量。失败记录保留旧应用指纹；成功记录的应用、共享包和诊断脚本指纹均对应本批最终源码。

修复由接收端在失效 peer 完成清理后关闭自己的信令连接，使用既有认证/重连流程重新加入房间，促使车辆发布端产生新 offer。发布端、其他接收端和相机源不整体重启；真实双接收端回归确认另一接收端继续使用原连接。重复失效、会话替换、新 offer 及主动退出的竞态不能关闭新连接。无信令协议变更，无自动恢复驾驶授权。

正式记录没有缩短 aioice 的 consent 检测周期；恢复时间仍受其默认失败检测影响，不能称为快速恢复。pytest 的两项 TURN 场景显式使用 `--diagnostic-consent-interval 0.2` 缩短故障检测等待，报告记录该值，其耗时不能当作生产恢复性能。CI 安装 coturn 前屏蔽系统服务启动，并设置 `RD_TEST_REQUIRE_TURN=1`，确保缺少依赖时失败；本地未提供 coturn 时这两项会明确跳过。

## 尚未覆盖的条件

- B26 的实际 ICE 候选均为回环 host；B27 已补充回环 relay。跨 NAT、跨机器、外部 TURN、高分辨率中继负载或真实无线网络仍需目标环境验证。
- 轻度扰动的合成画面码率低于设定带宽上限。字节限速已通过真实 UDP 校准，但本批不能代替媒体负载超过可用带宽的饱和拥塞验收。
- UDP 延迟包含 STUN/DTLS/QUIC 握手及媒体数据。TCP/WSS 未做带宽/丢包整形；信令使用有序关闭/重新监听场景。
- B26 的应用服务重启为新 runtime/监听器/注册表实例；关闭会通知客户端。B27 只验证了 coturn 的真实 SIGKILL；应用服务进程硬退出、断电和目标机 systemd 重启仍未覆盖。
- 本机功能测试的时间分布不构成实时停车上界。目标网络时钟误差、相机曝光到物理显示、ROS2/真实底盘制动与停车确认仍待目标环境验收。
- B19 的偶发非零退出、B20 的长期原生内存滞留没有因本批短时正常退出而被标为解决。

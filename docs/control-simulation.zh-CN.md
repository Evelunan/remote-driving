# 无硬件控制链路验证

B10 使用真实 DrivingControlClient、VehicleControlClient 和认证 QUIC 中继，连接内存 MockController。它验证注册、连接配对、直接发送首条指令、配置限值、输入新鲜度、失效撤销和新代次恢复，不打开 USB、摄像头、串口或 ROS。

当前正式 GUI/CLI 入口仍在迁移，不能把该模拟结果当作默认入口已切换新版协议。新版控制客户端已经接收不可变配置；后续入口应直接使用这些客户端，不能再复制连接/握手实现。

## 复现

在已安装工作区依赖的 Python 3.10.12 环境运行：

~~~bash
uv sync --all-packages --group dev --python 3.10.12
uv run --no-sync python scripts/simulate_control.py --output /tmp/control-simulation.json
~~~

脚本只绑定回环地址，自动生成临时证书和随机令牌；运行结束删除临时证书并关闭客户端、监听器和设备线程。默认保持模拟运动目标 1 秒，可用 --motion-seconds 调整为 0.1–60 秒。

模拟器在本机启动两端，点击开始后发送新采样，随后模拟输入中断并检查零速请求。恢复阶段重新提供中立采样并核对新代次的执行回执。

[本机记录](../benchmarks/results/control-b10-local.json)来自 Python 3.12.16、WSL2，仅用于确认工具和软件路径。模拟限值为 1.2 m/s、0.4 rad/s；项目默认的 5.0 m/s、2.0 rad/s 未改变。

| 报告字段 | 含义 |
| --- | --- |
| execution_acks | 设备 API 完成后收到的执行确认数量 |
| capture_to_ack_ms_* | 原始采样到收到执行确认的本机单调时钟耗时；包含输入停止后有效期内的重复样本，不是网络 RTT |
| input_expiry_to_mock_zero_target_ms | 停止模拟采样后，内存底盘观察到零速目标的耗时，不是物理停车时间 |
| new_epoch_after_neutral_recovery | 新鲜中立输入后是否获得不同的配对代次 |
| device_threads_closed | 设备执行与监护线程是否正常退出 |
| physical_stop_verified | 固定为 false；没有真实底盘反馈 |

该报告没有视频性能对比，不能替代后续视频基准、弱网测试或实车验收。一次运行的延迟不能作为部署阈值。

## 客户端约束

两端校验服务器证书、主机名与协议修订。控制指令按车端滚动签发的随机标记核验新鲜度，车端单调时钟决定窗口何时到期，详见[控制协议](control-protocol-v2.zh-CN.md)。握手和注册仍有超时。

驾驶端从开始请求后的新采样起发送控制。输入失效或回执超时触发保护；再次发送时使用有效的新代次。重复发送仍保留原始采样时间。

车端以独立监护/执行线程消费最新有效命令，按已消耗的采样年龄缩短 watchdog 预算。API 执行完成后才发 EXECUTION；不生成虚假实测速度。设备调用阻塞时，独立停车接口仍可收到通知，但软件不能强杀已经进入硬件库的调用，也不能据此保证物理停车。

测试还覆盖仅重启驾驶端、旧连接回调、执行 ACK 丢失、阻塞设备、证书主机名不匹配、墙钟偏差、迟到指令及强制关闭期限。真实 ROS2、底盘 watchdog/制动和传感器反馈仍需部署方接入验收。

模拟器直接启动独立的控制会话，无需视频或合成画面事件。实际回环视频及 Qt 呈现测试见[视频状态与控制授权](video-safety.zh-CN.md)。

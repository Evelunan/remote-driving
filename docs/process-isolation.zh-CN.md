# 车辆端进程隔离

正式 `rd-vehicle` 入口启动父进程，以及独立 control、media 子进程；仍属于同一个车辆应用和 wheel。`--components control` 或 `--components media` 只启动所选子进程，支持由部署系统分别管理。controller=none 不启动 control。

## 职责与通信

- 父进程加载并校验配置，以 multiprocessing 的 spawn 模式传入不可变配置对象；管理退出、故障和信号，不创建底盘或相机。
- control 负责认证 QUIC、租约、独立执行线程与超时监护线程、ROS2/串口/模拟适配器；不导入 OpenCV、aiortc、AV、Qt。
- media 负责相机线程、共享采集源、认证信令、WebRTC 编码和连接清理。真实图像留在媒体进程，不经过通用进程队列。

父子之间各有私有 socketpair，只传固定长度的本机单调时钟心跳。写端不阻塞事件循环，最多保留一条未完成心跳；接收端限制每轮读取量，并验证时间不回退、不在未来。积压心跳保留原始时间，不能在父进程恢复时被刷新成新样本。心跳表示事件循环能调度，不代表摄像头、网络、视频解码或底盘已就绪。

只在启动时复制小型配置；IPC 不携带图像、运动命令或令牌明文。令牌仍由子进程读取既有环境变量引用。配置 `logging.directory` 时，子进程分别写入 control、media 子目录，避免多个进程轮转同一日志文件。

## 故障与退出

父进程分别监护 control 和 media。某一组件启动失败、意外退出、关闭健康通道或心跳超时时，父进程单独清理并重试该组件。媒体卡死期间，控制进程继续收发 QUIC 和监护底盘。控制进程故障期间，媒体进程继续向已连接的观看者推流。

正常 SIGINT/SIGTERM 或取消父任务会停止重试，并关闭所有仍在运行的子进程。父进程关闭各健康通道的发送方向，子进程收到 EOF 后各自清理。父进程意外退出也会触发 EOF。control 在有序清理时撤销租约，关闭控制监护并请求零速。重复取消父任务时，父进程仍等待清理完成。启动前已有停止请求时直接退出。

Windows 的 Ctrl+C 是停止请求，不是暂停。正式入口由父进程统一处理 SIGINT，子进程忽略控制台广播的 SIGINT，由父进程关闭健康通道通知停止，避免把正常中断误判为子进程故障。信号处理器只标记请求，由事件循环每 50ms 检查并设置停止事件；处理器保持到清理结束，重复 Ctrl+C 不跳过清理，结束后恢复原处理器。该间隔不是阻塞情况下的退出时限；原有超时强制结束和失败报告仍保留。

等待正常关闭超过期限，父进程先 terminate，继续超时再 kill，并回收退出状态。全局退出时，任何强制结束或回收失败都会导致失败退出。强制结束 control 时日志标明物理停车未经确认。底盘是否实际停车仍需真实制动、停车反馈及硬件 watchdog 验证。

父进程在两次启动尝试之间至少等待 `max(0.25 秒, network.reconnect_delay_s)`。重启前必须回收同一组件的旧进程。设备进程若一直无法回收，该组件暂停启动，另一个组件继续运行。重启的 control 需要重新认证并取得新租约，驾驶端仍处于控制模式时会自动重试，收到新的中立输入及执行确认后才能发送运动指令。

## 配置和使用

```yaml
processes:
  heartbeat_interval_s: 0.1
  heartbeat_timeout_s: 1.0
  startup_timeout_s: 15.0
  shutdown_timeout_s: 5.0
  kill_timeout_s: 1.0
```

首次心跳受 `startup_timeout_s` 限定。媒体子进程配置好媒体服务后开始心跳，此后的心跳间隔按 `heartbeat_timeout_s` 检查。空闲媒体进程保持心跳，首位授权观看者加入后才打开摄像头，最后一位离开后释放设备。车辆控制通过独立的 `control.command_timeout_ms` 检测指令超时。这些默认值来自本机模拟，目标硬件仍需验证。回收单个子进程最多依次等待 `shutdown_timeout_s`、两次 `kill_timeout_s`，加上有限轮询开销。

```bash
rd-vehicle --config /path/vehicle.mock.yaml --check-config
rd-vehicle --config /path/vehicle.mock.yaml
rd-vehicle --config /path/vehicle.mock.yaml --components control
rd-vehicle --config /path/vehicle.mock.yaml --components media
```

先提供匹配的 CA、令牌和认证服务端；无硬件测试使用 mock 与 synthetic。生产目标仍为用户确认的 ROS2 / Python 3.10.12；真实 ROS 发行版及系统兼容性须在目标机验收。

## 验证边界

`tests/test_vehicle_processes.py` 使用真实 spawn 和私有 socketpair，检查控制在尚无观看者时获得执行确认，以及视频打开和关闭后继续使用同一租约。故障测试覆盖媒体事件循环卡死、任一子进程崩溃、父通道 EOF 和启动失败重试，验证只替换故障组件。全局退出测试检查有序回收、重复取消和模拟底盘零速请求。

B25 使用当时的正式组件入口和监督器，在四路合成媒体负载下分别测量了共进程与分进程的资源、尾延迟和重连成本。目标机器和实机性能仍待验收。驾驶端使用 PyQt 工作线程。[历史性能记录](video-performance.zh-CN.md#b25-正式车辆进程拓扑的测量)

B25 还将接口包导出改为按需导入，避免加载底盘接口时连带加载相机接口和 NumPy；使用 mock 的正式控制子进程已验证未导入 NumPy、相机接口、OpenCV、aiortc、AV 或 Qt。ROS2 自身的依赖仍以目标环境为准。

安装后可直接使用安装环境的 Python 运行仓库中的无硬件脚本：

```bash
/path/to/vehicle-venv/bin/python /path/to/repository/scripts/smoke_vehicle_processes.py
```

脚本自行生成临时 CA 和随机令牌，保留本机端口以免连接其他服务；只启用 mock/synthetic，测试正式命令的双模块/两个单模块心跳、SIGTERM、退出码和子进程回收，异常时清理其创建的进程组。此脚本使用 POSIX 信号；B15 在 Linux/Python 3.12.16 的干净 wheel 环境通过，结果见 [安装验证报告](validation/vehicle-processes-b15.json)。全量回归 225 项通过，进程专项 7 项通过。

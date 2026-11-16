# 车辆端进程隔离

正式 `rd-vehicle` 入口启动父进程，以及独立 control、media 子进程；仍属于同一个车辆应用和 wheel。`--components control` 或 `--components media` 只启动所选子进程，支持由部署系统分别管理。controller=none 不启动 control。

## 职责与通信

- 父进程加载并校验配置，以 multiprocessing 的 spawn 模式传入不可变配置对象；管理退出、故障和信号，不创建底盘或相机。
- control 负责认证 QUIC、租约、独立执行线程与超时监护线程、ROS2/串口/模拟适配器；不导入 OpenCV、aiortc、AV、Qt。
- media 负责相机线程、共享采集源、认证信令、WebRTC 编码和连接清理。真实图像留在媒体进程，不经过通用进程队列。

父子之间各有私有 socketpair，只传固定长度的本机单调时钟心跳。写端不阻塞事件循环，最多保留一条未完成心跳；接收端限制每轮读取量，并验证时间不回退、不在未来。积压心跳保留原始时间，不能在父进程恢复时被刷新成新样本。心跳表示事件循环能调度，不代表摄像头、网络、视频解码或底盘已就绪。

只在启动时复制小型配置；IPC 不携带图像、运动命令或令牌明文。令牌仍由子进程读取既有环境变量引用。配置 `logging.directory` 时，子进程分别写入 control、media 子目录，避免多个进程轮转同一日志文件。

## 故障与退出

任一必需子进程意外返回、退出、关闭健康通道或心跳超时，父进程把整个组视为失败，关闭各健康通道的发送方向，向子进程送达 EOF。control 由自己的事件循环退出运行时、撤销租约、调用控制监护关闭和零速请求；不需要媒体响应。子进程也会在父连接丢失时主动请求自身关闭。模拟环境验证了媒体卡死时控制循环仍能调度和清理。

正常 SIGINT/SIGTERM 走相同的清理流程。重复取消父任务不能跳过清理；部分子进程启动失败会回滚已经启动的子进程。启动前已有停止请求时不创建子进程。

等待正常关闭超过期限，父进程先 terminate，继续超时再 kill，并回收退出状态。发生强制结束时返回失败，明确记录物理停车未确认。进程退出码为 0 也不证明底盘已经停车；当前适配器只请求零速，真实制动/停车反馈/watchdog 接口仍待接入。处于操作系统不可中断状态的设备进程可能无法及时退出，需要部署守护和真实硬件保护；软件不伪称已清理。

程序不在内部循环重启子进程。故障退出后由部署系统决定整体重启，新的客户端仍需重新认证、新授权代次和新鲜中立输入；重启不是恢复原控制权。

## 配置和使用

```yaml
processes:
  heartbeat_interval_s: 0.1
  heartbeat_timeout_s: 1.0
  startup_timeout_s: 15.0
  shutdown_timeout_s: 5.0
  kill_timeout_s: 1.0
```

这些是本机模拟默认值，尚未作为目标硬件的性能验收值。首次心跳受 startup_timeout_s 限定，此后的心跳间隔立即按 heartbeat_timeout_s 检查，不继续享受启动宽限。退出最多依次等待 shutdown_timeout_s、两次 kill_timeout_s，加上有限轮询开销；实际停车不能以这一软件时间推断。独立控制超时仍使用 `control.command_timeout_ms`，不依赖父进程心跳。

```bash
rd-vehicle --config /path/vehicle.mock.yaml --check-config
rd-vehicle --config /path/vehicle.mock.yaml
rd-vehicle --config /path/vehicle.mock.yaml --components control
rd-vehicle --config /path/vehicle.mock.yaml --components media
```

先提供匹配的 CA、令牌和认证服务端；无硬件测试使用 mock 与 synthetic。生产目标仍为用户确认的 ROS2 / Python 3.12；真实 ROS 发行版及系统兼容性须在目标机验收。

## 验证边界

`tests/test_vehicle_processes.py` 使用真实 spawn 和私有 socketpair，覆盖独立 PID、真实回环 QUIC 执行 ACK、WSS/WebRTC 视频、控制进程依赖隔离、有序退出、媒体事件循环卡死、子进程崩溃、父通道丢失、重复取消、部分启动失败。模拟卡死测试检查控制循环继续推进、模拟零速已请求且物理确认仍为 false。

心跳本身不代表相机健康；B16 已增加独立主采集超时检查，故障先关闭父健康通道再清理设备，并在驾驶端按主画面呈现证据限制控制，见 [画面与控制](video-safety.zh-CN.md)。B25 已使用正式组件入口/监督器，完成四路合成媒体负载下共进程与分进程各三轮的资源、尾延迟及重连测量，见 [性能记录](video-performance.zh-CN.md#b25-正式车辆进程拓扑的测量)。分进程的 PSS/CPU/启动成本均有记录，没有据此宣称所有指标更快；目标机器和实机性能仍待验收。驾驶端仍使用 PyQt 工作线程，车端的本次隔离不等同于驾驶端 UI/控制已分进程。

B25 还将接口包导出改为按需导入，避免加载底盘接口时连带加载相机接口和 NumPy；使用 mock 的正式控制子进程已验证未导入 NumPy、相机接口、OpenCV、aiortc、AV 或 Qt。ROS2 自身的依赖仍以目标环境为准。

安装后可直接使用安装环境的 Python 运行仓库中的无硬件脚本：

```bash
/path/to/vehicle-venv/bin/python /path/to/repository/scripts/smoke_vehicle_processes.py
```

脚本自行生成临时 CA 和随机令牌，保留本机端口以免连接其他服务；只启用 mock/synthetic，测试正式命令的双模块/两个单模块心跳、SIGTERM、退出码和子进程回收，异常时清理其创建的进程组。此脚本使用 POSIX 信号；B15 在 Linux/Python 3.12.16 的干净 wheel 环境通过，结果见 [安装验证报告](validation/vehicle-processes-b15.json)。全量回归 225 项通过，进程专项 7 项通过。

# 车辆端

正式包名为 `rd-vehicle`，命名空间为 `rd_vehicle`，入口为 `rd-vehicle`。用户确认目标环境为 ROS2、Python 3.10.12；`rclpy` 由匹配 Python 3.10.12 的 ROS 环境提供。

```bash
uv run --package rd-vehicle rd-vehicle --config configs/vehicle.mock.yaml --dump-config
uv run --package rd-vehicle rd-vehicle --config configs/vehicle.mock.yaml --check-config
uv run --package rd-vehicle rd-vehicle --config configs/vehicle.mock.yaml
# 仅运行模拟控制，不打开摄像头
uv run --package rd-vehicle rd-vehicle --config configs/vehicle.mock.yaml --components control
```

[无硬件示例](../../configs/vehicle.mock.yaml) 使用模拟控制器和合成视频；启动需要提供 CA 文件与 `RD_VEHICLE_TOKEN`。[ROS2 模板](../../configs/vehicle.yaml) 包含必须替换的稳定相机路径，修改前不能直接用于实车。`--check-config` 校验所选模块的凭据、TLS 与控制器依赖导入，不打开设备；不能替代相机、ROS 及底盘运行验收。

默认配置的控制器为 none，此时不注册控制会话，也不产生假执行确认。可选 mock、ros2、serial。`components` 选择 control/media；正式 CLI 使用父进程管理独立 control/media 子进程；单模块模式只启动所选子进程。任一组件退出或心跳超时，父进程请求整体停止并等待清理，超时后强制结束且报告失败。详见[进程隔离](../../docs/process-isolation.zh-CN.md)。

运动限值使用 m/s、rad/s，按用户要求暂保留 5.0 和 2.0，并实际传入适配器；不是已验收的实车安全上限。`control.command_timeout_ms: 300` 为模拟默认值。目标执行与超时监护各有独立线程；失效锁存后恢复需要新授权代次及新鲜中立输入。已阻塞的设备调用不能通过取消线程可靠终止。

`request_safe_stop(reason)` 及独立停车回调为真实接口保留接入点。现有适配器只请求零速目标，StopResult.confirmed 为 False，不能解释为底盘已经停车。真实制动与底盘 watchdog 后续接入；串口编码及单位需硬件方确认。

相机按 front/back/left/right 命名，可配置合成源、设备索引或稳定路径，各路尺寸和 FPS 独立；实际设备协商参数会记录，不能把请求值当作实测值。旧包内配置已删除，规则见[配置说明](../../docs/configuration.zh-CN.md)，安装见[根 README](../../README.md)，状态见[实施记录](../../docs/refactoring-progress.zh-CN.md)。

摄像头不足时，可设置 `camera_fallback_source: front`：有显式 source 的视角照常采集，缺少 source 或未列出的视角复用 front，补齐四路测试画面。源设备只打开一次，复制帧保留原始采集时间；真实设备故障仍报错。[单摄像头示例](../../configs/vehicle.single-camera.yaml) 使用模拟底盘，需按实际设备修改前摄像头路径。

媒体发布携带配置中的 session 并接受服务端授权校验；主相机超时会在设备清理前报告故障，使父进程先停止控制。`video` 配置与驾驶端策略见 [画面与控制](../../docs/video-safety.zh-CN.md)。

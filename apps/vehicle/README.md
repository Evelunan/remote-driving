# 车辆端

正式包名为 `rd-vehicle`，命名空间为 `rd_vehicle`，入口为 `rd-vehicle`。用户确认目标环境为 ROS2、Python 3.12。

```bash
uv run --package rd-vehicle rd-vehicle --help
uv run --package rd-vehicle rd-vehicle --session 1234 --room 1234
```

实际启动仍需要相机和连接配置；暂存示例在 `src/rd_vehicle/config`，统一配置、稳定相机路径、控制/媒体进程隔离仍在实施。默认控制器为 none，不连接底盘。ROS 的 rclpy 由匹配 Python 3.12 的 ROS 环境提供，不能假设任意系统 Python 都兼容。

运动限值使用 m/s、rad/s，按用户要求暂保留 5.0 和 2.0，由配置传入 ROS 适配器。当前 `control_timeout_s: 0.3` 仅为模拟默认值，实车须独立验收。

控制目标在独立设备线程串行执行，监护线程检测有效输入超时及设备执行阻塞，失效后锁存并清除未执行目标。恢复需要新控制会话及中立输入；可靠授权恢复将在新版协议中实现。

`request_safe_stop(reason)` 及独立停车回调为真实安全接口预留接入点。现有适配器只请求零速目标，StopResult.confirmed 为 False，不能解释为底盘已经停车。串口编码单位、实际制动与底盘 watchdog 均需硬件方确认。已阻塞的设备调用不能通过取消线程可靠终止。

构建与独立安装见[根 README](../../README.md)，验证和未完成项见[实施记录](../../docs/refactoring-progress.zh-CN.md)。

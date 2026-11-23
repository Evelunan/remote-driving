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

## ROS2 控制器与底盘启动

当前现场使用 ROS2 Humble、Python 3.10.12 和底盘工作区 `/home/akun/workspace/Car_jetson/real_ws`。下面的命令由操作者在各终端手动执行；其他机器需替换安装路径，并确保各终端的 `ROS_DOMAIN_ID` 与底盘一致。

控制链路为：

```text
rd-vehicle → /cmd_vel（geometry_msgs/msg/Twist）
           → cmdvel_2_ecu → /ecu（yunle_msgs/msg/Ecu）
           → chassis_driver → 底盘
```

`cmdvel_2_ecu` 和 `chassis_driver` 来自上述底盘工作区，不随 `rd-vehicle` 自动启动。若整车驱动已经启动相关节点，不要重复启动。

1. **终端一：启动转换节点，保持运行。**

   ```bash
   source /opt/ros/humble/setup.bash
   source /home/akun/workspace/Car_jetson/real_ws/install/setup.bash

   ros2 run cmdvel_2_ecu cmdvel_2_ecu --ros-args \
     --params-file /home/akun/workspace/Car_jetson/real_ws/install/cmdvel_2_ecu/share/cmdvel_2_ecu/config/config.yaml
   ```

   正常会输出类似：

   ```text
   /cmd_vel → /ecu 节点已启动
   ```

2. **终端二：查询转换节点和话题。**

   ```bash
   source /opt/ros/humble/setup.bash
   source /home/akun/workspace/Car_jetson/real_ws/install/setup.bash

   ros2 node list --no-daemon
   ros2 topic info /cmd_vel --verbose --no-daemon
   ```

   预期看到节点 `/cmdvel_to_ecu`。仅启动一个转换节点、尚未启动车辆端或其他发布者时，话题信息应包含：

   ```text
   Type: geometry_msgs/msg/Twist
   Publisher count: 0
   Subscription count: 1
   ```

   此时发布者为 `0` 正常，表示转换器已等待输入。`--check-config` 不创建 ROS 节点，不能代替这里的启动步骤。

3. **终端三：需要连接真实底盘时，启动底盘驱动，保持运行。**

   这一步会打开底盘通信；如果整车驱动已经运行，不要重复启动。启动前核对底盘工作区中 launch 文件的车型、网卡及 CAN 网络地址。

   ```bash
   source /opt/ros/humble/setup.bash
   source /home/akun/workspace/Car_jetson/real_ws/install/setup.bash

   ros2 launch chassis_driver chassis_driver.launch.py
   ```

4. **终端四：启用车辆端的 ROS2 控制器。**

   先通过环境提供有效的 `RD_VEHICLE_TOKEN`，并核对配置的服务地址、CA、session、room、相机路径和运动限值。以下命令使用现场局域网配置，同时启动 ROS2 控制与四路视频。[车辆端局域网配置](../../configs/vehicle.lan.yaml)

   ```bash
   source /opt/ros/humble/setup.bash
   source /home/akun/workspace/Car_jetson/real_ws/install/setup.bash
   cd /home/akun/workspace/my-driving

   .venv/bin/rd-vehicle --config configs/vehicle.lan.yaml
   ```

   仅排查控制接口时，可在同一命令中增加 `--components control`，不打开摄像头。已有车辆端实例时，先结束旧实例再启动。返回终端二查询 `/cmd_vel`，单实例下预期新增发布节点 `cmd_vel_publisher`，发布者和转换器订阅者各为 `1`。

车辆端的 `Command received and accepted` 日志表示指令已被控制监护接收；ROS 发布成功也不代表底盘已执行。当前现场的 `cmdvel_2_ecu` 只有在 `/vehicle_status` 反馈新鲜且 `is_autodrive=true` 时才下发 `/ecu`，反馈超时阈值由底盘配置 `input_timeout_sec` 决定，当前为 `0.20 s`。只有转向输入、`throttle=0` 时，车辆端请求的线速度仍为零。

如果收到指令却没有底盘响应，在终端二查询以下信息；`echo` 命令只订阅消息，5 秒未收到消息会超时退出：

```bash
timeout 5s ros2 topic echo /cmd_vel geometry_msgs/msg/Twist --once --no-daemon
timeout 5s ros2 topic echo /vehicle_status yunle_msgs/msg/VehicleStatus --once --no-daemon
ros2 topic info /ecu --verbose --no-daemon
```

依次确认 `/cmd_vel` 有实际输入、`/vehicle_status` 持续更新且自动驾驶状态允许输出，以及 `/ecu` 有底盘驱动订阅。节点数量和话题可见只说明通信端点被发现，不能作为实际停车或运动的确认。

## 摄像头启动排查

使用 `configs/vehicle.yaml` 启动时，媒体进程同时写终端和 `logs/vehicle/media/app.log`（自动轮转）。在仓库根目录用 PowerShell 执行 `Get-Content .\logs\vehicle\media\app.log -Tail 80 -Wait` 可跟踪各路启动。

按视角名对照：`opening` 给出设备索引、后端和请求模式；`opened` 给出驱动报告的协商参数；`receiving frames` 才表示读到画面。`isOpened=False` 表示驱动未打开该设备，`open timed out` 表示打开超时，`returned no frame` 表示设备已打开但没有读到画面。任一设备打开失败仍会触发整体清理；已成功的设备随后关闭是预期行为。

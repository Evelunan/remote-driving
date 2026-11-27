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

默认配置的控制器为 none，此时不注册控制会话，也不产生假执行确认。可选 mock、ros2、serial。`components` 选择 control/media；正式 CLI 使用父进程管理独立 control/media 子进程；单模块模式只启动所选子进程。单个组件退出或心跳超时时，父进程隔离并回收该组件，再尝试独立重启。详见[进程隔离](../../docs/process-isolation.zh-CN.md)。

运动限值使用 m/s、rad/s，按用户要求暂保留 5.0 和 2.0，并实际传入适配器；不是已验收的实车安全上限。`control.command_timeout_ms: 300` 为模拟默认值。目标执行与超时监护各有独立线程；失效锁存后恢复需要新的配对代次和有效的新输入。已阻塞的设备调用不能通过取消线程可靠终止。

`request_safe_stop(reason)` 及独立停车回调为真实接口保留接入点。现有适配器只请求零速目标，StopResult.confirmed 为 False，不能解释为底盘已经停车。真实制动与底盘 watchdog 后续接入；串口编码及单位需硬件方确认。

相机按 front/back/left/right 命名，可配置合成源、设备索引或稳定路径，各路尺寸和 FPS 独立；实际设备协商参数会记录，不能把请求值当作实测值。旧包内配置已删除，规则见[配置说明](../../docs/configuration.zh-CN.md)，安装见[根 README](../../README.md)，状态见[实施记录](../../docs/refactoring-progress.zh-CN.md)。

摄像头不足时，可设置 `camera_fallback_source: front`：有显式 source 的视角照常采集，缺少 source 或未列出的视角复用 front，补齐四路测试画面。源设备只打开一次，复制帧保留原始采集时间；真实设备故障仍报错。[单摄像头示例](../../configs/vehicle.single-camera.yaml) 使用模拟底盘，需按实际设备修改前摄像头路径。

媒体进程启动后连接认证信令房间，使用配置中的 session 表明车辆身份。首位有权限的驾驶端或观看端加入视频房间时打开摄像头。多个观看端共享同一组采集，最后一位离开或车端信令断开时停止采集并释放设备。主相机超时会清除关联画面并单独重试，当前控制通道继续工作。`video` 配置与驾驶端策略见 [画面与控制](../../docs/video-safety.zh-CN.md)。

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

   先通过环境提供有效的 `RD_VEHICLE_TOKEN`，并核对配置的服务地址、CA、session、room、相机路径和运动限值。以下命令使用现场局域网配置，同时启动 ROS2 控制组件和媒体服务。驾驶端选择「连接视频」后，车辆才开始采集并发送四路视频。[车辆端局域网配置](../../configs/vehicle.lan.yaml)

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

## ROS2 底盘状态反馈

车辆端订阅 `/vehicle_status`（`yunle_msgs/msg/VehicleStatus`），通过 QUIC 中继响应同一会话中已授权驾驶端的底盘状态请求。反馈在驾驶端注册后可用，可以在开始控制之前和停止之后接收。`configs/vehicle.lan.yaml` 和 `configs/vehicle.yaml` 已启用反馈。

```yaml
feedback:
  enabled: true
  topic: /vehicle_status
  wheel_speed_unit: m/s
  send_hz: 10
  stale_timeout_ms: 300
```

回传车速、转向角、左右轮速、档位、自动驾驶使能、驾驶模式和刹车状态。用户于 2026-11-24 确认车速 `cur_speed` 单位为 m/s，按原值传输与显示；转向角使用 °。左右轮速源单位单独配置，默认 m/s 原值；若源为 km/h，则除以 3.6 统一成 m/s。该配置不改变 `/cmd_vel` 的线速度 m/s 和角速度 rad/s。

驾驶端将主要信息放在“车辆反馈”的“底盘实际状态”，轮速、模式和已确认指令值放在同组“诊断详情”。反馈挡位显示 D 前进（1）、N／停止（2）、R 倒退（3），未知值保留编码；驾驶模式继续显示原始编码，缺失字段显示 `--`。刹车状态和 N／停止不是车辆已停稳的物理确认。mock 控制器不会用目标指令伪造这些反馈。“驾驶端输入”独立显示本地采样，与车辆反馈相邻；完整说明见[驾驶端](../driving/README.md)。

`drive_mode` 是底盘报告的当前控制模式，描述自动驾驶通道、遥控/PAD 或方向盘控制等控制来源；`shift_level` 则表示前进、停止或后退等档位，两者分别显示。`is_autodrive` 是独立的自动驾驶按钮状态，当前 `cmdvel_2_ecu` 使用它决定是否允许上位机指令下发。在本项目中，远程驾驶的 ROS 指令也使用上位机控制通道，不代表正在执行自主导航。

模式编码尚有冲突：外部底盘 README 写 `1=遥控、2=Reservation、3=自动驾驶`，`chassis_driver/chassis_driver/include/Msg.h` 的反馈位注释写 `0=不影响、1=自动驾驶、2=PAD、3=方向盘`，解析实现仅提取原始 3 bit 数值。未获得匹配车型的协议或现场核对前，驾驶端保留原始值，不据此切换控制策略。

以下步骤由操作者执行，本次开发未启动真实 ROS 节点或实车程序：

1. 配套升级服务端、车辆端、驾驶端及共享包，注册修订为 `direct-control/5`。`direct-control/4` 或更旧修订会收到 `protocol_upgrade_required`。回退时恢复配套旧版本和旧配置。
2. 按上文准备 ROS 和底盘环境；已有驱动时查询即可，不重复启动。检查消息定义、发布者和一次反馈：

   ```bash
   source /opt/ros/humble/setup.bash
   source /home/akun/workspace/Car_jetson/real_ws/install/setup.bash
   ros2 interface show yunle_msgs/msg/VehicleStatus
   ros2 topic info /vehicle_status --verbose --no-daemon
   timeout 5s ros2 topic echo /vehicle_status yunle_msgs/msg/VehicleStatus --once --no-daemon
   ```

3. 依照上文启动车辆端。日志应出现 `ROS chassis feedback subscribed`。依赖缺失时先确认已加载正确工作区，不在本项目任务中修改或构建底盘包。
4. 驾驶端注册目标车辆会话后检查底盘状态是否实时更新。分别在开始控制前和停止控制后确认反馈仍然更新。该验收可以在车辆静止时进行。
5. 对照同一次现场查询和界面，检查车速、转向角、反馈编码、使能、刹车和诊断轮速。分别记录外设“所选挡位”和底盘“反馈挡位”。核对无效字段显示未知，以及底盘状态过期时界面清空旧值。

车辆端接收 ROS 消息时，用本机系统时间和 `header.stamp` 计算初始采样年龄，随后用本机单调时钟计时。源时间为零、已过期、在未来或重复时跳过该样本。车端默认只使用 300 毫秒以内的样本，按 `send_hz` 限制响应频率，每个有效新样本最多发送一次。驾驶端用自己的单调时钟测量往返耗时，再结合车辆报告的年龄判断反馈是否过期。年龄反映整条 ROS 消息，底盘驱动内部缓存的轮速字段没有单独的采样时间。[反馈协议](../../docs/control-protocol-v2.zh-CN.md#已实现的中继业务)

软件回归使用模拟 ROS、Qt offscreen 和仅绑定回环动态端口的 QUIC 测试，不连接实车：

```bash
.venv/bin/python scripts/test_vehicle_feedback.py
```

脚本使用现有 `.venv`，关闭 pytest 插件自动加载，将所有临时文件、缓存、日志和测试报告写入项目内 `.rd-runtime/feedback-tests/run-*`，不安装依赖、不修改系统网络配置。需要允许本机回环 socket。开发进度和提交记录见[开发计划](../../docs/vehicle-status-feedback-plan.zh-CN.md)。

## 摄像头启动排查

使用 `configs/vehicle.yaml` 启动时，媒体进程同时写终端和 `logs/vehicle/media/app.log`（自动轮转）。先从驾驶端连接视频，再在仓库根目录用 PowerShell 执行 `Get-Content .\logs\vehicle\media\app.log -Tail 80 -Wait` 跟踪各路启动。

按视角名对照 `opening` 中的设备索引、后端和请求模式，再确认 `opened` 中的协商参数。看到 `receiving frames` 后才有采集结果。`isOpened=False` 表示驱动未打开该设备，`open timed out` 表示打开超时，`returned no frame` 表示设备已打开但没有读到画面。故障摄像头会单独重试，正常的视角可继续传输。

# 远程驾驶与 Nav2 共用转换程序的双配置方案

记录日期：2026-11-28。状态：供车辆端与小车负责人评审，尚未实施。

本文只讨论“同一份 `cmdvel_2_ecu` 程序，通过互斥配置分别服务远程驾驶和 Nav2”的过渡方案。它不复制转换器源码，也不实现远程、Nav2、充电对接同时在线时的完整控制权仲裁。

相关但相互独立的方案：

- [保持共享 `/cmd_vel` 的最小远程独占方案](remote-driving-exclusive-transition-plan.zh-CN.md)
- [多控制源最终仲裁方案](vehicle-command-arbitration-plan.zh-CN.md)

本次只整理文档，没有修改 `/home/akun/workspace/Car_jetson`、ROS2 包、车辆配置或实车程序。

## 1. 方案结论

使用同一份 `cmdvel_2_ecu` 源码，根据运行模式选择不同参数：

| 模式 | 输入话题 | 定位门控 | 转换器实例数 |
| --- | --- | --- | ---: |
| Nav2 | `/cmd_vel` | `require_localization=true` | 1 |
| 远程驾驶 | `/cmd_vel_remote` | `require_localization=false` | 1 |

两种模式不能同时启动两个转换器。无论选择哪种模式，系统中都只能存在：

```text
1 个 cmdvel_2_ecu
1 个 /ecu 发布者
1 个 chassis_driver
```

方案核心是“同一程序、不同参数、互斥启动”，不是“同时运行两个配置实例”。

## 2. 目标链路

### 2.1 Nav2 模式

```text
Nav2 controller / behavior
        ↓ /cmd_vel_nav
velocity_smoother
        ↓ /cmd_vel_raw
collision_monitor
        ↓ /cmd_vel
cmdvel_2_ecu
  cmd_vel_topic=/cmd_vel
  require_localization=true
        ↓ /ecu
chassis_driver
        ↓ CAN
真实底盘
```

### 2.2 远程驾驶模式

```text
驾驶端
  ↓ 认证网络控制
rd-vehicle
  ↓ /cmd_vel_remote
cmdvel_2_ecu
  cmd_vel_topic=/cmd_vel_remote
  require_localization=false
  ↓ /ecu
chassis_driver
  ↓ CAN
真实底盘
```

远程模式下，Nav2 即使误向 `/cmd_vel` 发布，远程配置的转换器也不会订阅该话题。但仍应停止 Nav2，避免操作者误判系统状态，并防止整车 launch 额外启动第二个转换器。

## 3. 为什么不复制转换器源码

`cmdvel_2_ecu` 不只是转发话题，还承担：

- m/s 到 ECU km/h 的单位转换；
- 线速度到 D/N/R 挡位的转换；
- 自行车模型转向角计算；
- 低速补偿、停车死区和最终限幅；
- 制动字段和双阿克曼标志生成；
- `/vehicle_status` 新鲜度检查；
- `is_autodrive` 检查；
- 非法输入和指令超时处理。

复制一个 `remote_cmdvel_2_ecu` 包会产生两套转换和安全逻辑。以后调整速度、转向、制动或底盘协议时，两套实现容易不一致。

本方案只保留一套源码和一套基础标定，通过参数表达运行模式差异。

## 4. 需要修改的组件

### 4.1 `cmdvel_2_ecu`：增加定位门控参数

外部工作区源码：

```text
/home/akun/workspace/Car_jetson/real_ws/src/driver/car_control/
cmdvel_2_ecu/cmdvel_2_ecu/convert.py
```

新增参数：

```yaml
require_localization: true
```

参数行为：

| 参数值 | 行为 |
| --- | --- |
| `true` | 保持当前逻辑，定位必须为新鲜的 `ACTIVE` 状态 |
| `false` | 不以 LIO 定位状态阻止远程指令，其他公共保护保持不变 |

默认值必须为 `true`，以保证旧 Nav2 启动方式在没有显式配置时仍然失效关闭。

实现时需要同时处理：

1. 参数声明和读取；
2. 初始 `localization_stop_latched` 状态；
3. `/lio_sam/map_odom_status` 订阅或回调；
4. `/cmd_vel` 回调中的定位锁检查；
5. watchdog 中的定位状态超时和重新锁止；
6. 定位恢复及连续静止样本解除锁定流程。

不能只跳过 `/cmd_vel` 回调中的一个判断，否则 watchdog 仍会因为没有定位状态重新锁止。

关闭定位门控后仍须保留：

- `/vehicle_status` 新鲜；
- `is_autodrive=true`；
- 指令新鲜度和 `input_timeout_sec`；
- NaN/Inf 拒绝；
- 速度和转角限幅；
- 速度、挡位、转向和制动转换；
- 停车请求与物理停车确认分离。

### 4.2 `rd-vehicle`：输出话题配置化

当前车辆端发布器固定发布：

```text
/cmd_vel
```

源码位置：

```text
/home/akun/workspace/my-driving/apps/vehicle/src/rd_vehicle/
infrastructure/controller/ros2_cmd_vel_controller.py
```

需要将话题名称改为车辆配置项。远程实车配置使用：

```yaml
ros2:
  cmd_vel_topic: /cmd_vel_remote
```

具体配置字段名称在实现时按照现有 `VehicleSettings` 结构确定；要求是：

- 配置经过现有配置加载和校验机制；
- 必须使用绝对 ROS 话题名；
- 空字符串或非法名称在启动前拒绝；
- `--check-config` 验证配置，但不创建 ROS 节点；
- 创建 `RCLpyCmdVelController` 时显式注入话题；
- 日志输出实际生效的话题名称。

此修改只涉及车辆端，不改变驾驶端、中继协议或驾驶输入映射。

### 4.3 两侧测试

`cmdvel_2_ecu` 至少验证：

1. `require_localization` 默认值为 `true`；
2. 门控开启且定位未 `ACTIVE` 时禁止运动输出；
3. 门控关闭、底盘反馈新鲜且 `is_autodrive=true` 时允许合法输入进入转换；
4. 门控关闭时定位缺失、过期或退化不会重新锁止；
5. 底盘反馈过期、自动驾驶按钮关闭、指令超时和非法值仍然失效关闭；
6. 现有 Nav2 定位退化和恢复测试继续通过。

`my-driving` 至少验证：

1. ROS 输出话题从配置进入控制器；
2. `vehicle.lan.yaml` 使用 `/cmd_vel_remote`；
3. 非法或非绝对话题在配置校验时拒绝；
4. 发布、停车请求和关闭时都使用同一个配置话题；
5. mock、serial 和其他控制器不受新增 ROS 话题配置影响；
6. `--check-config` 不打开 ROS 节点或真实设备。

## 5. 配置组织

### 5.1 基础标定只保留一份

普通 `config.yaml` 继续保存轴距、速度、转角、低速补偿和超时等基础标定：

```yaml
/cmdvel_to_ecu:
  ros__parameters:
    L: 0.5
    navigation_max_speed: 0.5555555555555556
    MAX_STEER_DEG: 26.56505117707799
    input_timeout_sec: 0.20
    cmd_vel_topic: /cmd_vel
    require_localization: true
```

远程模式不要复制整套标定文件。启动时加载同一基础配置，只覆盖：

```text
cmd_vel_topic=/cmd_vel_remote
require_localization=false
```

这样速度和转向参数只有一个维护来源。

### 5.2 Nav2 启动参数

Nav2 模式直接使用基础配置：

```bash
ros2 run cmdvel_2_ecu cmdvel_2_ecu --ros-args \
  --params-file /home/akun/workspace/Car_jetson/real_ws/install/cmdvel_2_ecu/share/cmdvel_2_ecu/config/config.yaml
```

实际参数：

```text
cmd_vel_topic=/cmd_vel
require_localization=true
```

### 5.3 远程启动参数

完成代码修改并重新构建后，远程模式使用：

```bash
ros2 run cmdvel_2_ecu cmdvel_2_ecu --ros-args \
  --params-file /home/akun/workspace/Car_jetson/real_ws/install/cmdvel_2_ecu/share/cmdvel_2_ecu/config/config.yaml \
  -p cmd_vel_topic:=/cmd_vel_remote \
  -p require_localization:=false
```

实际参数：

```text
cmd_vel_topic=/cmd_vel_remote
require_localization=false
```

在增加并声明 `require_localization` 之前，上述参数不能产生预期效果。

## 6. 推荐启动入口

不建议长期依赖操作者手工输入多个 `-p` 参数。建议由小车工作区负责人提供两个明确、互斥的 launch 入口，或一个带模式参数的统一入口。

### 6.1 两个明确入口

```text
navigation_control.launch.py
remote_control.launch.py
```

二者都复用同一基础标定，但分别注入：

```text
navigation_control:
  cmd_vel_topic=/cmd_vel
  require_localization=true

remote_control:
  cmd_vel_topic=/cmd_vel_remote
  require_localization=false
```

### 6.2 单一模式入口

也可以提供：

```bash
ros2 launch vehicle_control vehicle_control.launch.py control_mode:=navigation
```

或者：

```bash
ros2 launch vehicle_control vehicle_control.launch.py control_mode:=remote
```

launch 必须根据模式只创建一个转换器。无论采用哪种入口，都不能同时创建 Nav2 配置实例和远程配置实例。

## 7. 运行规则

### 7.1 远程模式

```text
启动：1 个 chassis_driver
启动：1 个远程配置 cmdvel_2_ecu
启动：rd-vehicle control 组件，发布 /cmd_vel_remote
停止：Nav2 控制链
停止：对接 command_mux 和对接控制器
停止：网页 /cmd_vel 调试
```

### 7.2 Nav2 模式

```text
启动：1 个 chassis_driver
启动：1 个 Nav2 配置 cmdvel_2_ecu
启动：Nav2
停止：rd-vehicle control 组件
```

如果车辆仍需提供视频，可以只运行 `rd-vehicle` 的媒体组件，不能让其控制组件继续发布远程速度指令。

### 7.3 唯一实例约束

禁止：

```text
Nav2配置 cmdvel_2_ecu ──┐
                         ├→ /ecu
远程配置 cmdvel_2_ecu ──┘
```

正确数量：

| 运行模式 | Nav2 配置转换器 | 远程配置转换器 | `/ecu` 发布者 | `chassis_driver` |
| --- | ---: | ---: | ---: | ---: |
| Nav2 | 1 | 0 | 1 | 1 |
| 远程驾驶 | 0 | 1 | 1 | 1 |

节点名称不同不能消除 `/ecu` 冲突。唯一约束必须针对实际发布话题和硬件设备，而不只是节点名称。

## 8. 运行检查

### 8.1 远程模式

```bash
ros2 topic info /cmd_vel_remote --verbose --no-daemon
ros2 topic info /cmd_vel --verbose --no-daemon
ros2 topic info /ecu --verbose --no-daemon
ros2 topic info /vehicle_status --verbose --no-daemon
ros2 param get /cmdvel_to_ecu cmd_vel_topic
ros2 param get /cmdvel_to_ecu require_localization
```

预期：

```text
/cmd_vel_remote：
  Publisher count: 1     # rd-vehicle
  Subscription count: 1  # cmdvel_2_ecu

/cmd_vel：
  没有正在运行的 Nav2 控制发布者

/ecu：
  Publisher count: 1     # cmdvel_2_ecu
  Subscription count: 1  # chassis_driver

cmd_vel_topic=/cmd_vel_remote
require_localization=false
```

### 8.2 Nav2 模式

预期：

```text
/cmd_vel：
  最终发布者为 Nav2 collision_monitor
  订阅者为 cmdvel_2_ecu

/cmd_vel_remote：
  没有 rd-vehicle control 发布者

/ecu：
  Publisher count: 1
  Subscription count: 1

cmd_vel_topic=/cmd_vel
require_localization=true
```

发现发布者数量、参数或模式与预期不一致时，不进行实车运动测试。

## 9. 构建和部署边界

该方案涉及两个代码位置：

| 工作区 | 修改内容 |
| --- | --- |
| `/home/akun/workspace/my-driving` | `rd-vehicle` ROS 输出话题配置化、配置校验和测试 |
| `/home/akun/workspace/Car_jetson/real_ws` | `cmdvel_2_ecu` 定位门控参数、启动入口和测试 |

不需要修改：

- 驾驶端输入和界面；
- 统一服务端；
- QUIC 控制协议；
- `chassis_driver`；
- `/ecu` 消息定义；
- Nav2 内部控制链；
- 完整多来源仲裁器。

两个工作区必须使用匹配版本共同部署。仅修改 `rd-vehicle` 使其发布 `/cmd_vel_remote`，但未修改转换器输入时，远程消息没有消费者；仅修改转换器输入但未修改 `rd-vehicle` 时，同样无法形成控制链。

## 10. 验收

### 10.1 无实车验证

1. 两套参数配置均能正确加载并显示实际值。
2. 远程配置只订阅 `/cmd_vel_remote`，Nav2 配置只订阅 `/cmd_vel`。
3. 远程配置不因定位缺失或过期重新锁止。
4. Nav2 配置保持现有定位失效关闭行为。
5. 两种配置都保留底盘反馈、自动驾驶使能、输入超时、非法值和最终限幅。
6. 同时启动两种配置的错误场景能够被启动流程或运行检查发现。
7. `rd-vehicle` 停车请求和关闭流程使用配置后的同一话题。

### 10.2 台架与封闭场地验证

由具备车辆安全条件的人员执行：

1. 远程模式未启动 LIO 时，先验证零输入和停止行为。
2. 在低速、封闭环境验证远程非零指令。
3. 验证 `is_autodrive=false`、底盘反馈过期和远程输入超时均能阻止持续运动。
4. 验证停止 `rd-vehicle`、断开网络和停止 `/cmd_vel_remote` 后的实车行为。
5. Nav2 模式重新验证定位退化、定位恢复和碰撞监控停车。
6. 对照底盘反馈与独立现场观测记录制动响应和物理停车，不用软件发布成功代替停车确认。

## 11. 需要负责人确认的问题

1. 是否允许远程模式在 LIO 未启动或非 `ACTIVE` 时运行？
2. 是否接受远程和 Nav2 采用不同输入话题、互斥启动？
3. 是否同意 `require_localization` 默认值保持 `true`？
4. 标准入口采用两个 launch，还是一个带 `control_mode` 参数的 launch？
5. 谁负责保证整车 launch 不再额外启动第二个转换器？
6. 远程模式是否需要保留其他定位、障碍物或区域限制？
7. `input_timeout_sec=0.20 s` 是否符合实车底盘和远程链路要求？
8. `brake`、`p_status`、CAN 丢失、实体急停和底盘 watchdog 的真实行为是什么？

## 12. 方案边界

本方案比复制转换器源码更容易维护，也比继续让远程和 Nav2 共用 `/cmd_vel` 更能隔离误发布。但它仍依靠启动入口保证两种模式互斥，不支持在线切换，也不构成完整控制权仲裁。

如果以后要求远程、Nav2 和充电对接同时在线，应迁移到独立输入话题、车辆端仲裁器、唯一 `/cmd_vel_selected` 和唯一底盘出口的最终架构。

## 13. 实施状态

| 项目 | 状态 |
| --- | --- |
| 方案整理 | 已完成 |
| 车辆端与小车负责人评审 | 待进行 |
| `rd-vehicle` 话题配置化 | 未开始 |
| `cmdvel_2_ecu` 定位门控参数 | 未开始 |
| 互斥启动入口 | 未开始 |
| 两侧无实车测试 | 未开始 |
| 配套构建与部署 | 未开始 |
| 台架/封闭场地验收 | 未开始 |

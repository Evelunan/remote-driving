# 车辆多控制源仲裁改造计划

记录日期：2026-11-28。状态：方案草案，尚未实施。

本文整理远程驾驶、Nav2、充电对接及后续手柄控制共用真实底盘时的现状、风险、目标架构、实施顺序和验收标准。当前任务仅在 `my-driving` 中记录方案，没有修改 `/home/akun/workspace/Car_jetson`、启动 ROS2 节点或连接实车。

## 1. 结论

当前底层转换器和底盘驱动已经存在，主要缺口不是再开发一套远程驾驶底盘驱动，而是：

1. 远程驾驶与 Nav2 最终都可能发布 `/cmd_vel`，ROS2 不会自动仲裁控制权。
2. 已有 `command_mux` 只处理 Nav2 与充电对接，不认识远程驾驶和手柄，也不在普通启动模式下启用。
3. `cmdvel_2_ecu` 同时承担通用底盘转换和 Nav2 定位门控，职责没有按控制来源分开。
4. 多处“实测静止”判断仍按旧 km/h 假设除以 `3.6`，但当前底盘状态契约已确认 `cur_speed` 为 m/s。
5. 零速、CAN 制动位、底盘制动反馈和物理停车确认尚未形成一致且经过实车验证的契约。
6. 手工命令和整车 launch 都能启动转换器或驱动，缺少唯一实例约束。

目标不是建立多个 `/ecu` 出口，而是建立一个车辆端本地仲裁器，并保持唯一的 `cmdvel_2_ecu` 和唯一的 `chassis_driver`。

## 2. 当前已有组件

| 组件 | 状态 | 当前接口与说明 |
| --- | --- | --- |
| 远程驾驶接收与监护 | 已有 | `rd-vehicle` 接收经过认证和租约约束的远程指令，当前固定发布 `/cmd_vel` |
| Nav2 | 已有 | 控制器和行为服务器发布 `/cmd_vel_nav`，速度平滑后发布 `/cmd_vel_raw`，碰撞监控最终发布 `/cmd_vel` |
| 充电对接控制 | 已有 | 发布 `/cmd_vel_dock` |
| 手柄独立输入 | 未实现 | 没有正式的 `/cmd_vel_manual`；网页调试功能会直接向 `/cmd_vel` 发布一次性命令，但不能当作正式手柄通道 |
| 控制权仲裁 | 部分已有 | `charging_dock/command_mux` 只在 Nav2 与充电对接之间选择，输出 `/cmd_vel_selected` |
| `cmdvel_2_ecu` | 已有 | 输入话题可配置；普通配置订阅 `/cmd_vel`，对接配置可订阅 `/cmd_vel_selected`，输出 `/ecu` |
| `chassis_driver` | 已有 | 订阅 `/ecu`，通过以太网 CAN/UDP 驱动 JD03，并发布 `/vehicle_status`、`/battery_status` |
| 实体急停与底盘硬件 watchdog | 未确认 | 现有代码只能证明自动驾驶使能反馈、CAN 制动位和软件超时逻辑，不能证明存在已验收的独立硬件安全回路 |

本项目内的远程 ROS 发布器位于 [`ros2_cmd_vel_controller.py`](../apps/vehicle/src/rd_vehicle/infrastructure/controller/ros2_cmd_vel_controller.py)。外部底盘工作区的现状依据如下：

- `/home/akun/workspace/Car_jetson/real_ws/src/charging_dock/charging_dock/command_mux.py`
- `/home/akun/workspace/Car_jetson/real_ws/src/launch_real/launch/run.launch.py`
- `/home/akun/workspace/Car_jetson/real_ws/src/driver/car_control/cmdvel_2_ecu/cmdvel_2_ecu/convert.py`
- `/home/akun/workspace/Car_jetson/real_ws/src/driver/car_control/chassis_driver/chassis_driver/launch/chassis_driver.launch.py`
- `/home/akun/workspace/Car_jetson/nav2/src/navigation2/nav2_bringup/launch/navigation_launch.py`
- `/home/akun/workspace/Car_jetson/nav2/src/nav2/config/nav2_real_params.yaml`

## 3. 当前实际控制链路

### 3.1 普通模式

Nav2 的 `/cmd_vel_nav` 不是最终安全输出。它还要经过速度平滑和碰撞监控：

```text
Nav2 controller / behavior
        ↓ /cmd_vel_nav
velocity_smoother
        ↓ /cmd_vel_raw
collision_monitor
        ↓ /cmd_vel ─────────────┐
                                │
rd-vehicle 远程驾驶 ─→ /cmd_vel ┼→ cmdvel_2_ecu → /ecu → chassis_driver → 底盘
                                │
网页调试控制 ────────→ /cmd_vel ┘
```

这条链路中，`cmdvel_2_ecu` 只能看到 `Twist`，无法识别消息来自 Nav2、远程驾驶还是网页调试。多个发布者同时存在时，消息会交错到达，不存在“远程自动优先”或“最后启动者独占”的 ROS2 规则。

### 3.2 可选充电对接模式

整车 launch 在 `enable_relative_lidar_docking=true` 时会启用已有仲裁器：

```text
Nav2 最终输出 /cmd_vel ─────┐
                             ├→ command_mux → /cmd_vel_selected
对接控制 /cmd_vel_dock ─────┘                     ↓
                                           cmdvel_2_ecu
                                                 ↓ /ecu
                                           chassis_driver
```

该仲裁器具备输入新鲜度检查、零速切换周期、停止锁存和可选实测静止确认，可以作为通用仲裁器的实现基础。但它目前只有 `nav` 与 `dock` 两个来源，并把远程驾驶发布到同一 `/cmd_vel` 的消息也当成 `nav`，无法执行远程与导航之间的控制权隔离。

普通整车 launch 默认 `enable_relative_lidar_docking=false`，因此普通远程驾驶启动流程没有经过该仲裁器。

## 4. 已确认问题与优先级

P0 表示在多控制源接入实车前必须解决；P1 表示影响可维护性、诊断或后续扩展。

| 编号 | 优先级 | 问题 | 影响 |
| --- | --- | --- | --- |
| A01 | P0 | 远程驾驶、Nav2 最终安全输出和网页调试共用 `/cmd_vel` | 指令可能交错，无法证明当前控制来源 |
| A02 | P0 | `rd-vehicle` 没有独立 `/cmd_vel_remote` | 仲裁器无法区分远程指令，远程租约不能映射为本地控制模式 |
| A03 | P0 | 现有 `command_mux` 只区分 Nav2 和对接 | 不能管理 `REMOTE`、`MANUAL` 或完整的安全状态机 |
| A04 | P0 | 当前 `/cmd_vel_nav` 位于速度平滑和碰撞监控之前 | 如果直接拿它接仲裁器，会绕过 Nav2 最终限速和障碍停车 |
| A05 | P0 | `cmdvel_2_ecu` 内置 `/lio_sam/map_odom_status` 门控 | Nav2 专属条件会无差别限制远程驾驶；直接删除又会削弱导航保护 |
| A06 | P0 | 当前 `command_mux` 和 `cmdvel_2_ecu` 的静止判断仍对 `cur_speed` 除以 `3.6` | 与已确认的 m/s 契约冲突，可能错误判定车辆已经静止 |
| A07 | P0 | 零速、`Ecu.brake`、`p_status`、停车确认的语义不一致且未实车验收 | 软件“已请求停车”可能被误当成车辆“已经停稳” |
| A08 | P0 | 手工启动与整车 launch 都能启动转换器和底盘驱动 | 可能出现多个 `/ecu` 发布者或多个进程占用同一 CAN/UDP 接口 |
| A09 | P0 | 实体急停、ECU 断指令行为和硬件 watchdog 未确认 | 软件故障、Jetson 断电或 CAN 中断后的车辆行为没有证据保证 |
| A10 | P1 | 网页调试直接发布 `/cmd_vel` | 调试入口绕过未来的模式申请和授权边界 |
| A11 | P1 | 已有仲裁器位于 `charging_dock` 包 | 整车通用控制权职责与充电业务耦合，后续模式扩展困难 |
| A12 | P1 | 缺少统一模式、当前来源和拒绝原因诊断 | 现场难以判断车辆为什么不动或当前由谁控制 |

### 4.1 `cur_speed` 单位问题

本项目已经按用户确认将 `/vehicle_status.cur_speed` 作为 m/s 原值处理，见[配置说明](configuration.zh-CN.md#ros2-底盘状态反馈)。但外部工作区当前仍存在类似以下逻辑：

```python
vehicle_speed = float(message.cur_speed) / 3.6
vehicle_stopped = abs(msg.cur_speed / 3.6) <= 0.01
```

如果输入已经是 m/s，这会把实测速度缩小 3.6 倍，使静止门槛偏松。实施仲裁前必须统一消息定义、驱动解码、显示和静止判断的单位，并增加固定数值测试；不能通过调大或调小阈值掩盖单位错误。

### 4.2 制动与停车语义问题

当前不同层次的“停止”含义必须分开：

| 信号或结果 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 发布零 `Twist` | 上层请求零目标速度 | 底盘已收到、已制动或已停稳 |
| 发布 `/ecu` 且 `brake=true` | CAN 驱动收到制动请求 | CAN 已送达、制动器已执行或车辆已停稳 |
| `p_status=true` | 底盘报告某个制动状态位 | 车辆线速度为零或停车保持可靠 |
| `cur_speed` 接近零 | 当前反馈样本接近静止 | 独立驻车已经建立或反馈不会过期 |
| `StopResult.confirmed=false` | 软件只完成停车请求 | 不能对外显示“停车已确认” |

外部 `cmdvel_2_ecu` 的源码、配置和既有 README 对零速是否设置 `brake` 还存在版本差异。修改仲裁逻辑前，应以匹配车型、固件和实车的协议为准，统一代码、测试和文档。

## 5. 目标架构

```text
Nav2 完整安全链路 → /cmd_vel_nav_safe ─┐
远程驾驶           → /cmd_vel_remote ───┤
充电对接           → /cmd_vel_dock ─────┼→ vehicle_command_arbiter
手柄（后续接入）    → /cmd_vel_manual ───┘
                                              ↓ /cmd_vel_selected
                                      唯一 cmdvel_2_ecu
                                              ↓ /ecu
                                      唯一 chassis_driver
                                              ↓ CAN
                                           真实底盘
```

### 5.1 话题职责

| 话题 | 唯一职责 |
| --- | --- |
| `/cmd_vel_remote` | `rd-vehicle` 接受并执行有效远程控制租约后的本地速度目标 |
| `/cmd_vel_nav_safe` | Nav2 经过速度平滑和碰撞监控后的最终安全输出 |
| `/cmd_vel_dock` | 充电对接控制器输出 |
| `/cmd_vel_manual` | 预留给正式本地手柄通道；第一版可不实现 |
| `/cmd_vel_selected` | 仲裁器唯一输出，也是 `cmdvel_2_ecu` 唯一运动输入 |
| `/ecu` | `cmdvel_2_ecu` 唯一输出，`chassis_driver` 唯一输入 |
| `/vehicle_status` | 底盘实测反馈，不作为任何输入源延长控制租约的依据 |

`Twist` 本身没有认证身份、控制租约、会话代次或原始采样时间。远程模式不能只凭“最近收到 `/cmd_vel_remote`”就授予控制权；`rd-vehicle` 还需要向本地仲裁器提供经过认证后的控制状态，至少能表达：

- 当前远程控制租约是否有效；
- 当前车辆会话和代次是否仍匹配；
- 是否处于允许发送运动指令的中立输入之后；
- 控制输入是否已经失效；
- 远程控制进程是否准备好以及是否正在退出。

具体采用话题、服务还是进程内适配，在实现阶段确定；不能让任意未认证的 ROS 发布者自行声明获得远程控制权。

## 6. 控制权与状态机

控制权由车辆端本地 `vehicle_command_arbiter` 最终决定。驾驶端、Nav2、对接控制器和手柄只能申请模式，不能直接向 `/cmd_vel_selected` 或 `/ecu` 发布。

建议状态：

```text
DISABLED
NAVIGATION
REMOTE
DOCKING
MANUAL       # 后续接入
SAFE_STOP
```

| 状态 | 允许的输入 | 主要准入条件 |
| --- | --- | --- |
| `DISABLED` | 无 | 初始状态或操作者明确禁用 |
| `NAVIGATION` | `/cmd_vel_nav_safe` | 定位有效、Nav2 最终安全输出新鲜、公共底盘条件满足 |
| `REMOTE` | `/cmd_vel_remote` | 远程会话与租约有效、输入新鲜、公共底盘条件满足 |
| `DOCKING` | `/cmd_vel_dock` | 对接动作已授权、低速配置生效、所需定位/传感器和底盘反馈有效 |
| `MANUAL` | `/cmd_vel_manual` | 本地手柄已接入并明确授权；第一版不实现 |
| `SAFE_STOP` | 仅停车输出 | 超时、非法值、反馈失效、切换中、明确停止或故障 |

### 6.1 所有软件模式的公共条件

- `/vehicle_status` 新鲜且时间戳有效；
- `is_autodrive=true`；
- 当前输入值有限且在配置范围内；
- 当前来源的指令与授权状态持续新鲜；
- 不存在停止锁存或未处理故障；
- `/cmd_vel_selected` 和 `/ecu` 均只有预期的唯一发布者；
- 输入过期后不能用缓存、重复消息或普通网络心跳续期。

实体急停和人工接管如果经过实车确认，应始终具有最高优先级。确认前只能把它们列为待验收能力，不能在软件界面或文档中宣称已有可靠的实体安全保护。

### 6.2 来源专属条件

- `NAVIGATION`：定位状态 `ACTIVE` 且新鲜；必须使用碰撞监控之后的最终输出。
- `REMOTE`：认证会话、控制租约、会话代次和输入新鲜度有效；是否要求视频新鲜按现有远程驾驶安全策略执行；不默认要求 LIO 定位 `ACTIVE`。
- `DOCKING`：对接传感器、位姿、车辆状态和低速限制满足既有对接契约。
- `MANUAL`：在正式定义本地手柄身份、优先级和失效行为前，不接入实车链路。

### 6.3 安全切换流程

任意两个运动模式之间都执行：

```text
收到模式申请
    ↓
停止转发当前来源，进入 SAFE_STOP
    ↓
持续执行已定义的停车动作
    ↓
等待新鲜 /vehicle_status 确认实测速度在阈值内并稳定一段时间
    ↓
清除所有来源的缓存命令，更新控制代次
    ↓
授予新模式
    ↓
等待新来源的新中立输入
    ↓
允许该来源后续的非零指令
```

切换过程中禁止：

- 使用切换前缓存的非零指令；
- 以“最后一条消息”决定来源；
- 远程断线后自动恢复旧远程目标；
- 远程退出后自动恢复 Nav2；
- Nav2 恢复定位后自动退出 `SAFE_STOP`；
- 用过期的底盘反馈确认静止。

## 7. 分阶段修改计划

涉及三个工作区，必须按匹配版本联合部署：

| 工作区 | 计划修改 |
| --- | --- |
| `/home/akun/workspace/my-driving` | 远程 ROS 输出话题配置、远程控制状态接口、对应测试与文档 |
| `/home/akun/workspace/Car_jetson/nav2` | 将碰撞监控后的最终输出从共享 `/cmd_vel` 调整为独立安全话题 |
| `/home/akun/workspace/Car_jetson/real_ws` | 通用仲裁器、静止单位修正、转换器职责调整、统一 launch 与底盘侧测试 |

当前文档只位于 `my-driving`；修改另外两个工作区需要单独确认范围、分支和构建/实车边界。

### 阶段 0：冻结现状并确认底盘契约

1. 保存运行时节点图、话题发布者/订阅者和有效配置。
2. 确认 `VehicleStatus.cur_speed` 的消息定义、驱动解码和实车单位全部为 m/s。
3. 向底盘厂商确认 JD03 的 `brake`、`p_status`、CAN 丢失行为和硬件 watchdog。
4. 确认是否存在实体急停、人工接管和独立驻车，以及它们是否绕过 Jetson/ROS/CAN。
5. 记录当前 Nav2、远程驾驶和对接各自的速度、加速度、转角及超时限制。

底盘硬件契约不影响先进行无硬件软件改造，但未完成前不得把方案标记为实车可用。

### 阶段 1：拆分远程驾驶输出

在 `my-driving` 中：

1. 将 ROS2 运动输出话题改为显式配置，而不是固定 `/cmd_vel`。
2. 实车新配置使用 `/cmd_vel_remote`；旧配置的兼容和迁移规则需要明确。
3. 将远程租约、会话代次、输入有效性和退出状态提供给本地仲裁边界。
4. 保留现有远程 watchdog、断线停车请求和恢复后先回中的规则。
5. 增加配置、话题映射、失效和退出测试，不启动真实 ROS 或底盘。

### 阶段 2：建立通用车辆仲裁器

在 `real_ws` 中以现有 `charging_dock/command_mux` 为基础：

1. 抽取或新建通用 `vehicle_command_arbiter`，避免整车职责继续依赖充电业务包。
2. 支持 `NAVIGATION`、`REMOTE`、`DOCKING`、`SAFE_STOP`；预留 `MANUAL`。
3. 为每个来源分别维护最新消息、接收时刻、授权状态和控制代次。
4. 实现明确模式申请、拒绝原因、当前模式和当前来源诊断。
5. 复用并加强停止锁存、缓存清除、实测静止和切换确认。
6. 修复 `cur_speed` 单位，不再对 m/s 反馈除以 `3.6`。
7. 非法值、反馈过期、授权失效或内部异常一律进入 `SAFE_STOP`。

### 阶段 3：拆分 Nav2 最终输出

在 Nav2 工作区中：

1. 保留 `controller/behavior → /cmd_vel_nav → velocity_smoother → /cmd_vel_raw`。
2. 将 `collision_monitor` 的最终输出改为 `/cmd_vel_nav_safe`。
3. 仲裁器只接 `/cmd_vel_nav_safe`，禁止直接接碰撞监控之前的 `/cmd_vel_nav`。
4. 增加启动配置检查，避免又有 Nav2 组件直接发布 `/cmd_vel_selected` 或 `/ecu`。

### 阶段 4：收敛唯一底盘出口

1. `vehicle_command_arbiter` 成为 `/cmd_vel_selected` 唯一发布者。
2. 唯一 `cmdvel_2_ecu` 只订阅 `/cmd_vel_selected`。
3. 唯一 `chassis_driver` 只订阅 `/ecu` 并持有 CAN/UDP 设备。
4. `cmdvel_2_ecu` 保留单位换算、转向模型、挡位生成、最终限幅、公共底盘反馈门控和输出超时保护。
5. 只有在仲裁器的 `NAVIGATION` 模式已经建立等价定位保护并通过测试后，才从转换器中移走全局 LIO 定位锁。
6. 统一零速、制动位和 watchdog 失效动作；保留“请求”和“物理确认”的语义区别。

### 阶段 5：统一启动与部署

提供一个车辆控制 launch，保证一次只启动：

```text
1 个 vehicle_command_arbiter
1 个 cmdvel_2_ecu
1 个 chassis_driver
```

同时：

- 禁止网页调试继续直接向共享 `/cmd_vel` 发布；改为专用测试来源并经过仲裁，或只允许在底盘断开时使用。
- 整车 launch 与手工启动说明明确互斥关系。
- 启动时检测重复 `/cmd_vel_selected`、`/ecu` 发布者和重复底盘驱动；检测失败时保持停止并报错。
- 三个工作区使用匹配的配置修订共同升级和共同回退，不能只部署其中一部分。

### 阶段 6：软件验证

先使用模拟 ROS、模拟反馈和内存控制器验证，不连接实车：

1. Nav2 与远程同时持续发布时，只转发当前被授予的来源。
2. 每个来源分别测试消息停止、过期、乱序、非法值和进程退出。
3. 远程租约失效、断网和会话代次变化进入 `SAFE_STOP`。
4. 定位失效只禁止 `NAVIGATION`，不把纯远程模式错误解释成 Nav2。
5. 模式切换期间没有非零脉冲，旧缓存不会在新模式中重放。
6. 车辆未实测静止时拒绝完成切换。
7. 底盘反馈过期、未来时间戳或单位错误时拒绝静止确认。
8. 重复发布者和重复驱动实例被诊断并阻止进入运动模式。
9. 仲裁器、转换器或车辆端任一进程退出时，停车请求和未确认物理停车状态可观测。

### 阶段 7：台架和封闭场地验收

由具备车辆安全条件的人员执行，先架空或使用封闭低速环境：

1. 验证远程、Nav2、对接模式各自的正常控制和限值。
2. 在车辆静止条件下验证模式申请、拒绝、切换和回中要求。
3. 验证断开远程网络、停止发布指令、结束车辆端进程和停止 CAN 通信后的实际行为。
4. 验证自动驾驶按钮、人工接管、实体急停和复位行为。
5. 对照 `/vehicle_status` 与独立观测记录制动响应和物理停车时间。
6. 验证 `brake`、`p_status` 和实际停车之间的关系，不用单一状态位替代物理结论。

软件测试通过不能替代本阶段，也不能据此给出真实停车时间上界。

## 8. 验收标准

### 8.1 结构验收

- 远程、Nav2、对接使用相互独立的输入话题。
- 普通运行中没有业务节点继续发布共享 `/cmd_vel`。
- `/cmd_vel_selected` 只有仲裁器一个发布者。
- `/ecu` 只有 `cmdvel_2_ecu` 一个发布者。
- CAN/UDP 设备只有 `chassis_driver` 一个持有者。
- Nav2 输入来自碰撞监控之后的最终安全输出。

现场可以使用以下只读命令检查端点，不把端点存在当作车辆执行证明：

```bash
ros2 node list --no-daemon
ros2 topic info /cmd_vel_remote --verbose --no-daemon
ros2 topic info /cmd_vel_nav_safe --verbose --no-daemon
ros2 topic info /cmd_vel_dock --verbose --no-daemon
ros2 topic info /cmd_vel_selected --verbose --no-daemon
ros2 topic info /ecu --verbose --no-daemon
ros2 topic info /vehicle_status --verbose --no-daemon
```

### 8.2 行为验收

- 未授予控制权的来源无论发布频率多高，都不能影响输出。
- 任何模式都不能绕过公共底盘反馈、自动驾驶使能、限值和超时保护。
- 远程断开或租约失效后停车并保持锁存，不自动恢复旧目标或 Nav2。
- 定位失效禁止 Nav2；恢复定位后仍需明确重新授权和中立输入。
- 模式切换使用新鲜 m/s 实测速度确认静止，且不会重放旧命令。
- 停车请求、CAN 制动请求、底盘反馈和物理停车在日志与界面中分别表达。

### 8.3 实车安全验收

- 厂商确认且现场验证 CAN 丢失和 ECU watchdog 行为。
- 实体急停和人工接管的接线、优先级、锁存及复位行为有记录。
- 已测量各类失效条件下的制动响应和停车距离/时间，测试条件可追溯。
- 在这些证据完成前，发布说明继续标注“真实停车未确认”。

## 9. 发布与回退

本改造跨越远程驾驶、Nav2 和底盘工作区，协议和话题迁移必须作为一个配套版本发布：

1. 为三个工作区记录匹配提交、构建产物和配置修订。
2. 新旧链路不能同时连接同一 `/ecu` 或 CAN 设备。
3. 部署前保存当前可回退版本和现场配置。
4. 回退时整体恢复旧远程话题、Nav2 输出、转换器输入和 launch，不能只回退一个工作区。
5. 任何阶段发现发布者数量、单位、制动契约或底盘反馈不符合预期，保持车辆静止并停止后续实车测试。

## 10. 暂不纳入第一版

- 正式 `/cmd_vel_manual` 手柄通道；只预留接口，不在优先级和失效策略未定义时接入。
- 未经厂商和实车确认的硬件急停、驻车或 watchdog 能力声明。
- 以远程指令直接发布 `/ecu` 或新增第二个 `cmdvel_2_ecu`。
- 远程断开后自动恢复 Nav2、定位恢复后自动继续旧路径等自动恢复策略。
- 用软件模拟结果推断真实制动距离、停车时间或安全等级。

## 11. 实施状态

| 项目 | 状态 |
| --- | --- |
| 现状梳理 | 已完成 |
| 方案文档 | 已完成 |
| `my-driving` 远程话题拆分 | 未开始 |
| 通用车辆仲裁器 | 未开始 |
| Nav2 最终安全话题拆分 | 未开始 |
| 唯一底盘出口与统一 launch | 未开始 |
| 无硬件联合验证 | 未开始 |
| 实体安全能力确认 | 未开始 |
| 台架/封闭场地实车验收 | 未开始 |

开始实施前应重新核对三个工作区的 Git 状态、已安装产物和运行时 ROS 图，避免本文记录之后发生的外部改动造成判断失效。

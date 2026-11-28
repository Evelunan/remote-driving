# 远程驾驶独占过渡方案：定位门控可配置化

记录日期：2026-11-28。状态：供小车负责人评审，尚未实施。

本文只讨论近期“远程驾驶独占”方案，不包含远程驾驶、Nav2、充电对接之间的完整控制权仲裁设计。完整仲裁方案另见[车辆多控制源仲裁改造计划](vehicle-command-arbitration-plan.zh-CN.md)。

本次记录没有修改 `/home/akun/workspace/Car_jetson`、ROS2 安装、底盘驱动或实车配置，也没有启动 ROS2 节点或连接实车。

## 1. 讨论目标

近期希望继续使用现有车辆控制链路，不新增 ROS 话题、不修改驾驶端和服务端，只让纯远程驾驶在未启动 LIO 定位时也能工作：

```text
驾驶端
  ↓ 认证网络控制
rd-vehicle
  ↓ /cmd_vel
cmdvel_2_ecu
  ↓ /ecu
chassis_driver
  ↓ CAN
真实底盘
```

近期方案只将 `cmdvel_2_ecu` 中的 LIO 定位门控做成可配置开关，不跳过整个转换器。远程独占运行期间，不允许 Nav2、充电对接或网页底盘控制同时向 `/cmd_vel` 发布。

## 2. 当前问题

`cmdvel_2_ecu` 当前同时承担两类职责：

1. 通用底盘转换和公共保护：
   - 订阅 `/cmd_vel`；
   - 将 m/s、rad/s 的 `Twist` 转换成底盘 `Ecu` 消息；
   - 计算速度、转角和挡位；
   - 执行速度、转角、低速和零速处理；
   - 检查 `/vehicle_status` 是否新鲜；
   - 检查 `is_autodrive=true`；
   - 检查非法输入和指令超时。
2. Nav2 专属的定位保护：
   - 订阅 `/lio_sam/map_odom_status`；
   - 启动时默认锁定运动；
   - 要求定位状态为 `ACTIVE` 且持续新鲜；
   - 定位退化或超时后重新锁止。

第二类保护适用于 Nav2，但纯远程驾驶是否必须依赖 LIO 定位，需要由小车负责人确认。当前代码没有关闭定位门控的参数，因此仅在启动命令中添加一个未声明参数不能绕过 `ACTIVE` 检查。

不建议采用以下绕过方式：

- 伪造 `/lio_sam/map_odom_status=ACTIVE`；
- 删除全部定位保护代码；
- 让 `rd-vehicle` 直接发布 `/ecu`；
- 另启一个远程专用 `cmdvel_2_ecu`；
- 远程与 Nav2 同时发布 `/cmd_vel`。

## 3. 建议方案

给现有 `cmdvel_2_ecu` 增加一个布尔参数：

```yaml
require_localization: true
```

参数语义：

| 配置 | 行为 | 使用场景 |
| --- | --- | --- |
| `require_localization: true` | 保持现有行为，必须收到新鲜的定位 `ACTIVE` 状态 | Nav2、自动导航 |
| `require_localization: false` | 不以 LIO 定位状态阻止 `/cmd_vel`，其余公共保护继续生效 | 近期远程驾驶独占 |

默认值必须为 `true`，避免未显式配置的 Nav2 启动方式意外失去定位保护。

### 3.1 关闭定位门控时跳过什么

当 `require_localization=false` 时，只跳过：

- 启动时的定位停止锁；
- `/cmd_vel` 回调中的定位锁检查；
- watchdog 对定位状态缺失、过期和非 `ACTIVE` 的锁止；
- 定位恢复后等待连续静止样本才能解除定位锁的流程。

可以不创建 `/lio_sam/map_odom_status` 订阅；如果为了代码结构仍保留订阅，其回调也不得在该模式下改变运动许可。

### 3.2 必须继续保留什么

关闭 LIO 定位门控不等于关闭底盘安全保护。以下行为必须保持：

- `/vehicle_status` 必须持续新鲜；
- `is_autodrive` 必须为 `true`；
- `/cmd_vel` 必须持续更新；
- 非法的 NaN/Inf 输入不能生成运动指令；
- 最终速度和转向角限幅继续生效；
- 速度、挡位、转角和制动字段仍由现有转换器生成；
- `input_timeout_sec` 超时处理继续生效，当前配置为 `0.20 s`；
- `rd-vehicle` 继续执行认证、控制租约、网络断线、输入失效和恢复后先回中的保护；
- 停车请求与车辆物理停稳继续分开表达。

## 4. 需要修改的内容

### 4.1 必须修改：转换器源码

文件：

```text
/home/akun/workspace/Car_jetson/real_ws/src/driver/car_control/
cmdvel_2_ecu/cmdvel_2_ecu/convert.py
```

需要完成：

1. 声明并读取 `require_localization`，默认值为 `true`。
2. 根据该参数设置初始 `localization_stop_latched`。
3. 仅在参数启用时创建定位状态订阅，或保证禁用时回调不改变控制状态。
4. `cmd_callback()` 仅在参数启用时应用定位锁。
5. `watchdog_callback()` 仅在参数启用时检查定位状态年龄并重新锁止。
6. 定位状态回调和定位恢复流程仅在参数启用时生效。
7. 不改变 `/vehicle_status`、`is_autodrive`、指令超时、限幅和 ECU 转换逻辑。

不能只修改 `/cmd_vel` 回调中的一处判断，因为 watchdog 仍会因缺少定位状态把转换器重新锁住。

### 4.2 启动参数

Nav2 使用的普通配置继续保持定位门控开启：

```yaml
/cmdvel_to_ecu:
  ros__parameters:
    require_localization: true
```

远程独占模式可以使用专用配置文件设置：

```yaml
/cmdvel_to_ecu:
  ros__parameters:
    cmd_vel_topic: /cmd_vel
    require_localization: false
```

也可以在代码增加并声明该参数、重新构建后，通过启动命令覆盖：

```bash
ros2 run cmdvel_2_ecu cmdvel_2_ecu --ros-args \
  --params-file /home/akun/workspace/Car_jetson/real_ws/install/cmdvel_2_ecu/share/cmdvel_2_ecu/config/config.yaml \
  -p require_localization:=false
```

建议最终提供明确的远程专用配置或启动入口，避免现场操作者遗漏参数。不能把普通 Nav2 配置永久改成 `false`。

### 4.3 必须修改：转换器测试

文件：

```text
/home/akun/workspace/Car_jetson/real_ws/src/driver/car_control/
cmdvel_2_ecu/test/test_docking_conversion.py
```

至少增加以下测试：

1. 默认 `require_localization=true`。
2. 门控开启且定位未 `ACTIVE` 时，非零 `/cmd_vel` 不能下发运动 `/ecu`。
3. 门控关闭、底盘反馈新鲜且 `is_autodrive=true` 时，允许合法 `/cmd_vel` 进入原有转换流程。
4. 门控关闭但 `/vehicle_status` 过期时，仍然禁止运动输出。
5. 门控关闭但 `is_autodrive=false` 时，仍然禁止运动输出。
6. 门控关闭时，定位状态缺失、过期或退化不会重新锁止远程控制。
7. 指令超过 `input_timeout_sec` 后仍执行原有超时动作。
8. NaN/Inf、速度限幅、转角限幅、零速和挡位测试继续通过。
9. 门控开启时原有定位退化、停车锁存和恢复测试继续通过。

### 4.4 必须更新：启动说明

在实现完成后更新车辆端说明，明确：

- 远程独占使用哪个参数文件或启动命令；
- Nav2 使用的配置仍然要求定位；
- 远程独占期间禁止启动 Nav2、充电对接和网页底盘控制；
- 如果整车 launch 已经启动转换器或底盘驱动，不得再次手工启动；
- 远程模式仍不能把软件零速或制动请求解释为车辆已经物理停稳。

### 4.5 需要重新构建

修改源码后必须重新构建 `cmdvel_2_ecu` 并重新加载工作区，否则启动命令仍会运行旧的安装版本。建议由小车工作区负责人按照该工作区现有构建流程执行，例如：

```bash
source /opt/ros/humble/setup.bash
cd /home/akun/workspace/Car_jetson/real_ws
colcon build --symlink-install --packages-select cmdvel_2_ecu
source install/setup.bash
```

构建前应先核对该工作区的未提交改动和现有构建约定，不能覆盖其他开发内容。

## 5. 不需要修改的内容

近期最小方案继续使用 `/cmd_vel`，因此不需要修改：

- 驾驶端应用；
- 服务端及网络协议；
- `rd-vehicle` 当前 `/cmd_vel` 发布话题；
- `chassis_driver`；
- Nav2 的话题结构；
- `/ecu` 消息定义；
- 完整控制权仲裁器。

如果以后需要远程驾驶、Nav2 和充电对接同时在线，再单独实施多控制源仲裁方案。本方案不解决多来源共存。

## 6. 运行约束

远程独占运行期间必须满足：

```text
/cmd_vel 发布者：仅 rd-vehicle 的 cmd_vel_publisher
/ecu 发布者：仅 cmdvel_to_ecu
/ecu 订阅者：仅预期的 chassis_driver
底盘驱动实例：仅 1 个
```

不得同时启动：

- Nav2 导航控制链；
- 充电对接控制器或其 `command_mux`；
- 网页 `/cmd_vel` 调试控制；
- 第二个 `cmdvel_2_ecu`；
- 第二个 `chassis_driver`。

启动后先执行只读检查：

```bash
ros2 node list --no-daemon
ros2 topic info /cmd_vel --verbose --no-daemon
ros2 topic info /ecu --verbose --no-daemon
ros2 topic info /vehicle_status --verbose --no-daemon
```

预期 `/cmd_vel` 只有一个发布者，节点名为 `cmd_vel_publisher`；`/ecu` 只有一个发布者，来自 `cmdvel_to_ecu`。发现数量不符时，不进行实车运动测试。

## 7. 验收步骤

### 7.1 无实车软件验证

1. 使用模拟消息验证 `require_localization=true/false` 两套行为。
2. 确认关闭定位门控不会关闭底盘反馈、自动驾驶使能、限幅和超时保护。
3. 确认默认配置和现有 Nav2 测试行为不变。
4. 确认命令行参数能够覆盖配置文件中的默认值。
5. 确认构建后的安装文件包含新参数实现和所需配置。

### 7.2 台架或封闭场地验证

由具备车辆安全条件的人员执行：

1. 保持车辆静止，确认未启动 LIO 时远程零输入不会被定位门控拦截。
2. 在低速、封闭环境验证远程非零指令。
3. 验证 `is_autodrive=false` 时远程指令不能进入底盘。
4. 验证停止 `/cmd_vel`、断开远程网络和结束 `rd-vehicle` 后的实际行为。
5. 对照 `/vehicle_status` 和独立现场观测，记录制动响应及物理停车。
6. 验证完毕前，不把软件“已发布零速或制动”写成“车辆已经停稳”。

## 8. 需要小车负责人确认的问题

1. 是否允许纯远程驾驶在 LIO 未启动或定位非 `ACTIVE` 时运行？
2. 是否同意 `require_localization` 默认保持 `true`，仅远程专用启动显式设为 `false`？
3. 远程独占期间能否通过现场流程保证 Nav2、充电对接和网页控制不运行？
4. 现场标准启动方式是手工命令还是整车 launch？由哪个入口唯一启动 `cmdvel_2_ecu` 和 `chassis_driver`？
5. `input_timeout_sec=0.20 s` 是否符合当前底盘和远程驾驶的实车要求？
6. `brake=true`、`p_status`、零速目标和实际停车在 JD03 上分别是什么语义？
7. CAN 指令中断后 ECU 的具体行为、超时值和恢复条件是什么？
8. 车辆是否具有已验证的实体急停、人工接管和硬件 watchdog？

## 9. 方案边界

本方案的优点是修改范围小，能够继续使用现有 `/cmd_vel → cmdvel_2_ecu → /ecu → chassis_driver` 链路，并保持导航默认行为不变。

本方案的限制是依靠运行规范保证远程独占，不提供远程与 Nav2 的在线仲裁，也不能解决尚未确认的实体急停、硬件 watchdog 和物理停车契约。完成本方案的软件测试不等于完成实车安全验收。

## 10. 实施状态

| 项目 | 状态 |
| --- | --- |
| 方案整理 | 已完成 |
| 小车负责人评审 | 待进行 |
| 外部 `cmdvel_2_ecu` 源码修改 | 未开始 |
| 配置与测试修改 | 未开始 |
| ROS2 包重新构建 | 未开始 |
| 无实车验证 | 未开始 |
| 台架/封闭场地验收 | 未开始 |

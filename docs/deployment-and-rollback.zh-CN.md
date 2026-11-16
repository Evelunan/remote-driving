# 部署、迁移与回退

适用对象为三个正式应用及控制协议 `rd-control/2`、注册修订 `video-bound/1`。本流程以已通过[独立安装验收](software-validation.zh-CN.md)的六个 wheel 为输入，在各机器仅安装需要的角色。仓库中的模板和本地演练不表示已经部署公网或验收实车。

## 版本和目录

[版本管理工具](../scripts/manage_release.py)提供 `prepare`、`check`、`activate`、`run`。准备不会启动应用，切换不会自动重启服务。

```text
/opt/remote-driving/server-main/          # 每个服务实例独立的安装根目录
  .release.lock                         # 运行与切换互斥
  current -> releases/20261116-b21
  releases/
    20261116-b19/
    20261116-b21/
      manifest.json                     # 完成状态、协议、Python、各文件 SHA-256
      config.json                       # 该版本的有效实例配置；JSON 也是合法 YAML
      requirements.txt                  # 本角色依赖的固定版本和下载哈希
      uv.lock
      acceptance.json
      wheels/                           # 本角色与公共包
      venv/                             # 安装到最终路径的独立环境
/etc/remote-driving/server-main.env      # 凭据引用需要的环境变量
/etc/remote-driving/tls/20261116/         # 外部证书/私钥，按版本保留
/var/lib/remote-driving/server-main/     # 可写运行数据，与版本目录分离
```

包版本目前同为 `0.2.0`，不能仅靠文件名或版本号区分两批构建。使用唯一发布 ID，并保留 wheel、锁文件、验收报告、匹配源码归档和 SHA-256。发布 ID 不可重用，也不能使用保留字 `none`。不在已准备的 venv 中执行升级或修改配置；修改后准备新发布。

`prepare`核对完整验收报告、六个 wheel 哈希和锁文件；从指定源码导出本角色依赖，使用 `uv pip sync --require-hashes --no-build` 安装，不在目标机临时编译依赖。应用 wheel 使用本地路径和哈希，其他角色不会随之安装。配置通过该版本的正式命令解析，所有默认值和路径固化后再做凭据/TLS校验。

`check`和`activate`检查安装文件、配置、基础 Python 可执行文件及启用组件的外部 TLS 文件有无变化，并执行正式 `--check-config`。`run`检查文件后固定解析到一个版本，将其命令与配置一起启动；锁会跨 `exec` 保留，阻止同时启动第二个实例或运行中切换。停止后才能激活目标版本，`--expect-current`防止根据过期状态覆盖别人的选择。`current`通过同目录原子替换更新，切换失败保留此前指向。

哈希用于发现意外漂移，不是数字签名。部署来源、发布工具及清单应由部署管理员管理；服务账号仅有读取权限。锁只能约束通过此工具启动的实例；绕过工具直接运行命令、遗留外部进程及服务管理器重启策略仍需停机流程检查。保留解释器和系统原生库；venv 不能搬到另一条路径，也不是包含操作系统的便携镜像。

## 主机、设备和权限

| 角色 | 系统条件 | 运行账号和边界 |
| --- | --- | --- |
| 统一服务端 | Python 3.12；服务端 wheel 及共享 core/quic；可达 WSS/TCP 和 QUIC/UDP 监听端口 | 独立 `rd-server` 账号；不安装 Qt、OpenCV、aiortc，不需要设备权限 |
| 车辆端 | Python 3.12；相机使用的 V4L2/OpenCV 库；ROS2 的 rclpy 与该 Python 匹配，或对应串口驱动 | 独立 `rd-vehicle` 账号；按实际设备授予 video/dialout 等组权限，使用 `/dev/v4l/by-id/...`、`/dev/serial/by-id/...` |
| 驾驶端 | Python 3.12；Qt 桌面会话、USB 库、显示驱动 | 已登录的桌面用户；USB 设备通过针对 VID/PID 的 udev 规则授权，勿用 root 运行 GUI |

本地验证使用 Python 3.12.16、uv 0.13.8。Ubuntu 24.04 CI 的基础原生库清单为 `libgl1 libegl1 libopengl0 libglib2.0-0t64 libusb-1.0-0`；真实桌面还需平台插件的原生依赖，摄像头和 ROS2 需对应系统包。先在目标机运行配置检查及适用的设备检查，再认定该环境可部署。其他系统/CPU、真实 GPU 与 USB 尚无此批验收证据。

ROS2 发行版尚未由用户确认，不预设发行版名称。使用与 ROS2 安装匹配的 Python 3.12 准备车辆环境；不要通过 pip 安装一个同名包来替代系统 rclpy。按目标环境加载 `setup.bash` 后，检查：

```bash
python3.12 -c 'import sys,rclpy; print(sys.version); print(rclpy.__file__)'
```

车辆服务的[启动包装器](../deploy/run_vehicle.sh)在配置了绝对路径 `RD_ROS_SETUP` 时加载该可信脚本，再启动版本管理工具。`PYTHONPATH`、`LD_LIBRARY_PATH` 等 ROS 环境属于主机契约，不打包进 wheel；更换 ROS 环境后需要重新验收。`ProtectHome=yes`要求服务解释器、ROS 安装和设备配置不要放在用户 home 下；可部署到 `/opt` 或使用系统 Python。避免依赖临时 `/tmp` 的解释器。

服务端 WSS 默认 TCP 8765，QUIC 默认 UDP 4433；生产示例监听回环地址，开放外部访问必须显式设置监听地址、证书名称和防火墙。WebRTC 媒体不经 QUIC 中继：主机候选需要相应 UDP 可达性，跨 NAT 时配置 STUN/TURN 及其地址、端口和凭据。外部 TURN、弱网和真实网络的验收另行执行。

令牌只在环境或秘密管理设施中注入，配置仅保存变量名。systemd 环境文件须限制为 root 读取或按需要由服务组读取；私钥目录使用限定组和访问权限，例如 root:`rd-server`、目录 0750、私钥 0640。公共 CA 可以只读分发。`EnvironmentFile`中的内容不是 shell 脚本；不写命令替换。不同角色只获得需要的令牌。私钥和令牌不进入发布目录、Git 或验收报告。

## 准备和选择发布

先按[软件验收流程](software-validation.zh-CN.md)产生六个 wheel 和 `passed: true` 的报告。部署准备需要 reviewed checkout 或匹配的源码归档；`--source`用于指定它。服务运行仅需已准备目录及标准库版管理工具，不依赖源码仓库、uv 或构建环境。

以下以服务端 `server-main` 为例，命令由具有目标目录写权限的部署账号执行。先按实例配置提供凭据变量，并确认配置引用的外部证书路径有效。`RD_CONFIG`及 `RD_SERVER__...` 等覆盖变量须清除，实例参数全部写入此次配置；工具拒绝这些隐式覆盖。

```bash
python3.12 scripts/manage_release.py prepare \
  --root /opt/remote-driving/server-main \
  --release 20261116-b21 --role server \
  --source /srv/rd-source/04c6f49 \
  --wheels /srv/rd-artifacts/b21/wheels \
  --acceptance /srv/rd-artifacts/b21/wheel-acceptance.json \
  --config /etc/remote-driving/instances/server-main.yaml \
  --python /usr/bin/python3.12

python3.12 scripts/manage_release.py check \
  --root /opt/remote-driving/server-main --release 20261116-b21
python3.12 scripts/manage_release.py activate \
  --root /opt/remote-driving/server-main --release 20261116-b21 \
  --expect-current none
```

车辆选择 `--role vehicle`，驾驶端选择 `--role driving`，各自使用独立根目录和实例配置。准备完成不修改 `current`；失败目录的清单保持 `ready: false`，不能被激活，应检查原因并使用新发布 ID。`--offline`只使用已预热的 uv 缓存；它不是自动下载 wheelhouse 的命令。首次离线安装前在相同平台预热全部依赖和构建工具缓存并验证可安装。**已准备版本之间的回退不需要包源或 uv。**

外部证书必须使用可保留的版本路径。原地覆盖证书或私钥会令旧发布的完整性检查失败；准备新发布并记录新路径。令牌本身不做哈希存档，回退时必须保证快照引用的凭据仍有效且权限匹配；已经撤销的令牌不能为回退而恢复。证书过期、服务端撤销授权、系统库变化等不能通过回切代码解决。

## 服务模板和停止策略

模板位于 [deploy/systemd](../deploy/systemd)。安装模板、创建账号、授予设备权限和启用服务是部署操作，本次改造没有在主机执行它们。

- [rd-server@.service](../deploy/systemd/rd-server@.service)：`server-main`实例默认同时运行两模块；故障重启间隔 3 秒、60 秒最多 5 次。需故障隔离时，建立不同根目录/实例配置，分别指定 `services: [signaling]`和`services: [relay]`，仍使用同一个服务端 wheel。先停共同运行实例，再启用拆分实例，避免端口冲突。
- [rd-vehicle@.service](../deploy/systemd/rd-vehicle@.service)：应用内部管理 control/media 进程及重连；进程监护失败后保持退出，检查设备和停车接口后重新启动。`KillMode=mixed`先通知父进程，让父进程关闭并回收子进程；最终超时由 systemd 处理整个进程组。默认 20 秒停止预算大于当前 5+1+1 秒进程退出预算；修改这些配置需同步校核。
- [rd-driving@.service](../deploy/systemd/rd-driving@.service)：桌面用户服务，发布根目录为 `%h/.local/share/remote-driving/%i`。由图形会话启动并提供 DISPLAY/WAYLAND_DISPLAY、XDG_RUNTIME_DIR 等会话环境；不在系统服务中硬编码其他用户的显示权限。关闭窗口走已验证的异步清理路径；当前 GUI 未处理 Unix SIGTERM 为优雅退出，终止后的远端失效保护仍由各端超时与连接关闭承担。

部署时将 `manage_release.py`、车辆包装器放入 `/usr/local/lib/remote-driving/`，模板放入相应 systemd system/user unit 目录。模板中的 `/usr/bin/python3.12`需对应目标机器的长期保留解释器；修改路径后执行 `systemd-analyze verify`。服务数据写到独立 StateDirectory；如另设 `logging.directory`，须确保服务账号可写且在版本目录以外。

模板使用 `Type=exec`，它不代表身份认证、两模块启动和设备就绪已经通过。启动后检查日志、端口及认证连接，车辆检查两子进程心跳和相机帧，驾驶端检查可信主视角、输入与控制状态。systemd 选项依据官方 [service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html) 文档；固定依赖安装依据 [uv CLI](https://docs.astral.sh/uv/reference/cli/#uv-pip-sync)。

## 从旧目录和旧协议迁移

| 旧内容 | 当前对应 |
| --- | --- |
| `clients/driving`、`clients/vehicle`及工作目录相关启动 | 安装对应应用 wheel，使用 `rd-driving`、`rd-vehicle`正式命令 |
| `servers/webrtc`与`servers/quic`各自配置/日志 | `rd-server`中的`signaling`与`relay`配置；默认同进程，也可两个实例单模块运行 |
| 多份 logger、codec、连接构建和配置加载器 | core/quic/webrtc公共包；不要从旧目录复制文件或设置跨应用 PYTHONPATH |
| 关闭证书校验的 QUIC、明文 ws、固定 TURN 密码 | 配置 CA、服务端名称、WSS、按角色的凭据引用；轮换曾部署的历史私钥与令牌 |
| 旧 HELLO / 12 字节控制包 | 明确版本的 v2 报文和可靠认证注册；需匹配 `video-bound/1`，不可静默混用 |
| UI 回显当成实测速度、底盘零速当成停车确认 | 分开展示执行确认、传感器遥测和停车反馈；真实停车接口未接入时 `confirmed=False` |

迁移前记录三端 session、room、车辆身份、服务地址/TCP/UDP 端口、CA/证书名称、令牌权限与有效期、相机路径、ROS 环境和运动单位。数值配置不是逐字段机械复制：当前速度单位是 m/s、rad/s，默认 5.0/2.0 仅按用户要求保留，尚未实车确认。

先在模拟环境完整验证新三端；现场停止控制并确认由车辆已有物理安全措施保持安全状态后，停止旧入口并检查端口和旧子进程。准备、校验和切换对应三端版本，再启动统一服务端、车辆端和驾驶端。恢复控制必须看到来源匹配且持续新鲜的主视角，并重新产生中立输入；不能沿用迁移前的租约。旧无认证入口仅用于回环历史测试，不作为生产兼容方案；发布工具明确拒绝 `allow_legacy_development: true`。

## 升级与回退

回退选择一个**先前已验证、协议修订相同、配套配置和凭据仍可用**的发布。优先使用本次改造内已记录的版本；基线 `65ec86e`和旧无认证控制不能作为实车回退路径。回退不会保存租约或恢复运动目标。

1. 在升级前，确认旧发布和目标发布均通过 `check`，记录当前发布 ID、制品哈希、外部 TLS 路径及需要停的服务实例。新版本 `prepare`可以提前完成。
2. 结束驾驶会话，释放租约；现场通过已确认的物理措施保证车辆安全。当前软件的零速请求不等于已确认停车。
3. 停止受影响的应用/服务。对于 systemd 实例先执行 `systemctl stop rd-server@server-main`等适用命令，确认退出、子进程回收和端口释放。显式 stop 避免故障重启与切换竞争。
4. 对每个需要回退的角色选择其旧发布。下面服务端例子要求当前确实是 B21：

```bash
python3.12 /usr/local/lib/remote-driving/manage_release.py activate \
  --root /opt/remote-driving/server-main \
  --release 20261116-b19 --expect-current 20261116-b21
```

5. 按服务端、车辆端、驾驶端顺序启动。验证认证、正确房间/车辆来源、控制准备/新中立输入、主视角新鲜度、断联保护及退出清理；任何一项不满足都保持不可驾驶。不同机器间没有跨主机原子切换，维护窗口中逐端核对发布清单。相同修订的单端回退也应先在模拟环境验证该组合。
6. 保留失败候选、日志和第一次失败证据，记录实际回退结果。不要用自动重试覆盖失败，不删除仍需回退的旧 venv、解释器、证书或源码归档。

## 可复现的软件演练与边界

[演练工具](../scripts/smoke_release_rollback.py)在临时目录为三个角色各准备旧/新两个发布，依次启动 A → B → A。使用真实 wheel、锁定依赖、临时证书和随机令牌；服务端验证真实 WSS 加入与 QUIC 认证，车辆验证 mock/synthetic 双进程心跳和 SIGTERM 回收，驾驶端验证 offscreen 窗口事件循环及启动器互斥。每步核对配套配置，并验证运行中切换和重复启动被拒绝。

```bash
python3.12 scripts/smoke_release_rollback.py \
  --old-source /srv/rd-source/e55e4dc \
  --old-wheels /srv/rd-artifacts/b19/wheels \
  --old-acceptance /srv/rd-artifacts/b19/wheel-acceptance.json \
  --new-source /srv/rd-source/04c6f49 \
  --new-wheels /srv/rd-artifacts/b21/wheels \
  --new-acceptance /srv/rd-artifacts/b21/wheel-acceptance.json \
  --offline --output /tmp/rd-rollback.json
```

输出只有全部步骤通过才置 `passed: true`，失败保留此前完成的阶段。这里的演练逐角色执行，不代表三端联动、真实系统服务、真实设备或公网网络全部验收；完整三端模拟另见[控制验证](control-simulation.zh-CN.md)和[视频安全策略](video-safety.zh-CN.md)。ROS2 发行版、真实制动/停车反馈、物理显示、相机/USB权限以及外部 TURN 仍待目标环境确认。

B22 本地[原始报告](validation/release-rollback-b22.json)已通过：旧制品取自 B19（匹配源码 `e55e4dc`），新制品取自 B21（`04c6f49`），九次角色启动/退出及三组配置选择完成。安装环境使用 Python 3.12.16。六套复制环境需要数 GiB 临时空间；本机 `/tmp` 容量不足，实际以 `TMPDIR`指定空间充足且被 Git 忽略的 `artifacts/b22-tmp`后完成，退出时清理。

# 软件回归与独立安装验收

此流程覆盖无硬件软件检查。真实相机、驾驶外设、ROS2、底盘停车反馈、物理显示及网络性能按对应实机项目验收。

## 可复现的本地流程

使用 Python 3.10.12、uv 0.13.8 和仓库 `uv.lock`。第三方运行与测试依赖按锁文件安装；六个 wheel 从临时 sdist 构建，构建后端固定在 [requirements-build.txt](../requirements-build.txt)。

```bash
uv python install 3.10.12
uv sync --all-packages --group dev --locked --python 3.10.12
uv run --no-sync ruff check --isolated --target-version py310 --select E9,F63,F7,F82 apps packages scripts tests
uv run --no-sync pytest -q tests --junitxml=artifacts/pytest.xml
uv run --no-sync python scripts/build_wheels.py --output-dir artifacts/wheels
uv run --no-sync python scripts/verify_wheels.py --wheels artifacts/wheels --output artifacts/wheel-acceptance.json
```

TURN 两项回归还需要本机 coturn：从 `PATH` 查找，或用 `RD_TEST_TURNSERVER` 指定可执行文件，解包的依赖库可用 `RD_TEST_TURN_LIBRARY_DIR` 指定。完整验收设置 `RD_TEST_REQUIRE_TURN=1`，缺少 coturn 将失败；未设置时这两项明确跳过。测试仅启动随机端口的私有回环进程，详见[网络验收](network-validation.zh-CN.md#b27-强制-turn-与中继进程重启)。

构建与独立安装脚本都支持 `--offline`，要求对应 Python、构建后端及锁定依赖已在 uv 缓存中。独立安装不使用根环境的应用包、editable 安装或 `PYTHONPATH`。脚本在临时目录创建三个环境，直接安装本次应用和公共 wheel，第三方依赖受锁文件导出的版本约束限制；完成后清理环境。已有输出报告会先置为未通过，失败时不会残留上一次的成功结论。

静态检查目前阻止语法错误、未定义名称及部分确定的逻辑错误；全量格式、未使用导入及历史风格问题尚未设为门槛。`--isolated` 保证结果不依赖开发者用户目录中的 Ruff 配置。

## 安装检查内容

| 检查 | 验证内容 |
| --- | --- |
| 六个制品 | 包名、版本、完整性；拒绝缺失、重复、额外 wheel、打包凭据及 CSV 日志；记录 SHA-256 |
| 三个独立环境 | 实际从 wheel 安装、文件内容与 wheel 相同、依赖可满足；其他两个应用不存在 |
| 正式命令 | 从仓库外运行帮助、脱敏配置转储、临时凭据/TLS 校验及单组件配置检查 |
| 导入边界 | 应用顶层导入不初始化 Qt、OpenCV 或 ROS；服务端环境不安装 Qt、OpenCV、WebRTC 媒体依赖 |
| 服务端 | 双模块及两个单模块启动；真实 WSS 房间认证、QUIC 注册和无车辆时拒绝租约；停止后 TCP/UDP 端口可重新绑定 |
| 车辆端 | 正式 CLI 的 control+media、control、media 三模式；mock 底盘和合成相机，SIGTERM 后正常退出、所有子进程回收 |
| 驾驶端 | Qt offscreen 创建、显示和关闭主窗口；正式命令 SIGINT/SIGTERM 正常退出；完整控制/媒体联动由全量回归覆盖 |

测试只监听回环地址。TLS 证书、私钥和随机令牌只放在自动清理的临时目录中，报告不包含令牌或私钥。车辆脚本使用保留的本机端口，不连接已有服务或真实设备。临时环境、运行目录和证书不作为 CI 制品上传。

## CI 与环境矩阵

[GitHub Actions 工作流](../.github/workflows/validation.yml) 在 push、pull_request 或手工触发时执行相同的软件门槛。没有自动发布或部署步骤。权限为只读仓库内容，不持久化检出凭据；Actions 引用固定提交，失败时也保留已有报告，报告和候选 wheel 保存 14 天。仅 `wheel-acceptance.json` 的 `passed: true` 表示该次独立安装检查全部通过，不能用“下载到了 wheel”判断发布合格。

| 环境 | 状态和边界 |
| --- | --- |
| 本机 Linux/WSL2，x86_64，Python 3.10.12 | 锁定依赖安装成功，349 项回归通过（含 TURN），1 项 Windows 控制台专用测试跳过。六个 wheel 构建成功，三个角色均通过独立安装验收。 |
| 本机 Linux/WSL2，x86_64，Python 3.12.16 | 以 `--locked` 建立全新开发环境，当时全量 310 项回归通过、无跳过；独立安装重复检查通过，B19 首次车辆退出失败尚待定位（详见下文） |
| GitHub 托管 Ubuntu 24.04，x86_64，Python 3.10.12 | 工作流已配置；本次仅本地提交，尚未推送或执行远端 Actions。远端 apt/包源及 runner 的执行结果仍需首次运行确认 |
| 车辆 ROS2 + Python 3.10.12 | 用户已确认技术路线；ROS2 发行版、系统依赖、实际底盘停车接口仍待实机验证 |
| 真实桌面 / GPU / 摄像头 / USB | offscreen 与合成设备通过不能代替对应验收 |
| 其他系统、CPU 架构或 Python 版本 | 尚无本项目验收证据；包元数据的最低 Python 要求不表示这些组合已经通过 |

Linux offscreen 仍需要 Qt/OpenCV 原生库。工作流安装 `libgl1`、`libegl1`、`libopengl0`、`libglib2.0-0t64` 和 `libusb-1.0-0`，另安装 coturn 并预先屏蔽其系统服务启动；真实桌面、相机及 ROS2 依赖另按目标系统准备。CI 使用锁文件校验，依赖变更必须显式更新并重新验证；不会在 CI 中静默更新锁文件。

B17 本地报告见 [独立安装记录](validation/wheel-acceptance-b17.json)。其中 `source_commit` 为开始验证时的父提交，`source_dirty: true` 表示本批验证脚本尚未提交；制品以 SHA-256 标识。Git 提交关联见 [实施记录](refactoring-progress.zh-CN.md)。

工作流配置依据 [uv 的 GitHub Actions 文档](https://docs.astral.sh/uv/guides/integration/github/)、[checkout v7.0.1](https://github.com/actions/checkout/tree/v7.0.1) 和 [upload-artifact v7.0.2](https://github.com/actions/upload-artifact/tree/v7.0.2)。

B19 视频优化后，独立安装首次在车辆退出阶段返回 1；失败记录没有删除。随后复查及额外 12 轮全新环境验收通过，目前不能认定已修复偶发退出问题。[首次失败](validation/wheel-acceptance-b19-first-failed.json)、[复查](validation/wheel-acceptance-b19.json) 和 [B20 重复检查](validation/wheel-acceptance-b20-repeats.json) 均保留。验收入口不自动重试失败，不把后一次通过替代失败原因。

持续重连与资源观测命令、边界及结果见 [视频性能记录](video-performance.zh-CN.md)。

B21 的预算适配固定 aiortc 1.14.0 / PyAV 16.1.0，未升级实际依赖版本；[安装报告](validation/wheel-acceptance-b21.json) 验证独立角色安装、文件一致性及启用编码预算的车辆三模式启动/退出。

发布准备工具进一步将已验收 wheel、依赖哈希、实例配置和协议修订绑定，并在切换前校验安装文件。服务模板和真实 wheel 的升级/回退演练见[部署说明](deployment-and-rollback.zh-CN.md)。

B22 使用 B19/B21 原制品完成三角色 old→new→old 的安装/配置配对、运行互斥、启动和回退，见[原始记录](validation/release-rollback-b22.json)。全量回归为 279 passed（52.28s）。未变更应用实现，复用已验收 wheel；GUI Unix SIGTERM 的优雅清理在后续 B23 完成（历史报告保留原行为）。

B23 的[独立安装记录](validation/wheel-acceptance-b23.json)额外验证正式 `rd-driving`进程事件循环就绪后接收 `SIGTERM`、`SIGINT`均返回 0，未连接驾驶外设或远端服务。

本批全量回归 284 passed（55.66s），B21→B23→B21 的[新一轮回退记录](validation/release-rollback-b23.json)同时保留旧版直接退出和新版正常清理退出的差异。

B24 清理废弃文件后，全量回归为 283 passed（51.07s）：仅移除了未接入正式入口的 OpenGL 实验所对应的 1 项测试，其余运行路径覆盖保留。独立安装校验使用清理后的锁文件（减少 10 个不再使用的包），新 wheel 已确认不带回已删除源码或 DLL。

B25 全量回归为 294 passed（60.48s），新增两种正式车辆拓扑的四路链路/进程资源/控制关联校准，以及比较条件变化和退出不完整的拒绝检查。控制接口按需导入修复已重新构建六个 wheel，并通过[三角色独立安装验收](validation/wheel-acceptance-b25.json)。四路正式尺寸的两组各三轮性能记录见[进程拓扑测量](video-performance.zh-CN.md#b25-正式车辆进程拓扑的测量)；不将短时正常退出作为 B19 首次退出失败已修复的证明。

B26 新增 UDP 注入工具校准 2 项、实际媒体/控制恢复场景 8 项，全量 304 passed（96.51s）。[异常网络验收](network-validation.zh-CN.md)记录轻度扰动、仅剩可靠 RPC、两类黑洞、时钟预算及三种有序服务重启；明确保留 TURN、饱和带宽与真实进程/设备故障边界。本批没有修改应用源码或锁文件，B25 的 wheel 和独立安装证据继续适用。

B24 的[安装报告](validation/wheel-acceptance-b24.json)三角色均通过，包含车辆三种运行模式和驾驶端 SIGINT/SIGTERM 检查。

B27 增加 4 项失效清理竞态、2 项真实 coturn 场景，并增强原有双接收端实际媒体测试：一个接收端失效后会重建自身连接，另一个保留原连接和相机源。设置 `RD_TEST_REQUIRE_TURN=1` 的全量结果为 310 passed（109.23s），没有跳过。六个新 wheel 的 115 个 Python 文件与源码一致，[三角色独立安装报告](validation/wheel-acceptance-b27.json)通过，锁文件未变化。默认 ICE 时序下的 TURN 重启原始记录和修复前失败均保留；软件已请求零速不表示真实底盘已停车。

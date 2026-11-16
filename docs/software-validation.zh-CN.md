# 软件回归与独立安装验收

此流程覆盖无硬件软件检查。真实相机、驾驶外设、ROS2、底盘停车反馈、物理显示及网络性能按对应实机项目验收。

## 可复现的本地流程

使用 Python 3.12.16、uv 0.13.8 和仓库 `uv.lock`。第三方运行与测试依赖按锁文件安装；六个 wheel 从临时 sdist 构建，构建后端固定在 [requirements-build.txt](../requirements-build.txt)。

```bash
uv python install 3.12.16
uv sync --all-packages --group dev --locked --python 3.12.16
uv run --no-sync ruff check --isolated --select E9,F63,F7,F82 apps packages scripts tests
uv run --no-sync pytest -q tests --junitxml=artifacts/pytest.xml
uv run --no-sync python scripts/build_wheels.py --output-dir artifacts/wheels
uv run --no-sync python scripts/verify_wheels.py --wheels artifacts/wheels --output artifacts/wheel-acceptance.json
```

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
| 驾驶端 | Qt offscreen 创建、显示和关闭主窗口；完整控制/媒体联动由全量回归覆盖 |

测试只监听回环地址。TLS 证书、私钥和随机令牌只放在自动清理的临时目录中，报告不包含令牌或私钥。车辆脚本使用保留的本机端口，不连接已有服务或真实设备。临时环境、运行目录和证书不作为 CI 制品上传。

## CI 与环境矩阵

[GitHub Actions 工作流](../.github/workflows/validation.yml) 在 push、pull_request 或手工触发时执行相同的软件门槛。没有自动发布或部署步骤。权限为只读仓库内容，不持久化检出凭据；Actions 引用固定提交，失败时也保留已有报告，报告和候选 wheel 保存 14 天。仅 `wheel-acceptance.json` 的 `passed: true` 表示该次独立安装检查全部通过，不能用“下载到了 wheel”判断发布合格。

| 环境 | 状态和边界 |
| --- | --- |
| 本机 Linux/WSL2，x86_64，Python 3.12.16 | 以 `--locked` 建立全新开发环境，全量 235 项回归通过；三个干净角色环境的安装和生命周期检查通过 |
| GitHub 托管 Ubuntu 24.04，x86_64，Python 3.12.16 | 工作流已配置；本次仅本地提交，尚未推送或执行远端 Actions。远端 apt/包源及 runner 的执行结果仍需首次运行确认 |
| 车辆 ROS2 + Python 3.12 | 用户已确认技术路线；ROS2 发行版、系统依赖、实际底盘停车接口仍待实机验证 |
| 真实桌面 / GPU / 摄像头 / USB | offscreen 与合成设备通过不能代替对应验收 |
| 其他系统、CPU 架构或 Python 版本 | 尚无本项目验收证据；包元数据的最低 Python 要求不表示这些组合已经通过 |

Linux offscreen 仍需要 Qt/OpenCV 原生库。工作流安装 `libgl1`、`libegl1`、`libopengl0`、`libglib2.0-0t64` 和 `libusb-1.0-0`；真实桌面、相机及 ROS2 依赖另按目标系统准备。CI 使用锁文件校验，依赖变更必须显式更新并重新验证；不会在 CI 中静默更新锁文件。

B17 本地报告见 [独立安装记录](validation/wheel-acceptance-b17.json)。其中 `source_commit` 为开始验证时的父提交，`source_dirty: true` 表示本批验证脚本尚未提交；制品以 SHA-256 标识。Git 提交关联见 [实施记录](refactoring-progress.zh-CN.md)。

工作流配置依据 [uv 的 GitHub Actions 文档](https://docs.astral.sh/uv/guides/integration/github/)、[checkout v7.0.1](https://github.com/actions/checkout/tree/v7.0.1) 和 [upload-artifact v7.0.2](https://github.com/actions/upload-artifact/tree/v7.0.2)。

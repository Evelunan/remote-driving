# 远程驾驶系统

Python 3.12 工作区，包含三个可独立安装的应用：

| 应用 | 包名 / 命令 | 职责 |
| --- | --- | --- |
| [驾驶端](apps/driving/README.md) | rd-driving / rd-driving | PyQt6 界面、驾驶外设、WebRTC 接收与 QUIC 控制 |
| [车辆端](apps/vehicle/README.md) | rd-vehicle / rd-vehicle | 相机、WebRTC 推流、控制监护及 ROS2/串口适配 |
| [统一服务端](apps/server/README.md) | rd-server / rd-server | 可共同或分别启动的信令与 QUIC 中继模块 |

改造仍在实施中。统一配置已接入服务端，信令已支持身份与房间授权，中继已实现认证注册与独占租约；新版两端控制客户端已通过无硬件完整链路验证；三个正式入口已统一配置并接入认证控制/媒体；车端控制/媒体已分进程；主视角失效联动已接入；完整视频性能和部署验收尚未完成。具体状态见[实施记录](docs/refactoring-progress.zh-CN.md)，设计依据见[改造方案](docs/refactoring-plan.zh-CN.md)，配置规则见[配置说明](docs/configuration.zh-CN.md)。

## 目录

```text
apps/
  driving/src/rd_driving/
  vehicle/src/rd_vehicle/
  server/src/rd_server/{signaling,relay}/
packages/
  rd-core/src/rd_core/       # 控制监护、配置、认证、采样契约、旧协议和日志
  rd-webrtc/src/rd_webrtc/   # 共享连接封装与 ICE 解析
  rd-quic/src/rd_quic/       # v2 可靠 RPC 与有界 QUIC 适配
examples/legacy/            # 待迁移的历史实验，不是正式入口
benchmarks/legacy/          # 历史数据，不作为本次性能测量结果
tests/
docs/
```

应用边界代表安装和部署职责；公共代码不按部署机器复制。每个 wheel 使用唯一命名空间，不再依赖切换工作目录才能导入。

## 开发与回归

```bash
uv sync --all-packages --group dev --locked --python 3.12.16
uv run --no-sync pytest -q tests
uv run --no-sync rd-driving --help
uv run --no-sync rd-vehicle --help
uv run --no-sync rd-server --help
```

完整 v2 控制模拟及报告说明见[无硬件验证](docs/control-simulation.zh-CN.md)。

CI、独立 wheel 安装检查和环境矩阵见[软件验收说明](docs/software-validation.zh-CN.md)。

测试包含模拟输入/底盘、Qt offscreen、临时证书和仅绑定回环地址的监听器。运行环境需要允许本机 socketpair 和回环 socket；这些测试不连接实车或外部服务。

## 构建与独立安装

```bash
python scripts/build_wheels.py --output-dir dist
# 将 dist 中相应应用及公共包 wheel 交给目标机器后安装：
python -m pip install --find-links dist rd-server==0.2.0
# 驾驶端用 rd-driving，车辆端用 rd-vehicle；无需安装另外两个应用。
```

构建出的应用 wheel 依赖公共包 wheel；第三方运行依赖仍需通过包源或预备 wheelhouse 安装。ROS2 的 rclpy 由匹配 Python 3.12 的 ROS 环境提供；发行版及实车接口仍待确认。

应用不再在导入时初始化日志或清空代理变量。入口显式配置日志；诊断输出目录可由 `RD_RUNTIME_DIR` 指定。相机、显示器、USB、串口、ROS2 和真实网络的验收与软件模拟结果分开记录。

服务器私钥和固定 TURN 凭据已从当前树移除。曾实际部署的历史凭据需要部署方轮换，Git 历史没有被改写。

仓库目前未声明许可证。

# 驾驶端

正式包名为 `rd-driving`，Python 命名空间为 `rd_driving`，入口为 `rd-driving`。

```bash
uv run --package rd-driving rd-driving
uv run --package rd-driving rd-driving --session 1234 --room 1234
```

当前开发模式保留原有房间/会话参数；它们不是身份认证。可靠认证与新版控制协议仍在实施，不作为实车部署完成态。

输入在 USB 读取完成时记录采样时间，经最新值队列按固定周期发送。输入失效或过期触发 STOP，车端超时监护提供后续保护。界面显示控制器接口执行确认与采样至确认耗时，实测车速和停车反馈尚未接入。

暂存配置位于 `src/rd_driving/config`，统一配置入口将在后续阶段替换它。当前示例使用回环服务地址、无固定 TURN 凭据、QUIC 证书验证开启。日志由入口配置，导入库不会创建日志文件。

PyQt6 需要可用的显示环境；安装验证覆盖 wheel、命令帮助和 offscreen 登录窗口，不代表实际 GPU/多路视频验收。完整构建命令见[根 README](../../README.md)，进度见[实施记录](../../docs/refactoring-progress.zh-CN.md)。

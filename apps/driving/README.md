# 驾驶端

正式包名为 `rd-driving`，Python 命名空间为 `rd_driving`，入口为 `rd-driving`。使用 Python 3.12。

```bash
uv run --package rd-driving rd-driving --config configs/driving.yaml --dump-config
uv run --package rd-driving rd-driving --config configs/driving.yaml --check-config
uv run --package rd-driving rd-driving --config configs/driving.yaml --connect
```

[配置示例](../../configs/driving.yaml) 需要部署方提供 CA 文件与 `RD_OPERATOR_TOKEN`。`--dump-config` 不读取秘密值；`--check-config` 校验配置、凭据和 TLS，不启动 GUI 或打开 USB。默认打开会话选择页面；`--connect` 使用配置的 session/room 连接，或同时提供 `--session 1234 --room 1234`。会话和房间是独立资源 ID，真实授权由服务端核验，页面确认参数不代表认证成功。

正式入口使用 `rd_core` 不可变配置和 v2 QUIC 客户端；旧模块内 YAML 与单例加载器已删除。USB VID/PID、界面主题/尺寸/刷新周期、网络及凭据引用均由同一配置实例注入，优先级和迁移字段见[配置说明](../../docs/configuration.zh-CN.md)。

输入在 USB 读取完成时记录采样时间，经最新值队列按固定周期发送。有效中立输入及车端执行确认后才允许运动；输入失效或确认过期撤销控制。界面分别显示控制器接口执行确认、采样至确认耗时和已收到的实测遥测；实测值过期/断联清除，不用控制目标冒充车速。当前真实车速与停车反馈尚未接入。

PyQt6 需要可用显示环境。OpenCV 导入封装保留用户原有 Qt 插件/字体路径，避免第三方 wheel 覆盖；offscreen 验证不代表真实桌面、GPU 或设备验收。构建命令见[根 README](../../README.md)，剩余工作见[实施记录](../../docs/refactoring-progress.zh-CN.md)。

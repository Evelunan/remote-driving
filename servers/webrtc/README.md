### webrtc - WebRTC 信令服务器

用于 WebRTC 连接的信令协商，支持多房间、多对等连接管理。

```
webrtc/
├── main.py              # 程序入口
├── server/              # 服务器实现
│   └── signaling_server.py  # 信令服务器
├── services/            # 服务层
│   └── room_manager.py  # 房间管理
├── models/              # 数据模型
│   ├── peer.py          # 对等端模型
│   └── room.py          # 房间模型
├── config/              # 配置
│   ├── server.yaml      # 服务器配置
│   └── log.yaml         # 日志配置
└── common/              # 公共常量
```

## 打包可执行文件

使用 nuitka 打包成独立可执行文件：

```bash
# 安装 nuitka
uv add --dev nuitka

# 打包（config目录会打包进可执行文件）
uv run python -m nuitka main.py `
  --standalone `
  --output-dir=dist `
  --output-filename=webrtc_server `
  --include-data-dir=config=config `
  --lto=yes `
  --jobs=8 `
  --python-flag=-O `
  --python-flag=no_site `
  --windows-console-mode=force `
  --assume-yes-for-downloads
```

打包后在 `dist/` 目录生成 `webrtc_server(.exe)` 可执行文件。
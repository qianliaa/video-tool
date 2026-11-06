# videotool

基于 ffmpeg + yt-dlp 的视频合并与下载工具，提供 Web 界面，可打包为飞牛 OS（fnOS）应用。

## 功能

- 视频合并（无损复制 / 强制重编码）
- 视频下载（m3u8、网页链接，yt-dlp 支持的大部分站点）
- 网页实时日志

## 打包

```bash
python pack.py
```

输出 `video-tool.fpk`，在飞牛应用中心手动安装即可。

打包脚本会：

- 将 `fpk/app/` 的内容打进 `app.tgz`，安装后映射到 `TRIM_APPDEST`（target）
- 统一 `cmd/` 脚本为 LF 换行并设置 755 权限
- 只对文本文件做换行/BOM 规范化，避免破坏 PNG 和 `yt-dlp` 等二进制文件

## 运行依赖

- Python 3.11（飞牛应用中心的 Python 3.11，manifest 中通过 `install_dep_apps` 声明）
- 系统 ffmpeg
- `fpk/app/bin/yt-dlp` 已随包内置，无需额外安装

后端只使用 Python 标准库，不依赖 Flask / pip / venv，安装后即可启动。

## 目录

```
fpk/
├── manifest            # 应用信息
├── ICON.PNG            # 64x64
├── ICON_256.PNG        # 256x256
├── app/                # 安装后 = target
│   ├── server/app.py   # 标准库 HTTP 后端
│   ├── bin/yt-dlp      # 内置下载器
│   └── ui/             # 前端页面与桌面入口配置
├── cmd/                # 生命周期脚本
├── config/             # privilege / resource
└── wizard/
```

## CI

推送到 `main` 或打 `v*` 标签时，GitHub Actions 会自动执行 `python pack.py` 并上传 fpk 产物；打标签时会附加到 Release。

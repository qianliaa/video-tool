# video-tool

基于 ffmpeg 的视频处理工具箱，提供 Web 界面，可打包为飞牛 OS（fnOS）应用。

## 功能

### 视频处理（ffmpeg）

- **合并**：按顺序无损拼接多个视频，或强制统一重新编码
- **格式转换**：MP4 / MKV / MOV / WEBM / AVI，以及 MP3 / M4A / WAV / FLAC 音频
- **压缩**：CRF 画质、编码速度、缩放
- **剪辑**：按时间截取片段，支持无损复制
- **分辨率**：自定义宽高或常用 4K / 1080p / 720p / 480p
- **旋转翻转**：90° / 180° / 270°、水平 / 垂直翻转
- **提取音频**：MP3 / M4A / AAC / WAV / FLAC / OPUS / OGG
- **字幕烧录**：srt / ass / ssa / vtt 直接压制进画面
- **视频截图**：JPG / PNG
- **自定义命令**：使用 `{input}` / `{output}` 占位符执行任意 ffmpeg 参数

### 链接下载（ffmpeg 原生）

应用内置下载能力完全由 ffmpeg 实现，不再依赖也不再内置 yt-dlp。

- 直链媒体：http/https 下的 mp4 / mkv / webm / mov / ts / flv 等
- HLS：解析 m3u8 主播放列表，按画质选择子流
- DASH：读取 mpd 清单下载
- 页面解析：从网页的 `og:video`、`<video>` / `<source>`、`contentUrl` 等公开字段提取媒体地址，支持一层 iframe
- 视频原样封装 / 仅音频转码，可选 MP3 / M4A / AAC / WAV / FLAC / OPUS / OGG
- 自定义 UA、Referer、请求头与代理

受 ffmpeg 能力限制，需要登录、DRM 解密或站点私有协议的视频无法下载；这类站点需要专门的站点解析器，属于 yt-dlp 的站点提取器范畴，ffmpeg 本身不具备。

### 界面与任务

- 现代响应式界面，支持浅色 / 深色主题
- 桌面侧边导航 + 手机底部导航，适配窄屏与安全区域
- 支持添加到手机主屏幕（PWA manifest 与多尺寸图标）
- 输入 / 输出目录由用户在已授权文件夹中自行选择，也可直接粘贴原始路径
- 输入目录自动递归遍历子文件夹，文件列表显示相对路径
- 任务进度、实时日志、运行中取消
- 所有处理均在本机完成，不上传任何文件

## 访问方式

应用不内置密码或登录，启动后打开 Web 界面即可直接使用，所有接口无需认证。

- 从 1.3.7 起移除了访问密码、会话 Cookie、Host 白名单与 Origin / Content-Type 校验，反向代理或自定义域名访问不会再被误判为 403。
- 升级前遗留的 `${TRIM_PKGVAR}/auth.json` 不会再被读取或校验，也不会导致启动报错，可保留或自行删除。
- `VT_ALLOWED_HOSTS`、`VT_RESET_AUTH` 已不再使用。
- 如果应用暴露在公网，请依赖 fnOS 的反向代理 / 网关做访问控制，或在网络层限制来源 IP。
- 实际配置目录可在应用「设置 → 配置目录」中查看，fnOS 上通常为 `/vol1/@appdata/video-tool`。

## 目录设置

- **输入目录**：递归读取待处理的视频、音频与字幕文件
- **输出目录**：保存所有处理结果，输出文件名支持子目录（如 `out/result.mp4`）
- 应用不内置输入 / 输出目录，首次使用需自行设置
- 先在 fnOS「系统设置 › 应用 › 视频助手 › 访问权限」中授权目标文件夹，再在应用内选择；看不到文件夹时，可直接从「文件管理」复制原始路径粘贴到设置或选择器中
- 路径必须是完整绝对路径（如 `/vol1/1000/视频`），并具备输入读取、输出写入权限

目录配置保存在 `${TRIM_PKGVAR}/config.json`。

## 检查更新

设置页「关于与更新」卡片可查询 GitHub Releases（仓库 `qianliaa/video-tool`），与当前版本比较并提示是否已是最新；有新版本时给出 `.fpk` 下载链接。未登录 GitHub API 有每小时 60 次的限流，网络失败或限流都会在卡片内给出明确提示，不影响任务与其他功能。可通过环境变量 `VT_GITHUB_TOKEN` 提升限额。

发布者与开发者信息也展示在同一卡片中，项目主页：<https://github.com/qianliaa/video-tool>。

## 打包

```bash
python pack.py
```

输出 `video-tool-<版本>.fpk`（版本取自 `fpk/manifest` 的 `version`，例如 `video-tool-1.3.7.fpk`），在飞牛应用中心手动安装即可。

打包脚本会：

- 将 `fpk/app/` 的内容打进 `app.tgz`，安装后映射到 `TRIM_APPDEST`（target）
- 统一 `cmd/` 脚本为 LF 换行并设置 755 权限
- 只对文本文件做换行/BOM 规范化，避免破坏 PNG 等二进制文件

## 运行依赖

- Python 3.11（飞牛应用中心的 Python 3.11，manifest 中通过 `install_dep_apps` 声明）
- ffmpeg（含 ffprobe）：使用系统已安装的 ffmpeg，按 PATH、`/usr/bin`、`/usr/local/bin`、飞牛应用目录依次查找；找不到时会给出明确错误
- 不再内置 yt-dlp，也没有其他第三方 Python 依赖

后端只使用 Python 标准库，不依赖 Flask / pip / venv，安装后即可启动。

## 目录

```
fpk/
├── manifest                # 应用信息
├── ICON.PNG                # 64x64
├── ICON_256.PNG            # 256x256
├── app/                    # 安装后 = target
│   ├── server/app.py       # 标准库 HTTP 后端
│   └── ui/                 # 前端页面、PWA manifest 与桌面入口配置
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       ├── manifest.webmanifest
│       └── images/         # 64 / 180 / 192 / 256 / 512 图标
├── cmd/                    # 生命周期脚本
├── config/                 # privilege / resource
└── wizard/
```

## CI

推送到 `main` 或打 `v*` 标签时，GitHub Actions 会自动执行 `python pack.py` 并上传 fpk 产物；打标签时会附加到 Release。

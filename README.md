# video-tool

基于 ffmpeg 的视频处理工具箱，提供 Web 界面，可打包为飞牛 OS（fnOS）应用。

当前版本：**1.7.3**。打开 `http://<NAS>:18080/` 即可使用，**无需登录、无账号体系**。

## 功能

### 视频处理（ffmpeg）

- **合并**：按顺序无损拼接多个视频，或强制统一重新编码；输出文件名自动识别输入文件名的公共部分并预填
- **格式转换**：MP4 / MKV / MOV / WEBM / AVI，以及 MP3 / M4A / WAV / FLAC 音频
- **压缩**：CRF 画质、编码速度、缩放
- **剪辑**：按时间截取片段，支持无损复制
- **分辨率 / 旋转翻转 / 截图**：自定义宽高或常用档位、90°/180°/270° 旋转、JPG / PNG 截图
- **提取音频**：MP3 / M4A / AAC / WAV / FLAC / OPUS / OGG
- **字幕烧录**：srt / ass / ssa / vtt 直接压制进画面
- **自定义命令**：使用 `{input}` / `{output}` 占位符执行 ffmpeg 参数（受选项白名单约束）

### 链接下载（ffmpeg 原生）

直链媒体、HLS(m3u8) 画质选择、DASH(mpd)、网页公开媒体地址（`og:video`、`<video>` / `<source>`、`contentUrl`、iframe）；视频原样封装 / 仅音频转码；自定义 UA、Referer、请求头与代理。需要登录、DRM 或站点私有协议的地址不支持。

### 界面与任务

- 浅色 / 深色主题，桌面侧边导航 + 手机底部导航，PWA 图标
- 任务进度、实时日志、运行中取消、排队状态
- 提交任务后不弹日志遮罩，可连续添加多个任务；日志在「任务」页按需查看
- 下载与转码使用独立并发队列（默认 3 / 2，可用 `VT_MAX_DOWNLOADS` / `VT_MAX_TASKS` 调整）
- 同名输出自动追加 `(1)`、`(2)`，不会覆盖已有文件
- 所有处理均在本机完成，不上传任何文件

## 目录设置

- 先在 fnOS「系统设置 › 应用 › 视频助手 › 访问权限」中授权目标文件夹
- 应用内的输入 / 输出目录**只能从已授权目录里浏览选择**，不接受手填路径；授权变更后无需重启
- 输入目录递归读取子文件夹，输出文件名支持子目录（如 `out/result.mp4`）
- 输入与输出**可以是同一目录**
- 配置保存在 `${TRIM_PKGVAR}/config.json`；应用自身目录不可作为输入 / 输出
- 安装与授权变更时，`cmd/acl_apply` 只为已授权目录补齐应用运行用户的读写 ACL

> 服务默认监听 `0.0.0.0:18080`，免登录且无 TLS / CSRF 校验，请只在可信网络使用，或通过网关 / 防火墙限制访问。

## 检查更新

设置页「关于与更新」查询本仓库的 GitHub Releases，比较版本并给出 `.fpk` 下载链接。未登录 GitHub API 每小时限 60 次，可通过环境变量 `VT_GITHUB_TOKEN` 提升限额。

## 打包

```bash
python pack.py
```

`pack.py` 转发到 `build_fpk.py`（`--src fpk --inner-src fpk/app`），输出 `video-tool-<版本>.fpk`，版本取自 `fpk/manifest`。打包采用固定白名单 + 手工 TarInfo + 手写 gzip 头，保证无 `__pycache__` 杂质、权限正确（目录与 `cmd/*` 0755、其余 0644）、LF 换行、可复现构建；构建后会读回逐项自检。

## 运行依赖

- Python 3.8+（优先使用飞牛应用中心的 Python 3.11/3.12/3.10/3.9）
- ffmpeg（含 ffprobe）：按 PATH、`/usr/bin`、`/usr/local/bin`、飞牛应用目录依次查找
- 后端只用 Python 标准库，不依赖 Flask / pip / venv，也不需要 yt-dlp

## 目录

```
fpk/
├── manifest                # 应用信息与版本
├── ICON.PNG / ICON_256.PNG # 图标
├── app/                    # 安装后 = target（打进 app.tgz）
│   ├── server/app.py       # 标准库 HTTP 后端
│   └── ui/                 # 前端页面、PWA manifest、桌面入口与图标
├── cmd/                    # 生命周期脚本（含 acl_apply）
└── config/                 # privilege / resource
```

## CI

推送到 `main` 或打 `v*` 标签时，GitHub Actions 自动执行 `python pack.py` 并上传 fpk 产物；**打标签时会自动创建 Release 并附上 fpk**。

#!/usr/bin/env bash
# 视频助手 独立桌面版（Linux）—— 免安装绿色运行
set -e
export VT_STANDALONE=1
export VT_BIND_HOST=127.0.0.1
export VT_BIND_PORT=18080

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/video-tool" --open

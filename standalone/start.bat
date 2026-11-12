@echo off
setlocal
REM 视频助手 独立桌面版（Windows）—— 免安装绿色运行
set VT_STANDALONE=1
set VT_BIND_HOST=127.0.0.1
set VT_BIND_PORT=18080

"%~dp0video-tool.exe" --open

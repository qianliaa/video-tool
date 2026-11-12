视频助手 独立桌面版（Windows / Linux）
========================================

这是「视频助手」的免安装绿色版本，把 Web 服务与 ffmpeg 一起打包，
解压后无需安装 Python，也不需要飞牛（fnOS）环境，可直接在本机使用。

使用方法
--------
Windows：双击 start.bat
Linux  ：在终端执行 ./start.sh

启动后会自动打开浏览器，访问 http://127.0.0.1:18080/ 。
服务只监听本机回环地址（127.0.0.1），不会暴露到局域网；
关闭窗口 / 按 Ctrl+C 即停止。请在界面里「浏览」选择本机任意文件夹作为输入 / 输出目录。

目录说明
--------
video-tool(.exe)   主程序（已内嵌前端页面）
bin/ffmpeg(.exe)   视频处理引擎（随包附带，无需另行安装）
bin/ffprobe(.exe)  媒体信息探测（随包附带）
start.bat/start.sh 启动脚本（设置独立模式并打开浏览器）
README.txt         本说明

配置与数据
----------
配置（输入 / 输出目录等）保存在当前用户目录：
  Windows：%USERPROFILE%\.video-tool\
  Linux  ：~/.video-tool\
删除该目录即可重置所有设置。

高级选项（可选，作为环境变量或启动参数）
-----------------------------------------
VT_BIND_PORT     监听端口，默认 18080
VT_BIND_HOST     监听地址，默认 127.0.0.1（仅本机）
VT_MAX_TASKS     同时转码任务数，默认 2（1-8）
VT_MAX_DOWNLOADS 同时下载任务数，默认 3（1-8）

也可直接运行主程序并加参数，例如：
  video-tool.exe --host 127.0.0.1 --port 18080 --open

说明
----
所有视频处理均在本地完成，不会上传任何文件。
本程序基于 ffmpeg，遵循其许可证；ffmpeg 静态构建来自 gyan.dev（Windows）
或系统发行版（Linux）。

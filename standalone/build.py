# -*- coding: utf-8 -*-
"""跨平台 PyInstaller 单文件构建：把原生操作面板与服务端一起冻结成独立可执行文件。

用法：python standalone/build.py
产物：dist/video-tool(.exe)，已内嵌前端 ui 目录；双击即弹出原生面板。

入口必须是 launcher.py：它先设置独立模式环境变量，再启动本机服务与
Tkinter 面板。若直接用 app.py 作入口，桌面版会退回浏览器界面且缺少面板。

ffmpeg / ffprobe 由 CI 在打包阶段拷贝到 exe 同级的 bin/ 目录
（Windows 下载 gyan.dev 静态构建，Linux 取自系统 apt 安装的 ffmpeg），
运行时 app.py 会优先使用 bin/ 下的 ffmpeg。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = ";" if os.name == "nt" else ":"
UI_ARG = os.path.join("fpk", "app", "ui") + SEP + "ui"
SERVER = os.path.join("fpk", "app", "server")
SCRIPT = os.path.join("standalone", "launcher.py")
DIST = "dist"


def main():
    os.chdir(ROOT)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", "video-tool",
        "--paths", SERVER,
        "--add-data", UI_ARG,
        "--noconfirm",
        "--clean",
        "--distpath", DIST,
        "--workpath", "build",
        SCRIPT,
    ]
    print("[build] " + " ".join(cmd))
    subprocess.check_call(cmd)
    out = os.path.join(DIST, "video-tool.exe" if os.name == "nt" else "video-tool")
    print("[build] 产出：" + os.path.abspath(out))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""跨平台 PyInstaller 单文件构建：把 app.py 与前端 ui 打包成独立可执行文件。

用法：python standalone/build.py
产物：dist/video-tool(.exe)，已内嵌前端 ui 目录。

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
SCRIPT = os.path.join("fpk", "app", "server", "app.py")
DIST = "dist"


def main():
    os.chdir(ROOT)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "video-tool",
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

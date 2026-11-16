# -*- coding: utf-8 -*-
"""Windows / Linux 桌面独立版启动器：纯原生 Tkinter 操作面板（单机版）。

双击 video-tool(.exe) 即进入独立模式（绕过 fnOS 授权），在后台线程启动本机
HTTP 服务（127.0.0.1:18080），随后弹出原生 Tkinter 操作面板。所有视频处理
均在本机完成，ffmpeg 随包内置，全程不连接任何外网、不打开浏览器。
"""
import os
import sys
import time
import socket
import threading

# ---- 在任何模块导入前强制设定独立桌面模式 ----
os.environ.setdefault("VT_STANDALONE", "1")
os.environ.setdefault("VT_BIND_HOST", "127.0.0.1")
os.environ.setdefault("VT_BIND_PORT", "18080")
os.environ["PYTHONUTF8"] = "1"

HOST = os.environ.get("VT_BIND_HOST") or "127.0.0.1"
PORT = int(os.environ.get("VT_BIND_PORT") or "18080")

# 让 PyInstaller 能定位 server/app.py
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "fpk", "app", "server")))

import app  # noqa: E402  (STANDALONE 已在导入前通过环境变量置真)
import panel  # noqa: E402

_boot_error = {}


def _server_thread():
    """在后台线程启动 HTTP 服务；线程内无法注册信号，已由 app.py 容错。"""
    try:
        sys.argv = ["videotool", "--no-open",
                    "--host", HOST, "--port", str(PORT)]
        app.main()
    except SystemExit:
        pass
    except Exception as exc:  # 启动失败也要让面板给出提示
        _boot_error["msg"] = "服务启动失败：%s" % exc


if __name__ == "__main__":
    threading.Thread(target=_server_thread, daemon=True).start()
    panel.main()

# -*- coding: utf-8 -*-
"""CI 入口：调用确定性打包脚本构建 fpk。

仓库结构为 fpk/（manifest、cmd、config）+ fpk/app/（server、ui），
通过 --inner-src 指定内层根，产物与本地验证过的 fpk 字节一致。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.call([
    sys.executable, os.path.join(BASE, "build_fpk.py"),
    "--src", os.path.join(BASE, "fpk"),
    "--inner-src", os.path.join(BASE, "fpk", "app"),
]))

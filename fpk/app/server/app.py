# -*- coding: utf-8 -*-
"""视频助手后端：仅依赖 Python 标准库。

功能：
  - 输入/输出目录配置与浏览
  - ffmpeg：合并、格式转换、压缩、裁剪、提取音频、字幕、分辨率、旋转、截图、自定义参数
  - ffmpeg 原生下载：直链、HLS(m3u8)、DASH(mpd)、页面公开媒体地址，视频/音频与画质选择
  - 任务进度、日志、取消
"""
import argparse
import codecs
import hashlib
import html as html_lib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urljoin, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BASE_DIR)
UI_DIR = os.path.join(APP_DIR, "ui")
BIN_DIR = os.path.join(APP_DIR, "bin")

APP_VERSION = "1.7.4"
PROJECT_URL = "https://github.com/qianliaa/video-tool"
GITHUB_REPO = "qianliaa/video-tool"
PUBLISHER = "千里"
DEVELOPER = "千里"
UPDATE_TTL = 300
UPDATE_FAIL_TTL = 30
UPDATE_CACHE = {"at": 0.0, "data": None, "ok": False}
UPDATE_LOCK = threading.Lock()

PKGVAR = os.environ.get("TRIM_PKGVAR") or os.path.join(os.path.expanduser("~"), ".video-tool")
CONFIG_PATH = os.path.join(PKGVAR, "config.json")
LIST_DIR = os.path.join(PKGVAR, "lists")
# fnOS 在「应用 › 访问权限」变更授权目录时执行 config 流程，
# config_callback 会把 TRIM_DATA_*_PATHS 落盘到这两个文件，供运行中的服务即时刷新。
ACCESSIBLE_PATHS_FILE = os.path.join(PKGVAR, "accessible_paths")
SHARE_PATHS_FILE = os.path.join(PKGVAR, "share_paths")

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv", ".webm", ".m4v",
              ".wmv", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob", ".ogv", ".rmvb")
AUDIO_EXTS = (".mp3", ".aac", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".wma")
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")

URL_MEDIA_EXTS = (".m3u8", ".mpd", ".mp4", ".mkv", ".mov", ".m4v", ".webm",
                  ".ts", ".flv", ".avi", ".mp3", ".m4a", ".aac", ".flac",
                  ".wav", ".ogg", ".opus")
DOWNLOAD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0 Safari/537.36 VideoTool/%s" % APP_VERSION)
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SCAN_FILES = 5000
MAX_SCAN_DEPTH = 8

VIDEO_CODECS = {
    ".mp4": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    ".mkv": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
    ".mov": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
    ".webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"],
    ".avi": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "mp3", "-b:a", "192k"],
}

AUDIO_CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".ogg": ["-c:a", "libvorbis", "-b:a", "192k"],
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

TASKS = {}
TASKS_LOCK = threading.Lock()
# 同时跑的 ffmpeg 任务上限。NAS 上多个 libx264 并发会打满 CPU/内存，
# 超出的任务进入排队状态，可用环境变量 VT_MAX_TASKS 调整（1-8）。
try:
    MAX_CONCURRENT_TASKS = max(1, min(8, int(os.environ.get("VT_MAX_TASKS", "2"))))
except ValueError:
    MAX_CONCURRENT_TASKS = 2
TASK_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_TASKS)
# 下载是网络 IO，与 CPU 密集的转码分开排队；可用 VT_MAX_DOWNLOADS 调整（1-8）。
try:
    MAX_CONCURRENT_DOWNLOADS = max(1, min(8, int(os.environ.get("VT_MAX_DOWNLOADS", "3"))))
except ValueError:
    MAX_CONCURRENT_DOWNLOADS = 3
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)
# 请求体上界：避免超大/慢速 body 占满线程与内存。
MAX_REQUEST_BYTES = 1 * 1024 * 1024

FFMPEG_BIN = None
FFPROBE_BIN = None
FFMPEG_ERROR = ""

CONFIG_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_system_bin(name, exclude_dir=None):
    """在系统路径中查找二进制；exclude_dir 用于把应用内置目录排除在外。"""
    path = shutil.which(name)
    if path:
        rp = os.path.realpath(os.path.abspath(path))
        if not exclude_dir or not (rp == exclude_dir or rp.startswith(exclude_dir + os.sep)):
            return path
    candidates = ["/usr/bin/" + name, "/usr/local/bin/" + name]
    for app in ("ffmpeg", "ffmpeg7", "ffmpeg6"):
        candidates.append("/var/apps/%s/target/bin/%s" % (app, name))
    for cand in candidates:
        if _executable(cand):
            return cand
    return None


def find_ffprobe_near(ffmpeg):
    base = os.path.dirname(os.path.abspath(ffmpeg))
    names = ["ffprobe.exe", "ffprobe"] if os.name == "nt" else ["ffprobe", "ffprobe.exe"]
    for n in names:
        cand = os.path.join(base, n)
        if _executable(cand):
            return cand
    return None


def resolve_binaries():
    """固定使用系统原生 ffmpeg，解析结果放在模块全局变量中。"""
    global FFMPEG_BIN, FFPROBE_BIN, FFMPEG_ERROR
    FFMPEG_BIN = None
    FFPROBE_BIN = None
    FFMPEG_ERROR = ""
    FFMPEG_BIN = find_system_bin("ffmpeg", exclude_dir=BIN_DIR)
    if not FFMPEG_BIN:
        FFMPEG_ERROR = "未找到系统 ffmpeg，请先在飞牛系统或应用中心安装 ffmpeg 后重试。"
    if FFMPEG_BIN:
        FFMPEG_BIN = os.path.abspath(FFMPEG_BIN)
        FFPROBE_BIN = find_ffprobe_near(FFMPEG_BIN)
        if not FFPROBE_BIN:
            FFPROBE_BIN = find_system_bin("ffprobe", exclude_dir=BIN_DIR)
        if FFPROBE_BIN:
            FFPROBE_BIN = os.path.abspath(FFPROBE_BIN)
    return FFMPEG_BIN


def require_ffmpeg():
    resolve_binaries()
    if not FFMPEG_BIN:
        raise ValueError(FFMPEG_ERROR or "未找到可用的 ffmpeg")
    return FFMPEG_BIN


def require_ffprobe():
    if not FFMPEG_BIN:
        resolve_binaries()
    if not FFPROBE_BIN:
        raise ValueError("未找到系统 ffprobe，无法读取媒体信息，请安装完整的 ffmpeg")
    return FFPROBE_BIN


FFMPEG_OPTIONS = {}


def ffmpeg_support(ffmpeg, name):
    """检测 ffmpeg 是否支持某个选项，不同版本能力不同，结果缓存。"""
    key = (ffmpeg, name)
    if key not in FFMPEG_OPTIONS:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-h", "full"],
                                 capture_output=True, text=True, timeout=20)
            text = (out.stdout or "") + (out.stderr or "")
            FFMPEG_OPTIONS[key] = ("-" + name) in text
        except (OSError, subprocess.SubprocessError):
            FFMPEG_OPTIONS[key] = False
    return FFMPEG_OPTIONS[key]


def real(path):
    return os.path.realpath(os.path.abspath(path))


def is_app_dir(path):
    rp = real(path)
    for base in (APP_DIR, PKGVAR):
        rb = real(base)
        if rp == rb or rp.startswith(rb + os.sep):
            return True
    return False


def inside(path, base):
    if not base:
        return False
    rp = real(path)
    rb = real(base)
    return rp == rb or rp.startswith(rb + os.sep)


def scope_text(state):
    roots = (state or {}).get("roots") or []
    return "、".join(roots) if roots else "（尚未授权任何目录）"


OUT_OF_SCOPE_HINT = ("请先在 系统设置 › 应用 › 视频助手 › 访问权限 中授权该目录，"
                     "授权后回到应用重新选择。")


def within_user_root(state, path):
    """所有路径必须落在 fnOS 授权目录内；realpath 后判断，拦截 .. 与符号链接。"""
    return within_paths((state or {}).get("roots") or [], path)


def within_paths(paths, path):
    rp = real(path)
    for root in paths or []:
        rr = real(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def require_user_path(state, path, what="该路径"):
    """服务端统一越界校验：realpath 解析 .. 与符号链接后再判断。"""
    if not within_user_root(state, path):
        raise ValueError("只能使用已授权目录：%s（%s 已越界）。%s"
                         % (scope_text(state), what, OUT_OF_SCOPE_HINT))
    return real(path)


def is_blocked_app_dir(state, path):
    """应用自身目录始终禁止，避免输入输出落进程序目录。"""
    return bool(is_app_dir(path))


def dir_state(path, write=False, state=None, enforce_scope=True):
    if not path:
        return False, "未设置"
    if not os.path.isdir(path):
        return False, "目录不存在"
    if enforce_scope and state and not within_user_root(state, path):
        return False, "只能使用已授权目录：%s" % scope_text(state)
    if is_blocked_app_dir(state, path):
        return False, "不能使用应用自身目录"
    flags = os.R_OK | os.X_OK | (os.W_OK if write else 0)
    mode = "读写" if write else "读取"
    try:
        if not os.access(path, flags):
            return False, ("没有%s权限。请先在「系统设置 › 应用 › 视频助手 › 访问权限」"
                           "把该文件夹授权给应用" % mode)
    except OSError:
        return False, "无法访问该目录"
    return True, ""


def detect_roots(extra=(), state=None):
    return list((state or {}).get("roots") or [])


def add_root(state, path):
    """根集合只由 fnOS 授权目录决定，浏览时不再动态扩张。"""
    return real(path)


def within_roots(state, path):
    return within_user_root(state, path)


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# --------------------------------------------------------------------------
# 全局配置（无账号、无用户隔离）
# --------------------------------------------------------------------------
# 所有请求共享同一份状态：目录设置全局唯一，任务与日志也不再按账号隔离。
# 可访问目录只来自 fnOS「系统设置 › 应用 › 视频助手 › 访问权限」的授权列表，
# config_callback 落盘后由 refresh_roots() 即时刷新，无需重启服务。

APP_STATE = {
    "key": "global",
    "lock": CONFIG_LOCK,
    "dir": PKGVAR,
    "config_path": CONFIG_PATH,
    "username": "",
    "display_name": "",
    "role": "admin",
    "is_admin": True,
    "path_scope": "choose",
    "allow_download": True,
    "shared_input": "",
    "shared_output": "",
    "input_dir": "",
    "output_dir": "",
    "roots": [],
    "base_roots": [],
    "bootstrap_scope": False,
}


def _atomic_write_json(path, data, mode=0o600):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_env_paths(name):
    raw = os.environ.get(name) or ""
    sep = ";" if os.name == "nt" else ":"
    return [p.strip() for p in raw.split(sep) if p.strip()]


def _read_path_file(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return [line.strip() for line in fp.read().splitlines() if line.strip()]
    except OSError:
        return None


def _authorized_paths():
    """fnOS 为应用授权的目录（config_callback 落盘的文件优先）。"""
    result = []
    for file_path, env_name in ((ACCESSIBLE_PATHS_FILE, "TRIM_DATA_ACCESSIBLE_PATHS"),
                                (SHARE_PATHS_FILE, "TRIM_DATA_SHARE_PATHS")):
        values = _read_path_file(file_path)
        if values is None:
            values = _parse_env_paths(env_name)
        result.extend(values)
    return result


def refresh_roots():
    """每次请求重读授权目录，授权变更后无需重启服务。"""
    roots = []
    for p in _authorized_paths():
        if not p:
            continue
        rp = real(p)
        if not os.path.isdir(rp) or is_app_dir(rp) or rp in roots:
            continue
        roots.append(rp)
    with CONFIG_LOCK:
        APP_STATE["roots"] = roots
        APP_STATE["base_roots"] = list(roots)
        APP_STATE["bootstrap_scope"] = not roots
    return roots


def _valid_saved_dir(value, write):
    if not isinstance(value, str) or not value.strip():
        return ""
    path = real(value)
    ok, _err = dir_state(path, write, None, enforce_scope=False)
    if not ok or is_app_dir(path):
        return ""
    if not within_paths(APP_STATE["roots"], path):
        return ""
    return path


def load_config():
    for d in (PKGVAR, LIST_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    refresh_roots()
    data = _read_json(CONFIG_PATH)
    with CONFIG_LOCK:
        APP_STATE["input_dir"] = _valid_saved_dir(data.get("input_dir"), False)
        APP_STATE["output_dir"] = _valid_saved_dir(data.get("output_dir"), True)


def save_config():
    with CONFIG_LOCK:
        _atomic_write_json(CONFIG_PATH, {
            "version": 5,
            "input_dir": APP_STATE["input_dir"],
            "output_dir": APP_STATE["output_dir"],
        }, mode=0o600)


def init_app_state():
    resolve_binaries()
    load_config()


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "-"


def parse_time(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = [float(x) for x in text.split(":")]
            total = 0.0
            for part in parts:
                total = total * 60 + part
            return total
        return float(text)
    except ValueError:
        raise ValueError("时间格式不正确：%s" % value)


def clamp_int(value, low, high, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def safe_name(name, default_ext=".mkv"):
    name = (name or "").strip().replace("\\", "/")
    if not name:
        name = "output_%s%s" % (time.strftime("%Y%m%d_%H%M%S"), default_ext)
    if re.match(r"^[A-Za-z]:/", name):
        raise ValueError("输出文件名不合法")
    if name.startswith("/") or any(part in ("..", "") for part in name.split("/")[:-1]):
        raise ValueError("输出文件名不合法")
    if ".." in name.split("/"):
        raise ValueError("输出文件名不合法")
    if not os.path.splitext(name)[1]:
        name += default_ext
    return name


def require_input_dir(state):
    if not state["input_dir"]:
        raise ValueError("请先在设置中选择输入目录")
    ok, err = dir_state(state["input_dir"], False, state)
    if not ok:
        raise ValueError("输入目录不可用：%s" % err)


def require_output_dir(state):
    if not state["output_dir"]:
        raise ValueError("请先在设置中选择输出目录")
    ok, err = dir_state(state["output_dir"], True, state)
    if not ok:
        raise ValueError("输出目录不可用：%s" % err)


OUTPUT_PATH_LOCK = threading.Lock()
RESERVED_OUTPUTS = set()
LAST_OUTPUT = threading.local()


def release_output(path):
    """任务结束后释放预占的输出路径。"""
    if not path:
        return
    with OUTPUT_PATH_LOCK:
        RESERVED_OUTPUTS.discard(path)


def output_path(state, name, default_ext=".mkv"):
    require_output_dir(state)
    name = safe_name(name, default_ext)
    base = real(state["output_dir"])
    path = real(os.path.join(base, name))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("输出路径超出输出目录")
    require_user_path(state, path, "输出路径")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 统一防覆盖：ffmpeg 命令带 -y，若同名文件已存在会被静默覆盖。
    # 并发提交时文件可能还没被创建，所以除了磁盘存在性还要看进程内预占集合。
    # 所有动作一律自动追加 (1)/(2)，与合并页原有的命名行为保持一致。
    with OUTPUT_PATH_LOCK:
        if os.path.exists(path) or path in RESERVED_OUTPUTS:
            stem, ext = os.path.splitext(path)
            index = 1
            while True:
                candidate = "%s (%d)%s" % (stem, index, ext)
                if not os.path.exists(candidate) and candidate not in RESERVED_OUTPUTS:
                    break
                index += 1
                if index > 999:
                    raise ValueError("同名输出文件过多，请先整理输出目录")
            path = candidate
        RESERVED_OUTPUTS.add(path)
    LAST_OUTPUT.path = path
    return path


def input_path(state, value):
    if not value:
        raise ValueError("请选择输入文件")
    require_input_dir(state)
    base = real(state["input_dir"])
    path = real(value if os.path.isabs(value) else os.path.join(base, value))
    if not inside(path, base):
        raise ValueError("输入文件不在输入目录内")
    require_user_path(state, path, "输入文件")
    if not os.path.isfile(path):
        raise ValueError("输入文件不存在：%s" % value)
    return path


def default_out_name(src, suffix, ext=None):
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = ext or os.path.splitext(src)[1] or ".mkv"
    return "%s_%s%s" % (stem, suffix, ext)


def escape_filter_path(path):
    text = path.replace("\\", "/")
    text = text.replace(":", "\\:").replace("'", "\\'")
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = text.replace(",", "\\,")
    return text


def probe_duration(path):
    try:
        ffprobe = require_ffprobe()
    except ValueError:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def probe_file(path):
    try:
        ffprobe = require_ffprobe()
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            capture_output=True, text=True, timeout=25,
        )
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    fmt = data.get("format") or {}
    streams = []
    for s in data.get("streams") or []:
        streams.append({
            "type": s.get("codec_type"),
            "codec": s.get("codec_name"),
            "width": s.get("width"),
            "height": s.get("height"),
            "channels": s.get("channels"),
            "bit_rate": s.get("bit_rate"),
        })
    return {
        "duration": float(fmt.get("duration") or 0) or None,
        "size": int(fmt.get("size") or 0),
        "format": fmt.get("format_name"),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "streams": streams,
    }


def tool_info():
    resolve_binaries()
    ffmpeg = FFMPEG_BIN
    ffprobe = FFPROBE_BIN
    info = {
        "ffmpeg": ffmpeg,
        "ffmpeg_dir": os.path.dirname(ffmpeg) if ffmpeg else None,
        "ffprobe": ffprobe,
        "ffmpeg_version": None,
        "ffmpeg_ready": bool(ffmpeg),
        "ffmpeg_error": FFMPEG_ERROR,
        "downloader": "ffmpeg" if ffmpeg else None,
        "app_version": APP_VERSION,
        "publisher": PUBLISHER,
        "developer": DEVELOPER,
        "project_url": PROJECT_URL,
    }
    if ffmpeg:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
            info["ffmpeg_version"] = (out.stdout or "").splitlines()[0] if out.stdout else None
        except (OSError, subprocess.SubprocessError):
            pass
    return info


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------

def log_line(task, line):
    if not line:
        return
    task["log"].append(line)
    # log_seq 是单调递增的全局行号；log_start 记录当前数组第 0 行对应的行号，
    # 前端用 ?since= 增量拉取时不会因为日志被截断而错位。
    task["log_seq"] = task.get("log_seq", 0) + 1
    if len(task["log"]) > 4000:
        del task["log"][:1000]
        task["log"].insert(0, "... 日志过长，已截断 ...")
        task["log_start"] = task.get("log_start", 0) + 999


# ffmpeg -progress 输出的 key=value 行，只用于统计进度，不写进任务日志
PROGRESS_LINE_RE = re.compile(
    r"^(progress=|frame=|fps=|bitrate=|total_size=|out_time|dup_frames=|drop_frames=|speed=)")


def parse_progress(task, line):
    total = task.get("total_bytes")
    m = re.search(r"total_size=(\d+)", line)
    if m:
        task["written"] = int(m.group(1))
        if total:
            task["progress"] = min(99.0, int(m.group(1)) / float(total) * 100.0)
    # 注意：ffmpeg 的 out_time_ms 实际是微秒（与 out_time_us 同值）
    m = re.search(r"out_time_(?:ms|us)=(\d+)", line)
    if m:
        secs = int(m.group(1)) / 1000000.0
        task["elapsed"] = secs
        if task.get("duration"):
            task["progress"] = min(99.0, secs / task["duration"] * 100.0)
        return
    if task.get("duration"):
        m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
        if m:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            task["elapsed"] = secs
            task["progress"] = min(99.0, secs / task["duration"] * 100.0)


def run_task(task_id, cmd, duration=None, cleanup=None, partial=None):
    with TASKS_LOCK:
        task = TASKS[task_id]
        task["duration"] = duration
    acquired = False
    # 下载走独立队列，不与转码争 CPU 槽位；两类任务可以各自并行。
    slots = DOWNLOAD_SLOTS if task.get("action") == "download" else TASK_SLOTS
    try:
        # 并发槽位：超出的任务排队，避免多个 libx264 同时打满 NAS CPU。
        # 等待期间持续检查取消标记，让排队中的任务可以被立即取消。
        while True:
            if slots.acquire(timeout=0.2):
                acquired = True
                break
            with TASKS_LOCK:
                if task.get("canceled"):
                    task["status"] = "canceled"
                    task["finished"] = time.time()
                    return
        with TASKS_LOCK:
            if task.get("canceled"):
                task["status"] = "canceled"
                task["finished"] = time.time()
                return
            task["status"] = "running"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
        with TASKS_LOCK:
            task["proc"] = proc
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            parts = re.split(r"\r\n|\r|\n", pending)
            pending = parts.pop()
            for line in parts:
                line = line.rstrip()
                if line:
                    parse_progress(task, line)
                    if not PROGRESS_LINE_RE.match(line):
                        log_line(task, line)
        if pending.strip() and not PROGRESS_LINE_RE.match(pending.strip()):
            log_line(task, pending.strip())
        code = proc.wait()
        with TASKS_LOCK:
            task["returncode"] = code
            if task.get("canceled"):
                task["status"] = "canceled"
            else:
                task["status"] = "done" if code == 0 else "failed"
            if task["status"] == "done":
                task["progress"] = 100.0
    except Exception as exc:
        log_line(task, "ERROR: %s" % exc)
        with TASKS_LOCK:
            task["status"] = "failed"
    finally:
        with TASKS_LOCK:
            task["finished"] = time.time()
            task["proc"] = None
            failed = task.get("status") != "done"
        if acquired:
            try:
                slots.release()
            except ValueError:
                pass
        release_output(task.get("output"))
        if cleanup:
            for p in cleanup:
                try:
                    os.remove(p)
                except OSError:
                    pass
        if partial and failed:
            try:
                os.remove(partial)
            except OSError:
                pass


def start_task(tool, action, cmd, duration=None, cleanup=None, note=None,
               total_bytes=None, partial=None, owner=None, owner_name="", output=None):
    task_id = uuid.uuid4().hex
    task = {
        "id": task_id,
        "tool": tool,
        "action": action,
        "cmd": " ".join(shlex.quote(x) for x in cmd),
        "status": "queued",  # 拿到并发槽位后由 run_task 置为 running
        # 拿不到总时长/总字节时用 -1 表示进度未知，避免界面一直显示 0%
        "progress": 0.0 if (duration or total_bytes) else -1.0,
        "log": [],
        "log_seq": 0,
        "log_start": 0,
        "created": time.time(),
        "finished": None,
        "returncode": None,
        "proc": None,
        "canceled": False,
        "duration": duration,
        "total_bytes": total_bytes,
        "written": 0,
        "elapsed": 0.0,
        "owner": owner or "",
        "owner_name": owner_name or "",
        "output": output or "",
        # 任务名称取输出文件名，供任务列表直接展示
        "name": os.path.basename(output) if output else "",
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
        if len(TASKS) > 60:
            stale = sorted(TASKS.values(), key=lambda t: t["created"])[:20]
            for t in stale:
                if t["status"] != "running":
                    TASKS.pop(t["id"], None)
    log_line(task, "$ " + task["cmd"])
    if note:
        log_line(task, note)
    threading.Thread(target=run_task, args=(task_id, cmd, duration, cleanup, partial),
                     daemon=True).start()
    return task


def cancel_task(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return False
        proc = task.get("proc")
        task["canceled"] = True
        if task.get("status") == "queued":
            task["status"] = "canceled"
            task["finished"] = time.time()
            return True
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass
    if proc:
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    return True


# --------------------------------------------------------------------------
# ffmpeg 命令构建
# --------------------------------------------------------------------------

def ffmpeg_prefix(ffmpeg):
    return [ffmpeg, "-hide_banner", "-y", "-stats", "-stats_period", "1"]


# 自定义命令只接受下面这些选项。原先参数被原样拼进 argv，
# 可以追加 -i /任意路径 或第二个输出文件，绕过输入/输出目录沙箱读写任意文件。
# 现在改为：选项白名单 + 值校验 + 唯一输入/唯一输出。
CUSTOM_OPTS = {
    "-i": True, "-map": True, "-map_metadata": True, "-map_chapters": True,
    "-c": True, "-c:v": True, "-c:a": True, "-c:s": True, "-codec": True,
    "-b:v": True, "-b:a": True, "-ab": True, "-ar": True, "-ac": True,
    "-crf": True, "-preset": True, "-tune": True, "-profile:v": True,
    "-level": True, "-pix_fmt": True, "-r": True, "-s": True, "-aspect": True,
    "-vf": True, "-af": True, "-filter:v": True, "-filter:a": True,
    "-filter_complex": True, "-q:v": True, "-q:a": True, "-qscale:v": True,
    "-ss": True, "-t": True, "-to": True, "-threads": True,
    "-movflags": True, "-metadata": True, "-disposition": True,
    "-maxrate": True, "-bufsize": True, "-g": True, "-keyint_min": True,
    "-vsync": True, "-fps_mode": True, "-strict": True, "-vol": True,
    "-an": False, "-vn": False, "-sn": False, "-dn": False,
    "-shortest": False, "-copyts": False, "-avoid_negative_ts": True,
    "-write_tmcd": False, "-max_muxing_queue_size": True,
}
# 滤镜里能加载外部文件的构造，一律拒绝（防止 subtitles=/etc/... 之类读取越界文件）。
CUSTOM_FILTER_FILE_RE = re.compile(
    r"(?:^|[,;\[\]])\s*(?:movie|amovie|subtitles|ass|drawtext|concat|script|"
    r"[a-z]*file)\s*=", re.I)


def validate_custom_args(args, src, out):
    """校验自定义参数，返回可安全拼进命令的 argv 片段。"""
    if len(args) > 128:
        raise ValueError("自定义参数过长（最多 128 项）")
    checked = []
    inputs = 0
    outputs = 0
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("-"):
            if token not in CUSTOM_OPTS:
                raise ValueError("自定义命令不允许使用选项 %s" % token)
            checked.append(token)
            if not CUSTOM_OPTS[token]:
                i += 1
                continue
            if i + 1 >= len(args):
                raise ValueError("选项 %s 缺少参数值" % token)
            value = args[i + 1]
            if value.startswith("-") and token != "-i":
                raise ValueError("选项 %s 缺少参数值" % token)
            if token == "-i":
                if value != src:
                    raise ValueError("只能使用当前选择的输入文件，不能追加其他输入")
                inputs += 1
            else:
                _check_custom_value(token, value, src, out)
            checked.append(value)
            i += 2
            continue
        # 不以 - 开头的裸参数只能是唯一的输出文件，且必须等于系统生成的输出路径
        if token != out:
            raise ValueError("自定义命令不允许额外输出文件或裸参数：%s" % token)
        outputs += 1
        checked.append(token)
        i += 1
    if inputs > 1:
        raise ValueError("自定义命令只允许一个输入文件")
    if outputs > 1:
        raise ValueError("自定义命令只允许一个输出文件")
    # -vf/-af 等滤镜里若出现 file=.../movie=.../subtitles=... 之类外部文件加载，拒绝
    for idx, token in enumerate(checked[:-1]):
        if token in ("-vf", "-af", "-filter:v", "-filter:a", "-filter_complex"):
            if CUSTOM_FILTER_FILE_RE.search(checked[idx + 1]):
                raise ValueError("滤镜中不允许加载外部文件")
    return checked


def _check_custom_value(option, value, src, out):
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError("参数值包含非法控制字符")
    if value in (src, out):
        return
    if "://" in value:
        raise ValueError("选项 %s 不允许使用协议地址" % option)
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise ValueError("选项 %s 不允许使用绝对路径" % option)
    # key=/etc/... 这种「值里带绝对路径」的写法也要拦（如 -metadata comment=/etc/x）
    if re.search(r"=\s*(?:/|[A-Za-z]:[\\/])", value):
        raise ValueError("选项 %s 不允许使用绝对路径" % option)
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError("选项 %s 不允许路径穿越" % option)


def build_ffmpeg(state, action, p):
    ffmpeg = require_ffmpeg()
    prefix = ffmpeg_prefix(ffmpeg)

    if action == "merge":
        files = p.get("files") or []
        if len(files) < 2:
            raise ValueError("至少选择 2 个文件")
        paths = [input_path(state, f) for f in files]
        # output_path 已统一处理同名文件，这里不再重复重命名。
        out = output_path(state, p.get("output"), ".mkv")
        os.makedirs(LIST_DIR, exist_ok=True)
        listfile = os.path.join(LIST_DIR, uuid.uuid4().hex + ".txt")
        with open(listfile, "w", encoding="utf-8") as fp:
            for path in paths:
                fp.write("file '%s'\n" % path.replace("'", "'\\''"))
        if p.get("reencode"):
            cmd = prefix + ["-f", "concat", "-safe", "0", "-i", listfile,
                            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                            "-c:a", "aac", "-b:a", "192k", out]
        else:
            cmd = prefix + ["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out]
        total = 0.0
        for path in paths:
            total += probe_duration(path) or 0.0
        return cmd, total or None, [listfile]

    src = input_path(state, p.get("input"))

    if action == "convert":
        out = output_path(state, p.get("output"), ".mp4")
        ext = os.path.splitext(out)[1].lower()
        if ext in AUDIO_CODECS:
            cmd = prefix + ["-i", src, "-vn"] + AUDIO_CODECS[ext] + [out]
        elif ext in VIDEO_CODECS:
            if p.get("copy"):
                cmd = prefix + ["-i", src, "-c", "copy"]
                if ext == ".mp4":
                    cmd += ["-movflags", "+faststart"]
                cmd += [out]
            else:
                cmd = prefix + ["-i", src] + VIDEO_CODECS[ext] + [out]
        else:
            raise ValueError("不支持的输出格式：%s" % ext)
        return cmd, probe_duration(src), None

    if action == "compress":
        out = output_path(state, p.get("output"), ".mp4")
        crf = clamp_int(p.get("crf"), 0, 51, 23)
        preset = p.get("preset") if p.get("preset") in (
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow") else "medium"
        cmd = prefix + ["-i", src, "-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
        scale = (p.get("scale") or "").strip()
        if scale:
            cmd += ["-vf", "scale=" + scale]
        cmd += ["-c:a", "aac", "-b:a", "128k", out]
        return cmd, probe_duration(src), None

    if action == "trim":
        start = parse_time(p.get("start")) or 0.0
        duration = parse_time(p.get("duration"))
        out = output_path(state, p.get("output"), ".mkv")
        cmd = prefix + ["-ss", "%.3f" % start, "-i", src]
        if duration and duration > 0:
            cmd += ["-t", "%.3f" % duration]
        if p.get("copy", True):
            cmd += ["-c", "copy"]
        else:
            ext = os.path.splitext(out)[1].lower()
            cmd += VIDEO_CODECS.get(ext, VIDEO_CODECS[".mkv"])
        cmd += [out]
        return cmd, duration, None

    if action == "extract_audio":
        out = output_path(state, p.get("output"), ".mp3")
        ext = os.path.splitext(out)[1].lower()
        if ext not in AUDIO_CODECS:
            raise ValueError("不支持的音频格式：%s" % ext)
        cmd = prefix + ["-i", src, "-vn"] + AUDIO_CODECS[ext] + [out]
        return cmd, probe_duration(src), None

    if action == "subtitle":
        sub = p.get("subtitle")
        if not sub:
            raise ValueError("请选择字幕文件")
        sub = real(sub if os.path.isabs(sub) else os.path.join(state["input_dir"], sub))
        if not within_roots(state, sub) or not os.path.isfile(sub):
            raise ValueError("字幕文件不存在或不可访问")
        out = output_path(state, p.get("output"), ".mp4")
        vf = "subtitles=filename='%s'" % escape_filter_path(sub)
        font_size = p.get("font_size")
        if font_size:
            vf += ":force_style='FontSize=%d'" % clamp_int(font_size, 8, 120, 24)
        cmd = prefix + ["-i", src, "-vf", vf,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-b:a", "192k", out]
        return cmd, probe_duration(src), None

    if action == "resize":
        width = p.get("width")
        height = p.get("height")
        try:
            width = int(width) if width else 0
            height = int(height) if height else 0
        except (TypeError, ValueError):
            raise ValueError("宽高必须是数字")
        if width <= 0 and height <= 0:
            raise ValueError("请至少填写宽或高")
        if width > 0 and height > 0:
            scale = "scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2" % (width, height)
        elif width > 0:
            scale = "scale=%d:-2" % width
        else:
            scale = "scale=-2:%d" % height
        out = output_path(state, p.get("output"), ".mp4")
        cmd = prefix + ["-i", src, "-vf", scale,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-b:a", "192k", out]
        return cmd, probe_duration(src), None

    if action == "rotate":
        direction = p.get("direction") or "90"
        mapping = {
            "90": "transpose=1",
            "180": "hflip,vflip",
            "270": "transpose=2",
            "flip_h": "hflip",
            "flip_v": "vflip",
        }
        vf = mapping.get(direction)
        if not vf:
            raise ValueError("不支持的旋转方式")
        out = output_path(state, p.get("output"), ".mp4")
        cmd = prefix + ["-i", src, "-vf", vf,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-b:a", "192k", out]
        return cmd, probe_duration(src), None

    if action == "screenshot":
        at = parse_time(p.get("time")) or 0.0
        out = output_path(state, p.get("output"), ".jpg")
        cmd = prefix + ["-ss", "%.3f" % at, "-i", src, "-frames:v", "1", "-q:v", "2",
                        "-update", "1", out]
        return cmd, probe_duration(src), None

    if action == "custom":
        raw = (p.get("args") or "").strip()
        if not raw:
            raise ValueError("请填写 ffmpeg 参数")
        try:
            args = shlex.split(raw)
        except ValueError as exc:
            raise ValueError("参数解析失败：%s" % exc)
        out = output_path(state, p.get("output"), ".mkv")
        has_in = any("{input}" in a for a in args)
        has_out = any("{output}" in a for a in args)
        args = [a.replace("{input}", src).replace("{output}", out) for a in args]
        # 参数经过白名单与路径校验后才允许进入 argv。
        checked = validate_custom_args(args, src, out)
        cmd = list(prefix)
        if not has_in:
            cmd += ["-i", src]
        cmd += checked
        if not has_out:
            cmd += [out]
        return cmd, probe_duration(src), None

    raise ValueError("未知操作：%s" % action)


# --------------------------------------------------------------------------
# ffmpeg 原生下载
# --------------------------------------------------------------------------

def clean_media_url(url):
    """去掉粘贴地址时常见的包裹字符（反引号、引号、尖括号）与首尾空白。"""
    return (url or "").strip().strip("`'\"<>").strip()


def download_headers(p):
    headers = {"User-Agent": (p.get("user_agent") or "").strip() or DOWNLOAD_UA,
               "Accept": "*/*"}
    referer = (p.get("referer") or "").strip()
    if referer:
        headers["Referer"] = referer
    raw = (p.get("headers") or "").strip()
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key and value and len(key) <= 80 and len(value) <= 2000:
            headers[key] = value
    return headers


MAX_REDIRECTS = 5


def _public_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return bool(ip.is_global)


def validate_download_url(url):
    """下载前解析并校验目标：只允许公网 http/https，拒绝回环/私网/链路本地等。"""
    url = clean_media_url(url)
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("目前只支持 http/https 地址")
    if any(ch in url for ch in ("\r", "\n", "\t")):
        raise ValueError("地址包含非法控制字符")
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError("地址缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("地址不能包含用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        raise ValueError("地址端口不合法")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("无法解析地址 %s：%s" % (host, exc))
    ips = set()
    for info in infos:
        try:
            ips.add(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not ips:
        raise ValueError("无法解析地址 %s" % host)
    for ip in ips:
        if not _public_ip(ip):
            raise ValueError("拒绝访问内网/回环/链路本地地址：%s（%s）" % (host, ip))
    return url


def validate_proxy(proxy):
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if not re.match(r"^https?://", proxy, re.I):
        raise ValueError("代理地址只支持 http:// 或 https://")
    parsed = urlparse(proxy)
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    try:
        parsed.port
    except ValueError:
        raise ValueError("代理端口不合法")
    return proxy


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新做地址校验，并限制跳转次数。"""

    max_redirections = MAX_REDIRECTS
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        validate_download_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def make_opener(proxy=""):
    proxy = validate_proxy(proxy)
    handlers = [_SafeRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def fetch_text(url, headers, proxy="", limit=MAX_PAGE_BYTES):
    url = validate_download_url(url)
    req = urllib.request.Request(url, headers=headers)
    try:
        with make_opener(proxy).open(req, timeout=20) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(limit + 1)
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("地址需要登录或拒绝访问（HTTP %s）" % exc.code)
        raise ValueError("读取地址失败：HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise ValueError("无法连接地址：%s" % exc.reason)
    except OSError as exc:
        raise ValueError("读取地址失败：%s" % exc)
    if len(raw) > limit:
        raw = raw[:limit]
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    return text, ctype, final_url


def content_length(url, headers, proxy=""):
    url = validate_download_url(url)
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with make_opener(proxy).open(req, timeout=12) as resp:
            return int(resp.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def url_media_kind(url, ctype=""):
    ctype = (ctype or "").lower()
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext == ".m3u8" or "mpegurl" in ctype:
        return "hls"
    if ext == ".mpd" or "dash+xml" in ctype:
        return "dash"
    if ext in URL_MEDIA_EXTS:
        return "media"
    if ctype.startswith(("video/", "audio/")):
        return "media"
    if ctype in ("application/octet-stream", "binary/octet-stream"):
        return "media"
    return ""


def probe_url(url, headers, proxy=""):
    url = validate_download_url(url)
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with make_opener(proxy).open(req, timeout=12) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return resp.geturl(), ctype
    except (OSError, ValueError, urllib.error.URLError):
        return url, ""


def page_media_candidates(text, base_url):
    text = html_lib.unescape(text).replace("\\/", "/")
    patterns = [
        r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["\']',
        r'<(?:video|source)[^>]+src=["\']([^"\']+)',
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'https?://[^\s"\'<>]+?\.(?:m3u8|mpd|mp4|webm|m4v|mov|mkv)(?:\?[^\s"\'<>]*)?',
    ]
    found = []
    for pattern in patterns:
        for item in re.findall(pattern, text, re.I):
            if item not in found:
                found.append(item)
    iframes = []
    for item in re.findall(r'<iframe[^>]+src=["\']([^"\']+)', text, re.I):
        if item not in iframes:
            iframes.append(item)
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = m.group(1) if m else ""
    return [urljoin(base_url, x) for x in found], [urljoin(base_url, x) for x in iframes], title


def resolve_media(url, headers, proxy=""):
    kind = url_media_kind(url)
    if kind:
        return validate_download_url(url), kind, ""

    final_url, ctype = probe_url(url, headers, proxy)
    kind = url_media_kind(final_url, ctype)
    if kind:
        return final_url, kind, ""

    text, ctype, final_url = fetch_text(url, headers, proxy)
    kind = url_media_kind(final_url, ctype)
    if kind:
        return final_url, kind, ""

    media, iframes, title = page_media_candidates(text, final_url)
    for candidate in media:
        kind = url_media_kind(candidate)
        if kind and candidate.startswith(("http://", "https://")):
            try:
                return validate_download_url(candidate), kind, title
            except ValueError:
                continue

    for iframe in iframes[:3]:
        try:
            sub_text, sub_ctype, sub_final = fetch_text(iframe, headers, proxy)
        except ValueError:
            continue
        sub_kind = url_media_kind(sub_final, sub_ctype)
        if sub_kind:
            return sub_final, sub_kind, title
        sub_media, _, sub_title = page_media_candidates(sub_text, sub_final)
        for candidate in sub_media:
            sub_kind = url_media_kind(candidate)
            if sub_kind and candidate.startswith(("http://", "https://")):
                try:
                    return validate_download_url(candidate), sub_kind, title or sub_title
                except ValueError:
                    continue

    raise ValueError("没有找到可直接下载的媒体地址。ffmpeg 只能处理直链、HLS(m3u8)、DASH(mpd) 或页面内公开的媒体地址；需要登录或加密解析的站点无法下载。")


def hls_variants(text, base_url):
    lines = [line.strip() for line in text.splitlines()]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = line.split(":", 1)[1]
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs, re.I)
        bw = re.search(r"BANDWIDTH=(\d+)", attrs, re.I)
        j = i + 1
        while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
            j += 1
        if j >= len(lines):
            continue
        variants.append({
            "url": urljoin(base_url, lines[j]),
            "height": int(res.group(2)) if res else 0,
            "width": int(res.group(1)) if res else 0,
            "bandwidth": int(bw.group(1)) if bw else 0,
        })
    return variants


def hls_duration(text):
    """按 EXTINF 累计媒体播放列表的总时长，拿不到就返回 None。"""
    total = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF:"):
            continue
        try:
            total += float(line.split(":", 1)[1].split(",", 1)[0])
        except ValueError:
            continue
    return total if total > 0 else None


def pick_variant(variants, quality):
    if not variants:
        return None
    if quality == "worst":
        return min(variants, key=lambda v: (v["height"], v["bandwidth"]))
    if quality and quality != "best":
        target = clamp_int(quality, 144, 4320, 1080)
        fit = [v for v in variants if v["height"] and v["height"] <= target]
        if fit:
            return max(fit, key=lambda v: (v["height"], v["bandwidth"]))
        return min(variants, key=lambda v: abs((v["height"] or target) - target))
    return max(variants, key=lambda v: (v["height"], v["bandwidth"]))


def clean_title(title):
    title = re.sub(r"\s+", " ", html_lib.unescape(title or "")).strip()
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title[:80]


def default_download_name(media_url, title, mode, audio_format):
    name = clean_title(title)
    if not name:
        base = os.path.splitext(os.path.basename(unquote(urlparse(media_url).path).rstrip("/")))[0]
        if base and base.lower() not in ("playlist", "index", "master", "manifest"):
            name = base
        else:
            name = "video_" + time.strftime("%Y%m%d_%H%M%S")
    if mode == "audio":
        return name + "." + audio_format
    ext = os.path.splitext(urlparse(media_url).path)[1].lower()
    if ext not in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
        ext = ".mp4"
    return name + ext


def build_download(state, p):
    url = clean_media_url(p.get("url"))
    if not url:
        raise ValueError("请填写视频地址")
    url = validate_download_url(url)
    proxy = validate_proxy((p.get("proxy") or "").strip())

    ffmpeg = require_ffmpeg()
    require_output_dir(state)

    headers = download_headers(p)
    media_url, kind, title = resolve_media(url, headers, proxy)
    media_url = validate_download_url(media_url)

    note = ["下载引擎：ffmpeg", "媒体类型：%s" % {"hls": "HLS", "dash": "DASH"}.get(kind, "直链媒体")]
    total_time = None
    if kind == "hls":
        try:
            text, _, final_url = fetch_text(media_url, headers, proxy)
            variants = hls_variants(text, final_url)
            if variants:
                selected = pick_variant(variants, str(p.get("quality") or "best"))
                media_url = validate_download_url(selected["url"])
                note.append("画质：%s" % (
                    "%dx%d" % (selected["width"], selected["height"])
                    if selected["width"] else "%d kbps" % (selected["bandwidth"] // 1000)))
                text, _, final_url = fetch_text(media_url, headers, proxy)
            total_time = hls_duration(text)
        except ValueError:
            pass

    mode = "audio" if p.get("mode") == "audio" else "video"
    audio_format = (p.get("audio_format") or "mp3").lower()
    if audio_format not in ("mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"):
        raise ValueError("不支持的音频格式")

    filename = (p.get("filename") or "").strip().replace("\\", "/")
    if "/" in filename:
        raise ValueError("文件名不要包含目录，请使用保存子目录")
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if mode == "audio":
            if ext not in AUDIO_EXTS:
                filename = os.path.splitext(filename)[0] + "." + audio_format
        elif ext not in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
            filename = os.path.splitext(filename)[0] + ".mp4"
    else:
        filename = default_download_name(media_url, title, mode, audio_format)

    subdir = (p.get("subdir") or "").strip().replace("\\", "/").strip("/")
    if subdir.startswith("/") or ".." in subdir.split("/"):
        raise ValueError("子目录不合法")
    target = (subdir + "/" if subdir else "") + filename
    out = output_path(state, target, ".mp3" if mode == "audio" else ".mp4")

    cmd = [ffmpeg, "-hide_banner", "-y", "-nostdin", "-loglevel", "info",
           "-nostats", "-progress", "pipe:1"]
    if proxy:
        cmd += ["-http_proxy", proxy]
    ua = headers.get("User-Agent")
    if ua:
        cmd += ["-user_agent", ua]
    extra = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    if extra:
        cmd += ["-headers", "".join("%s: %s\r\n" % (k, v) for k, v in extra.items())]
    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    # ffmpeg 7.1+ 默认按 URL 扩展名校验分片，HLS 常见 .mp4 后缀的 mpegts 分片会被判为不匹配
    if kind == "hls" and ffmpeg_support(ffmpeg, "extension_picky"):
        cmd += ["-extension_picky", "0"]
    cmd += ["-protocol_whitelist", "http,https,tcp,tls,crypto"]
    cmd += ["-i", media_url]

    if mode == "audio":
        cmd += ["-vn", "-map", "0:a:0?"] + AUDIO_CODECS["." + audio_format]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
        if out.lower().endswith((".mp4", ".mov")):
            cmd += ["-movflags", "+faststart"]
    cmd += [out]

    total_bytes = None if kind in ("hls", "dash") else content_length(media_url, headers, proxy)
    return cmd, total_time, None, "\n".join(note), total_bytes, out


# --------------------------------------------------------------------------
# 检查更新
# --------------------------------------------------------------------------

def version_key(text):
    text = str(text or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    m = re.match(r"^(\d+(?:\.\d+)*)", text)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def version_compare(a, b):
    ka, kb = version_key(a), version_key(b)
    width = max(len(ka), len(kb))
    ka = ka + (0,) * (width - len(ka))
    kb = kb + (0,) * (width - len(kb))
    return (ka > kb) - (ka < kb)


def update_api_url():
    override = os.environ.get("VT_GITHUB_API", "").strip()
    if override:
        return override
    return "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO


def fetch_latest_release():
    """请求 GitHub Releases；任何失败都转换成可读信息，不抛异常。"""
    req = urllib.request.Request(update_api_url(), headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "VideoTool/%s" % APP_VERSION,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("VT_GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        headers = exc.headers or {}
        remain = str(headers.get("X-RateLimit-Remaining") or "").strip()
        reset = str(headers.get("X-RateLimit-Reset") or "").strip()
        retry_after = str(headers.get("Retry-After") or "").strip()
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        limited = (exc.code == 429 or remain == "0"
                   or "rate limit" in body.lower() or "api rate" in body.lower())
        if exc.code in (403, 429) and limited:
            msg = "GitHub API 速率限制（未登录时每小时 60 次）。"
            wait = ""
            if retry_after.isdigit():
                wait = "%d 秒后" % int(retry_after)
            elif reset.isdigit():
                try:
                    wait = "%s 后" % time.strftime("%H:%M", time.localtime(int(reset)))
                except (ValueError, OSError):
                    wait = ""
            if wait:
                msg += "预计 %s可重试。" % wait
            msg += "也可配置环境变量 VT_GITHUB_TOKEN 提升限额。"
            return {"ok": False, "status": "error", "code": "rate_limit", "message": msg}
        if exc.code == 404:
            return {"ok": False, "status": "error", "code": "not_found",
                    "message": "仓库暂无 Release 记录，或仓库不可公开访问。"}
        return {"ok": False, "status": "error", "code": "http_error",
                "message": "GitHub 返回 HTTP %d，请稍后重试。" % exc.code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            reason = "连接超时"
        reason = str(reason).strip().rstrip("。.")
        return {"ok": False, "status": "error", "code": "network",
                "message": "网络请求失败：%s。请检查飞牛的网络或代理设置后重试。" % reason}
    except Exception as exc:
        return {"ok": False, "status": "error", "code": "network",
                "message": "网络请求失败：%s" % exc}

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "status": "error", "code": "bad_json",
                "message": "GitHub 返回数据无法解析，请稍后重试。"}
    if not isinstance(data, dict):
        return {"ok": False, "status": "error", "code": "bad_json",
                "message": "GitHub 返回数据格式异常，请稍后重试。"}

    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    latest = tag[1:] if tag.lower().startswith("v") else tag
    if not version_key(latest):
        return {"ok": False, "status": "error", "code": "bad_version",
                "message": "GitHub Release 的版本号无法识别：%s" % (tag or "（空）")}

    release_url = data.get("html_url") or (PROJECT_URL + "/releases")
    asset = None
    for item in (data.get("assets") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.lower().endswith(".fpk"):
            continue
        if asset is None:
            asset = item
        if name.lower().startswith("video-tool-%s." % latest.lower()):
            asset = item
            break
    download_url = release_url
    asset_name = ""
    asset_size = 0
    if asset:
        download_url = asset.get("browser_download_url") or release_url
        asset_name = str(asset.get("name") or "")
        try:
            asset_size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            asset_size = 0

    notes = data.get("body")
    notes = notes.strip() if isinstance(notes, str) else ""
    if len(notes) > 600:
        notes = notes[:600] + "…"

    return {
        "ok": True,
        "status": "latest" if version_compare(APP_VERSION, latest) >= 0 else "update",
        "current": APP_VERSION,
        "latest": latest,
        "tag": tag,
        "download_url": download_url,
        "asset_name": asset_name,
        "size": asset_size,
        "published_at": data.get("published_at") or "",
        "notes": notes,
    }


def check_update(force=False):
    now = time.time()
    with UPDATE_LOCK:
        cached = UPDATE_CACHE.get("data")
        if cached is not None and not force:
            ttl = UPDATE_TTL if UPDATE_CACHE.get("ok") else UPDATE_FAIL_TTL
            if now - UPDATE_CACHE.get("at", 0.0) < ttl:
                result = dict(cached)
                result["cached"] = True
                return result
    result = fetch_latest_release()
    result.setdefault("current", APP_VERSION)
    result["checked_at"] = int(now)
    result["cached"] = False
    with UPDATE_LOCK:
        UPDATE_CACHE["at"] = now
        UPDATE_CACHE["data"] = result
        UPDATE_CACHE["ok"] = bool(result.get("ok"))
    return result


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class PayloadTooLarge(Exception):
    """请求体超过上限，统一回 413 而不是被当成普通 500。"""


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoTool/%s" % APP_VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        # 页面无内联脚本，样式里有少量 style="" 属性，因此只放开 style 的 inline。
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "font-src 'self' data:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'self'")

    def send_json(self, obj, code=200, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        try:
            with open(path, "rb") as fp:
                data = fp.read()
        except OSError:
            self.send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        etag = '"%s"' % hashlib.sha256(data).hexdigest()[:32]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise PayloadTooLarge("请求长度不合法")
        if length < 0:
            raise PayloadTooLarge("请求长度不合法")
        if length > MAX_REQUEST_BYTES:
            # 先把已发送的请求体排空再回 413，否则客户端还在写 body 时连接就被
            # 关闭，浏览器只会看到「连接被重置」而不是可读的错误提示。
            try:
                self.rfile.read(min(length, 8 * 1024 * 1024))
            except OSError:
                pass
            self.close_connection = True
            raise PayloadTooLarge("请求体过大（上限 1 MB）")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def current_user(self):
        refresh_roots()
        return APP_STATE

    # ---------------- GET ----------------

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        try:
            if path in ("/", "/index.html"):
                self.send_file(os.path.join(UI_DIR, "index.html"))
            elif path == "/api/config":
                self.api_config()
            elif path == "/api/tools":
                state = self.current_user()
                info = tool_info()
                info.update({
                    "input_dir": state["input_dir"],
                    "output_dir": state["output_dir"],
                    "roots": list(state["roots"]),
                    "pkgvar": PKGVAR,
                })
                self.send_json(info)
            elif path == "/api/update":
                self.api_update()
            elif path == "/api/browse":
                self.api_browse()
            elif path == "/api/files":
                self.api_files()
            elif path == "/api/probe":
                self.api_probe()
            elif path == "/api/tasks":
                self.api_tasks()
            elif path.startswith("/api/log/"):
                self.api_log(path[len("/api/log/"):])
            else:
                target = real(os.path.join(UI_DIR, path.lstrip("/")))
                if target.startswith(real(UI_DIR) + os.sep) and os.path.isfile(target):
                    self.send_file(target)
                else:
                    self.send_json({"error": "not found"}, 404)
        except PayloadTooLarge as exc:
            self.send_json({"error": str(exc), "code": "payload_too_large"}, 413)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def config_payload(self, state):
        input_ok, input_error = dir_state(state["input_dir"], False, state)
        output_ok, output_error = dir_state(state["output_dir"], True, state)
        resolve_binaries()
        return {
            "input_dir": state["input_dir"],
            "output_dir": state["output_dir"],
            "input_ok": input_ok,
            "input_error": input_error,
            "output_ok": output_ok,
            "output_error": output_error,
            "configured": input_ok and output_ok,
            "roots": list(state["roots"]),
            "pkgvar": PKGVAR,
            "ffmpeg": FFMPEG_BIN,
            "ffprobe": FFPROBE_BIN,
            "ffmpeg_ready": bool(FFMPEG_BIN),
            "ffmpeg_error": FFMPEG_ERROR,
        }

    def api_config(self):
        self.send_json(self.config_payload(self.current_user()))

    def api_update(self):
        q = self.query()
        force = str(q.get("refresh", "")).lower() in ("1", "true", "yes")
        self.send_json(check_update(force))

    def api_browse(self):
        state = self.current_user()
        raw = (self.query().get("path") or "").strip()
        roots = list(state["roots"])
        if not roots:
            self.send_json({
                "path": "", "parent": None, "roots": [], "dirs": [],
                "readable": False, "writable": False,
                "error": "尚未授权任何目录。请先在 系统设置 › 应用 › 视频助手 › 访问权限 "
                         "中添加文件夹，授权后回到这里重新选择。",
            })
            return
        if not raw:
            # 根视图：只列出已授权目录，不展示卷上其它目录，也不接受手填路径。
            dirs = []
            for root in roots:
                dirs.append({
                    "name": os.path.basename(root.rstrip(os.sep)) or root,
                    "path": root,
                    "writable": os.access(root, os.W_OK),
                })
            self.send_json({
                "path": "", "parent": None, "roots": roots, "dirs": dirs,
                "readable": True, "writable": False, "error": "",
            })
            return
        path = real(raw)
        if not within_user_root(state, path):
            self.send_json({
                "error": "只能浏览已授权目录：%s（%s 已越界）。%s"
                         % (scope_text(state), path, OUT_OF_SCOPE_HINT),
                "code": "path_outside_authorized_root",
            }, 403)
            return
        if is_app_dir(path):
            self.send_json({"error": "不能浏览应用自身目录"}, 403)
            return
        if not os.path.isdir(path):
            self.send_json({"error": "目录不存在：%s" % path}, 404)
            return

        dirs = []
        error = ""
        readable = True
        skip_dirs = {"#recycle", "@eadir", "$recycle.bin", "lost+found",
                     "system volume information"}
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.name.startswith(".") or entry.name.lower() in skip_dirs:
                        continue
                    try:
                        if not entry.is_dir():
                            continue
                        if is_app_dir(entry.path) or not within_user_root(state, entry.path):
                            continue
                        dirs.append({
                            "name": entry.name,
                            "path": entry.path,
                            "writable": os.access(entry.path, os.W_OK),
                        })
                    except OSError:
                        continue
        except PermissionError:
            readable = False
            error = "没有权限访问该目录，请先在 系统设置 › 应用 › 视频助手 › 访问权限 中授权"
        except OSError as exc:
            readable = False
            error = "读取目录失败：%s" % exc

        dirs.sort(key=lambda x: x["name"].lower())
        is_root = path in roots
        parent = "" if is_root else os.path.dirname(path)
        if not is_root and (parent == path or is_app_dir(parent)
                            or not within_user_root(state, parent)):
            parent = ""
        self.send_json({
            "path": path,
            "parent": parent,
            "roots": roots,
            "dirs": dirs,
            "readable": readable,
            "writable": os.access(path, os.W_OK),
            "error": error,
        })

    def api_files(self):
        state = self.current_user()
        q = self.query()
        if not state["input_dir"]:
            self.send_json({"error": "请先在设置中选择输入目录"}, 400)
            return
        path = q.get("path") or state["input_dir"]
        path = real(path)
        if not within_user_root(state, path):
            self.send_json({
                "error": "只能读取已授权目录：%s（%s 已越界）。%s"
                         % (scope_text(state), path, OUT_OF_SCOPE_HINT),
                "code": "path_outside_authorized_root",
            }, 403)
            return
        if not inside(path, state["input_dir"]):
            self.send_json({"error": "只能读取你自己的输入目录中的文件"}, 403)
            return
        if not os.path.isdir(path):
            self.send_json({"error": "目录不存在"}, 404)
            return
        ok, err = dir_state(path, False, state)
        if not ok:
            self.send_json({"error": "输入目录不可用：%s" % err}, 403)
            return

        files = []
        out_real = real(state["output_dir"]) if state["output_dir"] else ""
        skip_dirs = {"#recycle", "@eadir", "$recycle.bin", "lost+found", "system volume information"}

        def on_walk_error(exc):
            pass

        try:
            for root, dirs, names in os.walk(path, topdown=True, onerror=on_walk_error,
                                              followlinks=False):
                kept = []
                for d in dirs:
                    full = os.path.join(root, d)
                    if d.startswith(".") or d.lower() in skip_dirs:
                        continue
                    if out_real and real(full) == out_real:
                        continue
                    if is_blocked_app_dir(state, full):
                        continue
                    if not within_user_root(state, full):
                        continue
                    kept.append(d)
                kept.sort(key=str.lower)
                dirs[:] = kept

                relroot = os.path.relpath(root, path)
                depth = 0 if relroot == "." else relroot.count(os.sep) + 1
                if depth >= MAX_SCAN_DEPTH:
                    dirs[:] = []

                for name in sorted(names, key=str.lower):
                    if name.startswith("."):
                        continue
                    full = os.path.join(root, name)
                    ext = os.path.splitext(name)[1].lower()
                    kind = "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else "subtitle" if ext in SUB_EXTS else None
                    if not kind:
                        continue
                    if not within_user_root(state, full):
                        # 软链接指向允许根目录之外的文件一律不展示。
                        continue
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    files.append({
                        "name": name,
                        "rel": rel,
                        "path": full,
                        "ext": ext,
                        "kind": kind,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
                    if len(files) >= MAX_SCAN_FILES:
                        break
                if len(files) >= MAX_SCAN_FILES:
                    break
        except PermissionError:
            self.send_json({"error": "没有权限访问输入目录，请先在 系统设置 > 应用 > 视频助手 中授权该文件夹"}, 403)
            return

        files.sort(key=lambda x: x["rel"].lower())
        self.send_json({
            "path": path,
            "files": files,
            "recursive": True,
            "truncated": len(files) >= MAX_SCAN_FILES,
        })

    def api_probe(self):
        state = self.current_user()
        q = self.query()
        path = q.get("path") or ""
        if not path:
            self.send_json({"error": "缺少 path"}, 400)
            return
        path = real(path)
        if not within_user_root(state, path):
            self.send_json({
                "error": "只能读取已授权目录：%s（%s 已越界）。%s"
                         % (scope_text(state), path, OUT_OF_SCOPE_HINT),
                "code": "path_outside_authorized_root",
            }, 403)
            return
        if not state["input_dir"] or not inside(path, state["input_dir"]) or not os.path.isfile(path):
            self.send_json({"error": "文件不存在或不可访问"}, 404)
            return
        self.send_json(probe_file(path))

    def api_tasks(self):
        state = self.current_user()
        with TASKS_LOCK:
            items = []
            for task in sorted(TASKS.values(), key=lambda t: t["created"], reverse=True):
                if task.get("owner") != state["key"]:
                    continue
                items.append({
                    "id": task["id"],
                    "tool": task["tool"],
                    "action": task["action"],
                    "name": task.get("name") or "",
                    "status": task["status"],
                    "progress": task["progress"],
                    "written": task.get("written", 0),
                    "elapsed": task.get("elapsed", 0.0),
                    "created": task["created"],
                    "finished": task["finished"],
                    "owner": task.get("owner") or "",
                    "owner_name": task.get("owner_name") or "",
                })
                if len(items) >= 20:
                    break
        self.send_json({"tasks": items, "scope": "mine"})

    def api_log(self, task_id):
        state = self.current_user()
        try:
            since = max(0, int(self.query().get("since") or 0))
        except ValueError:
            since = 0
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("owner") != state["key"]:
                self.send_json({"error": "任务不存在"}, 404)
                return
            log_start = int(task.get("log_start") or 0)
            # 日志被截断过时下标会整体左移，用 log_start 把 since 换算回当前数组下标。
            chunk = list(task["log"][max(0, since - log_start):])
            payload = {
                "id": task["id"],
                "status": task["status"],
                "progress": task["progress"],
                "written": task.get("written", 0),
                "elapsed": task.get("elapsed", 0.0),
                "returncode": task["returncode"],
                "log": chunk,
                "since": since,
                "next": log_start + len(task["log"]),
                "total": int(task.get("log_seq") or 0),
            }
        self.send_json(payload)

    # ---------------- POST ----------------

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                self.api_save_config()
            elif path == "/api/mkdir":
                self.api_mkdir()
            elif path == "/api/task":
                self.api_task()
            elif path.startswith("/api/cancel/"):
                self.api_cancel(path[len("/api/cancel/"):])
            else:
                self.send_json({"error": "not found"}, 404)
        except PayloadTooLarge as exc:
            self.send_json({"error": str(exc), "code": "payload_too_large"}, 413)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def api_save_config(self):
        state = self.current_user()
        data = self.read_json()
        with CONFIG_LOCK:
            new_input = data.get("input_dir") if "input_dir" in data else state["input_dir"]
            new_output = data.get("output_dir") if "output_dir" in data else state["output_dir"]
            new_input = (new_input or "").strip()
            new_output = (new_output or "").strip()

            def check_new_dir(value, write, label):
                if not value:
                    return ""
                if not os.path.isabs(value):
                    raise ValueError("请输入完整路径，例如 /vol1/共享/视频")
                path = real(value)
                if not os.path.isdir(path):
                    raise ValueError("%s不存在：%s" % (label, path))
                if is_app_dir(path):
                    raise ValueError("%s不能使用应用自身目录" % label)
                if not within_user_root(state, path):
                    raise ValueError("%s不在已授权目录内：%s。%s"
                                     % (label, path, OUT_OF_SCOPE_HINT))
                ok, err = dir_state(path, write, None, enforce_scope=False)
                if not ok:
                    raise ValueError("%s不可用：%s" % (label, err))
                return path

            new_input = check_new_dir(new_input, False, "输入目录")
            new_output = check_new_dir(new_output, True, "输出目录")

            state["input_dir"] = new_input
            state["output_dir"] = new_output
            save_config()

        payload = self.config_payload(state)
        payload["ok"] = True
        self.send_json(payload)

    def api_mkdir(self):
        state = self.current_user()
        data = self.read_json()
        path = (data.get("path") or "").strip()
        if not path:
            raise ValueError("缺少 path")
        if not os.path.isabs(path):
            raise ValueError("请填写完整路径")
        path = real(path)
        require_user_path(state, path, "新建目录")
        if path == os.sep or is_blocked_app_dir(state, path):
            raise ValueError("不能使用应用自身目录")
        parent = os.path.dirname(path)
        require_user_path(state, parent, "上级目录")
        if not os.path.isdir(parent):
            raise ValueError("上级目录不存在")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("没有写入权限，请先在 系统设置 › 应用 › 视频助手 › 访问权限 中授权该目录")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise ValueError("创建失败：%s" % exc)
        if not os.path.isdir(path):
            raise ValueError("创建失败")
        add_root(state, parent)
        self.send_json({"ok": True, "path": path})

    def api_task(self):
        state = self.current_user()
        data = self.read_json()
        action = (data.get("action") or "").strip()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("参数格式不正确")

        # 每次请求从干净状态开始，构建命令时由 output_path 记录实际输出路径。
        LAST_OUTPUT.path = ""
        # 自定义 ffmpeg 参数在 build_ffmpeg 里经过选项白名单与路径校验。
        note = None
        total_bytes = None
        partial = None
        if action == "download":
            tool = "ffmpeg"
            cmd, duration, cleanup, note, total_bytes, partial = build_download(state, params)
        elif action.startswith("ytdlp"):
            raise ValueError("已移除 yt-dlp，请使用 ffmpeg 原生下载")
        else:
            tool = "ffmpeg"
            cmd, duration, cleanup = build_ffmpeg(state, action, params)

        task = start_task(tool, action, cmd, duration=duration, cleanup=cleanup,
                          note=note, total_bytes=total_bytes, partial=partial,
                          owner=state["key"], owner_name="本地",
                          output=getattr(LAST_OUTPUT, "path", ""))
        self.send_json({"task": task["id"], "cmd": task["cmd"]})

    def api_cancel(self, task_id):
        state = self.current_user()
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("owner") != state["key"]:
                self.send_json({"error": "任务不存在"}, 404)
                return
        ok = cancel_task(task_id)
        if not ok:
            self.send_json({"error": "任务不存在"}, 404)
            return
        self.send_json({"ok": True})


def stop_all_tasks():
    with TASKS_LOCK:
        procs = [t.get("proc") for t in TASKS.values() if t.get("proc")]
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.time() + 2.0
    for proc in procs:
        remain = deadline - time.time()
        if remain > 0:
            try:
                proc.wait(timeout=remain)
            except (subprocess.TimeoutExpired, OSError):
                pass
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass


def _startup_log(message):
    print("[startup] %s" % message, flush=True)


def _startup_fail(problems):
    for problem in problems:
        _startup_log("ERROR: " + problem)
    _startup_log("HINT: 请检查数据目录权限、python 版本和端口占用；"
                 "飞牛上可运行 ss -ltnp | grep 18080 查看端口。")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("VT_BIND_HOST") or "0.0.0.0")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("VT_BIND_PORT") or "18080"))
    args = parser.parse_args()

    _startup_log("VideoTool %s 启动中（Python %s）" % (
        APP_VERSION, ".".join(str(n) for n in sys.version_info[:3])))
    if sys.version_info < (3, 8):
        _startup_fail(["Python 版本过低：需要 3.8 及以上，当前为 %s"
                       % ".".join(str(n) for n in sys.version_info[:3])])

    init_app_state()

    problems = []
    for label, path in (("数据目录", PKGVAR), ("任务列表目录", LIST_DIR)):
        if not os.path.isdir(path):
            problems.append("%s不存在或无法创建：%s" % (label, path))
        elif not os.access(path, os.W_OK):
            problems.append("%s不可写：%s" % (label, path))
    if not os.path.isdir(UI_DIR):
        problems.append("前端目录不存在：%s" % UI_DIR)
    if problems:
        _startup_fail(problems)

    resolve_binaries()
    _startup_log("数据目录：%s（可写）" % PKGVAR)
    _startup_log("ffmpeg：%s" % (FFMPEG_BIN or ("未找到（" + FFMPEG_ERROR + "）")))
    _startup_log("ffprobe：%s" % (FFPROBE_BIN or "未找到（媒体信息读取会失败）"))
    if not FFMPEG_BIN:
        _startup_log("WARN: 系统未找到 ffmpeg，视频处理任务会失败；"
                     "请在飞牛应用中心或系统环境安装 ffmpeg 后重启应用。")
    elif not FFPROBE_BIN:
        _startup_log("WARN: 系统未找到 ffprobe，媒体时长/编码识别会失败；"
                     "请安装完整的 ffmpeg（包含 ffprobe）。")

    _startup_log("访问方式：无需登录；目录范围以 fnOS 应用授权目录为准")
    roots = refresh_roots()
    if roots:
        _startup_log("已授权目录：%s" % "、".join(roots))
    else:
        _startup_log("WARN: 尚未授权任何目录，请先在 系统设置 › 应用 › 视频助手 › 访问权限 中添加")
    _startup_log("运行用户：%s" % (os.environ.get("TRIM_USERNAME") or "video-tool"))

    httpd = None
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        hint = ""
        if getattr(exc, "errno", None) in (48, 98, 10048):
            hint = "端口可能已被其他进程占用。"
        _startup_log("ERROR: 无法监听 %s:%d：%s %s" % (
            args.host, args.port, exc, hint))
        _startup_log("HINT: 在飞牛上运行 ss -ltnp | grep ':%d' 查看占用进程；"
                     "也可以设置环境变量 VT_BIND_PORT 改用其他端口。" % args.port)
        sys.exit(1)
    httpd.daemon_threads = True

    def _on_signal(signum, frame):
        stop_all_tasks()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    _startup_log("已监听 %s:%d" % (args.host, args.port))
    if args.host not in ("127.0.0.1", "::1", "localhost", ""):
        _startup_log("NOTE: 当前监听非回环地址，局域网内可直接访问且无需登录。")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

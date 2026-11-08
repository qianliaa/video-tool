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
import hmac
import html as html_lib
import json
import os
import re
import secrets
import shlex
import shutil
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

APP_VERSION = "1.5.0"
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
USERS_PATH = os.path.join(PKGVAR, "users.json")
SESSIONS_PATH = os.path.join(PKGVAR, "sessions.json")
LIST_DIR = os.path.join(PKGVAR, "lists")

SESSION_COOKIE = "vt_session"
SESSION_TTL_REMEMBER = 400 * 24 * 3600
SESSION_TTL_BROWSER = 12 * 3600
LOGIN_WINDOW = 300
LOGIN_MAX_FAILS = 8
PASSWORD_MIN = 8
PASSWORD_MAX = 128

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

INPUT_DIR = ""
OUTPUT_DIR = ""
FFMPEG_BIN = None
FFPROBE_BIN = None
FFMPEG_ERROR = ""
ROOTS = []

USERS = []
USERS_LOCK = threading.RLock()
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()
LOGIN_FAILS = {}
LOGIN_FAILS_LOCK = threading.Lock()


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


def dir_state(path, write=False):
    if not path:
        return False, "未设置"
    if not os.path.isdir(path):
        return False, "目录不存在"
    if is_app_dir(path):
        return False, "不能使用应用自身目录"
    flags = os.R_OK | os.X_OK | (os.W_OK if write else 0)
    try:
        if not os.access(path, flags):
            return False, "没有%s权限，请先在系统设置中授权该文件夹" % ("读写" if write else "读取")
    except OSError:
        return False, "无法访问该目录"
    return True, ""


def mount_points():
    points = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mp = parts[1].replace("\\040", " ")
                if not mp.startswith("/"):
                    continue
                if mp == "/" or mp.startswith(("/proc", "/sys", "/dev", "/run",
                                               "/usr", "/etc", "/var", "/tmp", "/boot")):
                    continue
                points.append(mp)
    except OSError:
        pass
    return points


def detect_roots():
    roots = []
    candidates = ["/mnt", "/media", "/home"]
    candidates += mount_points()
    try:
        with os.scandir("/") as it:
            for entry in it:
                if re.match(r"^vol\d+$", entry.name, re.I):
                    candidates.append(entry.path)
    except OSError:
        pass
    for p in (INPUT_DIR, OUTPUT_DIR):
        if p:
            candidates.append(p)
    for p in candidates:
        if not p or not os.path.isdir(p):
            continue
        rp = real(p)
        if is_app_dir(rp) or rp in roots:
            continue
        roots.append(rp)
    return roots


def add_root(path):
    rp = real(path)
    if not rp or rp == os.sep:
        return rp
    if os.path.isdir(rp) and not is_app_dir(rp) and rp not in ROOTS:
        ROOTS.append(rp)
    return rp


def within_roots(path):
    rp = real(path)
    for root in ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return True
    return False


def load_config():
    global INPUT_DIR, OUTPUT_DIR, ROOTS
    INPUT_DIR = ""
    OUTPUT_DIR = ""
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, write in (("input_dir", False), ("output_dir", True)):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = real(value)
        if not os.path.isdir(path) or is_app_dir(path):
            continue
        if key == "input_dir":
            INPUT_DIR = path
        else:
            OUTPUT_DIR = path

    for d in (PKGVAR, LIST_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    resolve_binaries()
    ROOTS = detect_roots()


def save_config():
    data = {
        "input_dir": INPUT_DIR,
        "output_dir": OUTPUT_DIR,
    }
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 账号与会话
# --------------------------------------------------------------------------

# fnOS（飞牛）第三方应用目前只能拿到 TRIM_APPDEST / TRIM_PKGVAR 等路径类环境变量，
# 官方文档未提供“当前登录的飞牛账号”注入方式，应用网关也不下发用户身份请求头。
# 因此不猜测主账号身份：应用内首次设置的管理员就是最高权限账号。
FNOS_IDENTITY_SOURCE = "app-admin"
FNOS_IDENTITY_NOTE = "fnOS 未向第三方应用提供当前登录账号（无相关环境变量或请求头），应用内管理员为最高权限"


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _hash_password(password, salt=None, n=16384, r=8, p=1):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return {"algo": "scrypt", "n": n, "r": r, "p": p,
            "salt": salt.hex(), "hash": digest.hex()}


def _check_password(user, password):
    try:
        salt = bytes.fromhex(str(user.get("salt") or ""))
        expected = str(user.get("hash") or "")
        if not salt or not expected:
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(user.get("n") or 16384),
                                r=int(user.get("r") or 8),
                                p=int(user.get("p") or 1),
                                dklen=len(bytes.fromhex(expected)),
                                maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError, OSError):
        return False


def normalize_username(value):
    name = (value or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,32}$", name):
        raise ValueError("账号名需为 2-32 位字母、数字、下划线、点或短横线")
    return name.lower()


def check_password_value(password):
    if not isinstance(password, str):
        password = ""
    if len(password) < PASSWORD_MIN:
        raise ValueError("密码至少需要 %d 个字符" % PASSWORD_MIN)
    if len(password) > PASSWORD_MAX:
        raise ValueError("密码不能超过 %d 个字符" % PASSWORD_MAX)
    return password


def load_users():
    global USERS
    with USERS_LOCK:
        USERS = []
    if os.environ.get("VT_RESET_AUTH", "").strip().lower() in ("1", "true", "yes", "on"):
        for path in (USERS_PATH, SESSIONS_PATH):
            try:
                os.remove(path)
                sys.stderr.write("access control: removed %s by VT_RESET_AUTH\n" % path)
            except OSError:
                pass
        return
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return
    items = data.get("users") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    users = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("username") or "").strip()
        if (not name or item.get("algo") != "scrypt"
                or not item.get("salt") or not item.get("hash")):
            continue
        users.append({
            "username": name,
            "role": "admin" if item.get("role") == "admin" else "user",
            "enabled": bool(item.get("enabled", True)),
            "algo": "scrypt",
            "n": int(item.get("n") or 16384),
            "r": int(item.get("r") or 8),
            "p": int(item.get("p") or 1),
            "salt": str(item.get("salt")),
            "hash": str(item.get("hash")),
            "created": int(item.get("created") or 0),
            "updated": int(item.get("updated") or 0),
        })
    with USERS_LOCK:
        USERS = users


def save_users():
    with USERS_LOCK:
        _atomic_write_json(USERS_PATH, {"version": 1, "users": USERS})


def users_configured():
    with USERS_LOCK:
        return bool(USERS)


def find_user(name):
    target = (name or "").strip().lower()
    if not target:
        return None
    with USERS_LOCK:
        for user in USERS:
            if user["username"] == target:
                return user
    return None


def admin_count():
    with USERS_LOCK:
        return sum(1 for u in USERS if u.get("role") == "admin")


def create_user(name, password, role="user", enabled=True):
    username = normalize_username(name)
    password = check_password_value(password)
    with USERS_LOCK:
        if any(u["username"] == username for u in USERS):
            raise ValueError("账号已存在：%s" % username)
        now = int(time.time())
        user = {"username": username,
                "role": "admin" if role == "admin" else "user",
                "enabled": bool(enabled),
                "created": now, "updated": now}
        user.update(_hash_password(password))
        USERS.append(user)
        try:
            save_users()
        except OSError:
            USERS.remove(user)
            raise ValueError("保存账号失败，请检查配置目录权限")
        return user


def set_user_password(username, password):
    password = check_password_value(password)
    user = find_user(username)
    if not user:
        raise ValueError("账号不存在")
    updated = _hash_password(password)
    user.update(updated)
    user["updated"] = int(time.time())
    with USERS_LOCK:
        try:
            save_users()
        except OSError:
            raise ValueError("保存账号失败，请检查配置目录权限")
    return user


def _session_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_sessions():
    global SESSIONS
    data = {}
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        data = {}
    items = data.get("sessions") if isinstance(data, dict) else None
    now = time.time()
    sessions = {}
    if isinstance(items, dict):
        for key, item in items.items():
            if not isinstance(item, dict):
                continue
            exp = item.get("exp")
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                continue
            if exp <= now or not item.get("u"):
                continue
            sessions[key] = {"u": str(item.get("u")),
                             "exp": exp,
                             "remember": bool(item.get("remember", True))}
    with SESSIONS_LOCK:
        SESSIONS = sessions


def save_sessions():
    with SESSIONS_LOCK:
        try:
            _atomic_write_json(SESSIONS_PATH, {"version": 1, "sessions": SESSIONS})
        except OSError:
            pass


def issue_session(username, remember):
    token = secrets.token_urlsafe(32)
    now = time.time()
    ttl = SESSION_TTL_REMEMBER if remember else SESSION_TTL_BROWSER
    with SESSIONS_LOCK:
        for key in [k for k, v in SESSIONS.items() if v.get("exp", 0) <= now]:
            SESSIONS.pop(key, None)
        SESSIONS[_session_key(token)] = {"u": username, "exp": now + ttl,
                                        "remember": bool(remember)}
    save_sessions()
    return token


def drop_session(token):
    if not token:
        return
    with SESSIONS_LOCK:
        if SESSIONS.pop(_session_key(token), None) is not None:
            save_sessions()


def drop_user_sessions(username, keep=None):
    target = (username or "").strip().lower()
    keep_key = _session_key(keep) if keep else None
    with SESSIONS_LOCK:
        keys = [k for k, v in SESSIONS.items()
                if v.get("u") == target and k != keep_key]
        for key in keys:
            SESSIONS.pop(key, None)
        if keys:
            save_sessions()


def cookie_value(raw, name):
    for part in (raw or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value.strip()
    return ""


def session_user(handler):
    """返回当前请求对应的用户；顺带做会话滑动续期与账号启停校验。"""
    token = cookie_value(handler.headers.get("Cookie", ""), SESSION_COOKIE)
    if not token:
        return None
    key = _session_key(token)
    now = time.time()
    dirty = False
    with SESSIONS_LOCK:
        item = SESSIONS.get(key)
        if not item or item.get("exp", 0) <= now:
            if item:
                SESSIONS.pop(key, None)
                dirty = True
            item = None
        else:
            ttl = SESSION_TTL_REMEMBER if item.get("remember") else SESSION_TTL_BROWSER
            if item["exp"] - now < ttl / 2:
                item["exp"] = now + ttl
                dirty = True
    if dirty:
        save_sessions()
    if not item:
        return None
    user = find_user(item.get("u"))
    if not user or not user.get("enabled"):
        drop_session(token)
        return None
    return dict(user)


def login_fail_count(ip):
    now = time.time()
    with LOGIN_FAILS_LOCK:
        items = [t for t in LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
        LOGIN_FAILS[ip] = items
        return len(items)


def note_login_fail(ip):
    with LOGIN_FAILS_LOCK:
        LOGIN_FAILS.setdefault(ip, []).append(time.time())


def clear_login_fail(ip):
    with LOGIN_FAILS_LOCK:
        LOGIN_FAILS.pop(ip, None)


def make_session_cookie(token, max_age=None):
    parts = ["%s=%s" % (SESSION_COOKIE, token), "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age is not None:
        parts.append("Max-Age=%d" % max_age)
    return "; ".join(parts)


def user_public(user):
    return {"username": user["username"],
            "role": user["role"],
            "enabled": bool(user.get("enabled")),
            "created": user.get("created") or 0,
            "updated": user.get("updated") or 0}


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


def require_input_dir():
    if not INPUT_DIR:
        raise ValueError("请先在设置中选择输入目录")
    ok, err = dir_state(INPUT_DIR, False)
    if not ok:
        raise ValueError("输入目录不可用：%s" % err)


def require_output_dir():
    if not OUTPUT_DIR:
        raise ValueError("请先在设置中选择输出目录")
    ok, err = dir_state(OUTPUT_DIR, True)
    if not ok:
        raise ValueError("输出目录不可用：%s" % err)


def output_path(name, default_ext=".mkv"):
    require_output_dir()
    name = safe_name(name, default_ext)
    base = real(OUTPUT_DIR)
    path = real(os.path.join(base, name))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("输出路径超出输出目录")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def input_path(value):
    if not value:
        raise ValueError("请选择输入文件")
    require_input_dir()
    base = real(INPUT_DIR)
    path = real(value if os.path.isabs(value) else os.path.join(base, value))
    if not inside(path, base):
        raise ValueError("输入文件不在输入目录内")
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
    if len(task["log"]) > 4000:
        del task["log"][:1000]
        task["log"].insert(0, "... 日志过长，已截断 ...")


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
    try:
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
               total_bytes=None, partial=None):
    task_id = uuid.uuid4().hex
    task = {
        "id": task_id,
        "tool": tool,
        "action": action,
        "cmd": " ".join(shlex.quote(x) for x in cmd),
        "status": "running",
        # 拿不到总时长/总字节时用 -1 表示进度未知，避免界面一直显示 0%
        "progress": 0.0 if (duration or total_bytes) else -1.0,
        "log": [],
        "created": time.time(),
        "finished": None,
        "returncode": None,
        "proc": None,
        "canceled": False,
        "duration": duration,
        "total_bytes": total_bytes,
        "written": 0,
        "elapsed": 0.0,
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


def build_ffmpeg(action, p):
    ffmpeg = require_ffmpeg()
    prefix = ffmpeg_prefix(ffmpeg)

    if action == "merge":
        files = p.get("files") or []
        if len(files) < 2:
            raise ValueError("至少选择 2 个文件")
        paths = [input_path(f) for f in files]
        out = output_path(p.get("output"), ".mkv")
        base, ext = os.path.splitext(out)
        index = 1
        while os.path.exists(out):
            out = "%s (%d)%s" % (base, index, ext)
            index += 1
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

    src = input_path(p.get("input"))

    if action == "convert":
        out = output_path(p.get("output"), ".mp4")
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
        out = output_path(p.get("output"), ".mp4")
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
        out = output_path(p.get("output"), ".mkv")
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
        out = output_path(p.get("output"), ".mp3")
        ext = os.path.splitext(out)[1].lower()
        if ext not in AUDIO_CODECS:
            raise ValueError("不支持的音频格式：%s" % ext)
        cmd = prefix + ["-i", src, "-vn"] + AUDIO_CODECS[ext] + [out]
        return cmd, probe_duration(src), None

    if action == "subtitle":
        sub = p.get("subtitle")
        if not sub:
            raise ValueError("请选择字幕文件")
        sub = real(sub if os.path.isabs(sub) else os.path.join(INPUT_DIR, sub))
        if not within_roots(sub) or not os.path.isfile(sub):
            raise ValueError("字幕文件不存在或不可访问")
        out = output_path(p.get("output"), ".mp4")
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
        out = output_path(p.get("output"), ".mp4")
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
        out = output_path(p.get("output"), ".mp4")
        cmd = prefix + ["-i", src, "-vf", vf,
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-b:a", "192k", out]
        return cmd, probe_duration(src), None

    if action == "screenshot":
        at = parse_time(p.get("time")) or 0.0
        out = output_path(p.get("output"), ".jpg")
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
        out = output_path(p.get("output"), ".mkv")
        has_in = any("{input}" in a for a in args)
        has_out = any("{output}" in a for a in args)
        args = [a.replace("{input}", src).replace("{output}", out) for a in args]
        cmd = list(prefix)
        if not has_in:
            cmd += ["-i", src]
        cmd += args
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


def make_opener(proxy=""):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def fetch_text(url, headers, proxy="", limit=MAX_PAGE_BYTES):
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
        return url, kind, ""

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
            return candidate, kind, title

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
                return candidate, sub_kind, title or sub_title

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


def build_download(p):
    url = clean_media_url(p.get("url"))
    if not url:
        raise ValueError("请填写视频地址")
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("目前只支持 http/https 地址")

    ffmpeg = require_ffmpeg()
    require_output_dir()

    headers = download_headers(p)
    proxy = (p.get("proxy") or "").strip()
    media_url, kind, title = resolve_media(url, headers, proxy)

    note = ["下载引擎：ffmpeg", "媒体类型：%s" % {"hls": "HLS", "dash": "DASH"}.get(kind, "直链媒体")]
    total_time = None
    if kind == "hls":
        try:
            text, _, final_url = fetch_text(media_url, headers, proxy)
            variants = hls_variants(text, final_url)
            if variants:
                selected = pick_variant(variants, str(p.get("quality") or "best"))
                media_url = selected["url"]
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
    out = output_path(target, ".mp3" if mode == "audio" else ".mp4")

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

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoTool/%s" % APP_VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")

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
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
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
        return session_user(self)

    def _auth_guard(self, path):
        """静态资源与登录/状态接口放行，其余 API 均需登录。"""
        if not path.startswith("/api/"):
            return True
        open_paths = ("/api/auth/status", "/api/auth/setup",
                      "/api/auth/login", "/api/auth/logout")
        if path in open_paths:
            return True
        if self.current_user():
            return True
        self.send_json({"error": "未登录或登录已过期", "code": "unauthorized"}, 401)
        return False

    def _require_admin(self):
        user = self.current_user()
        if not user:
            self.send_json({"error": "未登录或登录已过期", "code": "unauthorized"}, 401)
            return None
        if user.get("role") != "admin":
            self.send_json({"error": "只有管理员可以管理用户", "code": "forbidden"}, 403)
            return None
        return user

    # ---------------- GET ----------------

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        try:
            if not self._auth_guard(path):
                return
            if path in ("/", "/index.html"):
                self.send_file(os.path.join(UI_DIR, "index.html"))
            elif path == "/api/auth/status":
                self.api_auth_status()
            elif path == "/api/users":
                self.api_users()
            elif path == "/api/config":
                self.api_config()
            elif path == "/api/tools":
                user = self.current_user()
                info = tool_info()
                info.update({
                    "input_dir": INPUT_DIR,
                    "output_dir": OUTPUT_DIR,
                    "roots": ROOTS,
                    "pkgvar": PKGVAR,
                    "auth_enabled": users_configured(),
                    "auth_file": USERS_PATH,
                    "username": user["username"] if user else "",
                    "role": user["role"] if user else "",
                    "is_admin": bool(user and user.get("role") == "admin"),
                    "identity_source": FNOS_IDENTITY_SOURCE,
                    "identity_note": FNOS_IDENTITY_NOTE,
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
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def api_auth_status(self):
        user = self.current_user()
        self.send_json({
            "configured": users_configured(),
            "authenticated": bool(user),
            "setup_required": not users_configured(),
            "password_min": PASSWORD_MIN,
            "username": user["username"] if user else "",
            "role": user["role"] if user else "",
            "is_admin": bool(user and user.get("role") == "admin"),
            "auth_file": USERS_PATH,
            "identity_source": FNOS_IDENTITY_SOURCE,
            "identity_note": FNOS_IDENTITY_NOTE,
        })

    def api_users(self):
        if not self._require_admin():
            return
        with USERS_LOCK:
            users = [user_public(u) for u in sorted(USERS, key=lambda x: x["username"])]
        self.send_json({"users": users, "current": self.current_user()["username"]})

    def api_config(self):
        input_ok, input_error = dir_state(INPUT_DIR, False)
        output_ok, output_error = dir_state(OUTPUT_DIR, True)
        resolve_binaries()
        self.send_json({
            "input_dir": INPUT_DIR,
            "output_dir": OUTPUT_DIR,
            "input_ok": input_ok,
            "input_error": input_error,
            "output_ok": output_ok,
            "output_error": output_error,
            "configured": input_ok and output_ok,
            "roots": ROOTS,
            "pkgvar": PKGVAR,
            "ffmpeg": FFMPEG_BIN,
            "ffprobe": FFPROBE_BIN,
            "ffmpeg_ready": bool(FFMPEG_BIN),
            "ffmpeg_error": FFMPEG_ERROR,
        })

    def api_update(self):
        q = self.query()
        force = str(q.get("refresh", "")).lower() in ("1", "true", "yes")
        self.send_json(check_update(force))

    def api_browse(self):
        q = self.query()
        path = q.get("path") or INPUT_DIR or OUTPUT_DIR or (ROOTS[0] if ROOTS else "/")
        if not path:
            self.send_json({"error": "缺少 path"}, 400)
            return
        path = real(path)
        if is_app_dir(path):
            self.send_json({"error": "不能浏览应用自身目录"}, 403)
            return
        if not os.path.isdir(path):
            self.send_json({"error": "目录不存在：%s" % path}, 404)
            return
        add_root(path)

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
                        if is_app_dir(entry.path):
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
            error = "没有权限访问该目录，请先在 系统设置 > 应用 > 视频助手 中授权，或直接粘贴完整路径"
        except OSError as exc:
            readable = False
            error = "读取目录失败：%s" % exc

        dirs.sort(key=lambda x: x["name"].lower())
        parent = os.path.dirname(path)
        if parent == path or is_app_dir(parent):
            parent = None
        self.send_json({
            "path": path,
            "parent": parent,
            "roots": ROOTS,
            "dirs": dirs,
            "readable": readable,
            "writable": os.access(path, os.W_OK),
            "error": error,
        })

    def api_files(self):
        q = self.query()
        if not INPUT_DIR:
            self.send_json({"error": "请先在设置中选择输入目录"}, 400)
            return
        path = q.get("path") or INPUT_DIR
        path = real(path)
        if not inside(path, INPUT_DIR):
            self.send_json({"error": "只能读取输入目录中的文件"}, 403)
            return
        if not os.path.isdir(path):
            self.send_json({"error": "目录不存在"}, 404)
            return
        ok, err = dir_state(path, False)
        if not ok:
            self.send_json({"error": "输入目录不可用：%s" % err}, 403)
            return

        files = []
        out_real = real(OUTPUT_DIR) if OUTPUT_DIR else ""
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
                    if is_app_dir(full):
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
        q = self.query()
        path = q.get("path") or ""
        if not path:
            self.send_json({"error": "缺少 path"}, 400)
            return
        path = real(path)
        if not INPUT_DIR or not inside(path, INPUT_DIR) or not os.path.isfile(path):
            self.send_json({"error": "文件不存在或不可访问"}, 404)
            return
        self.send_json(probe_file(path))

    def api_tasks(self):
        with TASKS_LOCK:
            items = []
            for task in sorted(TASKS.values(), key=lambda t: t["created"], reverse=True)[:20]:
                items.append({
                    "id": task["id"],
                    "tool": task["tool"],
                    "action": task["action"],
                    "status": task["status"],
                    "progress": task["progress"],
                    "written": task.get("written", 0),
                    "elapsed": task.get("elapsed", 0.0),
                    "created": task["created"],
                    "finished": task["finished"],
                })
        self.send_json({"tasks": items})

    def api_log(self, task_id):
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if not task:
                self.send_json({"error": "任务不存在"}, 404)
                return
            payload = {
                "id": task["id"],
                "status": task["status"],
                "progress": task["progress"],
                "written": task.get("written", 0),
                "elapsed": task.get("elapsed", 0.0),
                "returncode": task["returncode"],
                "log": list(task["log"]),
            }
        self.send_json(payload)

    # ---------------- POST ----------------

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if not self._auth_guard(path):
                return
            if path == "/api/auth/setup":
                self.api_auth_setup()
            elif path == "/api/auth/login":
                self.api_auth_login()
            elif path == "/api/auth/logout":
                self.api_auth_logout()
            elif path == "/api/auth/password":
                self.api_auth_password()
            elif path == "/api/users/create":
                self.api_user_create()
            elif path == "/api/users/update":
                self.api_user_update()
            elif path == "/api/users/password":
                self.api_user_password()
            elif path == "/api/users/delete":
                self.api_user_delete()
            elif path == "/api/config":
                self.api_save_config()
            elif path == "/api/checkdir":
                self.api_checkdir()
            elif path == "/api/mkdir":
                self.api_mkdir()
            elif path == "/api/task":
                self.api_task()
            elif path.startswith("/api/cancel/"):
                self.api_cancel(path[len("/api/cancel/"):])
            else:
                self.send_json({"error": "not found"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def _session_saved(self, token, remember):
        max_age = SESSION_TTL_REMEMBER if remember else None
        return [("Set-Cookie", make_session_cookie(token, max_age))]

    def api_auth_setup(self):
        if users_configured():
            self.send_json({"error": "管理员账号已设置，请直接登录", "code": "already_configured"}, 409)
            return
        data = self.read_json()
        username = normalize_username(data.get("username") or "admin")
        password = check_password_value(data.get("password"))
        remember = bool(data.get("remember", True))
        create_user(username, password, role="admin", enabled=True)
        token = issue_session(username, remember)
        self.send_json({"ok": True, "configured": True, "username": username,
                        "role": "admin", "is_admin": True},
                       extra_headers=self._session_saved(token, remember))

    def api_auth_login(self):
        if not users_configured():
            self.send_json({"error": "尚未设置管理员账号", "code": "setup_required"}, 409)
            return
        ip = self.client_address[0]
        if login_fail_count(ip) >= LOGIN_MAX_FAILS:
            self.send_json({"error": "尝试次数过多，请稍后再试", "code": "too_many_attempts"},
                           429, [("Retry-After", str(LOGIN_WINDOW))])
            return
        data = self.read_json()
        username = (data.get("username") or "").strip().lower()
        password = data.get("password")
        if not isinstance(password, str):
            password = ""
        user = find_user(username)
        if not user or not user.get("enabled") or not _check_password(user, password):
            note_login_fail(ip)
            time.sleep(0.35)
            if user and not user.get("enabled"):
                self.send_json({"error": "该账号已被停用", "code": "disabled"}, 403)
            else:
                self.send_json({"error": "账号或密码不正确", "code": "bad_password"}, 401)
            return
        clear_login_fail(ip)
        remember = bool(data.get("remember", True))
        token = issue_session(user["username"], remember)
        self.send_json({"ok": True, "configured": True,
                        "username": user["username"], "role": user["role"],
                        "is_admin": user["role"] == "admin"},
                       extra_headers=self._session_saved(token, remember))

    def api_auth_logout(self):
        drop_session(cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE))
        self.send_json({"ok": True},
                       extra_headers=[("Set-Cookie",
                                       "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                                       % SESSION_COOKIE)])

    def api_auth_password(self):
        user = self.current_user()
        if not user:
            self.send_json({"error": "未登录或登录已过期", "code": "unauthorized"}, 401)
            return
        data = self.read_json()
        set_user_password(user["username"], data.get("password"))
        token = cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE)
        drop_user_sessions(user["username"], keep=token)
        self.send_json({"ok": True})

    def api_user_create(self):
        if not self._require_admin():
            return
        data = self.read_json()
        username = normalize_username(data.get("username"))
        password = check_password_value(data.get("password"))
        role = "admin" if data.get("role") == "admin" else "user"
        user = create_user(username, password, role=role, enabled=True)
        self.send_json({"ok": True, "user": user_public(user)})

    def api_user_update(self):
        if not self._require_admin():
            return
        operator = self.current_user()
        data = self.read_json()
        username = (data.get("username") or "").strip().lower()
        user = find_user(username)
        if not user:
            raise ValueError("账号不存在")
        if "enabled" in data:
            enabled = bool(data.get("enabled"))
            if not enabled and user["username"] == operator["username"]:
                raise ValueError("不能停用当前登录的管理员账号")
            if (not enabled and user["role"] == "admin"
                    and admin_count() <= 1):
                raise ValueError("至少需要保留一个可用的管理员账号")
            user["enabled"] = enabled
        if "role" in data:
            role = "admin" if data.get("role") == "admin" else "user"
            if (role != "admin" and user["role"] == "admin"
                    and (user["username"] == operator["username"] or admin_count() <= 1)):
                raise ValueError("不能降级当前账号或最后一个管理员")
            user["role"] = role
        user["updated"] = int(time.time())
        try:
            save_users()
        except OSError:
            raise ValueError("保存账号失败，请检查配置目录权限")
        if not user.get("enabled"):
            drop_user_sessions(user["username"])
        self.send_json({"ok": True, "user": user_public(user)})

    def api_user_password(self):
        if not self._require_admin():
            return
        data = self.read_json()
        username = (data.get("username") or "").strip().lower()
        set_user_password(username, data.get("password"))
        drop_user_sessions(username)
        self.send_json({"ok": True})

    def api_user_delete(self):
        if not self._require_admin():
            return
        operator = self.current_user()
        data = self.read_json()
        username = (data.get("username") or "").strip().lower()
        if username == operator["username"]:
            raise ValueError("不能删除当前登录的管理员账号")
        with USERS_LOCK:
            target = next((u for u in USERS if u["username"] == username), None)
            if not target:
                raise ValueError("账号不存在")
            if target["role"] == "admin" and admin_count() <= 1:
                raise ValueError("至少需要保留一个管理员账号")
            USERS.remove(target)
            try:
                save_users()
            except OSError:
                USERS.append(target)
                raise ValueError("保存账号失败，请检查配置目录权限")
        drop_user_sessions(username)
        self.send_json({"ok": True})

    def api_save_config(self):
        global INPUT_DIR, OUTPUT_DIR
        data = self.read_json()
        new_input = data.get("input_dir") if "input_dir" in data else INPUT_DIR
        new_output = data.get("output_dir") if "output_dir" in data else OUTPUT_DIR
        new_input = (new_input or "").strip()
        new_output = (new_output or "").strip()

        if new_input:
            if not os.path.isabs(new_input):
                raise ValueError("请输入完整路径，例如 /vol1/1000/视频")
            new_input = real(new_input)
            if not os.path.isdir(new_input):
                raise ValueError("输入目录不存在：%s" % new_input)
            if is_app_dir(new_input):
                raise ValueError("输入目录不能使用应用自身目录")
            ok, err = dir_state(new_input, False)
            if not ok:
                raise ValueError("输入目录不可用：%s" % err)

        if new_output:
            if not os.path.isabs(new_output):
                raise ValueError("请输入完整路径，例如 /vol1/1000/输出")
            new_output = real(new_output)
            if not os.path.isdir(new_output):
                raise ValueError("输出目录不存在：%s" % new_output)
            if is_app_dir(new_output):
                raise ValueError("输出目录不能使用应用自身目录")
            ok, err = dir_state(new_output, True)
            if not ok:
                raise ValueError("输出目录不可用：%s" % err)

        INPUT_DIR = new_input
        OUTPUT_DIR = new_output
        for p in (INPUT_DIR, OUTPUT_DIR):
            if p:
                add_root(p)
        resolve_binaries()
        save_config()

        input_ok, input_error = dir_state(INPUT_DIR, False)
        output_ok, output_error = dir_state(OUTPUT_DIR, True)
        self.send_json({
            "ok": True,
            "input_dir": INPUT_DIR,
            "output_dir": OUTPUT_DIR,
            "input_ok": input_ok,
            "input_error": input_error,
            "output_ok": output_ok,
            "output_error": output_error,
            "roots": ROOTS,
            "ffmpeg": FFMPEG_BIN,
            "ffprobe": FFPROBE_BIN,
            "ffmpeg_ready": bool(FFMPEG_BIN),
            "ffmpeg_error": FFMPEG_ERROR,
        })

    def api_checkdir(self):
        data = self.read_json()
        path = (data.get("path") or "").strip()
        purpose = (data.get("purpose") or "input").strip()
        if not path:
            raise ValueError("请填写目录路径")
        if not os.path.isabs(path):
            raise ValueError("请填写完整路径，例如 /vol1/1000/视频")
        path = real(path)
        if is_app_dir(path):
            raise ValueError("不能使用应用自身目录")
        if not os.path.isdir(path):
            raise ValueError("目录不存在：%s" % path)
        ok, err = dir_state(path, purpose == "output")
        if not ok:
            raise ValueError(err)
        add_root(path)
        self.send_json({
            "ok": True,
            "path": path,
            "writable": os.access(path, os.W_OK),
            "roots": ROOTS,
        })

    def api_mkdir(self):
        data = self.read_json()
        path = (data.get("path") or "").strip()
        if not path:
            raise ValueError("缺少 path")
        if not os.path.isabs(path):
            raise ValueError("请填写完整路径")
        path = real(path)
        if path == os.sep or is_app_dir(path):
            raise ValueError("不能使用应用自身目录")
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise ValueError("上级目录不存在")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("没有写入权限，请先在 系统设置 > 应用 > 视频助手 中授权该文件夹")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise ValueError("创建失败：%s" % exc)
        if not os.path.isdir(path):
            raise ValueError("创建失败")
        add_root(parent)
        self.send_json({"ok": True, "path": path})

    def api_task(self):
        data = self.read_json()
        action = (data.get("action") or "").strip()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("参数格式不正确")

        note = None
        total_bytes = None
        partial = None
        if action == "download":
            tool = "ffmpeg"
            cmd, duration, cleanup, note, total_bytes, partial = build_download(params)
        elif action.startswith("ytdlp"):
            raise ValueError("已移除 yt-dlp，请使用 ffmpeg 原生下载")
        else:
            tool = "ffmpeg"
            cmd, duration, cleanup = build_ffmpeg(action, params)

        task = start_task(tool, action, cmd, duration=duration, cleanup=cleanup,
                          note=note, total_bytes=total_bytes, partial=partial)
        self.send_json({"task": task["id"], "cmd": task["cmd"]})

    def api_cancel(self, task_id):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()

    load_config()
    load_users()
    load_sessions()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    def _on_signal(signum, frame):
        stop_all_tasks()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print("serving on %s:%d" % (args.host, args.port))
    print("input:  %s" % INPUT_DIR)
    print("output: %s" % OUTPUT_DIR)
    print("ffmpeg: %s" % (FFMPEG_BIN or ("未就绪：" + FFMPEG_ERROR)))
    print("roots:  %s" % ", ".join(ROOTS))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

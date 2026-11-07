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
import html as html_lib
import json
import os
import re
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

PKGVAR = os.environ.get("TRIM_PKGVAR") or os.path.join(os.path.expanduser("~"), ".video-tool")
CONFIG_PATH = os.path.join(PKGVAR, "config.json")
LIST_DIR = os.path.join(PKGVAR, "lists")

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv", ".webm", ".m4v",
              ".wmv", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob", ".ogv", ".rmvb")
AUDIO_EXTS = (".mp3", ".aac", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".wma")
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")

URL_MEDIA_EXTS = (".m3u8", ".mpd", ".mp4", ".mkv", ".mov", ".m4v", ".webm",
                  ".ts", ".flv", ".avi", ".mp3", ".m4a", ".aac", ".flac",
                  ".wav", ".ogg", ".opus")
DOWNLOAD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0 Safari/537.36 VideoTool/1.3.1")
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
ROOTS = []


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def find_bin(name):
    path = shutil.which(name)
    if path:
        return path
    candidates = [os.path.join(BIN_DIR, name), "/usr/bin/" + name, "/usr/local/bin/" + name]
    for app in ("ffmpeg", "ffmpeg7", "ffmpeg6"):
        candidates.append("/var/apps/%s/target/bin/%s" % (app, name))
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


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
    ROOTS = detect_roots()


def save_config():
    data = {"input_dir": INPUT_DIR, "output_dir": OUTPUT_DIR}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except OSError:
        pass


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
    ffprobe = find_bin("ffprobe")
    if not ffprobe:
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
    ffprobe = find_bin("ffprobe")
    if not ffprobe:
        return {"error": "未找到 ffprobe"}
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
    info = {"ffmpeg": None, "ffmpeg_dir": None, "ffprobe": None,
            "ffmpeg_version": None, "downloader": "ffmpeg"}
    ffmpeg = find_bin("ffmpeg")
    ffprobe = find_bin("ffprobe")
    info["ffmpeg"] = ffmpeg
    info["ffprobe"] = ffprobe
    if ffmpeg:
        info["ffmpeg_dir"] = os.path.dirname(ffmpeg)
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
    ffmpeg = find_bin("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 ffmpeg，请确认系统已安装")
    prefix = ffmpeg_prefix(ffmpeg)

    if action == "merge":
        files = p.get("files") or []
        if len(files) < 2:
            raise ValueError("至少选择 2 个文件")
        paths = [input_path(f) for f in files]
        out = output_path(p.get("output"), ".mkv")
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

    ffmpeg = find_bin("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 ffmpeg，无法下载")
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
    if ffmpeg_support(ffmpeg, "extension_picky"):
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
# HTTP 处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoTool/1.3.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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

    # ---------------- GET ----------------

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        try:
            if path in ("/", "/index.html"):
                self.send_file(os.path.join(UI_DIR, "index.html"))
            elif path == "/api/config":
                self.api_config()
            elif path == "/api/tools":
                info = tool_info()
                info.update({
                    "input_dir": INPUT_DIR,
                    "output_dir": OUTPUT_DIR,
                    "roots": ROOTS,
                    "pkgvar": PKGVAR,
                })
                self.send_json(info)
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

    def api_config(self):
        input_ok, input_error = dir_state(INPUT_DIR, False)
        output_ok, output_error = dir_state(OUTPUT_DIR, True)
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
        })

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
            if path == "/api/config":
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
    print("roots:  %s" % ", ".join(ROOTS))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

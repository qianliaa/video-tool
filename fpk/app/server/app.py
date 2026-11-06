# -*- coding: utf-8 -*-
"""视频助手后端：只依赖 Python 标准库，避免安装期 pip/venv 失败导致应用无法启动。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BASE_DIR)
UI_DIR = os.path.join(APP_DIR, "ui")
BIN_DIR = os.path.join(APP_DIR, "bin")

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.environ.get("TRIM_PKGVAR", "/tmp"), "data")
MEDIA_DIR = os.environ.get("MEDIA_DIR") or os.path.join(os.environ.get("TRIM_PKGVAR", "/tmp"), "media")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError as _e:
    print("WARN: %s" % _e)
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
except OSError as _e:
    print("WARN: %s" % _e)

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv",
              ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".3gp")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

TASKS = {}
LOCK = threading.Lock()


def find_bin(name):
    path = shutil.which(name)
    if path:
        return path
    for cand in (os.path.join(BIN_DIR, name), "/usr/bin/" + name, "/usr/local/bin/" + name):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def safe_join(base, rel):
    target = os.path.realpath(os.path.join(base, rel))
    base_real = os.path.realpath(base)
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


def run_task(tid, cmd):
    with LOCK:
        task = TASKS[tid]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            task["log"].append(line.rstrip())
        code = proc.wait()
        task["status"] = "done" if code == 0 else "failed"
        task["log"].append("-- process exited with code %d --" % code)
    except Exception as exc:
        task["log"].append("ERROR: %s" % exc)
        task["status"] = "failed"


def start_task(cmd):
    tid = uuid.uuid4().hex
    with LOCK:
        TASKS[tid] = {"log": [" ".join(cmd)], "status": "running"}
    threading.Thread(target=run_task, args=(tid, cmd), daemon=True).start()
    return tid


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoTool/1.0"

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

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path in ("/", "/index.html"):
            self.send_file(os.path.join(UI_DIR, "index.html"))
            return

        if path == "/api/info":
            self.send_json({"media_dir": MEDIA_DIR})
            return

        if path == "/api/files":
            files = []
            for root_dir, _dirs, names in os.walk(MEDIA_DIR):
                for name in names:
                    if name.lower().endswith(VIDEO_EXTS):
                        full = os.path.join(root_dir, name)
                        files.append({
                            "path": os.path.relpath(full, MEDIA_DIR).replace("\\", "/"),
                            "size": os.path.getsize(full),
                        })
            files.sort(key=lambda x: x["path"].lower())
            self.send_json(files)
            return

        if path.startswith("/api/log/"):
            tid = path[len("/api/log/"):]
            with LOCK:
                task = TASKS.get(tid)
            if not task:
                self.send_json({"error": "任务不存在"}, 404)
                return
            self.send_json({"status": task["status"], "log": list(task["log"])})
            return

        target = safe_join(UI_DIR, path.lstrip("/"))
        if target and os.path.isfile(target):
            self.send_file(target)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/merge":
            self.handle_merge()
        elif path == "/api/download":
            self.handle_download()
        else:
            self.send_json({"error": "not found"}, 404)

    def handle_merge(self):
        data = self.read_json()
        files = data.get("files") or []
        output = (data.get("output") or "merged.mkv").strip()
        reencode = bool(data.get("reencode"))

        if len(files) < 2:
            self.send_json({"error": "至少需要 2 个文件"}, 400)
            return

        abs_files = []
        for rel in files:
            p = safe_join(MEDIA_DIR, rel)
            if not p or not os.path.isfile(p):
                self.send_json({"error": "文件不存在：%s" % rel}, 400)
                return
            abs_files.append(p)

        out_path = safe_join(MEDIA_DIR, output)
        if not out_path:
            self.send_json({"error": "输出路径非法"}, 400)
            return
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        ffmpeg = find_bin("ffmpeg")
        if not ffmpeg:
            self.send_json({"error": "未找到 ffmpeg，请确认系统已安装 ffmpeg"}, 500)
            return

        tid = uuid.uuid4().hex
        listfile = os.path.join(DATA_DIR, tid + ".txt")
        with open(listfile, "w", encoding="utf-8") as fp:
            for p in abs_files:
                fp.write("file '%s'\n" % p.replace("'", "'\\''"))

        if reencode:
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                   "-c:a", "aac", "-b:a", "192k", out_path]
        else:
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                   "-c", "copy", out_path]

        self.send_json({"task": start_task(cmd)})

    def handle_download(self):
        data = self.read_json()
        url = (data.get("url") or "").strip()
        subdir = (data.get("subdir") or "").strip()
        if not url:
            self.send_json({"error": "缺少 URL"}, 400)
            return

        out_dir = MEDIA_DIR if not subdir else safe_join(MEDIA_DIR, subdir)
        if not out_dir:
            self.send_json({"error": "目录非法"}, 400)
            return
        os.makedirs(out_dir, exist_ok=True)

        ytdlp = os.path.join(BIN_DIR, "yt-dlp")
        if os.path.isfile(ytdlp):
            cmd = [sys.executable, ytdlp, url]
        else:
            found = find_bin("yt-dlp")
            if not found:
                self.send_json({"error": "未找到 yt-dlp"}, 500)
                return
            cmd = [found, url]

        ffmpeg = find_bin("ffmpeg")
        if ffmpeg:
            cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
        cmd += ["-o", os.path.join(out_dir, "%(title)s.%(ext)s")]

        self.send_json({"task": start_task(cmd)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("serving on %s:%d, ui=%s, media=%s" % (args.host, args.port, UI_DIR, MEDIA_DIR))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

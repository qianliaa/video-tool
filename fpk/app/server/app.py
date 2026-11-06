# -*- coding: utf-8 -*-
import os
import subprocess
import threading
import uuid

from flask import Flask, request, jsonify, render_template

app = Flask(__name__, static_folder="../ui", static_url_path="")

DATA_DIR = os.environ.get("DATA_DIR", "/vol1/1000/appdata/videotool")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/vol1/1000/media")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv",
              ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".3gp")

TASKS = {}
LOCK = threading.Lock()


def run_task(tid, cmd, env=None):
    with LOCK:
        task = TASKS[tid]
    try:
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            task["log"].append(line.rstrip())
        code = proc.wait()
        task["status"] = "done" if code == 0 else "failed"
        task["log"].append("-- 进程结束，返回码 %d --" % code)
    except Exception as exc:
        task["log"].append("ERROR: %s" % exc)
        task["status"] = "failed"


def safe_join(base, rel):
    target = os.path.realpath(os.path.join(base, rel))
    if not target.startswith(os.path.realpath(base) + os.sep):
        return None
    return target


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/info")
def info():
    return jsonify({"media_dir": MEDIA_DIR})


@app.route("/api/files")
def list_files():
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
    return jsonify(files)


@app.route("/api/merge", methods=["POST"])
def merge():
    data = request.get_json(silent=True) or {}
    files = data.get("files") or []
    output = (data.get("output") or "merged.mkv").strip()
    reencode = bool(data.get("reencode"))

    if len(files) < 2:
        return jsonify({"error": "至少需要 2 个文件"}), 400

    abs_files = []
    for rel in files:
        p = safe_join(MEDIA_DIR, rel)
        if not p or not os.path.isfile(p):
            return jsonify({"error": "文件不存在：%s" % rel}), 400
        abs_files.append(p)

    out_path = safe_join(MEDIA_DIR, output)
    if not out_path:
        return jsonify({"error": "输出路径非法"}), 400
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tid = uuid.uuid4().hex
    listfile = os.path.join(DATA_DIR, tid + ".txt")
    with open(listfile, "w", encoding="utf-8") as fp:
        for p in abs_files:
            fp.write("file '%s'\n" % p.replace("'", "'\\''"))

    if reencode:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-c:a", "aac", "-b:a", "192k", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
               "-c", "copy", out_path]

    with LOCK:
        TASKS[tid] = {"log": [" ".join(cmd)], "status": "running"}
    threading.Thread(target=run_task, args=(tid, cmd), daemon=True).start()
    return jsonify({"task": tid})


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    subdir = (data.get("subdir") or "").strip()
    if not url:
        return jsonify({"error": "缺少 URL"}), 400

    out_dir = MEDIA_DIR if not subdir else safe_join(MEDIA_DIR, subdir)
    if not out_dir:
        return jsonify({"error": "目录非法"}), 400
    os.makedirs(out_dir, exist_ok=True)

    tid = uuid.uuid4().hex
    cmd = ["yt-dlp", url, "-o", os.path.join(out_dir, "%(title)s.%(ext)s")]

    with LOCK:
        TASKS[tid] = {"log": [" ".join(cmd)], "status": "running"}
    threading.Thread(target=run_task, args=(tid, cmd), daemon=True).start()
    return jsonify({"task": tid})


@app.route("/api/log/<tid>")
def get_log(tid):
    with LOCK:
        task = TASKS.get(tid)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"status": task["status"], "log": task["log"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)

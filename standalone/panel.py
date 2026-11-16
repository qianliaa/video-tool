# -*- coding: utf-8 -*-
"""视频助手 · 纯原生 Tkinter 操作面板（单机版，不依赖浏览器）。

后端仍是本机 127.0.0.1:18080 的 HTTP 服务（在 launcher 中以线程启动），
本面板作为本地 GUI 客户端直接调用其 /api/* 接口完成任务提交、进度与日志
查看、取消等操作。输入文件与输出路径在每个工具内直接选择（支持本机任意
绝对路径），无需预先配置工作目录。所有数据都在本机处理，不连接任何外网。
"""
import os
import sys
import json
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

BASE = "http://%s:%s" % (
    os.environ.get("VT_BIND_HOST", "127.0.0.1"),
    os.environ.get("VT_BIND_PORT", "18080"),
)

STATUS_TEXT = {
    "queued": "排队中", "running": "运行中", "paused": "已暂停",
    "done": "已完成", "failed": "失败", "canceled": "已取消",
}
BATCH_TEXT = {"start": "开始 / 继续", "pause": "暂停", "cancel": "取消", "delete": "删除"}


# --------------------------------------------------------------------------
# 本地 API 客户端（离线、仅本机回环）
# --------------------------------------------------------------------------
def api_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return {"error": body.get("error") or e.reason}
    except Exception as e:  # 服务未就绪等
        return {"error": str(e)}


def api_post(path, data):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return {"error": body.get("error") or e.reason}
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------
# 工具表单定义：每个工具对应一组字段
# 字段类型：entry / file / combo / check / int / text
# --------------------------------------------------------------------------
TOOLS = [
    {
        "key": "download", "label": "下载（直链 / HLS / DASH）", "action": "download",
        "fields": [
            {"k": "url", "t": "entry", "label": "视频地址 URL", "req": True},
            {"k": "filename", "t": "entry", "label": "文件名（可选，留空按原标题）"},
            {"k": "save_dir", "t": "outdir", "label": "保存目录（点击选择…）"},
            {"k": "mode", "t": "combo", "label": "模式",
             "vals": ["video", "audio"], "d": "video"},
            {"k": "audio_format", "t": "combo", "label": "音频格式",
             "vals": ["mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"], "d": "mp3"},
            {"k": "quality", "t": "entry", "label": "画质（HLS/DASH 可选，如 1080）"},
            {"k": "proxy", "t": "entry", "label": "代理（可选，http://127.0.0.1:7890）"},
            {"k": "referer", "t": "entry", "label": "Referer（可选）"},
        ],
    },
    {
        "key": "convert", "label": "格式转换", "action": "convert",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
            {"k": "copy", "t": "check", "label": "直接流复制（不重编码）", "d": False},
        ],
    },
    {
        "key": "compress", "label": "压缩体积", "action": "compress",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
            {"k": "crf", "t": "int", "label": "CRF（0-51，越小越清晰）", "d": 23, "min": 0, "max": 51},
            {"k": "preset", "t": "combo", "label": "编码速度",
             "vals": ["ultrafast", "superfast", "veryfast", "faster", "fast",
                      "medium", "slow", "slower", "veryslow"], "d": "medium"},
            {"k": "scale", "t": "entry", "label": "缩放（可选，如 1280:-2）"},
        ],
    },
    {
        "key": "trim", "label": "剪辑 / 截取片段", "action": "trim",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
            {"k": "start", "t": "entry", "label": "开始时间（秒 或 HH:MM:SS）", "d": "0"},
            {"k": "duration", "t": "entry", "label": "时长（秒，留空到结尾）"},
            {"k": "copy", "t": "check", "label": "流复制（不重编码）", "d": True},
        ],
    },
    {
        "key": "extract_audio", "label": "提取音频", "action": "extract_audio",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "output", "t": "outfile", "label": "输出路径（含扩展名，点击选择/修改）"},
        ],
    },
    {
        "key": "merge", "label": "合并视频", "action": "merge", "multi": True,
        "fields": [
            {"k": "reencode", "t": "check", "label": "重新编码（兼容性更好）", "d": False},
            {"k": "output", "t": "outfile", "label": "输出路径（默认 merged.mkv）"},
        ],
    },
    {
        "key": "subtitle", "label": "硬字幕（烧录）", "action": "subtitle",
        "fields": [
            {"k": "input", "t": "file", "label": "输入视频"},
            {"k": "subtitle", "t": "file", "label": "字幕文件（srt/ass）"},
            {"k": "font_size", "t": "int", "label": "字号（8-120，可选）", "d": 24, "min": 8, "max": 120},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
        ],
    },
    {
        "key": "resize", "label": "缩放尺寸", "action": "resize",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "width", "t": "int", "label": "宽（留 0 表示按比例）"},
            {"k": "height", "t": "int", "label": "高（留 0 表示按比例）"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
        ],
    },
    {
        "key": "rotate", "label": "旋转 / 翻转", "action": "rotate",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "direction", "t": "combo", "label": "方向",
             "vals": ["90", "180", "270", "flip_h", "flip_v"], "d": "90"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
        ],
    },
    {
        "key": "screenshot", "label": "截图（抽帧）", "action": "screenshot",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件"},
            {"k": "time", "t": "entry", "label": "时间点（秒，如 12.5）", "d": "0"},
            {"k": "output", "t": "outfile", "label": "输出路径（默认 .jpg）"},
        ],
    },
    {
        "key": "custom", "label": "自定义 ffmpeg 参数", "action": "custom",
        "fields": [
            {"k": "input", "t": "file", "label": "输入文件（{input} 占位）"},
            {"k": "args", "t": "entry", "label": "参数（可用 {input} {output}）"},
            {"k": "output", "t": "outfile", "label": "输出路径（点击选择/修改）"},
        ],
    },
]


def tool_by_key(key):
    for t in TOOLS:
        if t["key"] == key:
            return t
    return None


class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            import app as _app
            self.app_ver = getattr(_app, "APP_VERSION", "1.8.4")
            self._app_mod = _app
        except Exception:
            self.app_ver = "1.8.4"
            self._app_mod = None

        self.title("视频助手 · 操作面板  v%s" % self.app_ver)
        self.geometry("880x600")
        self.minsize(760, 540)
        self._boot_error = None
        self.last_dir = os.path.expanduser("~")
        self.current_tool = TOOLS[0]["key"]
        self.field_widgets = {}      # key -> widget
        self.input_var = None        # 当前工具「输入文件」字段的变量
        self.output_var = None       # 当前工具「输出路径」字段的变量
        self.merge_files = []        # 合并用的文件列表
        self.task_log_since = {}     # task_id -> since
        self.task_rows = []          # 最近一次拉取的任务数据
        self.task_checked = set()    # 批量操作勾选的任务 id
        self._rebuilding = False
        self._tasks_after = None

        self._build()
        self._poll_server()
        # 周期性刷新任务列表
        self._schedule_tasks()

    # ----------------------------- 构建 UI -----------------------------
    def _build(self):
        # 顶部状态栏
        top = ttk.Frame(self)
        top.pack(side="top", fill="x", padx=10, pady=6)
        self.status_var = tk.StringVar(value="○ 正在启动本地服务…")
        ttk.Label(top, textvariable=self.status_var, font=("Microsoft YaHei", 11)).pack(side="left")
        ttk.Button(top, text="退出", command=self.on_exit).pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.nb = nb

        # Tab 1: 工具
        self.tab_tools = ttk.Frame(nb)
        nb.add(self.tab_tools, text="工具")
        self._build_tools(self.tab_tools)

        # Tab 2: 任务
        self.tab_tasks = ttk.Frame(nb)
        nb.add(self.tab_tasks, text="任务")
        self._build_tasks(self.tab_tasks)

    def _build_tools(self, parent):
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=8)

        # 左侧工具列表
        left = ttk.Frame(pane, width=220)
        pane.add(left, weight=1)
        ttk.Label(left, text="工具", font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", padx=6, pady=4)
        self.tool_list = tk.Listbox(left, height=20)
        for t in TOOLS:
            self.tool_list.insert("end", t["label"])
        self.tool_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tool_list.bind("<<ListboxSelect>>", lambda e: self._on_tool_select())

        # 右侧表单
        right = ttk.Frame(pane, width=560)
        pane.add(right, weight=3)
        self.form_canvas = ttk.Frame(right)
        self.form_canvas.pack(fill="both", expand=True, padx=6, pady=4)

        self._on_tool_select()

    def _build_tasks(self, parent):
        f = ttk.Frame(parent, padding=8)
        f.pack(fill="both", expand=True)

        bar1 = ttk.Frame(f)
        bar1.pack(fill="x", pady=(0, 4))
        ttk.Button(bar1, text="刷新", command=self._refresh_tasks).pack(side="left")
        ttk.Button(bar1, text="全选", command=self._check_all_tasks).pack(side="left", padx=6)
        ttk.Button(bar1, text="反选", command=self._invert_tasks).pack(side="left")
        self.tasks_info = tk.StringVar(value="")
        ttk.Label(bar1, textvariable=self.tasks_info).pack(side="right")

        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", pady=(0, 6))
        ttk.Button(bar2, text="开始 / 继续",
                   command=lambda: self._batch("start")).pack(side="left")
        ttk.Button(bar2, text="暂停",
                   command=lambda: self._batch("pause")).pack(side="left", padx=6)
        ttk.Button(bar2, text="取消",
                   command=lambda: self._batch("cancel")).pack(side="left")
        ttk.Button(bar2, text="删除",
                   command=lambda: self._batch("delete")).pack(side="left", padx=6)
        ttk.Button(bar2, text="清除已结束",
                   command=self._clear_finished).pack(side="left")
        self.batch_var = tk.StringVar(value="")
        ttk.Label(bar2, textvariable=self.batch_var).pack(side="right")

        cols = ("sel", "id", "name", "action", "status", "progress")
        wrap = ttk.Frame(f)
        wrap.pack(fill="x", pady=(0, 6))
        self.task_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                      height=9, selectmode="extended")
        widths = {"sel": 34, "id": 92, "name": 190, "action": 96,
                  "status": 84, "progress": 76}
        heads = {"sel": "选", "id": "ID", "name": "名称",
                 "action": "动作", "status": "状态", "progress": "进度"}
        for c in cols:
            self.task_tree.heading(c, text=heads[c])
            self.task_tree.column(c, width=widths[c],
                                  anchor="center" if c in ("sel", "status", "progress") else "w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=vs.set)
        self.task_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.task_tree.bind("<<TreeviewSelect>>", lambda e: self._on_task_select())
        self.task_tree.bind("<Button-1>", self._on_tree_click)
        self._update_task_info()

        # 选中任务的进度 + 日志
        self.prog_var = tk.DoubleVar(value=0)
        self.prog = ttk.Progressbar(f, variable=self.prog_var, maximum=100)
        self.prog.pack(fill="x", pady=(0, 6))
        self.task_status_var = tk.StringVar(value="未选择任务")
        ttk.Label(f, textvariable=self.task_status_var).pack(anchor="w", pady=(0, 4))

        self.log_box = scrolledtext.ScrolledText(f, height=12, state="disabled",
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

    # ----------------------------- 工具表单 -----------------------------
    def _on_tool_select(self):
        sel = self.tool_list.curselection()
        if not sel:
            self.tool_list.selection_set(0)
            sel = (0,)
        idx = sel[0]
        self.current_tool = TOOLS[idx]["key"]
        self._render_form(TOOLS[idx])

    def _render_form(self, tool):
        for w in list(self.form_canvas.children.values()):
            w.destroy()
        self.field_widgets = {}
        self.merge_files = []
        self.input_var = None
        self.output_var = None

        row = 0
        ttk.Label(self.form_canvas, text=tool["label"],
                  font=("Microsoft YaHei", 12, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        if tool.get("multi"):
            # 合并：文件列表 + 增删
            ttk.Label(self.form_canvas, text="文件列表（按顺序合并）：").grid(
                row=row, column=0, columnspan=3, sticky="w")
            row += 1
            self.merge_listbox = tk.Listbox(self.form_canvas, height=6, width=60)
            self.merge_listbox.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
            row += 1
            mb = ttk.Frame(self.form_canvas)
            mb.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            ttk.Button(mb, text="添加文件…", command=self._merge_add).pack(side="left")
            ttk.Button(mb, text="移除选中", command=self._merge_remove).pack(side="left", padx=6)
            row += 1

        for fld in tool["fields"]:
            ttk.Label(self.form_canvas, text=fld["label"]).grid(
                row=row, column=0, sticky="w", pady=3)
            if fld["t"] == "entry":
                v = tk.StringVar(value=fld.get("d", ""))
                ttk.Entry(self.form_canvas, textvariable=v, width=46).grid(
                    row=row, column=1, columnspan=2, sticky="w", padx=6)
                self.field_widgets[fld["k"]] = v
            elif fld["t"] == "int":
                v = tk.IntVar(value=fld.get("d", 0))
                ttk.Spinbox(self.form_canvas, textvariable=v, from_=fld.get("min", 0),
                            to=fld.get("max", 100), width=10).grid(
                    row=row, column=1, sticky="w", padx=6)
                self.field_widgets[fld["k"]] = v
            elif fld["t"] == "combo":
                v = tk.StringVar(value=fld.get("d", fld["vals"][0]))
                ttk.Combobox(self.form_canvas, textvariable=v, values=fld["vals"],
                             width=14, state="readonly").grid(
                    row=row, column=1, sticky="w", padx=6)
                self.field_widgets[fld["k"]] = v
            elif fld["t"] == "check":
                v = tk.BooleanVar(value=fld.get("d", False))
                ttk.Checkbutton(self.form_canvas, variable=v,
                                text="启用").grid(row=row, column=1, sticky="w", padx=6)
                self.field_widgets[fld["k"]] = v
            elif fld["t"] == "file":
                v = tk.StringVar(value="")
                ent = ttk.Entry(self.form_canvas, textvariable=v, width=40)
                ent.grid(row=row, column=1, sticky="w", padx=6)
                ttk.Button(self.form_canvas, text="选择…",
                           command=lambda var=v: self._pick_file(var)).grid(row=row, column=2)
                self.field_widgets[fld["k"]] = v
                if self.input_var is None:
                    self.input_var = v
            elif fld["t"] == "outfile":
                v = tk.StringVar(value="")
                ent = ttk.Entry(self.form_canvas, textvariable=v, width=40)
                ent.grid(row=row, column=1, sticky="w", padx=6)
                ttk.Button(self.form_canvas, text="选择…",
                           command=lambda var=v: self._pick_outfile(var)).grid(row=row, column=2)
                self.field_widgets[fld["k"]] = v
                self.output_var = v
            elif fld["t"] == "outdir":
                v = tk.StringVar(value="")
                ent = ttk.Entry(self.form_canvas, textvariable=v, width=40)
                ent.grid(row=row, column=1, sticky="w", padx=6)
                ttk.Button(self.form_canvas, text="选择…",
                           command=lambda var=v: self._pick_outdir(var)).grid(row=row, column=2)
                self.field_widgets[fld["k"]] = v
            row += 1

        submit = ttk.Button(self.form_canvas, text="开始处理", command=self._submit)
        submit.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _pick_file(self, var):
        p = filedialog.askopenfilename(initialdir=self.last_dir)
        if not p:
            return
        var.set(p)
        self.last_dir = os.path.dirname(p)
        # 选好输入后，若输出路径还空着，自动给出同目录的同名默认路径
        if self.output_var is not None and not self.output_var.get().strip():
            stem, ext = os.path.splitext(os.path.basename(p))
            self.output_var.set(os.path.join(os.path.dirname(p),
                                            stem + "_out" + (ext or ".mp4")))

    def _pick_outfile(self, var):
        initial = var.get().strip()
        d = os.path.dirname(initial) if initial else self.last_dir
        name = os.path.basename(initial) if initial else ""
        p = filedialog.asksaveasfilename(
            initialdir=d, initialfile=name,
            filetypes=[("媒体文件", "*.mp4 *.mkv *.mov *.webm *.avi *.mp3 *.m4a *.aac *.wav *.flac *.opus *.ogg *.jpg *.png"),
                       ("所有文件", "*.*")])
        if p:
            var.set(p)
            self.last_dir = os.path.dirname(p)

    def _pick_outdir(self, var):
        d = var.get().strip() or self.last_dir
        p = filedialog.askdirectory(initialdir=d)
        if p:
            var.set(p)
            self.last_dir = p

    def _merge_add(self):
        files = filedialog.askopenfilenames(initialdir=self.last_dir)
        for p in files:
            if p not in self.merge_files:
                self.merge_files.append(p)
                self.merge_listbox.insert("end", p)

    def _merge_remove(self):
        sel = self.merge_listbox.curselection()
        for i in reversed(sel):
            self.merge_listbox.delete(i)
            del self.merge_files[i]

    def _submit(self):
        tool = tool_by_key(self.current_tool)
        if not tool:
            return
        params = {}
        for fld in tool["fields"]:
            w = self.field_widgets.get(fld["k"])
            if w is None:
                continue
            val = w.get()
            if isinstance(w, tk.BooleanVar):
                params[fld["k"]] = bool(val)
            elif isinstance(w, tk.IntVar):
                params[fld["k"]] = int(val)
            else:
                params[fld["k"]] = str(val).strip()
        # 下载：未选保存目录时，默认落到上次使用的目录
        if tool["action"] == "download" and not params.get("save_dir"):
            params["save_dir"] = self.last_dir
        # 去掉空字符串字段（让后端用默认值）
        params = {k: v for k, v in params.items()
                  if not (isinstance(v, str) and v == "")}

        if tool.get("multi"):
            if len(self.merge_files) < 2:
                messagebox.showwarning("提示", "合并至少需要 2 个文件。")
                return
            params["files"] = list(self.merge_files)
        else:
            # 校验必填
            for fld in tool["fields"]:
                if fld.get("req") and not params.get(fld["k"]):
                    messagebox.showwarning("提示", "请填写：%s" % fld["label"])
                    return

        res = api_post("/api/task", {"action": tool["action"], "params": params})
        if "error" in res:
            messagebox.showerror("提交失败", res["error"])
            return
        tid = res.get("task")
        messagebox.showinfo("已提交", "任务已创建：%s" % tid)
        self.nb.select(self.tab_tasks)
        self._refresh_tasks()

    # ----------------------------- 任务 / 日志 -----------------------------
    def _refresh_tasks(self):
        data = api_get("/api/tasks")
        if "error" in data:
            self.tasks_info.set("读取任务失败：%s" % data["error"])
            return
        tasks = data.get("tasks", [])
        self.task_rows = tasks
        valid = set(t["id"] for t in tasks)
        self.task_checked = set(i for i in self.task_checked if i in valid)
        prev_sel = self._selected_task_id()
        # 重建列表期间的 <<TreeviewSelect>> 不应把日志拉回全量，避免每 2 秒闪烁。
        self._rebuilding = True
        try:
            self.task_tree.delete(*self.task_tree.get_children())
            for t in tasks:
                tid = t["id"]
                self.task_tree.insert("", "end", iid=tid, values=(
                    "☑" if tid in self.task_checked else "☐",
                    tid[:12], t.get("name") or t.get("action"),
                    t.get("action"), STATUS_TEXT.get(t.get("status"), t.get("status")),
                    "%.0f%%" % (t.get("progress") or 0)))
            if prev_sel and self.task_tree.exists(prev_sel):
                self.task_tree.selection_set(prev_sel)
        finally:
            self._rebuilding = False
        self._update_task_info()
        sel = self._selected_task_id()
        if sel:
            self._poll_log(sel)

    def _on_tree_click(self, event):
        """点首列方框切换勾选；其余列沿用 Treeview 自身的选择行为查看日志。"""
        try:
            if self.task_tree.identify_region(event.x, event.y) != "cell":
                return None
            if self.task_tree.identify_column(event.x) != "#1":
                return None
            row = self.task_tree.identify_row(event.y)
        except Exception:
            return None
        if not row:
            return None
        if row in self.task_checked:
            self.task_checked.discard(row)
        else:
            self.task_checked.add(row)
        self.task_tree.set(row, "sel", "☑" if row in self.task_checked else "☐")
        self._update_task_info()
        return "break"

    def _update_task_info(self):
        total = len(getattr(self, "task_rows", []))
        self.tasks_info.set("共 %d 个任务 · 已选 %d 项" % (total, len(self.task_checked)))

    def _paint_checks(self):
        for t in getattr(self, "task_rows", []):
            tid = t["id"]
            if self.task_tree.exists(tid):
                self.task_tree.set(tid, "sel", "☑" if tid in self.task_checked else "☐")
        self._update_task_info()

    def _check_all_tasks(self):
        for t in getattr(self, "task_rows", []):
            self.task_checked.add(t["id"])
        self._paint_checks()

    def _invert_tasks(self):
        for t in getattr(self, "task_rows", []):
            tid = t["id"]
            if tid in self.task_checked:
                self.task_checked.discard(tid)
            else:
                self.task_checked.add(tid)
        self._paint_checks()

    def _checked_ids(self):
        return [t["id"] for t in getattr(self, "task_rows", [])
                if t["id"] in self.task_checked]

    def _clear_finished(self):
        ids = [t["id"] for t in getattr(self, "task_rows", [])
               if t.get("status") in ("done", "failed", "canceled")]
        if not ids:
            messagebox.showinfo("提示", "没有已结束的任务。")
            return
        self._batch("delete", ids)

    def _batch(self, op, ids=None):
        """调用后端批量接口；返回结果写在工具栏，不弹窗打断操作。"""
        ids = list(ids) if ids is not None else self._checked_ids()
        if not ids:
            messagebox.showinfo("提示", "请先勾选任务：点击列表首列的方框，或用「全选」。")
            return
        label = BATCH_TEXT.get(op, op)
        rows = dict((t["id"], t) for t in getattr(self, "task_rows", []))
        active = sum(1 for i in ids
                     if rows.get(i, {}).get("status") in ("queued", "running", "paused"))
        if op in ("cancel", "delete"):
            msg = "确定%s选中的 %d 个任务吗？" % (label, len(ids))
            if op == "delete":
                msg += "\n仅移除任务记录，不会删除已生成的文件。"
                if active:
                    msg += "\n其中 %d 个正在运行或排队，会先取消再移除。" % active
            if not messagebox.askyesno("确认", msg):
                return
        res = api_post("/api/tasks/batch", {"op": op, "ids": ids})
        if "error" in res:
            messagebox.showerror("%s失败" % label, res["error"])
            return
        applied = int(res.get("applied") or 0)
        skipped = int(res.get("skipped") or 0)
        reason = ""
        for value in (res.get("results") or {}).values():
            if isinstance(value, str) and value.startswith("skipped:") and not reason:
                reason = value.split(":", 1)[1]
        text = "%s：成功 %d 项" % (label, applied)
        if skipped:
            text += "，跳过 %d 项" % skipped
        if reason:
            text += "（%s）" % reason
        self.batch_var.set(text)
        self._refresh_tasks()

    def _on_task_select(self):
        if getattr(self, "_rebuilding", False):
            return
        sel = self._selected_task_id()
        if sel:
            self._poll_log(sel, reset=True)

    def _selected_task_id(self):
        cur = self.task_tree.selection()
        if cur:
            return cur[0]
        return None

    def _cancel_selected(self):
        tid = self._selected_task_id()
        if not tid:
            messagebox.showinfo("提示", "请先在列表中选择一个任务。")
            return
        self._batch("cancel", [tid])

    def _poll_log(self, tid, reset=False):
        if reset:
            self.task_log_since[tid] = 0
            self.log_box.config(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.config(state="disabled")
        since = self.task_log_since.get(tid, 0)
        qs = "?since=%d" % since
        data = api_get("/api/log/" + tid + qs)
        if "error" in data:
            return
        chunk = data.get("log", [])
        if chunk:
            self.log_box.config(state="normal")
            for line in chunk:
                self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.task_log_since[tid] = data.get("next", since)
        self.prog_var.set(data.get("progress") or 0)
        rc = data.get("returncode")
        st = data.get("status")
        extra = "" if rc is None else "　返回码 %s" % rc
        self.task_status_var.set("状态：%s%s" % (STATUS_TEXT.get(st, st), extra))

    def _schedule_tasks(self):
        try:
            self._refresh_tasks()
        except Exception:
            pass
        self._tasks_after = self.after(2000, self._schedule_tasks)

    # ----------------------------- 服务就绪轮询 -----------------------------
    def _poll_server(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            ok = s.connect_ex((os.environ.get("VT_BIND_HOST", "127.0.0.1"),
                               int(os.environ.get("VT_BIND_PORT", "18080")))) == 0
        except OSError:
            ok = False
        finally:
            s.close()
        if ok:
            self.status_var.set("● 本地服务已就绪（127.0.0.1:18080）")
        else:
            self.status_var.set("○ 正在启动本地服务…（首次约需几秒）")
            self.after(1000, self._poll_server)

    # ----------------------------- 退出 -----------------------------
    def on_exit(self):
        if self._tasks_after:
            self.after_cancel(self._tasks_after)
        try:
            fn = getattr(self._app_mod, "stop_all_tasks", None)
            if callable(fn):
                fn()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)


def main():
    # 等待服务尽量就绪后再进入 UI（避免首屏接口报错刷屏）
    import socket
    host = os.environ.get("VT_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("VT_BIND_PORT", "18080"))
    deadline = time.time() + 20
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            if s.connect_ex((host, port)) == 0:
                break
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.4)

    Panel().mainloop()


if __name__ == "__main__":
    main()

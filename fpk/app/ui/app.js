/* 视频助手 · 前端逻辑（无依赖） */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var state = {
    config: {
      input_dir: "", output_dir: "",
      input_ok: true, output_ok: true, input_error: "", output_error: ""
    },
    roots: [],
    tools: {},
    files: [],
    merge: [],
    mergeNameAuto: true,
    mergeNameLast: "",
    view: "merge",
    processSub: "convert",
    convertExt: "mp4",
    clipSub: "trim",
    audioExt: "mp3",
    dlMode: "video",
    picker: { mode: "input", path: "", parent: null },
    tasks: [],
    seen: {},
    logId: null,
    logTimer: null,
    logSeq: 0,
    logHasContent: false,
    taskTimer: null,
    updateBusy: false
  };

  var VIEW_META = {
    merge: { title: "视频合并", desc: "按顺序拼接多个视频，保持原有画质" },
    process: { title: "转换与处理", desc: "格式转换、压缩、分辨率与旋转" },
    clip: { title: "剪辑与截图", desc: "截取片段或从视频中导出画面" },
    audio: { title: "提取音频", desc: "从视频中分离音轨并转换格式" },
    subtitle: { title: "字幕烧录", desc: "将字幕直接压制进视频画面" },
    download: { title: "链接下载", desc: "ffmpeg 原生下载直链、HLS 与 DASH 媒体" },
    advanced: { title: "高级", desc: "直接执行自定义 ffmpeg 参数" },
    tasks: { title: "任务", desc: "查看运行状态、进度与完整日志" },
    settings: { title: "设置", desc: "配置输入输出目录并查看运行环境" }
  };

  var MORE_VIEWS = { audio: 1, subtitle: 1, advanced: 1, tasks: 1, settings: 1 };

  var ACTION_LABEL = {
    merge: "视频合并",
    convert: "格式转换",
    compress: "视频压缩",
    trim: "截取片段",
    extract_audio: "提取音频",
    subtitle: "字幕烧录",
    resize: "修改分辨率",
    rotate: "旋转 / 翻转",
    screenshot: "视频截图",
    custom: "自定义命令",
    download: "链接下载"
  };

  var STATUS_LABEL = { queued: "排队中", running: "运行中", done: "已完成", failed: "失败", canceled: "已取消" };

  // ---------------------------------------------------------------- 基础

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  }

  function fmtClock(sec) {
    sec = Number(sec) || 0;
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = Math.floor(sec % 60);
    var pad = function (x) { return x < 10 ? "0" + x : "" + x; };
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
  }

  // 拿不到总时长/总大小时，用已写入的大小和已处理时长作为可读进展
  function taskStat(t) {
    var parts = [];
    if (Number(t.elapsed) > 0) parts.push(fmtClock(t.elapsed));
    if (Number(t.written) > 0) parts.push(fmtSize(t.written));
    return parts.length ? parts.join(" · ") : "—";
  }

  function fmtDate(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var now = new Date();
    var time = d.toLocaleTimeString("zh-CN", { hour12: false });
    if (d.toDateString() === now.toDateString()) return time;
    return (d.getMonth() + 1) + "-" + d.getDate() + " " + time;
  }

  function baseName(path) {
    var parts = String(path || "").split(/[\\/]/);
    return parts[parts.length - 1] || "";
  }

  function stem(path) {
    return baseName(path).replace(/\.[^.]+$/, "");
  }

  function extOf(path) {
    var m = String(path || "").match(/\.([^.]+)$/);
    return m ? m[1].toLowerCase() : "";
  }

  function sanitizeFileName(name) {
    return String(name || "")
      .replace(/[<>:"\/\\|?*\u0000-\u001f]/g, "_")
      .replace(/\s+/g, " ")
      .replace(/^[.\s]+|[.\s]+$/g, "")
      .replace(/_{2,}/g, "_");
  }

  function mergePrefixInfo(paths) {
    var stems = paths.map(function (p) { return sanitizeFileName(stem(p)) || "merged"; });
    if (!stems.length) return { prefix: "", stems: stems, tooShort: true };
    var prefix = stems[0];
    for (var i = 1; i < stems.length; i++) {
      var j = 0;
      while (j < prefix.length && j < stems[i].length &&
             prefix.charAt(j).toLowerCase() === stems[i].charAt(j).toLowerCase()) {
        j++;
      }
      prefix = prefix.slice(0, j);
    }
    prefix = prefix.replace(/[\s._\-]+$/, "");
    var tooShort = prefix.length < 2 && stems.length > 1;
    if (prefix.length < 2) prefix = stems[0];
    prefix = sanitizeFileName(prefix) || "merged";
    return { prefix: prefix, stems: stems, tooShort: tooShort };
  }

  function mergeCommonName(paths) {
    if (!paths.length) return "";
    return (mergePrefixInfo(paths).prefix || "merged") + ".mp4";
  }

  function joinOut(name) {
    var base = (state.config.output_dir || "").replace(/\/+$/, "");
    return base ? base + "/" + name : name;
  }

  function toast(message, type, timeout) {
    var wrap = $("#toasts");
    var el = document.createElement("div");
    el.className = "toast" + (type ? " " + type : "");
    el.textContent = message;
    wrap.appendChild(el);
    setTimeout(function () {
      el.classList.add("hide");
      setTimeout(function () { el.remove(); }, 220);
    }, timeout || 3600);
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("请求失败（" + res.status + "）"));
          err.status = res.status;
          err.code = data && data.code;
          throw err;
        }
        return data;
      });
    });
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  // ---------------------------------------------------------------- 通用渲染

  function kvHtml(rows) {
    return (rows || []).map(function (row) {
      var value = row[1];
      if (value === undefined || value === null || value === "") value = "-";
      return '<div class="kv-row"><span>' + esc(row[0]) + "</span><strong>" +
        esc(String(value)) + "</strong></div>";
    }).join("");
  }

  function fmtLastSeen(sec) {
    if (!sec) return "-";
    try { return new Date(sec * 1000).toLocaleString(); } catch (e) { return "-"; }
  }

  function stopPolling() {
    clearInterval(state.taskTimer);
    state.taskTimer = null;
    clearInterval(state.logTimer);
    state.logTimer = null;
  }

  function startApp() {
    loadTools()
      .then(loadConfig)
      .then(loadFiles)
      .then(function () {
        processDefaults();
        clipDefaults();
        audioDefaults();
        subDefaults();
        customDefaults();
        updatePreviews();
      })
      .catch(function (err) {
        toast("初始化失败：" + (err && err.message ? err.message : "未知错误"), "error", 8000);
      });
    loadTasks(true);
    stopPolling();
    state.taskTimer = setInterval(function () {
      if (!document.hidden) loadTasks(true);
    }, 3000);
  }

  // ---------------------------------------------------------------- 主题

  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("vt-theme", theme); } catch (e) {}
    var btn = $("#themeBtn");
    if (btn) {
      var effective = theme === "auto" ? currentTheme() : theme;
      btn.title = effective === "dark" ? "切换到浅色主题" : "切换到深色主题";
      btn.innerHTML = effective === "dark" ? ICON_SUN : ICON_MOON;
    }
  }

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "dark") return "dark";
    if (t === "light") return "light";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // ---------------------------------------------------------------- 视图

  function setView(name) {
    if (!VIEW_META[name]) name = "merge";
    state.view = name;
    $$(".nav-item").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-view") === name);
    });
    $$(".mnav-item").forEach(function (b) {
      var view = b.getAttribute("data-view");
      var active = view ? view === name : (b.hasAttribute("data-more") && !!MORE_VIEWS[name]);
      b.classList.toggle("active", active);
    });
    var sheet = $("#moreSheet");
    if (sheet) sheet.hidden = true;
    $$(".view").forEach(function (v) {
      v.hidden = v.getAttribute("data-view") !== name;
    });
    $("#viewTitle").textContent = VIEW_META[name].title;
    $("#viewDesc").textContent = VIEW_META[name].desc;
    try {
      if (window.location.hash !== "#" + name) {
        window.history.replaceState(null, "", "#" + name);
      }
    } catch (e) {}
    if (name === "tasks") loadTasks(true);
    if (name === "settings") renderToolInfo();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---------------------------------------------------------------- 配置 / 工具

  function renderDirs() {
    var input = state.config.input_dir || "";
    var output = state.config.output_dir || "";
    var inputText = input || "未设置";
    var outputText = output || "未设置";
    var inputEl = $("#inputPath");
    var outputEl = $("#outputPath");
    if (inputEl) {
      inputEl.textContent = inputText;
      inputEl.title = input || "点击设置输入目录";
      inputEl.closest(".dir-pill").classList.toggle("unset", !input);
    }
    if (outputEl) {
      outputEl.textContent = outputText;
      outputEl.title = output || "点击设置输出目录";
      outputEl.closest(".dir-pill").classList.toggle("unset", !output);
    }

    var setInput = $("#setInput");
    var setOutput = $("#setOutput");
    if (setInput && document.activeElement !== setInput) setInput.value = input;
    if (setOutput && document.activeElement !== setOutput) setOutput.value = output;

    [setInput, setOutput].forEach(function (el) {
      if (el) el.readOnly = true;
    });

    var banner = $("#setupBanner");
    if (banner) {
      var inputReady = !!input && state.config.input_ok !== false;
      var outputReady = !!output && state.config.output_ok !== false;
      banner.hidden = inputReady && outputReady;
      var hint = $("#setupHint");
      if (hint) {
        if (!input) {
          hint.textContent = "还差输入目录：用于读取要处理的视频。";
        } else if (!inputReady) {
          hint.textContent = "输入目录不可用：" + (state.config.input_error || "请检查文件夹权限");
        } else if (!output) {
          hint.textContent = "还差输出目录：用于保存处理结果。";
        } else if (!outputReady) {
          hint.textContent = "输出目录不可用：" + (state.config.output_error || "请检查文件夹权限");
        } else {
          hint.textContent = "输入目录用于读取视频，输出目录用于保存结果。";
        }
      }
    }
    updatePreviews();
  }

  function applyConfig(cfg) {
    cfg = cfg || {};
    state.config.input_dir = cfg.input_dir || "";
    state.config.output_dir = cfg.output_dir || "";
    state.config.input_ok = cfg.input_ok !== false;
    state.config.output_ok = cfg.output_ok !== false;
    state.config.input_error = cfg.input_error || "";
    state.config.output_error = cfg.output_error || "";
    state.roots = cfg.roots || state.roots;
  }

  function loadConfig() {
    return api("/api/config").then(function (cfg) {
      applyConfig(cfg);
      renderDirs();
      return cfg;
    });
  }

  function loadTools() {
    return api("/api/tools").then(function (info) {
      state.tools = info || {};
      renderTools();
      renderToolInfo();
      renderAbout();
      return info;
    }).catch(function (err) {
      toast("读取环境信息失败：" + err.message, "error");
    });
  }

  function renderTools() {
    var t = state.tools || {};
    var ff = $("#chipFfmpeg");
    if (!ff) return;
    if (t.ffmpeg) {
      ff.className = "chip ok";
      ff.innerHTML = '<i class="dot"></i>ffmpeg 就绪';
      ff.title = t.ffmpeg_version || t.ffmpeg;
    } else {
      ff.className = "chip bad";
      ff.innerHTML = '<i class="dot"></i>ffmpeg 未就绪';
      ff.title = t.ffmpeg_error || "未找到 ffmpeg";
    }
  }

  function renderToolInfo() {
    var t = state.tools || {};
    var box = $("#toolInfo");
    if (!box) return;
    var ffmpegVer = t.ffmpeg_version || "";
    var m = ffmpegVer.match(/version\s+(\S+)/i);
    var rows = [
      ["ffmpeg", m ? "v" + m[1] : (ffmpegVer || "未就绪"), ffmpegVer],
      ["ffmpeg 目录", t.ffmpeg_dir || "-"],
      ["下载引擎", t.downloader === "ffmpeg" ? "ffmpeg 原生" : "未就绪"],
      ["配置目录", t.pkgvar || "-"]
    ];
    if (!t.ffmpeg && t.ffmpeg_error) rows.splice(1, 0, ["ffmpeg 错误", t.ffmpeg_error]);
    box.innerHTML = rows.map(function (r) {
      return '<div><span>' + esc(r[0]) + '</span><span title="' + esc(r[2] || r[1]) + '">' + esc(r[1]) + '</span></div>';
    }).join("");
  }

  function renderAbout() {
    var t = state.tools || {};
    var box = $("#aboutInfo");
    if (!box) return;
    var rows = [
      ["应用版本", t.app_version || "-"],
      ["发布者", t.publisher || "-"],
      ["开发者", t.developer || "-"]
    ];
    box.innerHTML = rows.map(function (r) {
      return '<div><span>' + esc(r[0]) + '</span><span>' + esc(r[1]) + '</span></div>';
    }).join("");
    var linkRow = document.createElement("div");
    linkRow.innerHTML = '<span>项目主页</span><span><a class="about-link" href="' +
      esc(t.project_url || "https://github.com/qianliaa/video-tool") +
      '" target="_blank" rel="noopener noreferrer">github.com/qianliaa/video-tool</a></span>';
    box.appendChild(linkRow);
  }

  // ---------------------------------------------------------------- 检查更新

  function renderUpdate(result) {
    var box = $("#updateStatus");
    if (!box) return;
    if (!result) {
      box.className = "update-status";
      box.textContent = "尚未检查";
      return;
    }
    if (!result.ok) {
      box.className = "update-status bad";
      box.textContent = (result.message || "检查更新失败") + "（可稍后重试）";
      return;
    }
    var current = result.current || "-";
    if (result.status === "latest") {
      box.className = "update-status ok";
      box.textContent = "当前版本 " + current + " 已是最新版本（GitHub 最新 " + (result.latest || "-") + "）";
      return;
    }
    box.className = "update-status ok";
    box.innerHTML = "发现新版本 " + esc(result.latest || "-") + "（当前 " + esc(current) + "），" +
      '<a href="' + esc(result.download_url || "#") + '" target="_blank" rel="noopener noreferrer">下载 ' +
      esc(result.asset_name || "Release") + "</a>";
  }

  function checkUpdate() {
    if (state.updateBusy) return;
    state.updateBusy = true;
    var btn = $("#checkUpdate");
    if (btn) btn.disabled = true;
    var box = $("#updateStatus");
    if (box) {
      box.className = "update-status";
      box.textContent = "正在查询 GitHub Releases…";
    }
    api("/api/update?refresh=1").then(function (result) {
      renderUpdate(result);
    }).catch(function (err) {
      var tip = $("#updateStatus");
      if (tip) {
        tip.className = "update-status bad";
        tip.textContent = "检查更新失败：" + err.message;
      }
    }).then(function () {
      state.updateBusy = false;
      if (btn) btn.disabled = false;
    });
  }

  // ---------------------------------------------------------------- 文件列表

  function kindOf(file) {
    return file.kind || "video";
  }

  function fileIcon(kind) {
    if (kind === "audio") {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
    }
    if (kind === "subtitle") {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M6 12h6M6 16h10"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3z"/></svg>';
  }

  function loadFiles() {
    var path = state.config.input_dir;
    if (!path) {
      state.files = [];
      state.merge = [];
      renderMergeFiles();
      renderMergeSelected();
      populateSelects();
      var emptyText = $("#mergeEmpty p");
      if (emptyText) emptyText.textContent = "请先设置输入目录";
      return Promise.resolve();
    }
    return api("/api/files?path=" + encodeURIComponent(path)).then(function (data) {
      state.files = data.files || [];
      state.config.input_ok = true;
      state.config.input_error = "";
      var exists = {};
      state.files.forEach(function (f) { exists[f.path] = true; });
      state.merge = state.merge.filter(function (p) { return exists[p]; });
      var emptyText = $("#mergeEmpty p");
      if (emptyText) emptyText.textContent = "输入目录中没有视频或音频文件";
      renderMergeFiles();
      renderMergeSelected();
      populateSelects();
    }).catch(function (err) {
      state.files = [];
      state.config.input_ok = false;
      state.config.input_error = err.message;
      renderDirs();
      renderMergeFiles();
      renderMergeSelected();
      populateSelects();
      toast("读取输入目录失败：" + err.message, "error", 6000);
    });
  }

  function renderMergeFiles() {
    var box = $("#mergeFiles");
    if (!box) return;
    var q = ($("#mergeSearch").value || "").trim().toLowerCase();
    var media = state.files.filter(function (f) { return kindOf(f) !== "subtitle"; });
    var list = media.filter(function (f) {
      var label = f.rel || f.name || "";
      return !q || label.toLowerCase().indexOf(q) >= 0;
    });
    box.innerHTML = "";
    list.forEach(function (f) {
      var selected = state.merge.indexOf(f.path) >= 0;
      var row = document.createElement("div");
      row.className = "file-row" + (selected ? " selected" : "");
      row.setAttribute("data-path", f.path);
      row.innerHTML =
        '<div class="check-mark">✓</div>' +
        '<div class="file-ico">' + fileIcon(kindOf(f)) + '</div>' +
        '<div class="file-main">' +
          '<div class="file-name" title="' + esc(f.rel || f.name) + '">' + esc(f.rel || f.name) + '</div>' +
          '<div class="file-meta"><span class="kind ' + esc(kindOf(f)) + '">' +
            ({ video: "视频", audio: "音频", subtitle: "字幕" }[kindOf(f)] || "文件") +
          '</span><span class="file-size">' + fmtSize(f.size) + '</span></div>' +
        '</div>';
      row.addEventListener("click", function () { toggleMerge(f.path); });
      box.appendChild(row);
    });
    $("#mergeFileCount").textContent = media.length;
    $("#mergeEmpty").hidden = media.length > 0;
  }

  function toggleMerge(path) {
    var i = state.merge.indexOf(path);
    if (i >= 0) state.merge.splice(i, 1);
    else state.merge.push(path);
    renderMergeFiles();
    renderMergeSelected();
  }

  function mergeFileByPath(path) {
    for (var i = 0; i < state.files.length; i++) {
      if (state.files[i].path === path) return state.files[i];
    }
    return null;
  }

  function renderMergeSelected() {
    var list = $("#mergeSelected");
    if (!list) return;
    list.innerHTML = "";
    state.merge.forEach(function (path, idx) {
      var f = mergeFileByPath(path);
      var name = f ? (f.rel || f.name) : baseName(path);
      var li = document.createElement("li");
      li.innerHTML =
        '<span class="idx">' + (idx + 1) + '</span>' +
        '<span class="name" title="' + esc(path) + '">' + esc(name) + '</span>' +
        '<button class="mini-btn" data-act="up" title="上移">↑</button>' +
        '<button class="mini-btn" data-act="down" title="下移">↓</button>' +
        '<button class="mini-btn remove" data-act="remove" title="移除">×</button>';
      li.querySelector('[data-act="up"]').addEventListener("click", function (e) { e.stopPropagation(); moveMerge(idx, -1); });
      li.querySelector('[data-act="down"]').addEventListener("click", function (e) { e.stopPropagation(); moveMerge(idx, 1); });
      li.querySelector('[data-act="remove"]').addEventListener("click", function (e) { e.stopPropagation(); toggleMerge(path); });
      list.appendChild(li);
    });
    $("#mergeSelCount").textContent = state.merge.length;
    $("#mergeSelEmpty").hidden = state.merge.length > 0;
    updateMergeNameAuto();
  }

  function moveMerge(idx, delta) {
    var to = idx + delta;
    if (to < 0 || to >= state.merge.length) return;
    var item = state.merge.splice(idx, 1)[0];
    state.merge.splice(to, 0, item);
    renderMergeFiles();
    renderMergeSelected();
  }

  function updateMergeNameAuto() {
    var input = $("#mergeOutput");
    var hint = $("#mergeNameHint");
    if (!input) return;
    if (!state.merge.length) {
      if (hint && state.mergeNameAuto) {
        hint.textContent = "选择输入文件后自动识别文件名公共部分，仍可手动修改";
      }
      return;
    }
    if (!state.mergeNameAuto) {
      if (hint) hint.textContent = "已手动修改文件名，增删输入文件不会再自动覆盖";
      return;
    }
    var info = mergePrefixInfo(state.merge);
    var value = (info.prefix || "merged") + ".mp4";
    input.value = value;
    state.mergeNameLast = value;
    if (hint) {
      if (info.stems.length === 1) hint.textContent = "已使用输入文件名：" + info.stems[0];
      else if (info.tooShort) hint.textContent = "公共部分过短，已使用第一个文件名：" + info.stems[0];
      else hint.textContent = "已按输入文件名识别公共部分：" + info.prefix;
    }
    updatePreviews();
  }

  function populateSelects() {
    $$("select.file-select").forEach(function (sel) {
      var kind = sel.getAttribute("data-kind") || "media";
      var prev = sel.value;
      var list = state.files.filter(function (f) {
        var k = kindOf(f);
        if (kind === "media") return k === "video" || k === "audio";
        if (kind === "video") return k === "video";
        return k === kind;
      });
      sel.innerHTML = "";
      if (!state.config.input_dir) {
        var o1 = document.createElement("option");
        o1.value = "";
        o1.textContent = "请先设置输入目录";
        sel.appendChild(o1);
        return;
      }
      if (!list.length) {
        var o0 = document.createElement("option");
        o0.value = "";
        o0.textContent = "（输入目录中没有可用的文件）";
        sel.appendChild(o0);
        return;
      }
      list.forEach(function (f) {
        var opt = document.createElement("option");
        opt.value = f.path;
        opt.textContent = (f.rel || f.name) + "  ·  " + fmtSize(f.size);
        sel.appendChild(opt);
      });
      if (prev && list.some(function (f) { return f.path === prev; })) sel.value = prev;
    });
  }

  // ---------------------------------------------------------------- 输出预览与默认值

  function updatePreviews() {
    var map = [
      ["#mergeOutput", "#mergePreview"],
      ["#processOutput", "#processPreview"],
      ["#clipOutput", "#clipPreview"],
      ["#audioOutput", "#audioPreview"],
      ["#subOutput", "#subPreview"],
      ["#customOutput", "#customPreview"]
    ];
    map.forEach(function (pair) {
      var input = $(pair[0]), box = $(pair[1]);
      if (!input || !box) return;
      var v = (input.value || "").trim();
      if (!v) box.textContent = "输出：-";
      else if (!state.config.output_dir) box.textContent = "输出：请先设置输出目录";
      else box.textContent = "输出：" + joinOut(v);
    });
    var dlBox = $("#dlPreview");
    if (dlBox) {
      var sub = ($("#dlSubdir").value || "").trim().replace(/^\/+|\/+$/g, "");
      var name = ($("#dlFilename").value || "").trim() || "自动命名";
      dlBox.textContent = state.config.output_dir
        ? "保存至：" + state.config.output_dir + (sub ? "/" + sub : "") + "/" + name
        : "保存至：请先设置输出目录";
    }
  }

  function setValue(id, value) {
    var el = $(id);
    if (el && document.activeElement !== el) el.value = value;
  }

  function processDefaults() {
    var file = $("#processFile").value;
    var sub = state.processSub;
    var ext = sub === "convert" ? state.convertExt : "mp4";
    var suffix = { convert: "converted", compress: "compressed", resize: "resized", rotate: "rotated" }[sub];
    setValue("#processOutput", (file ? stem(file) : "output") + "_" + suffix + "." + ext);
    updatePreviews();
  }

  function clipDefaults() {
    var file = $("#clipFile").value;
    if (state.clipSub === "trim") {
      setValue("#clipOutput", (file ? stem(file) : "clip") + "_clip.mkv");
    } else {
      setValue("#clipOutput", (file ? stem(file) : "shot") + "_shot." + $("#shotFormat").value);
    }
    updatePreviews();
  }

  function audioDefaults() {
    var file = $("#audioFile").value;
    setValue("#audioOutput", (file ? stem(file) : "audio") + "_audio." + state.audioExt);
    updatePreviews();
  }

  function subDefaults() {
    var file = $("#subFileVideo").value;
    setValue("#subOutput", (file ? stem(file) : "subtitled") + "_sub.mp4");
    updatePreviews();
  }

  function customDefaults() {
    var file = $("#customFile").value;
    setValue("#customOutput", (file ? stem(file) : "output") + "_custom.mkv");
    updatePreviews();
  }

  function updateConvertCopyState() {
    var audio = ["mp3", "m4a", "wav", "flac"].indexOf(state.convertExt) >= 0;
    var cb = $("#convertCopy");
    if (audio) { cb.checked = false; cb.disabled = true; }
    else cb.disabled = false;
  }

  // ---------------------------------------------------------------- 任务

  function taskLabel(t) {
    return ACTION_LABEL[t.action] || t.action;
  }

  // 任务名称优先用后端给出的输出文件名，缺失时退回动作名称
  function taskName(t) {
    return t.name || taskLabel(t);
  }

  function updateTaskBadge() {
    var n = state.tasks.filter(function (t) {
      return t.status === "running" || t.status === "queued";
    }).length;
    var badge = $("#taskBadge");
    if (!badge) return;
    badge.textContent = n;
    badge.hidden = n === 0;
  }

  function renderTasks() {
    var box = $("#taskList");
    if (!box) return;
    box.innerHTML = "";
    state.tasks.forEach(function (t) {
      var item = document.createElement("div");
      item.className = "task-item " + t.status;
      var unknown = Number(t.progress) < 0;
      var pct = unknown ? 0 : Math.max(0, Math.min(100, Number(t.progress) || 0));
      item.innerHTML =
        '<div class="task-top">' +
          '<span class="task-name">' + esc(taskName(t)) + '</span>' +
          (t.name ? '<span class="task-kind">' + esc(taskLabel(t)) + '</span>' : '') +
          '<span class="status ' + esc(t.status) + '">' + esc(STATUS_LABEL[t.status] || t.status) + '</span>' +
          (t.status === "running" ? '<span class="task-time">' + esc(unknown ? taskStat(t) : pct.toFixed(0) + "%") + '</span>' : '') +
          '<span class="task-time">' + esc(fmtDate(t.created)) + '</span>' +
          '<span class="task-actions">' +
            '<button class="btn ghost small" data-log="' + esc(t.id) + '">日志</button>' +
            ((t.status === "running" || t.status === "queued") ? '<button class="btn danger small" data-cancel="' + esc(t.id) + '">取消</button>' : '') +
          '</span>' +
        '</div>' +
        '<div class="progress"><div class="progress-bar' + (unknown ? ' unknown' : '" style="width:' + pct + '%') + '"></div></div>';
      box.appendChild(item);
    });
    $("#tasksEmpty").hidden = state.tasks.length > 0;
  }

  function loadTasks(silent) {
    return api("/api/tasks").then(function (data) {
      var prev = {};
      state.tasks.forEach(function (t) { prev[t.id] = t.status; });
      state.tasks = data.tasks || [];
      state.tasks.forEach(function (t) {
        var before = prev[t.id];
        if ((before === "running" || before === "queued") &&
            (t.status === "done" || t.status === "failed" || t.status === "canceled")) {
          if (t.status === "done") {
            toast(taskName(t) + " 已完成", "success");
            if (state.logId !== t.id) loadFiles();
          } else if (t.status === "failed") {
            toast(taskName(t) + " 失败，请查看日志", "error", 6000);
          } else if (t.status === "canceled") {
            toast(taskName(t) + " 已取消");
          }
        }
      });
      state.seen = {};
      state.tasks.forEach(function (t) { state.seen[t.id] = t.status; });
      renderTasks();
      updateTaskBadge();
    }).catch(function (err) {
      if (!silent) toast("读取任务失败：" + err.message, "error");
    });
  }

  function startTask(action, params, options) {
    var opts = options || {};
    return post("/api/task", { action: action, params: params }).then(function (data) {
      // 所有任务提交后都不弹日志遮罩：留在当前页可继续添加，日志在任务页手动查看。
      toast(opts.toast || "任务已加入队列，可继续添加", "success", 2200);
      loadTasks(true);
      if (opts.onSuccess) opts.onSuccess(data);
      return data;
    }).catch(function (err) {
      toast(err.message, "error", 6000);
    });
  }

  function cancelTask(id, fromLog) {
    post("/api/cancel/" + encodeURIComponent(id), {}).then(function () {
      toast("已发送取消请求");
      loadTasks(true);
      if (fromLog) pollLog();
    }).catch(function (err) {
      toast(err.message, "error");
    });
  }

  // ---------------------------------------------------------------- 日志

  function openLog(id) {
    state.logId = id;
    state.logSeq = 0;
    state.logHasContent = false;
    $("#logModal").hidden = false;
    $("#logBody").textContent = "正在读取日志…";
    $("#logProgress").style.width = "0%";
    $("#logSub").textContent = "任务 " + id.slice(0, 8);
    for (var i = 0; i < state.tasks.length; i++) {
      if (state.tasks[i].id === id) {
        $("#logTitle").textContent = taskName(state.tasks[i]);
        break;
      }
    }
    pollLog();
    clearInterval(state.logTimer);
    state.logTimer = setInterval(pollLog, 1000);
  }

  function closeLog() {
    state.logId = null;
    state.logSeq = 0;
    state.logHasContent = false;
    $("#logModal").hidden = true;
    clearInterval(state.logTimer);
    state.logTimer = null;
    loadTasks(true);
  }

  function pollLog() {
    var id = state.logId;
    if (!id) return;
    var since = state.logSeq || 0;
    // 只拉取新增日志行，长任务不再每秒传输完整日志（最多 4000 行）。
    api("/api/log/" + encodeURIComponent(id) + "?since=" + since).then(function (data) {
      if (state.logId !== id) return;
      var box = $("#logBody");
      var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
      var prev = state.logHasContent && box.textContent ? box.textContent.split("\n") : [];
      var lines = data.log || [];
      var all = prev.concat(lines);
      if (all.length > 4000) all = ["... 日志过长，已截断 ..."].concat(all.slice(-3999));
      if (all.length) {
        state.logHasContent = true;
        box.textContent = all.join("\n");
      } else {
        box.textContent = "等待输出…";
      }
      state.logSeq = Number(data.next != null ? data.next : since) || 0;
      if (nearBottom) box.scrollTop = box.scrollHeight;
      var unknown = Number(data.progress) < 0;
      var pct = unknown ? 0 : Math.max(0, Math.min(100, Number(data.progress) || 0));
      $("#logProgress").style.width = unknown ? "" : pct + "%";
      $("#logProgress").className = unknown ? "progress-bar unknown" : "progress-bar";
      var task = null;
      for (var i = 0; i < state.tasks.length; i++) {
        if (state.tasks[i].id === id) { task = state.tasks[i]; break; }
      }
      $("#logTitle").textContent = task ? taskName(task) : "任务日志";
      $("#logSub").textContent = (STATUS_LABEL[data.status] || data.status) +
        " · " + (unknown ? taskStat(data) : pct.toFixed(0) + "%") + " · " + id.slice(0, 8);
      var cancelBtn = $("#logCancel");
      var active = data.status === "running" || data.status === "queued";
      cancelBtn.hidden = !active;
      if (!active) {
        clearInterval(state.logTimer);
        state.logTimer = null;
        loadTasks(true);
      }
    }).catch(function (err) {
      $("#logBody").textContent = "读取日志失败：" + err.message;
      clearInterval(state.logTimer);
      state.logTimer = null;
    });
  }

  // ---------------------------------------------------------------- 目录选择

  // 目录只通过「浏览」逐层选择（fnOS 来自授权目录，独立版来自本机任意目录），输入框保持只读。

  function openPicker(mode) {
    state.picker.mode = mode;
    $("#pickerTitle").textContent = mode === "output" ? "选择输出目录" : "选择输入目录";
    $("#pickerModal").hidden = false;
    $("#pickerNewName").value = "";
    browseTo("");
  }

  function browseTo(path) {
    var url = path ? "/api/browse?path=" + encodeURIComponent(path) : "/api/browse";
    $("#pickerList").innerHTML = '<div class="picker-empty">加载中…</div>';
    api(url).then(function (data) {
      state.picker.path = data.path || "";
      state.picker.parent = data.parent;
      state.roots = data.roots || state.roots;
      $("#pickerPath").textContent = data.path || "根目录";
      $("#pickerPath").title = data.path || "根目录";
      $("#pickerUp").disabled = data.parent === null || data.parent === undefined;
      $("#pickerChoose").disabled = !data.path;
      $("#pickerNewName").disabled = !data.path;
      $("#pickerMkdir").disabled = !data.path;
      var box = $("#pickerList");
      box.innerHTML = "";
      if (data.error) {
        box.innerHTML = '<div class="picker-empty">' + esc(data.error) + '</div>';
        return;
      }
      (data.dirs || []).forEach(function (dir) {
        var btn = document.createElement("button");
        btn.className = "picker-item";
        btn.innerHTML =
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>' +
          '<span>' + esc(dir.name) + '</span>' +
          (dir.writable ? "" : '<span class="lock">只读</span>');
        btn.addEventListener("click", function () { browseTo(dir.path); });
        box.appendChild(btn);
      });
      if (!(data.dirs || []).length) {
        box.innerHTML = '<div class="picker-empty">该目录下没有子文件夹</div>';
      }
    }).catch(function (err) {
      $("#pickerList").innerHTML = '<div class="picker-empty">' + esc(err.message) + '</div>';
      toast(err.message, "error", 6000);
    });
  }

  function mkdirInPicker() {
    var name = ($("#pickerNewName").value || "").trim();
    if (!name) { toast("请输入文件夹名称", "error"); return; }
    if (name.indexOf("/") >= 0 || name.indexOf("\\") >= 0 || name === "." || name === "..") {
      toast("文件夹名称不合法", "error");
      return;
    }
    var path = state.picker.path.replace(/\/+$/, "") + "/" + name;
    post("/api/mkdir", { path: path }).then(function () {
      $("#pickerNewName").value = "";
      toast("已创建文件夹", "success");
      browseTo(state.picker.path);
    }).catch(function (err) { toast(err.message, "error"); });
  }

  function choosePickerDir() {
    var path = state.picker.path;
    if (!path) { toast("请先选择一个文件夹", "error"); return; }
    var payload = {
      input_dir: state.config.input_dir,
      output_dir: state.config.output_dir
    };
    if (state.picker.mode === "output") payload.output_dir = path;
    else payload.input_dir = path;

    post("/api/config", payload).then(function (data) {
      applyConfig(data);
      $("#pickerModal").hidden = true;
      renderDirs();
      toast(state.picker.mode === "output" ? "输出目录已更新" : "输入目录已更新", "success");
      loadFiles();
    }).catch(function (err) {
      toast(err.message, "error", 6000);
    });
  }

  // ---------------------------------------------------------------- 各操作提交

  function requireDirs(needInput, needOutput) {
    if (needInput && !state.config.input_dir) {
      toast("请先设置输入目录", "error");
      setView("settings");
      return false;
    }
    if (needOutput && !state.config.output_dir) {
      toast("请先设置输出目录", "error");
      setView("settings");
      return false;
    }
    return true;
  }

  function runMerge() {
    if (!requireDirs(true, true)) return;
    if (state.merge.length < 2) {
      toast("请至少选择 2 个文件", "error");
      return;
    }
    var output = ($("#mergeOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    startTask("merge", {
      files: state.merge.slice(),
      output: output,
      reencode: $("#mergeReencode").checked
    });
  }

  function runProcess() {
    if (!requireDirs(true, true)) return;
    var input = $("#processFile").value;
    if (!input) { toast("请选择源文件", "error"); return; }
    var output = ($("#processOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    var sub = state.processSub;
    var params = { input: input, output: output };
    if (sub === "convert") {
      params.copy = $("#convertCopy").checked;
      startTask("convert", params);
    } else if (sub === "compress") {
      params.crf = Number($("#compressCrf").value);
      params.preset = $("#compressPreset").value;
      params.scale = ($("#compressScale").value || "").trim();
      startTask("compress", params);
    } else if (sub === "resize") {
      params.width = ($("#resizeWidth").value || "").trim();
      params.height = ($("#resizeHeight").value || "").trim();
      if (!params.width && !params.height) { toast("请填写宽或高", "error"); return; }
      startTask("resize", params);
    } else if (sub === "rotate") {
      params.direction = $("#rotateDir").value;
      startTask("rotate", params);
    }
  }

  function runClip() {
    if (!requireDirs(true, true)) return;
    var input = $("#clipFile").value;
    if (!input) { toast("请选择源文件", "error"); return; }
    var output = ($("#clipOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    if (state.clipSub === "trim") {
      var start = ($("#trimStart").value || "").trim();
      if (!start) { toast("请填写开始时间", "error"); return; }
      startTask("trim", {
        input: input,
        output: output,
        start: start,
        duration: ($("#trimDuration").value || "").trim(),
        copy: $("#trimCopy").checked
      });
    } else {
      var time = ($("#shotTime").value || "").trim();
      if (!time) { toast("请填写截图时间点", "error"); return; }
      startTask("screenshot", { input: input, output: output, time: time });
    }
  }

  function runAudio() {
    if (!requireDirs(true, true)) return;
    var input = $("#audioFile").value;
    if (!input) { toast("请选择源文件", "error"); return; }
    var output = ($("#audioOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    startTask("extract_audio", { input: input, output: output });
  }

  function runSubtitle() {
    if (!requireDirs(true, true)) return;
    var input = $("#subFileVideo").value;
    var sub = $("#subFileSub").value;
    if (!input) { toast("请选择视频文件", "error"); return; }
    if (!sub) { toast("请选择字幕文件", "error"); return; }
    var output = ($("#subOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    startTask("subtitle", {
      input: input,
      subtitle: sub,
      output: output,
      font_size: ($("#subFontSize").value || "").trim()
    });
  }

  function runDownload() {
    if (!requireDirs(false, true)) return;
    var url = ($("#dlUrl").value || "").trim().replace(/^[`'"<>]+|[`'"<>]+$/g, "").trim();
    if (!url) { toast("请填写视频地址", "error"); return; }
    if (!/^https?:\/\//i.test(url)) { toast("请填写 http/https 地址", "error"); return; }
    var params = {
      url: url,
      mode: state.dlMode,
      filename: ($("#dlFilename").value || "").trim(),
      subdir: ($("#dlSubdir").value || "").trim(),
      proxy: ($("#dlProxy").value || "").trim(),
      referer: ($("#dlReferer").value || "").trim(),
      headers: ($("#dlHeaders").value || "").trim()
    };
    if (state.dlMode === "audio") {
      params.audio_format = $("#dlAudioFormat").value;
    } else {
      params.quality = $("#dlQuality").value;
    }
    startTask("download", params, {
      toast: "已加入下载队列，可继续添加",
      onSuccess: function () {
        $("#dlUrl").value = "";
        $("#dlFilename").value = "";
        updatePreviews();
        $("#dlUrl").focus();
      }
    });
  }

  function runCustom() {
    if (!requireDirs(true, true)) return;
    var input = $("#customFile").value;
    if (!input) { toast("请选择输入文件", "error"); return; }
    var args = ($("#customArgs").value || "").trim();
    if (!args) { toast("请填写 ffmpeg 参数", "error"); return; }
    var output = ($("#customOutput").value || "").trim();
    if (!output) { toast("请填写输出文件名", "error"); return; }
    startTask("custom", { input: input, output: output, args: args });
  }

  // ---------------------------------------------------------------- 事件绑定

  function bindNav() {
    $$(".nav-item").forEach(function (btn) {
      btn.addEventListener("click", function () { setView(btn.getAttribute("data-view")); });
    });
    $$(".mnav-item").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.hasAttribute("data-more")) {
          $("#moreSheet").hidden = false;
        } else {
          setView(btn.getAttribute("data-view"));
        }
      });
    });
    $$(".sheet-item").forEach(function (btn) {
      btn.addEventListener("click", function () {
        $("#moreSheet").hidden = true;
        setView(btn.getAttribute("data-view"));
      });
    });
    $("#moreClose").addEventListener("click", function () { $("#moreSheet").hidden = true; });
    $("#moreSheet").addEventListener("mousedown", function (e) {
      if (e.target === this) this.hidden = true;
    });
  }

  function bindPickButtons() {
    document.addEventListener("click", function (e) {
      var target = e.target.closest("[data-pick]");
      if (!target) return;
      e.preventDefault();
      openPicker(target.getAttribute("data-pick"));
    });
  }

  function bindMerge() {
    $("#mergeSearch").addEventListener("input", renderMergeFiles);
    $("#mergeClear").addEventListener("click", function () {
      state.merge = [];
      renderMergeFiles();
      renderMergeSelected();
    });
    $("#mergeRun").addEventListener("click", runMerge);
    $("#mergeOutput").addEventListener("input", function () {
      if (this.value !== state.mergeNameLast) {
        state.mergeNameAuto = false;
        var hint = $("#mergeNameHint");
        if (hint) hint.textContent = "已手动修改文件名，增删输入文件不会再自动覆盖";
      }
      updatePreviews();
    });
  }

  function bindProcess() {
    $$("#processSeg button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.processSub = btn.getAttribute("data-sub");
        $$("#processSeg button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        $$('[data-view="process"] .sub-panel').forEach(function (p) {
          p.hidden = p.getAttribute("data-sub") !== state.processSub;
        });
        processDefaults();
      });
    });

    $("#processFile").addEventListener("change", processDefaults);

    $$("#convertFormats button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.convertExt = btn.getAttribute("data-ext");
        $$("#convertFormats button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        updateConvertCopyState();
        processDefaults();
      });
    });

    $("#compressCrf").addEventListener("input", function () {
      $("#crfValue").textContent = this.value;
    });
    $("#processOutput").addEventListener("input", updatePreviews);
    $("#processRun").addEventListener("click", runProcess);
  }

  function bindClip() {
    $$("#clipSeg button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.clipSub = btn.getAttribute("data-sub");
        $$("#clipSeg button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        $$('[data-view="clip"] .sub-panel').forEach(function (p) {
          p.hidden = p.getAttribute("data-sub") !== state.clipSub;
        });
        clipDefaults();
      });
    });
    $("#clipFile").addEventListener("change", clipDefaults);
    $("#shotFormat").addEventListener("change", clipDefaults);
    $("#clipOutput").addEventListener("input", updatePreviews);
    $("#clipRun").addEventListener("click", runClip);
  }

  function bindAudio() {
    $("#audioFile").addEventListener("change", audioDefaults);
    $$("#audioFormats button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.audioExt = btn.getAttribute("data-ext");
        $$("#audioFormats button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        audioDefaults();
      });
    });
    $("#audioOutput").addEventListener("input", updatePreviews);
    $("#audioRun").addEventListener("click", runAudio);
  }

  function bindSubtitle() {
    $("#subFileVideo").addEventListener("change", subDefaults);
    $("#subOutput").addEventListener("input", updatePreviews);
    $("#subRun").addEventListener("click", runSubtitle);
  }

  function bindDownload() {
    $$("#dlMode button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.dlMode = btn.getAttribute("data-mode");
        $$("#dlMode button").forEach(function (b) { b.classList.toggle("active", b === btn); });
        var audio = state.dlMode === "audio";
        $("#dlQualityWrap").hidden = audio;
        $("#dlAudioFormatWrap").hidden = !audio;
        updatePreviews();
      });
    });
    $("#dlRun").addEventListener("click", runDownload);
    $("#dlSubdir").addEventListener("input", updatePreviews);
    $("#dlFilename").addEventListener("input", updatePreviews);
  }

  function bindAdvanced() {
    $("#customFile").addEventListener("change", customDefaults);
    $("#customOutput").addEventListener("input", updatePreviews);
    $("#customRun").addEventListener("click", runCustom);
  }

  function bindTasks() {
    $("#tasksRefresh").addEventListener("click", function () { loadTasks(false); });
    $("#taskList").addEventListener("click", function (e) {
      var logBtn = e.target.closest("[data-log]");
      if (logBtn) { openLog(logBtn.getAttribute("data-log")); return; }
      var cancelBtn = e.target.closest("[data-cancel]");
      if (cancelBtn) cancelTask(cancelBtn.getAttribute("data-cancel"), false);
    });
  }

  function bindSettings() {
    // 目录只能通过「浏览」选择（fnOS 授权目录 / 独立版本机目录），输入框保持只读。
    ["#setInput", "#setOutput"].forEach(function (sel) {
      var el = $(sel);
      if (el) el.readOnly = true;
    });
  }

  function bindAbout() {
    if ($("#checkUpdate")) $("#checkUpdate").addEventListener("click", checkUpdate);
  }

  function bindPicker() {
    $("#pickerClose").addEventListener("click", function () { $("#pickerModal").hidden = true; });
    $("#pickerCancel").addEventListener("click", function () { $("#pickerModal").hidden = true; });
    $("#pickerUp").addEventListener("click", function () {
      if (state.picker.parent !== null && state.picker.parent !== undefined) {
        browseTo(state.picker.parent);
      }
    });
    $("#pickerChoose").addEventListener("click", choosePickerDir);
    $("#pickerMkdir").addEventListener("click", mkdirInPicker);
    $("#pickerNewName").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); mkdirInPicker(); }
    });
  }

  function bindLog() {
    $("#logClose").addEventListener("click", closeLog);
    $("#logCancel").addEventListener("click", function () {
      if (state.logId) cancelTask(state.logId, true);
    });
  }

  function bindGlobal() {
    $("#themeBtn").addEventListener("click", function () {
      applyTheme(currentTheme() === "dark" ? "light" : "dark");
    });
    window.addEventListener("hashchange", function () {
      var name = (window.location.hash || "").replace(/^#/, "");
      if (VIEW_META[name] && name !== state.view) setView(name);
    });
    $("#refreshAll").addEventListener("click", function () {
      loadTools().then(loadFiles).then(function () { toast("已刷新", "success", 1600); });
    });
    $$(".modal-backdrop").forEach(function (backdrop) {
      backdrop.addEventListener("mousedown", function (e) {
        if (e.target !== backdrop) return;
        if (backdrop.id === "logModal") closeLog();
        else backdrop.hidden = true;
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (!$("#logModal").hidden) closeLog();
      else if (!$("#pickerModal").hidden) $("#pickerModal").hidden = true;
      else if (!$("#moreSheet").hidden) $("#moreSheet").hidden = true;
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) loadTasks(true);
    });
  }

  // ---------------------------------------------------------------- 启动

  function init() {
    var saved = null;
    try { saved = localStorage.getItem("vt-theme"); } catch (e) {}
    applyTheme(saved || "auto");

    bindNav();
    bindPickButtons();
    bindMerge();
    bindProcess();
    bindClip();
    bindAudio();
    bindSubtitle();
    bindDownload();
    bindAdvanced();
    bindTasks();
    bindSettings();
    bindAbout();
    bindPicker();
    bindLog();
    bindGlobal();

    var initial = (window.location.hash || "").replace(/^#/, "");
    setView(VIEW_META[initial] ? initial : "merge");

    updateConvertCopyState();
    startApp();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

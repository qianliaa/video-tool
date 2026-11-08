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
    mergeNameMode: "manual",
    mergeNamePath: "",
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
    taskTimer: null,
    updateBusy: false,
    users: [],
    auth: { configured: false, authenticated: false, setup: false, busy: false,
            username: "", role: "", isAdmin: false, authFile: "" }
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

  var STATUS_LABEL = { running: "运行中", done: "已完成", failed: "失败", canceled: "已取消" };

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

  function mergeNameFrom(path) {
    var value = sanitizeFileName(stem(path));
    return (value || "merged") + ".mp4";
  }

  function mergeCommonName(paths) {
    var stems = paths.map(function (p) { return sanitizeFileName(stem(p)) || "merged"; });
    if (!stems.length) return "";
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
    if (prefix.length < 2) prefix = stems[0];
    prefix = sanitizeFileName(prefix) || "merged";
    return prefix + ".mp4";
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
        if (res.status === 401) {
          var deny = new Error((data && data.error) || "未登录或登录已过期");
          deny.unauthorized = true;
          handleUnauthorized();
          throw deny;
        }
        if (!res.ok) {
          var err = new Error((data && data.error) || ("请求失败（" + res.status + "）"));
          err.status = res.status;
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

  // ---------------------------------------------------------------- 登录

  function authFetch(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("请求失败（" + res.status + "）"));
        return data;
      });
    });
  }

  function setAuthError(message) {
    var box = $("#authError");
    if (!box) return;
    box.textContent = message || "";
    box.hidden = !message;
  }

  function showAuth(configured) {
    state.auth.configured = !!configured;
    state.auth.setup = !configured;
    state.auth.authenticated = false;
    var modal = $("#authModal");
    if (!modal) return;
    modal.hidden = false;
    $("#authTitle").textContent = configured ? "登录" : "创建管理员账号";
    $("#authDesc").textContent = configured
      ? "请输入账号与密码继续使用"
      : "首次使用，请设置管理员账号与密码（密码至少 8 个字符）";
    $("#authSubmit").textContent = configured ? "登录" : "创建并进入";
    $("#authPassword").autocomplete = configured ? "current-password" : "new-password";
    $("#authPassword").value = "";
    $("#authRemember").checked = true;
    if (configured) {
      var saved = "";
      try { saved = localStorage.getItem("vt-user") || ""; } catch (e) {}
      $("#authUsername").value = saved;
      $("#authUsername").placeholder = "例如 admin";
    } else {
      $("#authUsername").value = "admin";
      $("#authUsername").placeholder = "admin";
    }
    $("#authUsername").disabled = false;
    $("#authRemember").disabled = false;
    $("#authHint").hidden = false;
    setAuthError("");
    setTimeout(function () {
      if (!modal.hidden) {
        if (!configured) $("#authUsername").focus();
        else $("#authPassword").focus();
      }
    }, 60);
  }

  function applyAuthStatus(data) {
    data = data || {};
    state.auth.configured = !!data.configured;
    state.auth.authenticated = !!data.authenticated;
    state.auth.username = data.username || "";
    state.auth.role = data.role || "";
    state.auth.isAdmin = !!data.is_admin;
    if (data.auth_file) state.auth.authFile = data.auth_file;
  }

  function handleUnauthorized() {
    stopPolling();
    state.auth.authenticated = false;
    if (state.logId) {
      state.logId = null;
      $("#logModal").hidden = true;
    }
    if (!$("#authModal").hidden) return;
    showAuth(state.auth.configured);
  }

  function checkAuth() {
    return fetch("/api/auth/status", { cache: "no-store" })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) throw new Error((data && data.error) || ("访问校验失败（" + res.status + "）"));
          return data;
        });
      })
      .then(function (data) {
        applyAuthStatus(data);
        if (state.auth.authenticated) startApp();
        else showAuth(state.auth.configured);
      })
      .catch(function (err) {
        showAuth(state.auth.configured);
        setAuthError("无法连接服务：" + err.message);
      });
  }

  function submitAuth(event) {
    event.preventDefault();
    if (state.auth.busy) return;
    var username = ($("#authUsername").value || "").trim();
    var password = $("#authPassword").value;
    if (!/^[A-Za-z0-9_.-]{2,32}$/.test(username)) {
      setAuthError("账号名需为 2-32 位字母、数字、下划线、点或短横线");
      $("#authUsername").focus();
      return;
    }
    if (password.length < 8) {
      setAuthError("密码至少需要 8 个字符");
      $("#authPassword").focus();
      return;
    }
    state.auth.busy = true;
    $("#authSubmit").disabled = true;
    setAuthError("");
    var wasSetup = state.auth.setup;
    var path = wasSetup ? "/api/auth/setup" : "/api/auth/login";
    authFetch(path, {
      username: username,
      password: password,
      remember: !!$("#authRemember").checked
    }).then(function (data) {
      try { localStorage.setItem("vt-user", username); } catch (e) {}
      state.auth.busy = false;
      state.auth.configured = true;
      state.auth.authenticated = true;
      state.auth.setup = false;
      state.auth.username = data.username || username;
      state.auth.role = data.role || "";
      state.auth.isAdmin = !!data.is_admin;
      $("#authSubmit").disabled = false;
      $("#authModal").hidden = true;
      toast(wasSetup ? "管理员账号已创建" : "已登录", "success", 1800);
      startApp();
    }).catch(function (err) {
      state.auth.busy = false;
      $("#authSubmit").disabled = false;
      setAuthError(err.message);
      $("#authPassword").select();
    });
  }

  // ---------------------------------------------------------------- 账号与用户

  function logout() {
    authFetch("/api/auth/logout", {}).catch(function () {}).then(function () {
      state.auth.authenticated = false;
      state.users = [];
      stopPolling();
      showAuth(true);
      toast("已退出登录", "success", 1800);
    });
  }

  function renderAccount() {
    var box = $("#accountInfo");
    if (!box) return;
    var rows = [
      ["当前账号", state.auth.username || "-"],
      ["角色", state.auth.role === "admin" ? "管理员" : "普通用户"],
      ["登录状态", state.auth.authenticated ? "已登录" : "未登录"],
      ["账号文件", state.auth.authFile || "-"]
    ];
    box.innerHTML = rows.map(function (r) {
      return '<div><span>' + esc(r[0]) + '</span><span title="' + esc(r[1]) + '">' + esc(r[1]) + '</span></div>';
    }).join("");
    var chip = $("#accountRole");
    if (chip) {
      chip.textContent = state.auth.role === "admin" ? "管理员" : "普通用户";
      chip.className = "role-chip" + (state.auth.role === "admin" ? " admin" : "");
    }
    var path = $("#authFilePath");
    if (path) path.textContent = state.auth.authFile || "users.json";
    if (state.auth.isAdmin && state.tools && state.tools.identity_note) {
      var note = $("#identityNote");
      if (note) {
        note.hidden = false;
        note.textContent = "管理员来源：" + (state.tools.identity_note || "");
      }
    }
  }

  function renderUsers() {
    var box = $("#userList");
    if (!box) return;
    var me = state.auth.username;
    var html = state.users.map(function (u) {
      var self = u.username === me;
      return '<div class="user-row">' +
        '<div class="user-main"><strong>' + esc(u.username) + '</strong>' +
        '<span class="role-chip' + (u.role === "admin" ? " admin" : "") + '">' +
        (u.role === "admin" ? "管理员" : "普通用户") + '</span>' +
        '<span class="user-state' + (u.enabled ? " ok" : " off") + '">' +
        (u.enabled ? "启用" : "已停用") + '</span>' +
        (self ? '<span class="user-self">当前</span>' : '') + '</div>' +
        '<div class="user-ops">' +
        '<button class="btn ghost small" data-user-reset="' + esc(u.username) + '">重置密码</button>' +
        '<button class="btn ghost small" data-user-toggle="' + esc(u.username) + '" data-enabled="' + (u.enabled ? "1" : "0") + '"' +
        (self ? ' disabled title="不能停用当前账号"' : '') + '>' + (u.enabled ? "停用" : "启用") + '</button>' +
        '<button class="btn danger small" data-user-delete="' + esc(u.username) + '"' +
        (self ? ' disabled title="不能删除当前账号"' : '') + '>删除</button>' +
        '</div></div>';
    }).join("");
    box.innerHTML = html || '<p class="hint">暂无用户</p>';
  }

  function loadUsers() {
    if (!state.auth.isAdmin) return Promise.resolve();
    return api("/api/users").then(function (data) {
      state.users = (data && data.users) || [];
      renderUsers();
    }).catch(function (err) {
      if (!err.unauthorized) toast("加载用户失败：" + err.message, "error", 6000);
    });
  }

  function createUser() {
    var username = ($("#newUserName").value || "").trim();
    var password = $("#newUserPassword").value;
    var role = $("#newUserRole").value;
    if (!/^[A-Za-z0-9_.-]{2,32}$/.test(username)) {
      toast("账号名需为 2-32 位字母、数字、下划线、点或短横线", "error", 5000);
      return;
    }
    if (password.length < 8) {
      toast("密码至少需要 8 个字符", "error", 5000);
      return;
    }
    post("/api/users/create", { username: username, password: password, role: role })
      .then(function () {
        $("#newUserName").value = "";
        $("#newUserPassword").value = "";
        toast("用户已创建", "success", 2000);
        loadUsers();
      }).catch(function (err) { toast(err.message, "error", 6000); });
  }

  function toggleUser(name, enabled) {
    post("/api/users/update", { username: name, enabled: enabled })
      .then(function () {
        toast(enabled ? "已启用账号" : "已停用账号", "success", 2000);
        loadUsers();
      }).catch(function (err) { toast(err.message, "error", 6000); });
  }

  function resetUserPassword(name) {
    var password = window.prompt("为账号 " + name + " 设置新密码（至少 8 个字符）", "");
    if (password === null) return;
    if (password.length < 8) { toast("密码至少需要 8 个字符", "error", 5000); return; }
    post("/api/users/password", { username: name, password: password })
      .then(function () {
        toast("密码已重置，该账号需重新登录", "success", 2600);
        loadUsers();
      }).catch(function (err) { toast(err.message, "error", 6000); });
  }

  function deleteUser(name) {
    if (!window.confirm("确定删除账号 " + name + "？该账号将立即失去登录权限。")) return;
    post("/api/users/delete", { username: name })
      .then(function () {
        toast("账号已删除", "success", 2000);
        loadUsers();
      }).catch(function (err) { toast(err.message, "error", 6000); });
  }

  function applyAuthFromTools(tools) {
    var t = tools || {};
    if (t.username) state.auth.username = t.username;
    if (t.role) state.auth.role = t.role;
    state.auth.isAdmin = !!t.is_admin;
    if (t.auth_file) state.auth.authFile = t.auth_file;
    renderAccount();
    var card = $("#usersCard");
    if (card) card.hidden = !state.auth.isAdmin;
    if (state.auth.isAdmin) loadUsers();
  }

  function stopPolling() {
    clearInterval(state.taskTimer);
    state.taskTimer = null;
    clearInterval(state.logTimer);
    state.logTimer = null;
  }

  function startApp() {
    state.auth.authenticated = true;
    $("#authModal").hidden = true;
    loadTools()
      .then(function (tools) {
        applyAuthFromTools(tools);
        return loadConfig();
      })
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
        if (!err || !err.unauthorized) toast("初始化失败：" + err.message, "error", 8000);
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
          hint.textContent = "输入目录不可用：" + (state.config.input_error || "请检查文件夹授权");
        } else if (!output) {
          hint.textContent = "还差输出目录：用于保存处理结果。";
        } else if (!outputReady) {
          hint.textContent = "输出目录不可用：" + (state.config.output_error || "请检查文件夹授权");
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
    updateMergeNameReference();
  }

  function moveMerge(idx, delta) {
    var to = idx + delta;
    if (to < 0 || to >= state.merge.length) return;
    var item = state.merge.splice(idx, 1)[0];
    state.merge.splice(to, 0, item);
    renderMergeFiles();
    renderMergeSelected();
  }

  function syncMergeNameSelect() {
    var sel = $("#mergeNameFrom");
    if (!sel) return;
    var value = state.mergeNameMode === "file"
      ? "file:" + state.mergeNamePath : state.mergeNameMode;
    var html = '<option value="manual">手动输入</option>' +
      '<option value="common">公共部分</option>';
    state.merge.forEach(function (path) {
      var f = mergeFileByPath(path);
      html += '<option value="file:' + esc(path) + '">文件名：' +
        esc(f ? (f.name || baseName(path)) : baseName(path)) + "</option>";
    });
    sel.innerHTML = html;
    sel.value = value;
    if (!sel.value) {
      state.mergeNameMode = "manual";
      state.mergeNamePath = "";
      sel.value = "manual";
    }
  }

  function applyMergeNameReference() {
    if (state.mergeNameMode === "manual") return;
    var value = "";
    if (state.mergeNameMode === "file") {
      if (state.merge.indexOf(state.mergeNamePath) >= 0) {
        value = mergeNameFrom(state.mergeNamePath);
      }
    } else if (state.merge.length) {
      value = mergeCommonName(state.merge);
    }
    if (!value) return;
    var input = $("#mergeOutput");
    if (input) input.value = value;
    updatePreviews();
  }

  function updateMergeNameReference() {
    if (state.mergeNameMode === "file" && state.merge.indexOf(state.mergeNamePath) < 0) {
      state.mergeNameMode = "common";
      state.mergeNamePath = "";
    }
    if (state.mergeNameMode === "common" && !state.merge.length) {
      state.mergeNameMode = "manual";
    }
    syncMergeNameSelect();
    applyMergeNameReference();
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

  function updateTaskBadge() {
    var n = state.tasks.filter(function (t) { return t.status === "running"; }).length;
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
          '<span class="task-name">' + esc(taskLabel(t)) + '</span>' +
          '<span class="status ' + esc(t.status) + '">' + esc(STATUS_LABEL[t.status] || t.status) + '</span>' +
          (t.status === "running" ? '<span class="task-time">' + esc(unknown ? taskStat(t) : pct.toFixed(0) + "%") + '</span>' : '') +
          '<span class="task-time">' + esc(fmtDate(t.created)) + '</span>' +
          '<span class="task-actions">' +
            '<button class="btn ghost small" data-log="' + esc(t.id) + '">日志</button>' +
            (t.status === "running" ? '<button class="btn danger small" data-cancel="' + esc(t.id) + '">取消</button>' : '') +
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
        if (before === "running" && t.status !== "running") {
          if (t.status === "done") {
            toast(taskLabel(t) + " 已完成", "success");
            if (state.logId !== t.id) loadFiles();
          } else if (t.status === "failed") {
            toast(taskLabel(t) + " 失败，请查看日志", "error", 6000);
          } else if (t.status === "canceled") {
            toast(taskLabel(t) + " 已取消");
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

  function startTask(action, params) {
    return post("/api/task", { action: action, params: params }).then(function (data) {
      toast("任务已启动", "success", 2200);
      loadTasks(true);
      openLog(data.task);
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
    $("#logModal").hidden = false;
    $("#logBody").textContent = "正在读取日志…";
    $("#logProgress").style.width = "0%";
    $("#logSub").textContent = "任务 " + id.slice(0, 8);
    pollLog();
    clearInterval(state.logTimer);
    state.logTimer = setInterval(pollLog, 1000);
  }

  function closeLog() {
    state.logId = null;
    $("#logModal").hidden = true;
    clearInterval(state.logTimer);
    state.logTimer = null;
    loadTasks(true);
  }

  function pollLog() {
    var id = state.logId;
    if (!id) return;
    api("/api/log/" + encodeURIComponent(id)).then(function (data) {
      if (state.logId !== id) return;
      var box = $("#logBody");
      var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
      var text = (data.log || []).join("\n");
      box.textContent = text || "等待输出…";
      if (nearBottom) box.scrollTop = box.scrollHeight;
      var unknown = Number(data.progress) < 0;
      var pct = unknown ? 0 : Math.max(0, Math.min(100, Number(data.progress) || 0));
      $("#logProgress").style.width = unknown ? "" : pct + "%";
      $("#logProgress").className = unknown ? "progress-bar unknown" : "progress-bar";
      var task = null;
      for (var i = 0; i < state.tasks.length; i++) {
        if (state.tasks[i].id === id) { task = state.tasks[i]; break; }
      }
      $("#logTitle").textContent = task ? taskLabel(task) : "任务日志";
      $("#logSub").textContent = (STATUS_LABEL[data.status] || data.status) +
        " · " + (unknown ? taskStat(data) : pct.toFixed(0) + "%") + " · " + id.slice(0, 8);
      var cancelBtn = $("#logCancel");
      cancelBtn.hidden = data.status !== "running";
      if (data.status !== "running") {
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

  function openPicker(mode) {
    state.picker.mode = mode;
    $("#pickerTitle").textContent = mode === "output" ? "选择输出目录" : "选择输入目录";
    $("#pickerModal").hidden = false;
    $("#pickerNewName").value = "";
    var start = mode === "output" ? state.config.output_dir : state.config.input_dir;
    $("#pickerPathInput").value = start || "";
    browseTo(start || "/");
  }

  function browseTo(path) {
    if (!path) {
      $("#pickerList").innerHTML = '<div class="picker-empty">没有可浏览的目录</div>';
      return;
    }
    $("#pickerList").innerHTML = '<div class="picker-empty">加载中…</div>';
    api("/api/browse?path=" + encodeURIComponent(path)).then(function (data) {
      state.picker.path = data.path;
      state.picker.parent = data.parent;
      state.roots = data.roots || state.roots;
      $("#pickerPath").textContent = data.path;
      $("#pickerPath").title = data.path;
      $("#pickerPathInput").value = data.path;
      $("#pickerUp").disabled = !data.parent;
      var box = $("#pickerList");
      box.innerHTML = "";
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
      if (data.error) {
        box.innerHTML = '<div class="picker-empty">' + esc(data.error) + '</div>';
      } else if (!(data.dirs || []).length) {
        box.innerHTML = '<div class="picker-empty">该目录下没有子文件夹</div>';
      }
    }).catch(function (err) {
      $("#pickerList").innerHTML = '<div class="picker-empty">' + esc(err.message) + '</div>';
      toast(err.message, "error", 6000);
    });
  }

  function gotoPickerPath() {
    var raw = ($("#pickerPathInput").value || "").trim();
    if (!raw) { toast("请填写目录路径", "error"); return; }
    post("/api/checkdir", { path: raw, purpose: state.picker.mode }).then(function (data) {
      state.roots = data.roots || state.roots;
      browseTo(data.path);
    }).catch(function (err) { toast(err.message, "error", 6000); });
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
    if (!path) return;
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
    startTask("download", params);
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
      if (state.mergeNameMode !== "manual") {
        state.mergeNameMode = "manual";
        state.mergeNamePath = "";
        syncMergeNameSelect();
      }
      updatePreviews();
    });
    $("#mergeNameFrom").addEventListener("change", function () {
      var value = this.value || "manual";
      if (value === "manual" || value === "common") {
        state.mergeNameMode = value;
        state.mergeNamePath = "";
      } else if (value.indexOf("file:") === 0) {
        state.mergeNameMode = "file";
        state.mergeNamePath = value.slice(5);
      } else {
        state.mergeNameMode = "manual";
        state.mergeNamePath = "";
      }
      updateMergeNameReference();
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
    $("#setSave").addEventListener("click", function () {
      post("/api/config", {
        input_dir: ($("#setInput").value || "").trim(),
        output_dir: ($("#setOutput").value || "").trim()
      }).then(function (data) {
        applyConfig(data);
        renderDirs();
        toast("目录设置已保存", "success");
        loadFiles();
      }).catch(function (err) { toast(err.message, "error", 6000); });
    });
    $("#authLogout").addEventListener("click", logout);
    $("#usersRefresh").addEventListener("click", function () { loadUsers(); });
    $("#newUserCreate").addEventListener("click", createUser);
    $("#newUserPassword").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); createUser(); }
    });
    $("#userList").addEventListener("click", function (e) {
      var reset = e.target.closest("[data-user-reset]");
      if (reset) { resetUserPassword(reset.getAttribute("data-user-reset")); return; }
      var toggle = e.target.closest("[data-user-toggle]");
      if (toggle) {
        toggleUser(toggle.getAttribute("data-user-toggle"),
                   toggle.getAttribute("data-enabled") !== "1");
        return;
      }
      var del = e.target.closest("[data-user-delete]");
      if (del) deleteUser(del.getAttribute("data-user-delete"));
    });
  }

  function bindAbout() {
    if ($("#checkUpdate")) $("#checkUpdate").addEventListener("click", checkUpdate);
  }

  function bindPicker() {
    $("#pickerClose").addEventListener("click", function () { $("#pickerModal").hidden = true; });
    $("#pickerCancel").addEventListener("click", function () { $("#pickerModal").hidden = true; });
    $("#pickerUp").addEventListener("click", function () {
      if (state.picker.parent) browseTo(state.picker.parent);
    });
    $("#pickerChoose").addEventListener("click", choosePickerDir);
    $("#pickerMkdir").addEventListener("click", mkdirInPicker);
    $("#pickerGo").addEventListener("click", gotoPickerPath);
    $("#pickerPathInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); gotoPickerPath(); }
    });
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
    $("#sideSettings").addEventListener("click", function () { setView("settings"); });
    $$(".modal-backdrop").forEach(function (backdrop) {
      backdrop.addEventListener("mousedown", function (e) {
        if (e.target !== backdrop) return;
        if (backdrop.id === "authModal") return;
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
    $("#authForm").addEventListener("submit", submitAuth);

    var initial = (window.location.hash || "").replace(/^#/, "");
    setView(VIEW_META[initial] ? initial : "merge");

    updateConvertCopyState();
    checkAuth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

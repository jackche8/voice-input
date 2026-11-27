/* 方案 C 侧栏效率工作站主页纯 UI（翻译框 / 图片 OCR / 历史 / 手机扫码 / 模型与热键设置 / 运行日志 / 关于）。
 * 严守纯展示边界：不发起网络请求、不读写剪贴板或存储、不拼接口路径，所有动作经 hooks 回调通知业务层。
 */
(function (V3) {
  "use strict";

  var NAV_ICONS = {
    translate: "globe",
    ocr: "scan",
    history: "clock",
    lan: "phone",
    setup: "settings",
    logs: "file-text",
    about: "info"
  };

  var PANEL_META = {
    translate: {
      title: "本地翻译与 OCR 工作台",
      sub: "手动粘贴文本即译 · Ctrl+V 粘贴截图自动提取文字并翻译 · 本地离线运行"
    },
    ocr: {
      title: "图片 OCR 识别提取",
      sub: "拖入图片、选择文件或按 Ctrl+V 粘贴截图 · 可调参预览并保存全局预处理预设（同时生效于截屏翻译与纯 OCR）"
    },
    history: {
      title: "语音输入与翻译历史",
      sub: "本机、手机与重发的处理记录（不代表文字已经输入到目标窗口）"
    },
    lan: {
      title: "手机局域网扫码语音录入",
      sub: "同一局域网下手机扫码打开录音页，按住说话识别后输入到电脑当前光标处"
    },
    setup: {
      title: "模型与热键设置",
      sub: ""
    },
    logs: {
      title: "运行日志",
      sub: "全宽日志阅读与胶囊切源 · 所有敏感令牌与代理密码均由后端脱敏为 ***"
    },
    about: {
      title: "关于",
      sub: "版本、运行环境、本地目录与双许可信息（由本地服务权威返回）"
    }
  };

  var HOTKEY_ORDER = ["hotkey_voice", "hotkey_screenshot", "hotkey_ocr", "hotkey_repeat", "hotkey_annotate"];

  function call(hooks, name, payload) {
    var fn = hooks && hooks[name];
    if (typeof fn !== "function") return undefined;
    var res = fn(payload);
    if (res && typeof res.then === "function") {
      res.then(undefined, function () {});
    }
    return res;
  }

  function isConnected(state) {
    return state.connection === "connected";
  }

  function asrReady(asr) {
    return !!(asr && asr.status === "ready" && asr.sidecarRunning === true);
  }

  function ollamaReady(ollama) {
    return !!(ollama && ollama.modelStatus === "ready" && ollama.running === true);
  }

  function sanitizeLanPatch(patch) {
    if (!patch || !Array.isArray(patch.lanEntries)) return patch;
    var copy = Object.assign({}, patch);
    copy.lanEntries = patch.lanEntries.map(function (entry) {
      entry = entry || {};
      return {
        ip: entry.ip || "",
        urlMasked: entry.urlMasked || entry.url_masked || "",
        qrSvg: entry.qrSvg || entry.qr_svg || "",
        qrDataUrl: entry.qrDataUrl || ""
      };
    });
    return copy;
  }

  function unpackMountArgs(initialStateOrOpts, maybeHooks) {
    if (
      maybeHooks === undefined &&
      initialStateOrOpts &&
      typeof initialStateOrOpts === "object" &&
      (Object.prototype.hasOwnProperty.call(initialStateOrOpts, "hooks") ||
        Object.prototype.hasOwnProperty.call(initialStateOrOpts, "initialState"))
    ) {
      return {
        initialState: initialStateOrOpts.initialState || {},
        hooks: initialStateOrOpts.hooks || {}
      };
    }
    return {
      initialState: initialStateOrOpts || {},
      hooks: maybeHooks || {}
    };
  }

  function mountHome(root, initialStateOrOpts, maybeHooks) {
    if (root && typeof root.__v3HomeCleanup === "function") {
      root.__v3HomeCleanup();
    }
    var unpacked = unpackMountArgs(initialStateOrOpts, maybeHooks);
    var hooks = unpacked.hooks;
    var cleanInit = sanitizeLanPatch(unpacked.initialState);
    var baseState = V3.defaults.homeState();
    if (
      (!cleanInit || !cleanInit.theme) &&
      typeof document !== "undefined" &&
      document.documentElement &&
      document.documentElement.dataset &&
      (document.documentElement.dataset.theme === "light" || document.documentElement.dataset.theme === "dark")
    ) {
      baseState.theme = document.documentElement.dataset.theme;
    }
    var state = V3.defaults.merge(baseState, cleanInit);
    if (cleanInit && cleanInit.panel && !cleanInit.activePanel) {
      state.activePanel = cleanInit.panel;
    }
    if (cleanInit && cleanInit.tab && !cleanInit.setupTab) {
      state.setupTab = cleanInit.tab;
    }
    if (state.translate_lang_pair && (!cleanInit || !cleanInit.translate || !cleanInit.translate.lang_pair)) {
      state.translate = Object.assign({}, state.translate, { lang_pair: state.translate_lang_pair });
    }
    if (state.translate_lang_pair && (!cleanInit || !cleanInit.ocr || !cleanInit.ocr.lang_pair)) {
      state.ocr = Object.assign({}, state.ocr, { lang_pair: state.translate_lang_pair });
    }
    if (state.log_max_lines != null && (!cleanInit || !cleanInit.logs || cleanInit.logs.log_max_lines == null)) {
      state.logs = Object.assign({}, state.logs, { log_max_lines: state.log_max_lines });
    } else if (state.logs && state.logs.log_max_lines != null && state.log_max_lines == null) {
      state.log_max_lines = state.logs.log_max_lines;
    }

    var destroyed = false;

    function applyThemeAttr(themeName) {
      var normalized = themeName === "dark" ? "dark" : "light";
      if (typeof document !== "undefined" && document.documentElement) {
        document.documentElement.dataset.theme = normalized;
      }
      return normalized;
    }

    state.theme = applyThemeAttr(state.theme);
    V3.ensureIcons();
    V3.dom.empty(root);

    /* ---------- 左侧边栏 ---------- */
    var connDot = V3.dom.el("span", { className: "dot", "aria-hidden": "true" });
    var connText = V3.dom.el("span", { text: "未连接" });
    var setupNavDot = V3.dom.el("span", { className: "dot", "aria-hidden": "true", hidden: true });
    var navButtons = {};
    var navListEl = V3.dom.el("div", { role: "navigation", "aria-label": "工作台导航" });

    (V3.labels.HOME_NAVS || []).forEach(function (item) {
      var children = [
        V3.dom.el("span", { className: "nav-item-label" }, [
          V3.dom.icon(NAV_ICONS[item.id] || "globe"),
          document.createTextNode(item.label)
        ])
      ];
      if (item.id === "setup") {
        children.push(setupNavDot);
      }
      var btn = V3.dom.el("button", {
        type: "button",
        className: "nav-item",
        dataset: { panel: item.id },
        onClick: function () { setPanel(item.id); }
      }, children);
      navButtons[item.id] = btn;
      navListEl.appendChild(btn);
    });

    navListEl.addEventListener("keydown", function (ev) {
      var list = Array.prototype.slice.call(navListEl.querySelectorAll("button[data-panel]"));
      var idx = list.indexOf(document.activeElement);
      if (idx < 0) return;
      var next = idx;
      if (ev.key === "ArrowDown" || ev.key === "ArrowRight") next = (idx + 1) % list.length;
      else if (ev.key === "ArrowUp" || ev.key === "ArrowLeft") next = (idx - 1 + list.length) % list.length;
      else if (ev.key === "Home") next = 0;
      else if (ev.key === "End") next = list.length - 1;
      else return;
      ev.preventDefault();
      list[next].focus();
    });

    var themeIconWrap = V3.dom.el("span", { className: "nav-item-label" });
    var themeBadge = V3.dom.el("span", { className: "badge", text: "浅色" });
    var themeBtn = V3.dom.el("button", {
      type: "button",
      className: "nav-item",
      dataset: { action: "toggle-theme" },
      onClick: function () {
        var next = state.theme === "dark" ? "light" : "dark";
        state = V3.defaults.merge(state, { theme: applyThemeAttr(next) });
        renderRail();
        call(hooks, "onToggleTheme", { theme: next });
      }
    }, [themeIconWrap, themeBadge]);

    var railStatusList = V3.dom.el("div", { className: "rail-status-list" });
    var railKeysBox = V3.dom.el("div", { className: "rail-keys", "aria-label": "全局快捷键" });

    var railEl = V3.dom.el("aside", { className: "rail" }, [
      V3.dom.el("div", { className: "rail-head" }, [
        V3.dom.el("span", { className: "rail-brand" }, [
          V3.dom.icon("mic"),
          document.createTextNode("Voice Studio")
        ]),
        V3.dom.el("span", { className: "rail-conn" }, [connDot, connText])
      ]),
      navListEl,
      V3.dom.el("div", { className: "rail-sep", "aria-hidden": "true" }),
      themeBtn,
      V3.dom.el("div", { className: "rail-foot" }, [railStatusList, railKeysBox])
    ]);

    /* ---------- 主工作区头部 ---------- */
    var pageTitleEl = V3.dom.el("h1", { text: PANEL_META.translate.title });
    var pageSubEl = V3.dom.el("div", { className: "studio-sub", text: PANEL_META.translate.sub });
    var headActionsEl = V3.dom.el("div", { className: "studio-actions" });
    var topBannerEl = V3.dom.el("div", { className: "banner danger", hidden: true, role: "alert" });

    /* ---------- 1. Translate 面板（持久化编辑框，对齐模板 A 原地最小扩展） ---------- */
    var srcTextarea = V3.dom.el("textarea", {
      className: "box-editor",
      placeholder: "在此输入或粘贴需要翻译的文字，或直接按 Ctrl+V 粘贴剪贴板截图…",
      "aria-label": "原文输入或粘贴截图",
      dataset: { role: "translate-input" }
    });
    if (state.translate && state.translate.sourceText) {
      srcTextarea.value = state.translate.sourceText;
    }

    var srcAutoHint = V3.dom.el("span", {
      className: "auto-hint",
      dataset: { role: "translate-auto-hint" },
      text: "Ctrl+Enter 提交"
    });
    var srcMetaEl = V3.dom.el("span", {
      dataset: { role: "translate-src-meta" },
      text: "支持拖入图片或 Ctrl+V 粘贴截图 · 0 字"
    });
    var copySourceBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm",
      dataset: { role: "copy-source" },
      onClick: function () {
        var raw = srcTextarea.value || (state.translate && state.translate.ocr_text) || "";
        if (!raw) return;
        call(hooks, "onCopy", { text: raw, kind: "source" });
      }
    }, [V3.dom.icon("copy", "ic ic-sm"), document.createTextNode("复制原文")]);

    var translateDirBadge = V3.dom.el("span", {
      className: "badge",
      dataset: { role: "translate-direction-badge" },
      text: "自动判定"
    });
    var translateSwapLabel = V3.dom.el("span", { text: "互换" });
    var translateSwapBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm swap-btn",
      dataset: { role: "translate-swap" },
      "aria-pressed": "false",
      title: "手动反转并临时锁定翻译方向，再次点击或清空输入恢复自动",
      onClick: function () {
        var trimmedNow = String(srcTextarea.value || "").trim();
        if (!trimmedNow || (state.translate && state.translate.phase === "loading")) return;
        call(hooks, "onSwapDirection", {
          text: trimmedNow,
          lang_pair: getTranslateLangPair()
        });
      }
    }, [V3.dom.icon("swap", "ic ic-sm"), translateSwapLabel]);

    var translatePairSelect = V3.dom.el("select", {
      "aria-label": "翻译语言对",
      dataset: { role: "translate-lang-pair" },
      onChange: function (ev) {
        call(hooks, "onChangeLangPair", ev.target.value);
      }
    });

    function getTranslateLangPair() {
      return (state.translate && state.translate.lang_pair) || state.translate_lang_pair || "zh-en";
    }

    function submitTranslateText() {
      var text = String(srcTextarea.value || "").trim();
      var langPair = getTranslateLangPair();
      if (!text) {
        state = V3.defaults.merge(state, {
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "error",
            error: "请先输入或粘贴需要处理的文字，或按 Ctrl+V 粘贴截图。",
            message: "",
            copyStatus: null
          }
        });
        renderTranslatePanel(false);
        return;
      }
      state = V3.defaults.merge(state, {
        copyStatus: null,
        copyKind: null,
        translate: { sourceText: srcTextarea.value, copyStatus: null, error: null, message: "" }
      });
      renderTranslatePanel(false);
      call(hooks, "onTranslateText", {
        text: text,
        lang_pair: langPair,
        trigger: "manual"
      });
    }

    function submitTranslateImage(file) {
      if (!file) return;
      state = V3.defaults.merge(state, {
        copyStatus: null,
        copyKind: null,
        translate: { copyStatus: null, error: null, message: "" }
      });
      renderTranslatePanel(false);
      call(hooks, "onSubmitImage", {
        file: file,
        mode: "translate",
        lang_pair: getTranslateLangPair(),
        panel: "translate"
      });
    }

    srcTextarea.addEventListener("input", function () {
      var val = srcTextarea.value;
      state.translate = Object.assign({}, state.translate, { sourceText: val });
      renderTranslatePanel(false);
      call(hooks, "onTranslateInput", { text: val });
    });

    srcTextarea.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
        ev.preventDefault();
        submitTranslateText();
      }
    });

    var translateInputBox = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "原文输入 / 粘贴截图 OCR" }),
        srcAutoHint
      ]),
      srcTextarea,
      V3.dom.el("div", { className: "box-foot" }, [
        srcMetaEl,
        V3.dom.el("div", { className: "box-foot-right" }, [copySourceBtn])
      ])
    ]);

    translateInputBox.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      translateInputBox.classList.add("drag-over");
    });
    translateInputBox.addEventListener("dragleave", function (ev) {
      if (!translateInputBox.contains(ev.relatedTarget)) {
        translateInputBox.classList.remove("drag-over");
      }
    });
    translateInputBox.addEventListener("drop", function (ev) {
      ev.preventDefault();
      translateInputBox.classList.remove("drag-over");
      var img = V3.ocrPresets.extractImageFile(ev.dataTransfer);
      if (img) submitTranslateImage(img);
    });

    var translateOutBody = V3.dom.el("div", { className: "box-body", dataset: { role: "translate-output" } });
    var translateOutMeta = V3.dom.el("span", { className: "mono", dataset: { role: "translate-meta" } });
    var translateCopyFeedback = V3.dom.el("span", { className: "saved", hidden: true });
    var copyTranslationBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary btn-sm",
      dataset: { role: "copy-translation" },
      onClick: function () {
        var tr = state.translate || {};
        var mainText = tr.text || "";
        if (!mainText) return;
        call(hooks, "onCopy", { text: mainText, kind: "translation" });
      }
    }, [V3.dom.icon("copy", "ic ic-sm"), document.createTextNode("复制译文")]);

    var translateOutputBox = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "本地输出" }),
        V3.dom.el("div", { className: "box-head-controls" }, [
          translateDirBadge,
          translateSwapBtn,
          translatePairSelect
        ])
      ]),
      translateOutBody,
      V3.dom.el("div", { className: "box-foot" }, [
        translateOutMeta,
        V3.dom.el("div", { className: "box-foot-right" }, [
          translateCopyFeedback,
          copyTranslationBtn
        ])
      ])
    ]);

    var translatePanelEl = V3.dom.el("section", {
      className: "studio-panel",
      dataset: { panelView: "translate" }
    }, [
      V3.dom.el("div", { className: "studio-split" }, [translateInputBox, translateOutputBox])
    ]);

    var confirmDialog = typeof V3.mountConfirm === "function" ? V3.mountConfirm(root) : null;

    /* ---------- 2. OCR 面板（模板 A 独立纯 UI 模块：预览对照 / 5 参数调校 / 全局预设列表） ---------- */
    var ocrPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "ocr" }
    });
    var ocrPanel = V3.ocrPanel && typeof V3.ocrPanel.create === "function"
      ? V3.ocrPanel.create(ocrPanelEl, {
          getState: function () { return state; },
          patchState: function (patch) { setState(patch); },
          hooks: hooks,
          confirm: confirmDialog
        })
      : null;

    /* ---------- 3. History 面板 ---------- */
    var histRowsBox = V3.dom.el("div", { className: "rows", dataset: { role: "history-list" } });
    var histMoreWrap = V3.dom.el("div");
    var historyPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "history" }
    }, [
      histRowsBox,
      histMoreWrap,
      V3.dom.el("p", {
        className: "hint",
        text: "这里是本地处理记录，不表示文字已经输入到目标窗口。"
      })
    ]);

    /* ---------- 4. LAN 手机扫码面板 ---------- */
    var lanListBox = V3.dom.el("div", { className: "lan-list", dataset: { role: "lan-list" } });
    var lanPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "lan" }
    }, [
      lanListBox,
      V3.dom.el("p", {
        className: "hint",
        text: "为保护凭证安全，页面仅显示脱敏地址与二维码；点击「复制网址」时由业务层写出完整链接。"
      })
    ]);

    /* ---------- 5. Setup 模型与热键设置面板 ---------- */
    var setupPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "setup" }
    });
    var setupPanel = V3.setupPanel && typeof V3.setupPanel.create === "function"
      ? V3.setupPanel.create(setupPanelEl, {
          getState: function () { return state; },
          patchState: function (patch) { setState(patch); },
          hooks: hooks
        })
      : null;

    /* ---------- 6. Logs 运行日志面板 ---------- */
    var logsPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "logs" }
    });
    var logsPanel = V3.logsPanel && typeof V3.logsPanel.create === "function"
      ? V3.logsPanel.create(logsPanelEl, {
          getState: function () { return state; },
          hooks: hooks,
          confirm: confirmDialog
        })
      : null;

    /* ---------- 7. About 关于面板 ---------- */
    var aboutPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "about" }
    });
    var aboutPanel = V3.aboutPanel && typeof V3.aboutPanel.create === "function"
      ? V3.aboutPanel.create(aboutPanelEl, {
          getState: function () { return state; },
          hooks: hooks
        })
      : null;

    var mainEl = V3.dom.el("main", { className: "studio-main" }, [
      V3.dom.el("header", { className: "studio-head" }, [
        V3.dom.el("div", {}, [pageTitleEl, pageSubEl]),
        headActionsEl
      ]),
      topBannerEl,
      translatePanelEl,
      ocrPanelEl,
      historyPanelEl,
      lanPanelEl,
      setupPanelEl,
      logsPanelEl,
      aboutPanelEl
    ]);

    var studioWin = V3.dom.el("div", { className: "studio-window" }, [railEl, mainEl]);
    root.appendChild(studioWin);

    /* ---------- 全局粘贴截图监听（按当前面板分发） ---------- */
    function onDocumentPaste(ev) {
      if (!ev || !ev.clipboardData) return;
      var img = V3.ocrPresets.extractImageFile(ev.clipboardData);
      if (!img) return;
      if (state.activePanel === "translate") {
        ev.preventDefault();
        submitTranslateImage(img);
      } else if (state.activePanel === "ocr") {
        ev.preventDefault();
        if (ocrPanel && typeof ocrPanel.handleImageFile === "function") {
          ocrPanel.handleImageFile(img);
        }
      }
    }
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("paste", onDocumentPaste);
    }

    /* ---------- 各分区的局部渲染 ---------- */
    function renderRail() {
      var conn = state.connection;
      var asr = state.asr || {};
      var ollama = state.ollama || {};
      if (conn === "connected") {
        if (asrReady(asr) && ollamaReady(ollama)) {
          connDot.className = "dot ok";
          connText.textContent = "已就绪";
        } else {
          connDot.className = "dot warn";
          connText.textContent = "已连接";
        }
      } else if (conn === "loading") {
        connDot.className = "dot warn";
        connText.textContent = "连接中";
      } else if (conn === "error") {
        connDot.className = "dot err";
        connText.textContent = "连接异常";
      } else {
        connDot.className = "dot";
        connText.textContent = "未连接";
      }

      Object.keys(navButtons).forEach(function (id) {
        var active = state.activePanel === id;
        var btn = navButtons[id];
        btn.className = "nav-item" + (active ? " on" : "");
        if (active) btn.setAttribute("aria-current", "page");
        else btn.removeAttribute("aria-current");
      });

      var setupDotCls = V3.setupPanel && typeof V3.setupPanel.statusDotClass === "function"
        ? V3.setupPanel.statusDotClass(state)
        : "";
      if (setupDotCls) {
        setupNavDot.hidden = false;
        setupNavDot.className = "dot " + setupDotCls;
      } else {
        setupNavDot.hidden = true;
        setupNavDot.className = "dot";
      }

      var isDark = state.theme === "dark";
      V3.dom.empty(themeIconWrap);
      themeIconWrap.appendChild(V3.dom.icon(isDark ? "moon" : "sun"));
      themeIconWrap.appendChild(document.createTextNode("外观主题"));
      themeBadge.textContent = isDark ? "深色" : "浅色";

      V3.dom.empty(railStatusList);
      var asrDot = !isConnected(state) ? "" : (asrReady(asr) ? "ok" : (asr.status === "failed" ? "err" : "warn"));
      var asrLabel = !isConnected(state)
        ? V3.labels.FIXED_MODELS.asr + " 未读取"
        : (asrReady(asr) ? V3.labels.FIXED_MODELS.asr + " 就绪" : V3.labels.FIXED_MODELS.asr + " 未就绪");
      var olDot = !isConnected(state) ? "" : (ollamaReady(ollama) ? "ok" : (ollama.modelStatus === "failed" || ollama.running === false ? "err" : "warn"));
      var olLabel = !isConnected(state)
        ? V3.labels.FIXED_MODELS.ollama + " 未读取"
        : (ollamaReady(ollama) ? V3.labels.FIXED_MODELS.ollama + " 就绪" : V3.labels.FIXED_MODELS.ollama + " 未就绪");

      railStatusList.appendChild(V3.dom.el("div", { className: "rail-status-row" }, [
        V3.dom.el("span", { className: "dot" + (asrDot ? " " + asrDot : ""), "aria-hidden": "true" }),
        V3.dom.el("span", { className: "rail-status-text", text: asrLabel })
      ]));
      railStatusList.appendChild(V3.dom.el("div", { className: "rail-status-row" }, [
        V3.dom.el("span", { className: "dot" + (olDot ? " " + olDot : ""), "aria-hidden": "true" }),
        V3.dom.el("span", { className: "rail-status-text", text: olLabel })
      ]));

      V3.dom.empty(railKeysBox);
      HOTKEY_ORDER.forEach(function (keyName) {
        var rawVal = state[keyName] || "";
        var formatted = rawVal
          ? V3.labels.formatHotkey(rawVal)
          : (isConnected(state) ? "未设置" : "未读取");
        railKeysBox.appendChild(V3.dom.el("div", { className: "rail-key-row" }, [
          V3.dom.el("span", { text: V3.labels.hotkeyLabel(keyName) }),
          V3.dom.el("kbd", {
            className: "rail-key-kbd",
            dataset: { hotkeyBadge: keyName, rawHotkey: rawVal },
            text: formatted
          })
        ]));
      });
    }

    function renderHeaderAndBanner() {
      var panel = state.activePanel || "translate";
      var meta = PANEL_META[panel] || PANEL_META.translate;
      pageTitleEl.textContent = meta.title;
      if (panel === "setup" && V3.setupPanel && typeof V3.setupPanel.headerNotice === "function") {
        var notice = V3.setupPanel.headerNotice(state);
        pageSubEl.textContent = notice.text;
        pageSubEl.className = "studio-sub" + (notice.tone && notice.tone !== "muted" ? " tone-" + notice.tone : "");
      } else if (panel === "translate") {
        pageSubEl.className = "studio-sub";
        if (state.translate_auto_enabled === true) {
          pageSubEl.textContent = "停止输入 1 秒自动判定语言并翻译 · Ctrl+V 粘贴截图自动提取文字并翻译 · 本地离线运行";
        } else if (state.translate_auto_enabled === false) {
          pageSubEl.textContent = "自动翻译已关闭 · 按 Ctrl+Enter 或点击「立即翻译」触发 · Ctrl+V 粘贴截图自动识别";
        } else {
          pageSubEl.textContent = meta.sub;
        }
      } else {
        pageSubEl.className = "studio-sub";
        pageSubEl.textContent = meta.sub;
      }

      if (panel === "logs") {
        if (logsPanel && logsPanel.headActionsEl) {
          if (headActionsEl.firstChild !== logsPanel.headActionsEl || headActionsEl.childNodes.length !== 1) {
            V3.dom.empty(headActionsEl);
            headActionsEl.appendChild(logsPanel.headActionsEl);
          }
        } else {
          V3.dom.empty(headActionsEl);
        }
      } else if (panel === "ocr") {
        if (ocrPanel && ocrPanel.headActionsEl) {
          if (headActionsEl.firstChild !== ocrPanel.headActionsEl || headActionsEl.childNodes.length !== 1) {
            V3.dom.empty(headActionsEl);
            headActionsEl.appendChild(ocrPanel.headActionsEl);
          }
        } else {
          V3.dom.empty(headActionsEl);
        }
      } else {
        V3.dom.empty(headActionsEl);
        if (panel === "translate") {
          var trPhase = (state.translate && state.translate.phase) || "empty";
          var loading = trPhase === "loading";
          headActionsEl.appendChild(V3.dom.el("button", {
            type: "button",
            className: "btn",
            dataset: { role: "translate-clear" },
            onClick: function () {
              srcTextarea.value = "";
              state = V3.defaults.merge(state, {
                copyStatus: null,
                copyKind: null,
                translate: {
                  phase: "empty",
                  sourceText: "",
                  source_lang: null,
                  target_lang: null,
                  direction_mode: null,
                  chunks: null,
                  debouncePending: false,
                  manualOverride: null,
                  text: "",
                  ocr_text: "",
                  elapsedMs: null,
                  error: null,
                  message: "",
                  details: null,
                  copyStatus: null,
                  copyKind: null
                }
              });
              renderTranslatePanel(false);
              call(hooks, "onClearTranslate");
            }
          }, [V3.dom.icon("trash"), document.createTextNode("清空")]));
          headActionsEl.appendChild(V3.dom.el("button", {
            type: "button",
            className: (state.translate_auto_enabled === false && trPhase !== "result") ? "btn primary" : "btn",
            disabled: loading,
            dataset: { role: "translate-submit" },
            text: loading ? "处理中…" : "立即翻译 (Ctrl+Enter)",
            onClick: submitTranslateText
          }));
        } else if (panel === "history") {
          headActionsEl.appendChild(V3.dom.el("button", {
            type: "button",
            className: "btn",
            disabled: state.connection === "loading",
            onClick: function () {
              if (hooks && typeof hooks.onRefreshHistory === "function") call(hooks, "onRefreshHistory");
              else call(hooks, "onRefreshStatus");
            }
          }, [V3.dom.icon("refresh"), document.createTextNode("刷新记录")]));
        } else if (panel === "lan") {
          headActionsEl.appendChild(V3.dom.el("button", {
            type: "button",
            className: "btn",
            disabled: state.connection === "loading",
            onClick: function () {
              if (hooks && typeof hooks.onRefreshLan === "function") call(hooks, "onRefreshLan");
              else call(hooks, "onRefreshStatus");
            }
          }, [V3.dom.icon("refresh"), document.createTextNode("刷新入口")]));
        } else if (panel === "setup") {
          headActionsEl.appendChild(V3.dom.el("button", {
            type: "button",
            className: "btn",
            dataset: { role: "setup-refresh" },
            disabled: state.connection === "loading",
            onClick: function () { call(hooks, "onRefreshStatus"); }
          }, [V3.dom.icon("refresh"), document.createTextNode("刷新状态")]));
        }
      }

      if (state.connection === "error" || state.connectionError || state.error) {
        topBannerEl.hidden = false;
        topBannerEl.className = "banner danger";
        topBannerEl.textContent = V3.labels.formatUiError(
          state.error || state.connectionError,
          state.message,
          null,
          "无法连接本地服务，请确认后台服务已启动。"
        );
      } else if (state.statusNotice) {
        topBannerEl.hidden = false;
        topBannerEl.className = "banner warn";
        topBannerEl.textContent = state.statusNotice;
      } else {
        topBannerEl.hidden = true;
        topBannerEl.className = "banner danger";
      }
    }

    function getLangPairOptions() {
      return state.langPairOptions && state.langPairOptions.length
        ? state.langPairOptions
        : V3.labels.LANG_PAIR_OPTIONS;
    }

    function populateLangPairOptions(selectEl, selectedVal) {
      var opts = getLangPairOptions();
      V3.dom.empty(selectEl);
      opts.forEach(function (item) {
        var option = V3.dom.el("option", { value: item.value, text: item.label });
        if (item.value === selectedVal) option.selected = true;
        selectEl.appendChild(option);
      });
    }

    function renderTranslatePanel(syncInputText) {
      var tr = state.translate || {};
      var langPair = getTranslateLangPair();
      populateLangPairOptions(translatePairSelect, langPair);
      if (syncInputText && typeof tr.sourceText === "string" && document.activeElement !== srcTextarea) {
        srcTextarea.value = tr.sourceText;
      }

      var rawInput = srcTextarea.value || "";
      var trimmed = rawInput.trim();
      var phase = tr.phase || "empty";
      var loading = phase === "loading";

      srcTextarea.placeholder = state.translate_auto_enabled === true
        ? "在此输入或粘贴需要翻译的文字（停顿 1 秒后自动翻译），或按 Ctrl+V 粘贴截图…"
        : "在此输入或粘贴需要翻译的文字，或直接按 Ctrl+V 粘贴剪贴板截图…";
      srcMetaEl.textContent = "支持拖入图片或 Ctrl+V 粘贴截图 · " + trimmed.length + " 字";
      copySourceBtn.disabled = !(rawInput || tr.ocr_text);

      V3.dom.empty(srcAutoHint);
      if (tr.debouncePending === true) {
        srcAutoHint.className = "auto-hint pending";
        srcAutoHint.appendChild(V3.dom.el("span", { className: "auto-pulse", "aria-hidden": "true" }));
        srcAutoHint.appendChild(document.createTextNode("等待停顿 · 1 秒后自动翻译…"));
      } else if (state.translate_auto_enabled === true) {
        srcAutoHint.className = "auto-hint";
        srcAutoHint.textContent = "停顿 1s 自动翻译 · Ctrl+Enter 立即";
      } else if (state.translate_auto_enabled === false) {
        srcAutoHint.className = "auto-hint";
        srcAutoHint.textContent = "自动翻译已关闭 · Ctrl+Enter 提交";
      } else {
        srcAutoHint.className = "auto-hint";
        srcAutoHint.textContent = "Ctrl+Enter 提交";
      }

      var lock = tr.manualOverride && tr.manualOverride.source_lang && tr.manualOverride.target_lang
        ? tr.manualOverride
        : null;
      var isLocked = !!lock;
      translateSwapBtn.disabled = !trimmed || loading;
      translateSwapBtn.setAttribute("aria-pressed", String(isLocked));
      translateSwapLabel.textContent = isLocked ? "恢复自动" : "互换";

      V3.dom.empty(translateDirBadge);
      if (isLocked) {
        translateDirBadge.className = "badge warn";
        translateDirBadge.appendChild(V3.dom.icon("lock", "ic ic-xs"));
        translateDirBadge.appendChild(document.createTextNode("手动：" + V3.labels.directionLabel(lock.source_lang, lock.target_lang)));
      } else if (phase === "result" && tr.source_lang && tr.target_lang) {
        translateDirBadge.className = "badge accent";
        translateDirBadge.textContent = "自动：" + V3.labels.directionLabel(tr.source_lang, tr.target_lang);
      } else {
        translateDirBadge.className = "badge";
        translateDirBadge.textContent = "自动判定";
      }

      V3.dom.empty(translateOutBody);
      var mainText = "";

      if (phase === "loading") {
        var loadingHint = trimmed.length > 260
          ? "正在本地分段翻译（长文本不截断）…"
          : "正在本地判定方向并翻译…";
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "box-loading",
          role: "status",
          "aria-live": "polite"
        }, [
          document.createTextNode(loadingHint),
          V3.dom.el("div", { className: "progress indeterminate", "aria-hidden": "true" }, [V3.dom.el("i")])
        ]));
      } else if (phase === "error") {
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "box-error",
          role: "alert"
        }, [
          V3.dom.icon("alert"),
          V3.dom.el("span", {
            text: V3.labels.formatUiError(tr.error, tr.message, tr.details, "翻译或图片识别失败")
          })
        ]));
      } else if (phase === "result") {
        mainText = tr.text || "";
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "studio-result-text",
          dataset: { role: "translation-text" },
          text: mainText || "（无输出文本）"
        }));
        if (tr.ocr_text && tr.ocr_text !== mainText) {
          translateOutBody.appendChild(V3.dom.el("div", {
            className: "studio-sub-block",
            dataset: { role: "translate-ocr-origin" }
          }, [
            V3.dom.el("div", { className: "studio-sub-head" }, [
              V3.dom.el("span", { text: "OCR 识别原文" }),
              V3.dom.el("button", {
                type: "button",
                className: "linkbtn",
                onClick: function () {
                  call(hooks, "onCopy", { text: tr.ocr_text, kind: "translate_ocr" });
                }
              }, [document.createTextNode("复制原文")])
            ]),
            V3.dom.el("div", { className: "studio-sub-text", text: tr.ocr_text })
          ]));
        }
      } else if (tr.debouncePending === true) {
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "box-empty",
          text: "检测到输入变化，停止输入满 1 秒后将自动请求本地翻译。也可立即按 Ctrl+Enter 提交。"
        }));
      } else {
        var emptyHint = state.connection === "disconnected"
          ? "尚未连接本地服务。输入文字按 Ctrl+Enter 或粘贴图片即可触发处理。"
          : (state.translate_auto_enabled === false
              ? "自动翻译已关闭。在左侧输入文字后按 Ctrl+Enter 或点击「立即翻译」触发，或按 Ctrl+V 粘贴截图。"
              : "在左侧输入或粘贴文字，停止输入 1 秒后自动识别语言并翻译；也可按 Ctrl+V 粘贴截图。");
        translateOutBody.appendChild(V3.dom.el("div", { className: "box-empty", text: emptyHint }));
      }

      var metaBits = [];
      if (phase === "result") {
        metaBits.push(V3.labels.FIXED_MODELS.ollama);
        if (typeof tr.elapsedMs === "number" && isFinite(tr.elapsedMs)) {
          metaBits.push(Math.round(tr.elapsedMs) + "ms");
        }
        if (typeof tr.chunks === "number" && isFinite(tr.chunks) && tr.chunks > 1) {
          metaBits.push("共 " + Math.round(tr.chunks) + " 段");
        }
      }
      translateOutMeta.textContent = metaBits.join(" · ");

      var copiedStatus = phase === "result"
        ? (tr.copyStatus || ((state.copyKind === "translation" || state.copyKind === "translate_ocr" || state.copyKind === "source") ? state.copyStatus : ""))
        : "";
      if (copiedStatus) {
        translateCopyFeedback.hidden = false;
        translateCopyFeedback.textContent = copiedStatus;
      } else {
        translateCopyFeedback.hidden = true;
        translateCopyFeedback.textContent = "";
      }
      copyTranslationBtn.disabled = phase !== "result" || !mainText;
    }

    function renderOcrPanel() {
      if (ocrPanel) ocrPanel.render(state);
    }

    function renderHistoryPanel() {
      var hist = Object.assign({}, state.history || {});
      if (!isConnected(state) && !(hist.records && hist.records.length) && !hist.message) {
        hist.message = "尚未读取处理记录。";
      }
      V3.history.renderList(histRowsBox, hist, {
        emptyText: "还没有记录，按住语音快捷键说一句话试试",
        onCopy: function (text, record) {
          call(hooks, "onCopy", { kind: "history", text: text, record: record });
        }
      });
      V3.dom.empty(histMoreWrap);
      var shown = (hist.records || []).length;
      if (hist.total != null && shown < hist.total) {
        histMoreWrap.appendChild(V3.dom.el("button", {
          type: "button",
          className: "btn more",
          disabled: !isConnected(state) || !!hist.loading,
          text: "加载更多（" + shown + " / " + hist.total + "）",
          onClick: function () { call(hooks, "onLoadMoreHistory"); }
        }));
      }
    }

    function renderLanPanel() {
      V3.dom.empty(lanListBox);
      var entries = state.lanEntries;
      if (!isConnected(state) && (!entries || !entries.length)) {
        lanListBox.appendChild(V3.dom.el("div", {
          className: "studio-box empty",
          text: "尚未读取局域网入口。"
        }));
        return;
      }
      if (!entries || !entries.length) {
        lanListBox.appendChild(V3.dom.el("div", {
          className: "studio-box empty",
          text: state.lanMessage || "未检测到局域网 IP"
        }));
        return;
      }
      var steps = V3.dom.el("ol", {}, [
        V3.dom.el("li", { text: "手机与电脑连接同一个 Wi-Fi / 局域网" }),
        V3.dom.el("li", { text: "用手机扫码打开录音页（首次自签或本地根证书按提示信任继续）" }),
        V3.dom.el("li", { text: "按住麦克风按钮说话，松开后识别并输入到电脑当前光标位置" })
      ]);
      var lanCopied = state.copyKind === "lan_url" && state.copyStatus ? state.copyStatus : "";
      entries.forEach(function (entry, index) {
        var qr = V3.dom.el("div", { className: "qr", "aria-label": "手机语音入口二维码" });
        if (entry.qrSvg) {
          qr.innerHTML = entry.qrSvg;
        } else if (entry.qrDataUrl) {
          qr.appendChild(V3.dom.el("img", { src: entry.qrDataUrl, alt: "扫码打开手机语音页" }));
        } else {
          qr.appendChild(V3.dom.el("span", { className: "muted", style: "font-size:var(--fs-xs)", text: "二维码不可用" }));
        }
        /* 严禁明文 token 入 DOM：只展示脱敏后的 urlMasked。 */
        var maskedUrl = entry.urlMasked || entry.url_masked || "";
        lanListBox.appendChild(V3.dom.el("div", { className: "lan-card" }, [
          qr,
          V3.dom.el("div", { style: "flex:1;min-width:220px" }, [
            index === 0 ? steps : null,
            V3.dom.el("div", { className: "url", text: maskedUrl || "（未提供脱敏网址）" }),
            V3.dom.el("div", { style: "display:flex;align-items:center;gap:var(--sp-2)" }, [
              V3.dom.el("button", {
                type: "button",
                className: "btn",
                disabled: !maskedUrl,
                onClick: function () {
                  call(hooks, "onCopy", { kind: "lan_url", index: index });
                }
              }, [V3.dom.icon("copy"), document.createTextNode("复制网址")]),
              lanCopied ? V3.dom.el("span", { className: "saved", text: lanCopied }) : null
            ])
          ])
        ]));
      });
    }

    function renderSetupPanel() {
      if (setupPanel) setupPanel.render(state);
    }

    function renderLogsPanel() {
      if (logsPanel) logsPanel.render(state);
    }

    function renderAboutPanel() {
      if (aboutPanel) aboutPanel.render(state);
    }

    function updatePanelVisibility() {
      var cur = state.activePanel || "translate";
      translatePanelEl.hidden = cur !== "translate";
      ocrPanelEl.hidden = cur !== "ocr";
      historyPanelEl.hidden = cur !== "history";
      lanPanelEl.hidden = cur !== "lan";
      setupPanelEl.hidden = cur !== "setup";
      logsPanelEl.hidden = cur !== "logs";
      aboutPanelEl.hidden = cur !== "about";
    }

    function renderAll(syncInputText) {
      renderRail();
      renderHeaderAndBanner();
      updatePanelVisibility();
      renderTranslatePanel(syncInputText);
      renderOcrPanel();
      renderHistoryPanel();
      renderLanPanel();
      if (state.activePanel === "setup") {
        renderSetupPanel();
      } else if (state.activePanel === "logs") {
        renderLogsPanel();
      } else if (state.activePanel === "about") {
        renderAboutPanel();
      }
    }

    function setPanel(id) {
      if (!PANEL_META[id]) return;
      var changed = state.activePanel !== id;
      state = V3.defaults.merge(state, { activePanel: id });
      if (changed) {
        renderRail();
        renderHeaderAndBanner();
        updatePanelVisibility();
        if (id === "translate") renderTranslatePanel(false);
        else if (id === "ocr") renderOcrPanel();
        else if (id === "setup") renderSetupPanel();
        else if (id === "logs") renderLogsPanel();
        else if (id === "about") renderAboutPanel();
      }
      call(hooks, "onSelectPanel", { panel: id });
    }

    function setState(rawPatch) {
      if (!rawPatch) return;
      var patch = sanitizeLanPatch(rawPatch);
      if (patch.panel && !patch.activePanel) {
        patch = Object.assign({}, patch, { activePanel: patch.panel });
      }
      if (patch.tab && !patch.setupTab) {
        patch = Object.assign({}, patch, { setupTab: patch.tab });
      }
      var syncInput = !!(patch.translate && Object.prototype.hasOwnProperty.call(patch.translate, "sourceText"));
      state = V3.defaults.merge(state, patch);
      if (patch.theme) {
        state.theme = applyThemeAttr(patch.theme);
      }
      if (patch.translate_lang_pair) {
        if (!(patch.translate && patch.translate.lang_pair)) {
          state.translate = Object.assign({}, state.translate, { lang_pair: patch.translate_lang_pair });
        }
        if (!(patch.ocr && patch.ocr.lang_pair)) {
          state.ocr = Object.assign({}, state.ocr, { lang_pair: patch.translate_lang_pair });
        }
      }
      if (
        Object.prototype.hasOwnProperty.call(patch, "log_max_lines") &&
        !(patch.logs && Object.prototype.hasOwnProperty.call(patch.logs, "log_max_lines"))
      ) {
        state.logs = Object.assign({}, state.logs, { log_max_lines: patch.log_max_lines });
      } else if (
        patch.logs &&
        Object.prototype.hasOwnProperty.call(patch.logs, "log_max_lines") &&
        !Object.prototype.hasOwnProperty.call(patch, "log_max_lines")
      ) {
        state.log_max_lines = patch.logs.log_max_lines;
      }
      renderAll(syncInput);
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("paste", onDocumentPaste);
      }
      if (ocrPanel && typeof ocrPanel.destroy === "function") {
        ocrPanel.destroy();
      }
      if (setupPanel && typeof setupPanel.destroy === "function") {
        setupPanel.destroy();
      }
      if (logsPanel && typeof logsPanel.destroy === "function") {
        logsPanel.destroy();
      }
      if (aboutPanel && typeof aboutPanel.destroy === "function") {
        aboutPanel.destroy();
      }
      if (confirmDialog && typeof confirmDialog.destroy === "function") {
        confirmDialog.destroy();
      }
      if (root && root.__v3HomeCleanup === destroy) {
        delete root.__v3HomeCleanup;
      }
      V3.dom.empty(root);
    }

    if (root) {
      root.__v3HomeCleanup = destroy;
    }

    renderAll(true);

    return {
      setState: setState,
      getState: function () { return state; },
      setPanel: setPanel,
      getPanel: function () { return state.activePanel; },
      setSetupTab: function (id) {
        if (setupPanel) setupPanel.setTab(id);
      },
      getSetupTab: function () {
        return setupPanel ? setupPanel.getTab() : (state.setupTab || "models");
      },
      destroy: destroy
    };
  }

  V3.home = {
    mount: mountHome
  };
  V3.mountHome = mountHome;
})(window.V3 = window.V3 || {});

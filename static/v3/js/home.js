/* 方案 C 侧栏效率工作站主页纯 UI（翻译框 / 图片 OCR / 历史 / 手机扫码 / 模型与热键设置）。
 * 严守纯展示边界：不发起网络请求、不读写剪贴板或存储、不拼接口路径，所有动作经 hooks 回调通知业务层。
 */
(function (V3) {
  "use strict";

  var NAV_ICONS = {
    translate: "globe",
    ocr: "scan",
    history: "clock",
    lan: "phone",
    setup: "settings"
  };

  var PANEL_META = {
    translate: {
      title: "本地翻译与 OCR 工作台",
      sub: "手动粘贴文本即译 · Ctrl+V 粘贴截图自动提取文字并翻译 · 本地离线运行"
    },
    ocr: {
      title: "图片 OCR 识别提取",
      sub: "拖入图片、选择文件或按 Ctrl+V 粘贴截图 · 可选纯提取文字或提取并翻译"
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

  function formatUiError(errCodeOrText, message, details, fallback) {
    if (typeof errCodeOrText === "string" && errCodeOrText) {
      if (Object.prototype.hasOwnProperty.call(V3.labels.ERROR_CODE, errCodeOrText)) {
        return V3.labels.formatError(errCodeOrText, message, details);
      }
      return (message && String(message) !== errCodeOrText)
        ? errCodeOrText + "：" + String(message)
        : errCodeOrText;
    }
    if (errCodeOrText && typeof errCodeOrText === "object") {
      var innerErr = errCodeOrText.error;
      var innerMsg = errCodeOrText.message != null && errCodeOrText.message !== "" ? errCodeOrText.message : message;
      var innerDetails = errCodeOrText.details || details;
      if (typeof innerErr === "string" && innerErr) {
        if (Object.prototype.hasOwnProperty.call(V3.labels.ERROR_CODE, innerErr)) {
          return V3.labels.formatError(innerErr, innerMsg, innerDetails);
        }
        return (innerMsg && String(innerMsg) !== innerErr)
          ? innerErr + "：" + String(innerMsg)
          : innerErr;
      }
      if (innerMsg) return V3.labels.formatError(null, innerMsg, innerDetails);
    }
    if (message) return String(message);
    return fallback || "发生错误";
  }

  function extractImageFile(dataTransfer) {
    if (!dataTransfer) return null;
    var items = dataTransfer.items;
    if (items && items.length) {
      for (var i = 0; i < items.length; i++) {
        var item = items[i];
        if (item && item.kind === "file" && String(item.type || "").indexOf("image/") === 0 && typeof item.getAsFile === "function") {
          var f = item.getAsFile();
          if (f) return f;
        }
      }
    }
    var files = dataTransfer.files;
    if (files && files.length) {
      for (var j = 0; j < files.length; j++) {
        if (files[j] && String(files[j].type || "").indexOf("image/") === 0) {
          return files[j];
        }
      }
    }
    return null;
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
    if (state.translate_target_lang && (!cleanInit || !cleanInit.translate || !cleanInit.translate.target_lang)) {
      state.translate = Object.assign({}, state.translate, { target_lang: state.translate_target_lang });
    }

    var userPickedTranslateLang = !!(cleanInit && cleanInit.translate && cleanInit.translate.target_lang);
    var ownedPreviewUrl = null;
    var currentOcrFile = null;
    var destroyed = false;

    function releasePreviewUrl() {
      if (ownedPreviewUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
        try { URL.revokeObjectURL(ownedPreviewUrl); } catch (err) {}
      }
      ownedPreviewUrl = null;
    }

    function applyThemeAttr(themeName) {
      var normalized = themeName === "light" ? "light" : "dark";
      if (typeof document !== "undefined" && document.documentElement) {
        document.documentElement.dataset.theme = normalized;
      }
      return normalized;
    }

    applyThemeAttr(state.theme);
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
    var themeBadge = V3.dom.el("span", { className: "badge", text: "深色" });
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

    /* ---------- 1. Translate 面板（持久化编辑框） ---------- */
    var srcTextarea = V3.dom.el("textarea", {
      className: "box-editor",
      placeholder: "在此输入或粘贴需要翻译的文字，或直接按 Ctrl+V 粘贴剪贴板截图…",
      "aria-label": "原文输入或粘贴截图",
      dataset: { role: "translate-input" }
    });
    if (state.translate && state.translate.sourceText) {
      srcTextarea.value = state.translate.sourceText;
    }

    var translateTargetSelect = V3.dom.el("select", {
      "aria-label": "目标语言",
      dataset: { role: "translate-target-lang" },
      onChange: function (ev) {
        userPickedTranslateLang = true;
        var val = ev.target.value;
        state = V3.defaults.merge(state, { translate: { target_lang: val } });
        renderTranslatePanel(false);
      }
    });

    function getTranslateTargetLang() {
      return (state.translate && state.translate.target_lang) || state.translate_target_lang || "zh";
    }

    function submitTranslateText() {
      var text = String(srcTextarea.value || "").trim();
      var targetLang = getTranslateTargetLang();
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
      call(hooks, "onTranslateText", { text: text, target_lang: targetLang });
    }

    function submitTranslateImage(file) {
      if (!file) return;
      var targetLang = getTranslateTargetLang();
      state = V3.defaults.merge(state, {
        copyStatus: null,
        copyKind: null,
        translate: { copyStatus: null, error: null, message: "" }
      });
      renderTranslatePanel(false);
      call(hooks, "onSubmitImage", {
        file: file,
        target_lang: targetLang,
        mode: "translate"
      });
    }

    srcTextarea.addEventListener("input", function () {
      state.translate = Object.assign({}, state.translate, { sourceText: srcTextarea.value });
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
        V3.dom.el("span", { className: "mono", text: "Ctrl+Enter 提交" })
      ]),
      srcTextarea,
      V3.dom.el("div", { className: "box-foot" }, [
        V3.dom.el("span", { text: "支持拖入图片或 Ctrl+V 粘贴剪贴板图片" }),
        V3.dom.el("div", { className: "box-foot-right" }, [
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "copy-source" },
            onClick: function () {
              var raw = srcTextarea.value || (state.translate && state.translate.ocr_text) || "";
              if (!raw) return;
              call(hooks, "onCopy", { text: raw, kind: "source" });
            }
          }, [V3.dom.icon("copy"), document.createTextNode("复制原文")])
        ])
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
      var img = extractImageFile(ev.dataTransfer);
      if (img) submitTranslateImage(img);
    });

    var translateOutBody = V3.dom.el("div", { className: "box-body", dataset: { role: "translate-output" } });
    var translateOutMeta = V3.dom.el("span", { className: "mono" });
    var translateCopyFeedback = V3.dom.el("span", { className: "saved", hidden: true });
    var copyTranslationBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary btn-sm",
      dataset: { role: "copy-translation" },
      onClick: function () {
        var tr = state.translate || {};
        var mainText = tr.text || (tr.target_lang === "raw" ? tr.ocr_text : "") || "";
        if (!mainText) return;
        call(hooks, "onCopy", { text: mainText, kind: "translation" });
      }
    }, [V3.dom.icon("copy"), document.createTextNode("复制译文")]);

    var translateOutputBox = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "本地输出" }),
        translateTargetSelect
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

    /* ---------- 2. OCR 面板（拖拽 / 选择文件 / Ctrl+V + 预览 URL 回收） ---------- */
    var ocrTargetSelect = V3.dom.el("select", {
      "aria-label": "OCR 处理方式",
      dataset: { role: "ocr-target-lang" },
      onChange: function (ev) {
        var val = ev.target.value;
        state = V3.defaults.merge(state, {
          ocr: {
            target_lang: val,
            mode: val === "raw" ? "raw" : "translate"
          }
        });
        renderOcrPanel();
      }
    });

    var ocrFileInput = V3.dom.el("input", {
      type: "file",
      accept: "image/*",
      hidden: true,
      "aria-label": "选择要识别的图片文件",
      dataset: { role: "ocr-file-input" }
    });

    function getOcrTargetLang() {
      return (state.ocr && state.ocr.target_lang) || "raw";
    }

    function handleOcrFile(file) {
      if (!file) return;
      currentOcrFile = file;
      releasePreviewUrl();
      var preview = "";
      if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
        try {
          ownedPreviewUrl = URL.createObjectURL(file);
          preview = ownedPreviewUrl;
        } catch (err) {
          ownedPreviewUrl = null;
        }
      }
      var targetLang = getOcrTargetLang();
      state = V3.defaults.merge(state, {
        copyStatus: null,
        copyKind: null,
        ocr: {
          fileName: file.name || "clipboard-image.png",
          previewUrl: preview,
          copyStatus: null,
          error: null,
          message: ""
        }
      });
      renderOcrPanel();
      call(hooks, "onSubmitImage", {
        file: file,
        target_lang: targetLang,
        mode: targetLang === "raw" ? "ocr" : "translate"
      });
    }

    ocrFileInput.addEventListener("change", function () {
      var file = ocrFileInput.files && ocrFileInput.files[0];
      if (file) handleOcrFile(file);
      ocrFileInput.value = "";
    });

    var ocrDropzone = V3.dom.el("div", {
      className: "ocr-dropzone",
      tabindex: "0",
      role: "button",
      "aria-label": "点击选择图片，或拖入图片、按 Ctrl+V 粘贴截图",
      onClick: function () { ocrFileInput.click(); },
      onKeyDown: function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          ocrFileInput.click();
        }
      }
    }, [
      V3.dom.icon("image"),
      V3.dom.el("div", { style: "font-weight:600;color:var(--c-text)", text: "点击选择图片 · 拖拽图片到此 · Ctrl+V 粘贴截图" }),
      V3.dom.el("div", { className: "muted", style: "font-size:var(--fs-xs)", text: "支持 PNG / JPEG / WebP 等常见截图格式，本地离线识别" })
    ]);

    ocrDropzone.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      ocrDropzone.classList.add("drag-over");
    });
    ocrDropzone.addEventListener("dragleave", function (ev) {
      if (!ocrDropzone.contains(ev.relatedTarget)) {
        ocrDropzone.classList.remove("drag-over");
      }
    });
    ocrDropzone.addEventListener("drop", function (ev) {
      ev.preventDefault();
      ocrDropzone.classList.remove("drag-over");
      var img = extractImageFile(ev.dataTransfer);
      if (img) handleOcrFile(img);
    });

    var ocrPreviewWrap = V3.dom.el("div", { className: "ocr-preview-wrap", hidden: true });
    var ocrPreviewImg = V3.dom.el("img", { className: "ocr-preview-img", alt: "待识别图片预览" });
    var ocrFileLabel = V3.dom.el("span", { className: "mono muted" });
    ocrPreviewWrap.appendChild(ocrPreviewImg);

    var ocrLeftBox = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "图片输入" }),
        ocrTargetSelect
      ]),
      ocrFileInput,
      ocrPreviewWrap,
      ocrDropzone,
      V3.dom.el("div", { className: "box-foot" }, [
        ocrFileLabel,
        V3.dom.el("div", { className: "box-foot-right" }, [
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            onClick: function () { ocrFileInput.click(); }
          }, [V3.dom.icon("upload"), document.createTextNode("选择图片")]),
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "ocr-clear" },
            onClick: function () {
              currentOcrFile = null;
              releasePreviewUrl();
              state = V3.defaults.merge(state, {
                copyStatus: null,
                copyKind: null,
                ocr: {
                  phase: "empty",
                  fileName: "",
                  previewUrl: "",
                  ocr_text: "",
                  text: "",
                  error: null,
                  message: "",
                  copyStatus: null
                }
              });
              renderOcrPanel();
            }
          }, [V3.dom.icon("trash"), document.createTextNode("清除")])
        ])
      ])
    ]);

    var ocrOutBody = V3.dom.el("div", { className: "box-body", dataset: { role: "ocr-output" } });
    var ocrCopyFeedback = V3.dom.el("span", { className: "saved", hidden: true });
    var copyOcrRawBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary btn-sm",
      dataset: { role: "copy-ocr" },
      onClick: function () {
        var oc = state.ocr || {};
        var rawText = oc.ocr_text || (oc.target_lang === "raw" ? oc.text : "") || "";
        if (!rawText) return;
        call(hooks, "onCopy", { text: rawText, kind: "ocr" });
      }
    }, [V3.dom.icon("copy"), document.createTextNode("复制 OCR 原文")]);

    var copyOcrTransBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm",
      hidden: true,
      dataset: { role: "copy-ocr-translation" },
      onClick: function () {
        var oc = state.ocr || {};
        if (!oc.text) return;
        call(hooks, "onCopy", { text: oc.text, kind: "ocr_translation" });
      }
    }, [V3.dom.icon("copy"), document.createTextNode("复制译文")]);

    var ocrRightBox = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "识别输出" }),
        V3.dom.el("span", { className: "mono", text: "Tesseract + 本地模型" })
      ]),
      ocrOutBody,
      V3.dom.el("div", { className: "box-foot" }, [
        V3.dom.el("span", { text: "识别结果可直接复制" }),
        V3.dom.el("div", { className: "box-foot-right" }, [
          ocrCopyFeedback,
          copyOcrTransBtn,
          copyOcrRawBtn
        ])
      ])
    ]);

    var ocrPanelEl = V3.dom.el("section", {
      className: "studio-panel",
      hidden: true,
      dataset: { panelView: "ocr" }
    }, [
      V3.dom.el("div", { className: "studio-split" }, [ocrLeftBox, ocrRightBox])
    ]);

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
      setupPanelEl
    ]);

    var studioWin = V3.dom.el("div", { className: "studio-window" }, [railEl, mainEl]);
    root.appendChild(studioWin);

    /* ---------- 全局粘贴截图监听（按当前面板分发） ---------- */
    function onDocumentPaste(ev) {
      if (!ev || !ev.clipboardData) return;
      var img = extractImageFile(ev.clipboardData);
      if (!img) return;
      if (state.activePanel === "translate") {
        ev.preventDefault();
        submitTranslateImage(img);
      } else if (state.activePanel === "ocr") {
        ev.preventDefault();
        handleOcrFile(img);
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

      var isDark = state.theme !== "light";
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
      } else {
        pageSubEl.className = "studio-sub";
        pageSubEl.textContent = meta.sub;
      }

      V3.dom.empty(headActionsEl);
      if (panel === "translate") {
        var loading = state.translate && state.translate.phase === "loading";
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
                text: "",
                ocr_text: "",
                error: null,
                message: "",
                copyStatus: null
              }
            });
            renderTranslatePanel(false);
          }
        }, [V3.dom.icon("trash"), document.createTextNode("清空")]));
        headActionsEl.appendChild(V3.dom.el("button", {
          type: "button",
          className: "btn primary",
          disabled: loading,
          dataset: { role: "translate-submit" },
          text: loading ? "处理中…" : "立即翻译 (Ctrl+Enter)",
          onClick: submitTranslateText
        }));
      } else if (panel === "ocr") {
        var ocrLoading = state.ocr && state.ocr.phase === "loading";
        headActionsEl.appendChild(V3.dom.el("button", {
          type: "button",
          className: "btn",
          disabled: ocrLoading,
          onClick: function () { ocrFileInput.click(); }
        }, [V3.dom.icon("upload"), document.createTextNode("选择图片")]));
        headActionsEl.appendChild(V3.dom.el("button", {
          type: "button",
          className: "btn primary",
          disabled: ocrLoading || !currentOcrFile,
          dataset: { role: "ocr-resubmit" },
          text: ocrLoading ? "识别中…" : "开始识别",
          onClick: function () {
            if (!currentOcrFile) return;
            var targetLang = getOcrTargetLang();
            state = V3.defaults.merge(state, {
              copyStatus: null,
              copyKind: null,
              ocr: { copyStatus: null, error: null, message: "" }
            });
            renderOcrPanel();
            call(hooks, "onSubmitImage", {
              file: currentOcrFile,
              target_lang: targetLang,
              mode: targetLang === "raw" ? "ocr" : "translate"
            });
          }
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

      if (state.connection === "error" || state.connectionError || state.error) {
        topBannerEl.hidden = false;
        topBannerEl.className = "banner danger";
        topBannerEl.textContent = formatUiError(
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

    function populateTargetOptions(selectEl, selectedVal, isOcrMode) {
      var opts = isOcrMode
        ? [
            { value: "raw", label: "纯提取文字（不翻译）" },
            { value: "zh", label: "提取并译为：中文" },
            { value: "en", label: "提取并译为：英文" },
            { value: "ja", label: "提取并译为：日文" },
            { value: "ko", label: "提取并译为：韩文" }
          ]
        : (state.targetLangOptions && state.targetLangOptions.length
            ? state.targetLangOptions
            : [
                { value: "zh", label: "译为：中文" },
                { value: "en", label: "译为：英文" },
                { value: "ja", label: "译为：日文" },
                { value: "ko", label: "译为：韩文" },
                { value: "raw", label: "仅识别文字（不翻译）" }
              ]);
      V3.dom.empty(selectEl);
      opts.forEach(function (item) {
        var option = V3.dom.el("option", { value: item.value, text: item.label });
        if (item.value === selectedVal) option.selected = true;
        selectEl.appendChild(option);
      });
    }

    function renderTranslatePanel(syncInputText) {
      var tr = state.translate || {};
      var targetLang = getTranslateTargetLang();
      populateTargetOptions(translateTargetSelect, targetLang, false);
      if (syncInputText && typeof tr.sourceText === "string" && document.activeElement !== srcTextarea) {
        srcTextarea.value = tr.sourceText;
      }

      V3.dom.empty(translateOutBody);
      var phase = tr.phase || "empty";
      var mainText = "";

      if (phase === "loading") {
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "box-loading",
          role: "status",
          "aria-live": "polite"
        }, [
          document.createTextNode("正在本地处理…"),
          V3.dom.el("div", { className: "progress", "aria-hidden": "true" }, [V3.dom.el("i")])
        ]));
      } else if (phase === "error") {
        translateOutBody.appendChild(V3.dom.el("div", {
          className: "box-error",
          role: "alert"
        }, [
          V3.dom.icon("alert"),
          V3.dom.el("span", {
            text: formatUiError(tr.error, tr.message, tr.details, "翻译或图片识别失败")
          })
        ]));
      } else if (phase === "result") {
        mainText = tr.text || (targetLang === "raw" ? tr.ocr_text : "") || "";
        if (targetLang === "raw" || (!tr.text && tr.ocr_text)) {
          translateOutBody.appendChild(V3.dom.el("span", {
            className: "badge accent",
            style: "align-self:flex-start",
            text: "识别文字（未翻译）"
          }));
        }
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
      } else {
        var emptyHint = state.connection === "disconnected"
          ? "尚未连接本地服务。输入文字按 Ctrl+Enter 或粘贴图片即可触发处理。"
          : "在左侧输入文字按 Ctrl+Enter 翻译，或按 Ctrl+V 粘贴截图自动提取识别。";
        translateOutBody.appendChild(V3.dom.el("div", { className: "box-empty", text: emptyHint }));
      }

      var metaBits = [];
      if (phase === "result") {
        metaBits.push(V3.labels.FIXED_MODELS.ollama);
        if (typeof tr.elapsedMs === "number" && isFinite(tr.elapsedMs)) {
          metaBits.push(Math.round(tr.elapsedMs) + "ms");
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
      var oc = state.ocr || {};
      var targetLang = getOcrTargetLang();
      populateTargetOptions(ocrTargetSelect, targetLang, true);

      if (oc.previewUrl) {
        ocrPreviewWrap.hidden = false;
        if (ocrPreviewImg.getAttribute("src") !== oc.previewUrl) {
          ocrPreviewImg.setAttribute("src", oc.previewUrl);
        }
      } else {
        ocrPreviewWrap.hidden = true;
        ocrPreviewImg.removeAttribute("src");
      }
      ocrFileLabel.textContent = oc.fileName || "未选择图片";

      V3.dom.empty(ocrOutBody);
      var phase = oc.phase || "empty";
      var rawOcr = "";
      var transText = "";

      if (phase === "loading") {
        ocrOutBody.appendChild(V3.dom.el("div", {
          className: "box-loading",
          role: "status",
          "aria-live": "polite"
        }, [
          document.createTextNode("正在识别图片文字…"),
          V3.dom.el("div", { className: "progress", "aria-hidden": "true" }, [V3.dom.el("i")])
        ]));
      } else if (phase === "error") {
        ocrOutBody.appendChild(V3.dom.el("div", {
          className: "box-error",
          role: "alert"
        }, [
          V3.dom.icon("alert"),
          V3.dom.el("span", {
            text: formatUiError(oc.error, oc.message, oc.details, "图片 OCR 识别失败")
          })
        ]));
      } else if (phase === "result") {
        rawOcr = oc.ocr_text || (targetLang === "raw" ? oc.text : "") || "";
        transText = targetLang !== "raw" && oc.text ? oc.text : "";

        ocrOutBody.appendChild(V3.dom.el("div", {}, [
          V3.dom.el("div", { className: "muted", style: "font-size:var(--fs-xs);margin-bottom:4px", text: "OCR 识别原文" }),
          V3.dom.el("div", {
            className: "studio-result-text",
            dataset: { role: "ocr-raw-text" },
            text: rawOcr || "（未识别到文字）"
          })
        ]));

        if (transText) {
          ocrOutBody.appendChild(V3.dom.el("div", {
            className: "studio-sub-block",
            dataset: { role: "ocr-translated-block" }
          }, [
            V3.dom.el("div", { className: "studio-sub-head" }, [
              V3.dom.el("span", { text: "译文（" + V3.labels.targetLang(targetLang) + "）" })
            ]),
            V3.dom.el("div", { className: "studio-sub-text", text: transText })
          ]));
        }
      } else {
        ocrOutBody.appendChild(V3.dom.el("div", {
          className: "box-empty",
          text: "拖入图片、点击选择文件或直接按 Ctrl+V 粘贴截图开始提取文字。"
        }));
      }

      var ocrCopied = phase === "result"
        ? (oc.copyStatus || ((state.copyKind === "ocr" || state.copyKind === "ocr_translation") ? state.copyStatus : ""))
        : "";
      if (ocrCopied) {
        ocrCopyFeedback.hidden = false;
        ocrCopyFeedback.textContent = ocrCopied;
      } else {
        ocrCopyFeedback.hidden = true;
        ocrCopyFeedback.textContent = "";
      }
      copyOcrRawBtn.disabled = phase !== "result" || !rawOcr;
      copyOcrTransBtn.hidden = !transText;
      copyOcrTransBtn.disabled = phase !== "result" || !transText;
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

    function updatePanelVisibility() {
      var cur = state.activePanel || "translate";
      translatePanelEl.hidden = cur !== "translate";
      ocrPanelEl.hidden = cur !== "ocr";
      historyPanelEl.hidden = cur !== "history";
      lanPanelEl.hidden = cur !== "lan";
      setupPanelEl.hidden = cur !== "setup";
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
      if (
        patch.ocr &&
        Object.prototype.hasOwnProperty.call(patch.ocr, "previewUrl") &&
        patch.ocr.previewUrl !== ownedPreviewUrl
      ) {
        releasePreviewUrl();
      }
      var syncInput = !!(patch.translate && Object.prototype.hasOwnProperty.call(patch.translate, "sourceText"));
      state = V3.defaults.merge(state, patch);
      if (patch.theme) {
        state.theme = applyThemeAttr(patch.theme);
      }
      if (patch.translate_target_lang && !userPickedTranslateLang && !(patch.translate && patch.translate.target_lang)) {
        state.translate = Object.assign({}, state.translate, { target_lang: patch.translate_target_lang });
      }
      renderAll(syncInput);
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("paste", onDocumentPaste);
      }
      if (setupPanel && typeof setupPanel.destroy === "function") {
        setupPanel.destroy();
      }
      currentOcrFile = null;
      releasePreviewUrl();
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

/* 图片 OCR 识别与预处理调参纯 UI 面板（模板 A 上半区 + 组合 ocr-presets.js 下半区）。
 * 严守纯展示边界：零 innerHTML、不发起网络请求、不读写剪贴板或存储、不复制起步模板默认参数，所有动作经 hooks 回调通知业务层。
 */
(function (V3) {
  "use strict";

  var BINARIZE_OPTIONS = ["off", "otsu", "manual"];
  var VIEW_MODES = [{ id: "processed", label: "处理后" }, { id: "original", label: "原图" }, { id: "split", label: "左右对照" }];
  var RANGE_SPECS = [
    { key: "contrast", label: "对比度", hint: "0.5 ~ 3.0", min: "0.5", max: "3.0", step: "0.05", digits: 2, def: "1.0" },
    { key: "brightness", label: "亮度", hint: "0.5 ~ 2.0", min: "0.5", max: "2.0", step: "0.05", digits: 2, def: "1.0" },
    { key: "sharpen", label: "锐化", hint: "0.0 ~ 3.0", min: "0.0", max: "3.0", step: "0.1", digits: 1, def: "0.0" }
  ];

  function call(hooks, name, payload) {
    var fn = hooks && hooks[name];
    if (typeof fn !== "function") return undefined;
    var res = fn(payload);
    if (res && typeof res.then === "function") res.then(undefined, function () {});
    return res;
  }

  function getOcrState(st) { return (st && st.ocr) || {}; }
  function getPresetsList(st) { return (st && Array.isArray(st.ocr_preprocess_presets)) ? st.ocr_preprocess_presets : []; }
  function getStarterTemplates(st) { return (st && Array.isArray(st.ocrStarterTemplates)) ? st.ocrStarterTemplates : []; }
  function getOcrMode(st) { return getOcrState(st).mode === "translate" ? "translate" : "raw"; }
  function getOcrLangPair(st) { return getOcrState(st).lang_pair || (st && st.translate_lang_pair) || "zh-en"; }
  function getLangPairOptions(st) {
    return (st && st.langPairOptions && st.langPairOptions.length)
      ? st.langPairOptions
      : (V3.labels && V3.labels.LANG_PAIR_OPTIONS ? V3.labels.LANG_PAIR_OPTIONS : [{ value: "zh-en", label: "中 ⇄ 英" }]);
  }

  function syncImgSrc(imgEl, url) {
    if (url) {
      imgEl.hidden = false;
      if (imgEl.getAttribute("src") !== url) imgEl.setAttribute("src", url);
    } else {
      imgEl.hidden = true;
      imgEl.removeAttribute("src");
    }
  }

  function paramRow(title, hint, controls, valEl, forId, extraAttrs) {
    var labelTag = forId ? "label" : "div";
    var labelAttrs = forId ? { className: "param-name", "for": forId } : { className: "param-name" };
    return V3.dom.el("div", Object.assign({ className: "param-row" }, extraAttrs || {}), [
      V3.dom.el(labelTag, labelAttrs, [V3.dom.el("span", { text: title }), V3.dom.el("small", { text: hint })]),
      V3.dom.el("div", { className: "param-control" }, controls),
      valEl
    ]);
  }

  function emptyOcrResetPatch(extra) {
    return Object.assign({
      phase: "empty", fileName: "", previewUrl: "", processedPreviewUrl: "",
      previewPhase: "idle", previewErrorCode: null, previewError: "",
      imageWidth: null, imageHeight: null, previewWidth: null, previewHeight: null, otsuThreshold: null,
      source_lang: null, target_lang: null, direction_mode: null,
      ocr_text: "", text: "", ocr_meta: null, lastRunKind: null,
      error: null, message: "", details: null, copyStatus: null, copyKind: null
    }, extra || {});
  }

  /* ---------- 1. 持久顶栏按钮（挂载于 home.js headActionsEl，模板 A 单一主按钮） ---------- */
  function createHeadActions(cb) {
    var fileInput = V3.dom.el("input", {
      type: "file", accept: "image/*", hidden: true,
      "aria-label": "选择要识别的图片文件", dataset: { role: "ocr-file-input" }
    });
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (file) cb.onFilePicked(file);
      fileInput.value = "";
    });
    var pickBtn = V3.dom.el("button", {
      type: "button", className: "btn", dataset: { role: "ocr-head-pick" },
      onClick: function () { fileInput.click(); }
    }, [V3.dom.icon("upload", "ic ic-sm"), document.createTextNode("选择图片")]);
    var runChainBtn = V3.dom.el("button", {
      type: "button", className: "btn", disabled: true, dataset: { role: "ocr-run-chain" },
      onClick: function () { cb.onSubmit("chain"); }
    }, [V3.dom.icon("scan", "ic ic-sm"), document.createTextNode("按启用预设识别")]);
    var runDraftLabel = document.createTextNode("用当前参数试识别");
    var runDraftBtn = V3.dom.el("button", {
      type: "button", className: "btn primary", disabled: true, dataset: { role: "ocr-run-draft" },
      onClick: function () { cb.onSubmit("draft"); }
    }, [V3.dom.icon("play", "ic ic-sm"), runDraftLabel]);
    var el = V3.dom.el("div", { className: "studio-actions", dataset: { role: "ocr-head-actions" } }, [pickBtn, runChainBtn, runDraftBtn]);
    return {
      el: el,
      fileInput: fileInput,
      triggerPick: function () { fileInput.click(); },
      sync: function (hasFile, ocrLoading) {
        pickBtn.disabled = ocrLoading;
        runChainBtn.disabled = !hasFile || ocrLoading;
        runDraftBtn.disabled = !hasFile || ocrLoading;
        runDraftLabel.nodeValue = ocrLoading ? "识别中…" : "用当前参数试识别";
      }
    };
  }

  /* ---------- 2. 左栏预览对照与整盒拖放容器 ---------- */
  function createPreviewBox(fileInput, tunerEl, cb) {
    var naturalDimsText = "";
    var lastModeOptSig = "";

    var viewModeButtons = {};
    var viewModeSeg = V3.dom.el("div", { className: "cap-seg", role: "group", "aria-label": "预览模式切换", dataset: { role: "ocr-view-mode" } });
    VIEW_MODES.forEach(function (item) {
      var btn = V3.dom.el("button", {
        type: "button", className: item.id === "processed" ? "on" : "",
        dataset: { view: item.id }, "aria-pressed": String(item.id === "processed"), disabled: true, text: item.label,
        onClick: function () { if (!btn.disabled) cb.onChangeViewMode(item.id); }
      });
      viewModeButtons[item.id] = btn;
      viewModeSeg.appendChild(btn);
    });

    var modeSelect = V3.dom.el("select", {
      "aria-label": "OCR 处理方式", dataset: { role: "ocr-mode" },
      onChange: function (ev) { cb.onChangeMode(ev.target.value); }
    });
    var dropzone = V3.dom.el("div", {
      className: "ocr-dropzone", tabindex: "0", role: "button",
      "aria-label": "点击选择图片，或拖入图片、按 Ctrl+V 粘贴截图", dataset: { role: "ocr-dropzone" },
      onClick: function () { cb.onPick(); },
      onKeyDown: function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cb.onPick(); } }
    }, [
      V3.dom.icon("image"),
      V3.dom.el("div", { className: "ocr-dropzone-title", text: "点击选择图片 · 拖拽图片到此 · Ctrl+V 粘贴截图" }),
      V3.dom.el("div", { className: "ocr-dropzone-sub", text: "贴图后可实时拖动下方 5 个参数查看后端同源处理效果，满意后直接保存为全局预设" })
    ]);

    var dimOrigEl = V3.dom.el("span", { className: "mono", dataset: { role: "ocr-dim-orig" } });
    var origImgEl = V3.dom.el("img", { alt: "待识别原图", dataset: { role: "ocr-orig-img" }, hidden: true });
    origImgEl.addEventListener("load", function () {
      if (origImgEl.naturalWidth > 0 && origImgEl.naturalHeight > 0) {
        naturalDimsText = origImgEl.naturalWidth + " × " + origImgEl.naturalHeight;
        var oc = getOcrState(cb.getState());
        if (!(oc.imageWidth && oc.imageHeight)) dimOrigEl.textContent = naturalDimsText;
      }
    });
    var paneOrigEl = V3.dom.el("div", { className: "ocr-canvas-pane", dataset: { role: "ocr-pane-orig" }, hidden: true }, [
      V3.dom.el("div", { className: "ocr-canvas-caption" }, [V3.dom.el("span", { text: "原图" }), dimOrigEl]),
      V3.dom.el("div", { className: "ocr-canvas-frame" }, [origImgEl])
    ]);

    var procMetaEl = V3.dom.el("span", { className: "mono", dataset: { role: "ocr-proc-meta" } });
    var procImgEl = V3.dom.el("img", { alt: "预处理结果预览图", dataset: { role: "ocr-proc-img" }, hidden: true });
    var loadingOverlay = V3.dom.el("div", {
      className: "ocr-canvas-overlay", role: "status", "aria-live": "polite", dataset: { role: "ocr-preview-loading" }, hidden: true
    }, [
      V3.dom.el("span", { text: "正在请求后端生成预处理预览图…" }),
      V3.dom.el("div", { className: "progress indeterminate", "aria-hidden": "true" }, [V3.dom.el("i")])
    ]);
    var paneProcEl = V3.dom.el("div", { className: "ocr-canvas-pane", dataset: { role: "ocr-pane-proc" } }, [
      V3.dom.el("div", { className: "ocr-canvas-caption" }, [V3.dom.el("span", { text: "预处理结果预览" }), procMetaEl]),
      V3.dom.el("div", { className: "ocr-canvas-frame" }, [procImgEl, loadingOverlay])
    ]);
    var canvasGridEl = V3.dom.el("div", { className: "ocr-canvas-grid", dataset: { role: "ocr-canvas-grid" } }, [paneOrigEl, paneProcEl]);

    var errTextEl = V3.dom.el("span", { dataset: { role: "ocr-preview-error-text" } });
    var retryBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-retry-preview" }, text: "重试",
      onClick: function () { cb.onRetryPreview(); }
    });
    var errorBox = V3.dom.el("div", { className: "box-error", role: "alert", dataset: { role: "ocr-preview-error" }, hidden: true }, [
      V3.dom.el("div", { className: "box-error-main" }, [V3.dom.icon("alert", "ic ic-sm"), errTextEl]),
      retryBtn
    ]);
    var stageEl = V3.dom.el("div", { className: "ocr-stage", dataset: { role: "ocr-stage" }, hidden: true }, [
      canvasGridEl, errorBox,
      V3.dom.el("div", { className: "ocr-algo-note" }, [V3.dom.el("span", { text: "预览与正式识别共用后端同一算法" })])
    ]);

    var fileLabelEl = V3.dom.el("span", { className: "mono muted", dataset: { role: "ocr-file-label" }, text: "未选择图片" });
    var footPickBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-foot-pick" }, onClick: function () { cb.onPick(); }
    }, [V3.dom.icon("upload", "ic ic-xs"), document.createTextNode("选择图片")]);
    var clearBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-clear" }, onClick: function () { cb.onClear(); }
    }, [V3.dom.icon("trash", "ic ic-xs"), document.createTextNode("清除")]);

    var boxEl = V3.dom.el("div", { className: "studio-box", id: "ocr-left-box", dataset: { role: "ocr-left-box" } }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("span", { text: "图片与预处理预览" }),
        V3.dom.el("div", { className: "box-head-controls" }, [viewModeSeg, modeSelect])
      ]),
      fileInput, dropzone, stageEl, tunerEl,
      V3.dom.el("div", { className: "box-foot" }, [fileLabelEl, V3.dom.el("div", { className: "box-foot-right" }, [footPickBtn, clearBtn])])
    ]);

    /* 左栏整盒拖放换图（空态与已有预览态均生效） */
    function setDragHighlight(on) { boxEl.classList.toggle("drag-over", on); dropzone.classList.toggle("drag-over", on); }
    boxEl.addEventListener("dragover", function (ev) { ev.preventDefault(); setDragHighlight(true); });
    boxEl.addEventListener("dragleave", function (ev) { if (!boxEl.contains(ev.relatedTarget)) setDragHighlight(false); });
    boxEl.addEventListener("drop", function (ev) {
      ev.preventDefault();
      setDragHighlight(false);
      var img = V3.ocrPresets.extractImageFile(ev.dataTransfer);
      if (img) cb.onDropImage(img);
    });

    function syncModeSelect(state) {
      var selectedVal = getOcrMode(state) === "raw" ? "raw" : getOcrLangPair(state);
      var optsList = [{ value: "raw", label: "纯提取文字（不翻译）" }].concat(getLangPairOptions(state).map(function (item) {
        return { value: item.value, label: "提取并翻译：" + item.label };
      }));
      var sig = optsList.map(function (o) { return o.value + "=" + o.label; }).join("|");
      if (sig !== lastModeOptSig) {
        V3.dom.empty(modeSelect);
        optsList.forEach(function (item) { modeSelect.appendChild(V3.dom.el("option", { value: item.value, text: item.label })); });
        lastModeOptSig = sig;
      }
      if (modeSelect.value !== selectedVal) modeSelect.value = selectedVal;
    }

    function render(state, ctx) {
      var oc = getOcrState(state);
      var hasImg = !!(ctx.currentFile || oc.previewUrl || oc.processedPreviewUrl);
      var ocrLoading = oc.phase === "loading";

      footPickBtn.disabled = ocrLoading;
      clearBtn.disabled = ocrLoading || !hasImg;
      dropzone.hidden = hasImg;
      stageEl.hidden = !hasImg;
      fileLabelEl.textContent = oc.fileName || (ctx.currentFile && ctx.currentFile.name) || "未选择图片";

      VIEW_MODES.forEach(function (item) {
        var btn = viewModeButtons[item.id], on = ctx.viewMode === item.id;
        btn.classList.toggle("on", on);
        btn.setAttribute("aria-pressed", String(on));
        btn.disabled = !hasImg;
      });

      if (!hasImg) {
        syncImgSrc(origImgEl, ""); syncImgSrc(procImgEl, "");
        loadingOverlay.hidden = true; errorBox.hidden = true;
        return;
      }

      syncImgSrc(origImgEl, oc.previewUrl);
      syncImgSrc(procImgEl, oc.processedPreviewUrl);
      dimOrigEl.textContent = (oc.imageWidth && oc.imageHeight) ? (oc.imageWidth + " × " + oc.imageHeight) : (naturalDimsText || "");
      canvasGridEl.classList.toggle("split", ctx.viewMode === "split");
      paneOrigEl.hidden = ctx.viewMode === "processed";
      paneProcEl.hidden = ctx.viewMode === "original";
      loadingOverlay.hidden = oc.previewPhase !== "loading";

      errorBox.hidden = oc.previewPhase !== "error";
      errTextEl.textContent = oc.previewPhase === "error"
        ? V3.labels.formatUiError(oc.previewErrorCode, oc.previewError, null, "预处理预览生成失败") : "";
      retryBtn.disabled = !ctx.currentFile;

      if (ctx.draft.binarize_mode === "otsu") {
        procMetaEl.textContent = oc.otsuThreshold != null ? ("Otsu 自动阈值 " + oc.otsuThreshold) : "Otsu 自动二值化";
      } else if (ctx.draft.binarize_mode === "manual") {
        procMetaEl.textContent = "手动二值阈值 " + ctx.draft.threshold;
      } else {
        procMetaEl.textContent = "灰度增强模式";
      }
    }

    return { el: boxEl, syncModeSelect: syncModeSelect, render: render, resetNaturalDims: function () { naturalDimsText = ""; }, resetCache: function () { naturalDimsText = ""; lastModeOptSig = ""; } };
  }

  /* ---------- 3. 原地 5 参数调校与保存栏（固定阈值槽零抖动） ---------- */
  function createTunerSection(cb) {
    var lastStarterSig = "";

    var editorTitleEl = V3.dom.el("span", { dataset: { role: "ocr-editor-title" }, text: "新建预处理预设" });
    var editorBadgeEl = V3.dom.el("span", { className: "badge accent", dataset: { role: "ocr-editor-badge" }, text: "编辑中", hidden: true });
    var resetParamsBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-reset-params" },
      onClick: function () { if (!resetParamsBtn.disabled) cb.onResetParams(); }
    }, [V3.dom.icon("rotate-ccw", "ic ic-xs"), document.createTextNode("重置默认")]);
    var starterRowEl = V3.dom.el("div", { className: "starter-row", "aria-label": "内置起步参考模板", dataset: { role: "ocr-starter-row" } });

    var invertSwitchBtn = V3.dom.el("button", {
      type: "button", className: "switch", role: "switch", "aria-checked": "false", "aria-label": "反色开关",
      dataset: { role: "ocr-param-invert" },
      onClick: function () { if (!invertSwitchBtn.disabled) cb.onToggleInvert(); }
    });
    var invertHintEl = V3.dom.el("span", { className: "hint", text: "关闭（保持原明暗）" });
    var valInvertEl = V3.dom.el("span", { className: "param-val", dataset: { role: "ocr-val-invert" }, text: "关" });

    var rangeCtrls = {};
    var rangeRows = RANGE_SPECS.map(function (spec) {
      var inputId = "v3-ocr-param-" + spec.key;
      var inputEl = V3.dom.el("input", {
        type: "range", className: "param-range", id: inputId,
        min: spec.min, max: spec.max, step: spec.step, value: spec.def, dataset: { role: "ocr-param-" + spec.key }
      });
      var valEl = V3.dom.el("span", { className: "param-val", dataset: { role: "ocr-val-" + spec.key }, text: Number(spec.def).toFixed(spec.digits) });
      inputEl.addEventListener("input", function () { cb.onChangeRange(spec.key, parseFloat(inputEl.value)); });
      rangeCtrls[spec.key] = { input: inputEl, val: valEl, digits: spec.digits };
      return paramRow(spec.label, spec.hint, [inputEl], valEl, inputId);
    });

    var binarizeButtons = {};
    var binarizeSegEl = V3.dom.el("div", { className: "cap-seg", role: "group", "aria-label": "黑白二值化模式", dataset: { role: "ocr-binarize-seg" } });
    BINARIZE_OPTIONS.forEach(function (modeKey) {
      var label = V3.labels && typeof V3.labels.ocrBinarizeMode === "function" ? V3.labels.ocrBinarizeMode(modeKey) : modeKey;
      var btn = V3.dom.el("button", {
        type: "button", className: modeKey === "off" ? "on" : "",
        dataset: { bin: modeKey }, "aria-pressed": String(modeKey === "off"), text: label,
        onClick: function () { if (!btn.disabled) cb.onChangeBinarize(modeKey); }
      });
      binarizeButtons[modeKey] = btn;
      binarizeSegEl.appendChild(btn);
    });
    var valBinarizeEl = V3.dom.el("span", { className: "param-val", dataset: { role: "ocr-val-binarize" }, text: "关闭" });

    var thresholdInput = V3.dom.el("input", {
      type: "range", className: "param-range", id: "v3-ocr-param-threshold",
      min: "0", max: "255", step: "1", value: "128", disabled: true, dataset: { role: "ocr-param-threshold" }
    });
    thresholdInput.addEventListener("input", function () { cb.onChangeThreshold(parseInt(thresholdInput.value, 10)); });
    var valThresholdEl = V3.dom.el("span", { className: "param-val", dataset: { role: "ocr-val-threshold" }, text: "--" });
    var thresholdRowEl = paramRow("手动阈值", "0 ~ 255", [thresholdInput], valThresholdEl, "v3-ocr-param-threshold", {
      className: "param-row disabled", dataset: { role: "ocr-row-threshold" }
    });

    var paramListEl = V3.dom.el("div", { className: "param-list" }, [
      paramRow("反色", "深底浅字转白底黑字", [invertSwitchBtn, invertHintEl], valInvertEl)
    ].concat(rangeRows, [
      paramRow("黑白二值化", "消除彩色/噪点底纹", [binarizeSegEl], valBinarizeEl),
      thresholdRowEl
    ]));

    var nameInput = V3.dom.el("input", {
      type: "text", className: "field", maxlength: "48",
      placeholder: "输入预设名称，例如：深底终端白字", "aria-label": "预设名称", dataset: { role: "ocr-preset-name" }
    });
    nameInput.addEventListener("input", function () { cb.onChangeName(nameInput.value); });
    var cancelEditBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-cancel-edit" }, text: "取消编辑", hidden: true,
      onClick: function () { cb.onCancelEdit(); }
    });
    var saveBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-save-preset" }, text: "保存为新预设",
      onClick: function () { cb.onSave(); }
    });
    var saveFeedbackEl = V3.dom.el("span", { className: "saved", dataset: { role: "ocr-save-feedback" }, text: "已保存", hidden: true });
    var saveErrorEl = V3.dom.el("div", { className: "save-err", role: "alert", dataset: { role: "ocr-save-error" }, hidden: true });

    var el = V3.dom.el("div", { className: "tuner-section", dataset: { role: "ocr-tuner" } }, [
      V3.dom.el("div", { className: "tuner-head" }, [
        V3.dom.el("div", { className: "tuner-title" }, [V3.dom.icon("sliders", "ic ic-sm"), editorTitleEl, editorBadgeEl]),
        resetParamsBtn
      ]),
      starterRowEl, paramListEl,
      V3.dom.el("div", { className: "save-bar" }, [nameInput, cancelEditBtn, saveBtn, saveFeedbackEl, saveErrorEl])
    ]);

    function renderStarters(state, activeKey, saving) {
      var starters = getStarterTemplates(state);
      var sig = starters.map(function (t) { return (t.key || "") + ":" + (t.label || ""); }).join("|") + "::" + String(activeKey || "") + "::" + String(!!saving);
      if (sig === lastStarterSig) return;
      V3.dom.empty(starterRowEl);
      starterRowEl.appendChild(V3.dom.el("span", { className: "starter-label", text: "参考模板起步：" }));
      if (!starters.length) {
        starterRowEl.appendChild(V3.dom.el("span", { className: "hint", text: "连接本地服务后载入内置参考模板" }));
      } else {
        starters.forEach(function (tpl) {
          var isOn = activeKey === tpl.key;
          starterRowEl.appendChild(V3.dom.el("button", {
            type: "button", className: "starter-pill" + (isOn ? " on" : ""),
            dataset: { starter: tpl.key || "" }, "aria-pressed": String(isOn), disabled: !!saving, text: tpl.label || tpl.key || "",
            onClick: function () { cb.onApplyStarter(tpl); }
          }));
        });
      }
      lastStarterSig = sig;
    }

    function render(state, ctx) {
      var oc = getOcrState(state), presets = getPresetsList(state), saving = oc.savingPresets === true;
      var editing = ctx.editingPresetId ? (presets.find(function (p) { return p && p.id === ctx.editingPresetId; }) || null) : null;

      editorTitleEl.textContent = editing ? ("编辑预设：" + editing.name) : "新建预处理预设";
      editorBadgeEl.hidden = !editing;
      cancelEditBtn.hidden = !editing;
      cancelEditBtn.disabled = saving;
      resetParamsBtn.disabled = saving;
      saveBtn.disabled = saving;
      saveBtn.textContent = saving ? "保存中…" : (editing ? "更新当前预设" : "保存为新预设");

      renderStarters(state, ctx.activeStarterKey, saving);
      invertSwitchBtn.setAttribute("aria-checked", ctx.draft.invert ? "true" : "false");
      invertSwitchBtn.disabled = saving;
      invertHintEl.textContent = ctx.draft.invert ? "已开启（深底浅字反转为白底黑字）" : "关闭（保持原明暗）";
      valInvertEl.textContent = ctx.draft.invert ? "开" : "关";

      RANGE_SPECS.forEach(function (spec) {
        var ctrl = rangeCtrls[spec.key];
        ctrl.input.value = String(ctx.draft[spec.key]);
        ctrl.input.disabled = saving;
        ctrl.val.textContent = Number(ctx.draft[spec.key]).toFixed(ctrl.digits);
      });
      BINARIZE_OPTIONS.forEach(function (modeKey) {
        var btn = binarizeButtons[modeKey], isOn = ctx.draft.binarize_mode === modeKey;
        btn.classList.toggle("on", isOn);
        btn.setAttribute("aria-pressed", String(isOn));
        btn.disabled = saving;
      });
      valBinarizeEl.textContent = V3.labels && typeof V3.labels.ocrBinarizeShort === "function"
        ? V3.labels.ocrBinarizeShort(ctx.draft.binarize_mode) : ctx.draft.binarize_mode;

      var isManual = ctx.draft.binarize_mode === "manual";
      thresholdRowEl.classList.toggle("disabled", !isManual);
      thresholdInput.disabled = saving || !isManual;
      thresholdInput.value = String(ctx.draft.threshold);
      valThresholdEl.textContent = isManual ? String(ctx.draft.threshold) : (ctx.draft.binarize_mode === "otsu" ? "Otsu" : "--");

      nameInput.disabled = saving;
      if (document.activeElement !== nameInput && nameInput.value !== (ctx.draft.name || "")) nameInput.value = ctx.draft.name || "";

      var formErr = ctx.localValidationError || (ctx.lastSaveSource === "form" ? (oc.presetsSaveError || "") : "");
      saveErrorEl.hidden = !formErr;
      saveErrorEl.textContent = formErr;
      saveFeedbackEl.hidden = !!formErr || !(oc.presetsSaved === true && ctx.lastSaveSource === "form");
    }

    return { el: el, focusName: function () { if (typeof nameInput.focus === "function") nameInput.focus(); }, render: render, resetCache: function () { lastStarterSig = ""; } };
  }

  /* ---------- 4. 右栏识别输出与质量早停摘要 ---------- */
  function createOutputBox(cb) {
    var lastOutputKey = null;

    var hitBadgeEl = V3.dom.el("span", { className: "badge", dataset: { role: "ocr-hit-badge" }, text: "等待识别" });
    var outBodyEl = V3.dom.el("div", { className: "box-body", dataset: { role: "ocr-output" } });
    var outMetaEl = V3.dom.el("span", { dataset: { role: "ocr-out-meta" }, text: "识别结果可直接复制" });
    var copyFeedbackEl = V3.dom.el("span", { className: "saved", dataset: { role: "ocr-copy-feedback" }, hidden: true });
    var copyTransBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", hidden: true, dataset: { role: "copy-ocr-translation" },
      onClick: function () { cb.onCopy("ocr_translation"); }
    }, [V3.dom.icon("copy", "ic ic-xs"), document.createTextNode("复制译文")]);
    var copyRawBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", disabled: true, dataset: { role: "copy-ocr" },
      onClick: function () { cb.onCopy("ocr"); }
    }, [V3.dom.icon("copy", "ic ic-xs"), document.createTextNode("复制 OCR 原文")]);

    var el = V3.dom.el("div", { className: "studio-box", dataset: { role: "ocr-right-box" } }, [
      V3.dom.el("div", { className: "box-head" }, [V3.dom.el("span", { text: "识别输出" }), hitBadgeEl]),
      outBodyEl,
      V3.dom.el("div", { className: "box-foot" }, [
        outMetaEl, V3.dom.el("div", { className: "box-foot-right" }, [copyFeedbackEl, copyTransBtn, copyRawBtn])
      ])
    ]);

    function render(state, ctx) {
      var P = V3.ocrPresets, oc = getOcrState(state), ocrMode = getOcrMode(state), phase = oc.phase || "empty";
      var enabledCnt = P.enabledCountOf(getPresetsList(state));
      var rawOcr = phase === "result" ? (oc.ocr_text || (ocrMode === "raw" ? oc.text : "") || "") : "";
      var transText = phase === "result" && ocrMode === "translate" && oc.text ? oc.text : "";
      var metaSummary = phase === "result" ? P.summarizeOcrMeta(oc.ocr_meta) : null;
      var effectiveRunKind = oc.lastRunKind || ctx.lastRunKind || "chain";

      var outKey = JSON.stringify({
        phase: phase, mode: ocrMode, rawOcr: rawOcr, transText: transText,
        source_lang: oc.source_lang || null, target_lang: oc.target_lang || null,
        error: oc.error || null, message: oc.message || "", details: oc.details || null,
        ocr_meta: oc.ocr_meta || null, enabledCnt: enabledCnt, runKind: effectiveRunKind, hasFile: !!ctx.currentFile
      });

      if (outKey !== lastOutputKey) {
        V3.dom.empty(outBodyEl);
        if (phase === "loading") {
          hitBadgeEl.className = "badge accent"; hitBadgeEl.textContent = "识别中…";
          var loadMsg = effectiveRunKind === "draft" ? "正在按当前调参参数试识别…"
            : (enabledCnt > 0 ? "正在按预设链路依次识别并评估质量…" : "正在识别图片文字…");
          outBodyEl.appendChild(V3.dom.el("div", { className: "box-loading", role: "status", "aria-live": "polite" }, [
            V3.dom.el("span", { text: loadMsg }),
            V3.dom.el("div", { className: "progress indeterminate", "aria-hidden": "true" }, [V3.dom.el("i")])
          ]));
        } else if (phase === "error") {
          hitBadgeEl.className = "badge danger"; hitBadgeEl.textContent = "识别失败";
          outBodyEl.appendChild(V3.dom.el("div", { className: "box-error", role: "alert" }, [
            V3.dom.el("div", { className: "box-error-main" }, [
              V3.dom.icon("alert"),
              V3.dom.el("span", { text: V3.labels.formatUiError(oc.error, oc.message, oc.details, "图片 OCR 识别失败") })
            ]),
            V3.dom.el("button", {
              type: "button", className: "btn btn-sm", dataset: { role: "ocr-retry" }, disabled: !ctx.currentFile, text: "重试",
              onClick: function () { cb.onRetryOcr(); }
            })
          ]));
        } else if (phase === "result") {
          if (metaSummary) {
            hitBadgeEl.className = "badge " + metaSummary.badgeTone; hitBadgeEl.textContent = metaSummary.badgeText;
            var hitChildren = [V3.dom.el("div", { className: "hit-strip-main" }, [
              V3.dom.el("span", { className: "dot " + metaSummary.dotTone, "aria-hidden": "true" }),
              V3.dom.el("span", { text: metaSummary.summaryText })
            ])];
            if (metaSummary.attempts && metaSummary.attempts.length) {
              var traceChildren = [V3.dom.el("span", { text: "执行链路：" })];
              metaSummary.attempts.forEach(function (att, idx) {
                if (idx > 0) traceChildren.push(V3.dom.el("span", { "aria-hidden": "true", text: "→" }));
                traceChildren.push(V3.dom.el("span", { className: "trace-step" + (att && att.accepted ? " hit" : ""), text: P.formatAttemptLabel(att) }));
              });
              hitChildren.push(V3.dom.el("div", { className: "trace-line", dataset: { role: "ocr-trace-line" } }, traceChildren));
            }
            outBodyEl.appendChild(V3.dom.el("div", { className: "hit-strip", dataset: { role: "ocr-hit-strip" } }, hitChildren));
          } else {
            hitBadgeEl.className = "badge ok"; hitBadgeEl.textContent = "识别完成";
          }
          outBodyEl.appendChild(V3.dom.el("div", {}, [
            V3.dom.el("div", { className: "ocr-section-label", text: "OCR 识别原文" }),
            V3.dom.el("div", { className: "studio-result-text", dataset: { role: "ocr-raw-text" }, text: rawOcr || "（未识别到文字）" })
          ]));
          if (transText) {
            var dirTitle = V3.labels && typeof V3.labels.directionLabel === "function" ? V3.labels.directionLabel(oc.source_lang, oc.target_lang) : "";
            outBodyEl.appendChild(V3.dom.el("div", { className: "studio-sub-block", dataset: { role: "ocr-translated-block" } }, [
              V3.dom.el("div", { className: "studio-sub-head" }, [
                V3.dom.el("span", { text: dirTitle ? ("译文（" + dirTitle + "）") : "译文" }),
                V3.dom.el("span", { className: "mono", text: V3.labels.FIXED_MODELS.ollama })
              ]),
              V3.dom.el("div", { className: "studio-sub-text", text: transText })
            ]));
          }
        } else {
          hitBadgeEl.className = "badge";
          hitBadgeEl.textContent = enabledCnt > 0 ? ("已就绪 · " + enabledCnt + " 条预设") : "原图直识";
          var emptySub = enabledCnt > 0
            ? ("当前已启用 " + enabledCnt + " / " + P.maxEnabledPresets() + " 个预处理预设，正式识别将按 1→" + enabledCnt + " 顺序质量早停执行。")
            : "当前未启用预处理预设，将直接按默认原图识别。";
          outBodyEl.appendChild(V3.dom.el("div", { className: "box-empty" }, [
            V3.dom.icon("scan"),
            V3.dom.el("div", { text: "拖入图片、选择文件或直接按 Ctrl+V 粘贴截图开始提取文字。" }),
            V3.dom.el("div", { className: "hint", text: emptySub })
          ]));
        }
        lastOutputKey = outKey;
      }

      if (phase === "loading") outMetaEl.textContent = "正在执行后端预处理与 Tesseract 识别";
      else if (phase === "error") outMetaEl.textContent = "错误信息由后端原样返回";
      else if (phase === "result") {
        var metaBits = ["Tesseract" + (transText ? (" + " + V3.labels.FIXED_MODELS.ollama) : "")];
        if (metaSummary && metaSummary.elapsedMs != null) metaBits.push(metaSummary.elapsedMs + "ms");
        outMetaEl.textContent = metaBits.join(" · ");
      } else outMetaEl.textContent = "识别结果可直接复制";

      var ocrCopied = phase === "result"
        ? (oc.copyStatus || ((state.copyKind === "ocr" || state.copyKind === "ocr_translation") ? state.copyStatus : "")) : "";
      copyFeedbackEl.hidden = !ocrCopied; copyFeedbackEl.textContent = ocrCopied || "";
      copyRawBtn.disabled = phase !== "result" || !rawOcr;
      copyTransBtn.hidden = !transText; copyTransBtn.disabled = phase !== "result" || !transText;
    }

    return { el: el, render: render, resetCache: function () { lastOutputKey = null; } };
  }

  /* ---------- 5. 面板协调器：组装 4 个分区与底部全局预设列表 ---------- */
  function createOcrPanel(container, options) {
    var P = V3.ocrPresets, opts = options || {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var hasPatcher = typeof opts.patchState === "function";
    var patchState = hasPatcher ? opts.patchState : function () {};
    var hooks = opts.hooks || {};

    var currentFile = null, ownedPreviewUrl = null, viewMode = "processed";
    var editingPresetId = null, activeStarterKey = null, lastRunKind = null;
    var draft = Object.assign({ name: "" }, P.DEFAULT_PARAMS);
    var localValidationError = "", awaitingFormSave = false, lastSaveSource = "form";

    function releasePreviewUrl() {
      if (ownedPreviewUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
        try { URL.revokeObjectURL(ownedPreviewUrl); } catch (err) {}
      }
      ownedPreviewUrl = null;
    }

    function uiCtx() {
      return {
        currentFile: currentFile, viewMode: viewMode, editingPresetId: editingPresetId,
        activeStarterKey: activeStarterKey, draft: draft, lastRunKind: lastRunKind,
        localValidationError: localValidationError, lastSaveSource: lastSaveSource
      };
    }

    function requestPreview(immediate) {
      if (!currentFile) return;
      call(hooks, "onRequestPreprocessPreview", { file: currentFile, params: P.cloneParams(draft), immediate: !!immediate });
    }

    function submitOcr(kind) {
      var st = getState();
      if (!currentFile || getOcrState(st).phase === "loading") return;
      var runKind = kind === "draft" ? "draft" : "chain";
      lastRunKind = runKind;
      var ocrMode = getOcrMode(st);
      patchState({ copyStatus: null, copyKind: null, ocr: { lastRunKind: runKind, copyStatus: null, copyKind: null, error: null, message: "", details: null } });
      var payload = { file: currentFile, mode: ocrMode, panel: "ocr" };
      if (ocrMode === "translate") payload.lang_pair = getOcrLangPair(st);
      if (runKind === "draft") payload.preprocess_params = P.cloneParams(draft);
      call(hooks, "onSubmitImage", payload);
    }

    function handleImageFile(file) {
      if (!file) return;
      currentFile = file; previewBox.resetNaturalDims(); lastRunKind = null; releasePreviewUrl();
      var preview = "";
      if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
        try { ownedPreviewUrl = URL.createObjectURL(file); preview = ownedPreviewUrl; } catch (err) { ownedPreviewUrl = null; }
      }
      patchState({ copyStatus: null, copyKind: null, ocr: emptyOcrResetPatch({ fileName: file.name || "clipboard-image.png", previewUrl: preview, previewPhase: "loading" }) });
      requestPreview(true); /* 选图/贴图/整盒拖放后 0ms 立即请求后端预览图，不自动发起 OCR。 */
    }

    function clearOcrImage() {
      currentFile = null; previewBox.resetNaturalDims(); lastRunKind = null; releasePreviewUrl();
      patchState({ copyStatus: null, copyKind: null, ocr: emptyOcrResetPatch() });
      call(hooks, "onClearOcr");
    }

    function applyStarterTemplate(tpl, shouldFocus) {
      var st = getState();
      if (!tpl || !tpl.params || getOcrState(st).savingPresets === true) return;
      activeStarterKey = tpl.key || null;
      draft = Object.assign({ name: editingPresetId ? draft.name : (tpl.suggested_name || tpl.label || "") }, P.cloneParams(tpl.params));
      localValidationError = "";
      tuner.render(st, uiCtx()); previewBox.render(st, uiCtx());
      if (shouldFocus) tuner.focusName();
      requestPreview(true);
    }

    function switchEditorTarget(preset, shouldFocus) {
      var st = getState();
      if (getOcrState(st).savingPresets === true) return;
      editingPresetId = preset ? preset.id : null;
      activeStarterKey = null; localValidationError = ""; lastSaveSource = "form";
      draft = preset ? Object.assign({ name: preset.name || "" }, P.cloneParams(preset)) : Object.assign({ name: "" }, P.DEFAULT_PARAMS);
      if (hasPatcher) patchState({ ocr: { presetsSaved: false, presetsSaveError: "" } });
      else render(st);
      if (shouldFocus) tuner.focusName();
      requestPreview(true);
    }

    function saveDraftPreset() {
      var st = getState();
      if (getOcrState(st).savingPresets === true) return;
      var trimmed = String(draft.name || "").trim();
      if (!trimmed) {
        localValidationError = "请先填写预设名称后再保存。";
        tuner.render(st, uiCtx()); tuner.focusName();
        return;
      }
      localValidationError = "";
      var mutation = P.buildNextPresetsForSave(getPresetsList(st), editingPresetId, trimmed, draft);
      awaitingFormSave = true; lastSaveSource = "form";
      tuner.render(st, uiCtx());
      call(hooks, "onSaveOcrPresets", mutation);
    }

    function updateDraftParam(immediate) {
      var st = getState(); activeStarterKey = null;
      tuner.render(st, uiCtx()); previewBox.render(st, uiCtx()); requestPreview(immediate);
    }

    var headActions = createHeadActions({ onFilePicked: handleImageFile, onSubmit: submitOcr });
    var tuner = createTunerSection({
      onResetParams: function () { draft = Object.assign({ name: draft.name }, P.DEFAULT_PARAMS); updateDraftParam(true); },
      onApplyStarter: function (tpl) { applyStarterTemplate(tpl, false); },
      onToggleInvert: function () { draft.invert = !draft.invert; updateDraftParam(true); },
      onChangeRange: function (key, val) { draft[key] = val; updateDraftParam(false); },
      onChangeBinarize: function (modeKey) { if (draft.binarize_mode !== modeKey) { draft.binarize_mode = modeKey; updateDraftParam(true); } },
      onChangeThreshold: function (val) { draft.threshold = val; updateDraftParam(false); },
      onChangeName: function (val) { draft.name = val; if (localValidationError) { localValidationError = ""; tuner.render(getState(), uiCtx()); } },
      onCancelEdit: function () { switchEditorTarget(null, false); },
      onSave: saveDraftPreset
    });
    var previewBox = createPreviewBox(headActions.fileInput, tuner.el, {
      getState: getState, onPick: headActions.triggerPick, onClear: clearOcrImage, onDropImage: handleImageFile,
      onRetryPreview: function () { requestPreview(true); },
      onChangeViewMode: function (mode) { if (viewMode !== mode) { viewMode = mode; previewBox.render(getState(), uiCtx()); } },
      onChangeMode: function (val) { patchState(val === "raw" ? { ocr: { mode: "raw" } } : { ocr: { mode: "translate", lang_pair: val } }); }
    });
    var outputBox = createOutputBox({
      onRetryOcr: function () { submitOcr(getOcrState(getState()).lastRunKind || lastRunKind || "chain"); },
      onCopy: function (kind) {
        var st = getState(), oc = getOcrState(st);
        if (kind === "ocr_translation") { if (oc.text) call(hooks, "onCopy", { text: oc.text, kind: "ocr_translation" }); return; }
        var rawText = oc.ocr_text || (getOcrMode(st) === "raw" ? oc.text : "") || "";
        if (rawText) call(hooks, "onCopy", { text: rawText, kind: "ocr" });
      }
    });
    var presetsCard = P.createPresetsCard({
      getState: getState, confirm: opts.confirm || null,
      onNewPreset: function () { switchEditorTarget(null, true); },
      onApplyStarter: function (tpl) { applyStarterTemplate(tpl, true); },
      onEditPreset: function (preset) { if (preset) switchEditorTarget(preset, false); },
      onMutatePresets: function (nextPresets, action, targetId) {
        lastSaveSource = "list"; awaitingFormSave = false; localValidationError = "";
        call(hooks, "onSaveOcrPresets", { presets: nextPresets, action: action, targetId: targetId });
      }
    });

    V3.dom.empty(container);
    container.appendChild(V3.dom.el("div", { className: "studio-split" }, [previewBox.el, outputBox.el]));
    container.appendChild(presetsCard.el);

    function render(state) {
      state = state || {};
      var oc = getOcrState(state), presets = getPresetsList(state);
      if (ownedPreviewUrl && oc.previewUrl !== ownedPreviewUrl) releasePreviewUrl();

      /* 表单保存结果收敛：仅在后端成功回读后退出编辑态；失败时保留 draft 与 editingPresetId。 */
      if (awaitingFormSave && oc.savingPresets !== true) {
        if (oc.presetsSaveError) awaitingFormSave = false;
        else if (oc.presetsSaved === true) { awaitingFormSave = false; editingPresetId = null; }
      }
      if (editingPresetId && !presets.some(function (p) { return p && p.id === editingPresetId; })) {
        editingPresetId = null; activeStarterKey = null; draft = Object.assign({ name: "" }, P.DEFAULT_PARAMS);
      }
      var ctx = uiCtx();
      headActions.sync(!!currentFile, oc.phase === "loading");
      previewBox.syncModeSelect(state);
      previewBox.render(state, ctx);
      tuner.render(state, ctx);
      outputBox.render(state, ctx);
      presetsCard.render(state, ctx);
    }

    function destroy() {
      currentFile = null; releasePreviewUrl();
      previewBox.resetCache(); tuner.resetCache(); outputBox.resetCache(); presetsCard.resetCache();
    }

    return { headActionsEl: headActions.el, handleImageFile: handleImageFile, render: render, destroy: destroy };
  }

  V3.ocrPanel = { create: createOcrPanel };
})(window.V3 = window.V3 || {});

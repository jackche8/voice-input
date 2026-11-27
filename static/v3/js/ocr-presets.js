/* 图片 OCR 预处理纯辅助能力与全局预设列表纯 UI 卡（模板 A 下半区）。
 * 严守纯展示边界：零 innerHTML、零网络/存储/剪贴板、不复制后端起步模板参数，动作经回调交由上层编排。
 */
(function (V3) {
  "use strict";

  var DEFAULT_PARAMS = {
    invert: false,
    contrast: 1.0,
    brightness: 1.0,
    sharpen: 0.0,
    binarize_mode: "off",
    threshold: 128
  };

  var fallbackIdSeq = 0;

  function maxEnabledPresets() {
    var n = V3.labels && V3.labels.OCR_PREPROCESS_MAX_ENABLED;
    return typeof n === "number" && n > 0 ? n : 3;
  }

  function enabledCountOf(list) {
    var arr = Array.isArray(list) ? list : [];
    var count = 0;
    for (var i = 0; i < arr.length; i += 1) {
      if (arr[i] && arr[i].enabled) count += 1;
    }
    return count;
  }

  function clampNum(val, min, max, fallback) {
    var n = Number(val);
    if (!isFinite(n)) return fallback;
    return n < min ? min : (n > max ? max : n);
  }

  function cloneParams(raw) {
    var src = raw || {};
    var bin = src.binarize_mode;
    if (bin !== "off" && bin !== "otsu" && bin !== "manual") bin = "off";
    return {
      invert: !!src.invert,
      contrast: Math.round(clampNum(src.contrast, 0.5, 3.0, 1.0) * 100) / 100,
      brightness: Math.round(clampNum(src.brightness, 0.5, 2.0, 1.0) * 100) / 100,
      sharpen: Math.round(clampNum(src.sharpen, 0.0, 3.0, 0.0) * 10) / 10,
      binarize_mode: bin,
      threshold: Math.round(clampNum(src.threshold, 0, 255, 128))
    };
  }

  function generatePresetId(existingPresets) {
    var taken = {};
    (existingPresets || []).forEach(function (p) {
      if (p && p.id) taken[String(p.id)] = true;
    });
    for (var tries = 0; tries < 8; tries += 1) {
      var candidate = "";
      if (typeof crypto !== "undefined" && crypto && typeof crypto.randomUUID === "function") {
        try { candidate = crypto.randomUUID(); } catch (err) { candidate = ""; }
      }
      if (!candidate) {
        fallbackIdSeq += 1;
        candidate = "preset-" + Date.now().toString(36) + "-" + fallbackIdSeq;
      }
      if (!taken[candidate]) return candidate;
    }
    fallbackIdSeq += 1;
    return "preset-" + Date.now().toString(36) + "-" + fallbackIdSeq + "-u";
  }

  /* 统一提取拖拽或粘贴事件中的首张图片文件（供 home.js 与 ocr-panel.js 单源共享）。 */
  function extractImageFile(dataTransfer) {
    if (!dataTransfer) return null;
    var items = dataTransfer.items;
    if (items && items.length) {
      for (var i = 0; i < items.length; i += 1) {
        var it = items[i];
        if (it && it.kind === "file" && String(it.type || "").indexOf("image/") === 0 && typeof it.getAsFile === "function") {
          var f = it.getAsFile();
          if (f) return f;
        }
      }
    }
    var files = dataTransfer.files;
    if (files && files.length) {
      for (var j = 0; j < files.length; j += 1) {
        if (files[j] && String(files[j].type || "").indexOf("image/") === 0) return files[j];
      }
    }
    return null;
  }

  function paramChipLabels(preset) {
    var p = cloneParams(preset);
    var chips = [];
    if (p.invert) chips.push("反色");
    chips.push("对比 " + p.contrast.toFixed(2), "亮度 " + p.brightness.toFixed(2));
    if (p.sharpen > 0) chips.push("锐化 " + p.sharpen.toFixed(1));
    if (p.binarize_mode === "otsu") chips.push("二值化 自动(Otsu)");
    else if (p.binarize_mode === "manual") chips.push("二值化 手动 " + p.threshold);
    else chips.push("二值化 关");
    return chips;
  }

  /* 对齐后端 OcrAttemptSummary 线格式（label 不含 #rank 前缀，由前端按 rank 拼接）。 */
  function formatAttemptLabel(att) {
    var base = String((att && att.label) || "");
    var prefix = (att && att.rank != null) ? ("#" + att.rank + (base ? " " : "")) : "";
    var conf = (att && typeof att.confidence === "number" && isFinite(att.confidence))
      ? (" " + Math.round(att.confidence) + "%")
      : "";
    return prefix + base + conf + (att && att.accepted ? " ✓" : "");
  }

  /* 依据 OcrRunMeta 5 态真值表生成命中徽章与动态尝试预设数摘要（严禁写死“3 条预设”）。 */
  function summarizeOcrMeta(meta) {
    if (!meta || typeof meta !== "object") return null;
    var runMode = meta.run_mode || "";
    var confNum = typeof meta.confidence === "number" && isFinite(meta.confidence) ? Math.round(meta.confidence) : null;
    var confSuffix = confNum != null ? (" · 置信度 " + confNum + "%") : "";
    var confDot = confNum != null ? (" · " + confNum + "%") : "";
    var elapsed = typeof meta.elapsed_ms === "number" && isFinite(meta.elapsed_ms) ? Math.round(meta.elapsed_ms) : null;
    var elapsedParen = elapsed != null ? ("（耗时 " + elapsed + "ms）") : "";
    var elapsedComma = elapsed != null ? ("，耗时 " + elapsed + "ms") : "";
    var attempts = Array.isArray(meta.attempts) ? meta.attempts : [];
    var presetAttemptCount = attempts.filter(function (a) { return a && (a.rank != null || a.preset_id != null); }).length;
    if (presetAttemptCount === 0 && meta.fallback_to_original && attempts.length > 1) {
      presetAttemptCount = attempts.length - 1;
    }
    var presetName = meta.matched_preset_name ? String(meta.matched_preset_name) : "";
    var rank = meta.matched_rank != null ? meta.matched_rank : null;

    function build(tone, badgeText, summaryText) {
      return { badgeTone: tone, dotTone: tone, badgeText: badgeText, summaryText: summaryText, attempts: attempts, elapsedMs: elapsed };
    }

    if (runMode === "draft_params") {
      return build("ok", "当前参数试识别" + confSuffix, "本次使用左侧未保存调参参数试识别" + elapsedComma);
    }
    if (runMode === "original") {
      return build("ok", "原图直识" + confSuffix, "未启用预处理预设，按默认原图识别" + elapsedParen);
    }
    if (runMode === "preset_chain") {
      if (meta.early_stopped === true && rank != null) {
        return build("ok", "命中 #" + rank + " " + (presetName || ("预设 " + rank)) + confDot,
          "命中顺位 #" + rank + (presetName ? ("「" + presetName + "」") : "") + "，质量达标已早停" + elapsedParen);
      }
      if (meta.fallback_to_original === true) {
        var countText = presetAttemptCount > 0 ? ("已尝试 " + presetAttemptCount + " 条预设") : "已尝试预设";
        if (rank == null) {
          return build("warn", "补跑原图择优" + confSuffix, countText + "均未达质量门限，已自动补跑原图并择优返回" + elapsedParen);
        }
        return build("warn", "择优 #" + rank + " " + (presetName || ("预设 " + rank)) + confDot,
          countText + "与补跑原图均未达门限，按质量择优采用 #" + rank + (presetName ? ("「" + presetName + "」") : "") + elapsedParen);
      }
      if (rank != null) {
        return build("ok", "命中 #" + rank + " " + (presetName || ("预设 " + rank)) + confDot,
          "采用顺位 #" + rank + (presetName ? ("「" + presetName + "」") : "") + elapsedParen);
      }
    }
    var modeLabel = V3.labels && typeof V3.labels.ocrRunMode === "function" ? V3.labels.ocrRunMode(runMode) : String(runMode || "识别完成");
    return build("ok", modeLabel + confSuffix, modeLabel + elapsedParen);
  }

  /* 统一生成新建/更新预设后的新列表（<上限自动启用，满额存为停用，无碰撞 ID）。 */
  function buildNextPresetsForSave(existing, editingId, name, draftParams) {
    var list = Array.isArray(existing) ? existing : [];
    var cleanParams = cloneParams(draftParams);
    if (editingId) {
      return {
        action: "update",
        targetId: editingId,
        presets: list.map(function (item) {
          return (!item || item.id !== editingId)
            ? Object.assign({}, item)
            : Object.assign({ id: item.id, name: name, enabled: !!item.enabled }, cleanParams);
        })
      };
    }
    var targetId = generatePresetId(list);
    var autoEnable = enabledCountOf(list) < maxEnabledPresets();
    var next = list.map(function (item) { return Object.assign({}, item); });
    next.push(Object.assign({ id: targetId, name: name, enabled: autoEnable }, cleanParams));
    return { action: "create", targetId: targetId, presets: next };
  }

  function buildEmptyPresetsBox(starters, saving, onApplyStarter) {
    var emptyStarterBtns = starters.map(function (tpl) {
      return V3.dom.el("button", {
        type: "button", className: "btn btn-sm", dataset: { emptyStarter: tpl.key || "" }, disabled: saving,
        text: "从「" + (tpl.label || tpl.key) + "」起步",
        onClick: function () { onApplyStarter(tpl); }
      });
    });
    var children = [V3.dom.el("p", {}, [
      V3.dom.el("strong", { text: "当前未启用预处理预设" }),
      V3.dom.el("br"),
      document.createTextNode("截屏翻译、纯 OCR 与工作台图片 OCR 均按默认原图识别。遇到深色背景、低对比度灰字或彩色字幕识别不准时，可贴图调参后保存为预设。")
    ])];
    if (emptyStarterBtns.length) children.push(V3.dom.el("div", { className: "starter-row" }, emptyStarterBtns));
    return V3.dom.el("div", { className: "presets-empty-box", dataset: { role: "ocr-presets-empty" } }, children);
  }

  function buildPresetRow(preset, idx, ctx) {
    var isEnabled = !!preset.enabled;
    var switchLocked = !isEnabled && ctx.atLimit;
    var isEditing = ctx.editingPresetId === preset.id;

    var nameLine = [V3.dom.el("span", { className: "preset-name", text: preset.name || "" })];
    if (isEditing) nameLine.push(V3.dom.el("span", { className: "badge accent", text: "正在编辑" }));

    var upBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm btn-icon", disabled: idx === 0 || ctx.saving,
      title: "上移顺位", "aria-label": "上移 " + (preset.name || ""), dataset: { presetAct: "up", presetId: preset.id },
      onClick: function () { if (!ctx.saving) ctx.onMove(idx, -1, preset.id); }
    }, [V3.dom.icon("chevron-up", "ic ic-xs")]);

    var downBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm btn-icon", disabled: idx === ctx.total - 1 || ctx.saving,
      title: "下移顺位", "aria-label": "下移 " + (preset.name || ""), dataset: { presetAct: "down", presetId: preset.id },
      onClick: function () { if (!ctx.saving) ctx.onMove(idx, 1, preset.id); }
    }, [V3.dom.icon("chevron-down", "ic ic-xs")]);

    var editBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", disabled: ctx.saving, dataset: { presetAct: "edit", presetId: preset.id },
      onClick: function () { if (!ctx.saving) ctx.onEdit(preset); }
    }, [V3.dom.icon("edit", "ic ic-xs"), document.createTextNode("编辑")]);

    var deleteBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm danger-outline", disabled: ctx.saving, dataset: { presetAct: "delete", presetId: preset.id },
      onClick: function () { if (!ctx.saving) ctx.onDelete(preset); }
    }, [V3.dom.icon("trash", "ic ic-xs"), document.createTextNode("删除")]);

    var toggleAttrs = {
      type: "button", className: "switch", role: "switch",
      "aria-checked": isEnabled ? "true" : "false", "aria-label": "启用或停用 " + (preset.name || ""),
      disabled: ctx.saving || switchLocked,
      title: switchLocked ? ("最多同时启用 " + ctx.maxEn + " 个预设，请先停用其他预设") : "切换启用状态",
      dataset: { presetAct: "toggle", presetId: preset.id },
      onClick: function () { if (!ctx.saving && !switchLocked) ctx.onToggle(preset); }
    };
    if (switchLocked) toggleAttrs["aria-disabled"] = "true";

    return V3.dom.el("div", {
      className: "preset-row" + (isEditing ? " editing" : ""), dataset: { presetId: preset.id }
    }, [
      V3.dom.el("div", { className: "preset-rank" }, [
        V3.dom.el("span", { className: ctx.rank ? "badge accent" : "badge", text: ctx.rank ? ("顺位 " + ctx.rank) : "未启用" })
      ]),
      V3.dom.el("div", { className: "preset-info" }, [
        V3.dom.el("div", { className: "preset-name-line" }, nameLine),
        V3.dom.el("div", { className: "preset-chips" }, paramChipLabels(preset).map(function (txt) {
          return V3.dom.el("span", { className: "param-chip", text: txt });
        }))
      ]),
      V3.dom.el("div", { className: "preset-controls" }, [
        upBtn, downBtn, editBtn, deleteBtn,
        V3.dom.el("div", { className: "preset-toggle-wrap" }, [
          V3.dom.el("span", { className: "preset-limit-tag", text: isEnabled ? "已启用" : (switchLocked ? "已达上限" : "已停用") }),
          V3.dom.el("button", toggleAttrs)
        ])
      ])
    ]);
  }

  /* 下半区「全局预处理预设列表」纯 UI 卡片组件。 */
  function createPresetsCard(options) {
    var opts = options || {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var confirmDialog = opts.confirm || null;
    var onNewPreset = typeof opts.onNewPreset === "function" ? opts.onNewPreset : function () {};
    var onApplyStarter = typeof opts.onApplyStarter === "function" ? opts.onApplyStarter : function () {};
    var onEditPreset = typeof opts.onEditPreset === "function" ? opts.onEditPreset : function () {};
    var onMutatePresets = typeof opts.onMutatePresets === "function" ? opts.onMutatePresets : function () {};

    var lastSig = "";

    var quotaBadgeEl = V3.dom.el("span", { className: "badge", dataset: { role: "ocr-quota-badge" }, text: "已启用 0 / 3" });
    var quotaHintEl = V3.dom.el("div", {
      className: "hint", dataset: { role: "ocr-quota-hint" },
      text: "当前未启用预处理预设，截屏翻译、纯 OCR 与工作台图片 OCR 均按默认原图识别。"
    });
    var newPresetBtn = V3.dom.el("button", {
      type: "button", className: "btn btn-sm", dataset: { role: "ocr-new-preset" },
      onClick: function () { onNewPreset(); }
    }, [V3.dom.icon("plus", "ic ic-xs"), document.createTextNode("新建预设")]);

    var presetsErrBarEl = V3.dom.el("div", { className: "presets-err-bar", role: "alert", dataset: { role: "ocr-presets-error" }, hidden: true });
    var presetsListBodyEl = V3.dom.el("div", { dataset: { role: "ocr-presets-list" } });

    var boxEl = V3.dom.el("div", { className: "studio-box", dataset: { role: "ocr-presets-box" } }, [
      V3.dom.el("div", { className: "presets-card-head" }, [
        V3.dom.el("div", { className: "presets-head-left" }, [
          V3.dom.el("div", { className: "presets-title-line" }, [V3.dom.el("h2", { text: "全局预处理预设列表" }), quotaBadgeEl]),
          quotaHintEl
        ]),
        newPresetBtn
      ]),
      presetsErrBarEl,
      presetsListBodyEl,
      V3.dom.el("div", { className: "presets-foot-bar" }, [
        V3.dom.el("span", { text: "顺序规则：按启用顺位 1 → 2 → 3 依次预处理并识别，质量达标即早停；全部未达标时自动补跑默认原图并择优返回。" }),
        V3.dom.el("span", { className: "mono", text: "全局持久化配置 · 截屏翻译 / 纯 OCR / 工作台共用" })
      ])
    ]);

    function currentPresets() {
      var st = getState();
      return (st && Array.isArray(st.ocr_preprocess_presets)) ? st.ocr_preprocess_presets : [];
    }

    function handleMove(idx, delta, targetId) {
      var list = currentPresets();
      var targetIdx = idx + delta;
      if (targetIdx < 0 || targetIdx >= list.length) return;
      var next = list.map(function (p) { return Object.assign({}, p); });
      var tmp = next[targetIdx];
      next[targetIdx] = next[idx];
      next[idx] = tmp;
      onMutatePresets(next, "reorder", targetId);
    }

    function handleToggle(preset) {
      var next = currentPresets().map(function (p) {
        return (!p || p.id !== preset.id) ? Object.assign({}, p) : Object.assign({}, p, { enabled: !p.enabled });
      });
      onMutatePresets(next, "toggle", preset.id);
    }

    function handleDelete(preset) {
      function doDelete() {
        var next = currentPresets()
          .filter(function (p) { return p && p.id !== preset.id; })
          .map(function (p) { return Object.assign({}, p); });
        onMutatePresets(next, "delete", preset.id);
      }
      if (confirmDialog && typeof confirmDialog.open === "function") {
        confirmDialog.open({
          title: "删除预设「" + (preset.name || "") + "」？",
          warning: "删除后将同步从本地配置移除，截屏翻译、纯 OCR 与工作台图片 OCR 将不再使用该预设。",
          cancelLabel: "取消",
          confirmLabel: "确认删除",
          onConfirm: doDelete
        });
      } else {
        doDelete();
      }
    }

    function render(state, uiCtx) {
      var oc = (state && state.ocr) || {};
      var presets = (state && Array.isArray(state.ocr_preprocess_presets)) ? state.ocr_preprocess_presets : [];
      var starters = (state && Array.isArray(state.ocrStarterTemplates)) ? state.ocrStarterTemplates : [];
      var saving = oc.savingPresets === true;
      var maxEn = maxEnabledPresets();
      var enCount = enabledCountOf(presets);
      var atLimit = enCount >= maxEn;
      var editingPresetId = uiCtx ? uiCtx.editingPresetId : null;
      var lastSaveSource = uiCtx ? uiCtx.lastSaveSource : "form";

      quotaBadgeEl.className = "badge" + (enCount === 0 ? "" : (atLimit ? " warn" : " accent"));
      quotaBadgeEl.textContent = "已启用 " + enCount + " / " + maxEn + (atLimit ? " · 已达上限" : "");
      quotaHintEl.textContent = enCount === 0
        ? "当前未启用预处理预设，截屏翻译、纯 OCR 与工作台图片 OCR 均按默认原图识别。"
        : (atLimit
            ? ("已达启用上限（最多 " + maxEn + " 条）：按 1→" + enCount + " 依次识别，质量达标即停，全未达标自动补跑原图择优；启用其他预设前请先停用一条。")
            : ("按 1→" + enCount + " 依次识别，质量达标即停，全部未达标时自动补跑默认原图并择优；全局生效于截屏翻译、纯 OCR 与工作台图片 OCR。"));

      newPresetBtn.disabled = saving;
      var listErr = (lastSaveSource === "list" && oc.presetsSaveError) ? String(oc.presetsSaveError) : "";
      presetsErrBarEl.hidden = !listErr;
      presetsErrBarEl.textContent = listErr;

      var sig = JSON.stringify({
        presets: presets,
        editingPresetId: editingPresetId,
        saving: saving,
        starters: starters.map(function (t) { return (t.key || "") + ":" + (t.label || ""); })
      });
      if (sig === lastSig) return;
      lastSig = sig;
      V3.dom.empty(presetsListBodyEl);

      if (!presets.length) {
        presetsListBodyEl.appendChild(buildEmptyPresetsBox(starters, saving, onApplyStarter));
        return;
      }

      var rankCounter = 0;
      presets.forEach(function (preset, idx) {
        var rank = preset && preset.enabled ? (++rankCounter) : null;
        presetsListBodyEl.appendChild(buildPresetRow(preset, idx, {
          rank: rank,
          total: presets.length,
          maxEn: maxEn,
          atLimit: atLimit,
          saving: saving,
          editingPresetId: editingPresetId,
          onMove: handleMove,
          onEdit: onEditPreset,
          onDelete: handleDelete,
          onToggle: handleToggle
        }));
      });
    }

    function resetCache() {
      lastSig = "";
    }

    return { el: boxEl, render: render, resetCache: resetCache };
  }

  V3.ocrPresets = {
    DEFAULT_PARAMS: DEFAULT_PARAMS,
    maxEnabledPresets: maxEnabledPresets,
    enabledCountOf: enabledCountOf,
    cloneParams: cloneParams,
    generatePresetId: generatePresetId,
    buildNextPresetsForSave: buildNextPresetsForSave,
    extractImageFile: extractImageFile,
    paramChipLabels: paramChipLabels,
    formatAttemptLabel: formatAttemptLabel,
    summarizeOcrMeta: summarizeOcrMeta,
    createPresetsCard: createPresetsCard
  };
})(window.V3 = window.V3 || {});

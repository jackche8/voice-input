/* 设置「模型与服务」子标签纯 UI：两个模型的就绪状态、一键装齐五步进度与单按钮、看护组件状态与「重试」、开机自启开关。
 * 持久 DOM 原地更新：一键装齐进行中每秒重读状态时，步骤行、进度槽、按钮与开关均不销毁重建，流动动画不被重置、点击不会被吞掉。
 * 只呈现业务层给出的状态，不发请求、不推算进度；动作经 invoke(钩子名, 参数) 交给业务层。
 */
(function (V3) {
  "use strict";

  var STEP_BADGE = { running: "warn", skipped: "ok", done: "ok", failed: "danger" };
  var COMPONENT_BADGE = { running: "ok", starting: "warn", backing_off: "warn", stopped: "danger" };

  function isConnected(state) {
    return state.connection === "connected";
  }

  function labelOf(mapName, value) {
    return V3.labels.labelOf(V3.labels[mapName], value);
  }

  function badge(tone, text) {
    return V3.dom.el("span", { className: "badge" + (tone ? " " + tone : ""), text: text });
  }

  function setBadge(el, tone, text) {
    var cls = "badge" + (tone ? " " + tone : "");
    if (el.className !== cls) el.className = cls;
    if (el.textContent !== text) el.textContent = text;
  }

  function sub(text, isError) {
    return V3.dom.el("div", { className: "row-sub" + (isError ? " err" : ""), text: text });
  }

  function validPercent(progress) {
    if (!progress || typeof progress.percent !== "number" || !isFinite(progress.percent)) return null;
    return Math.max(0, Math.min(100, progress.percent));
  }

  function formatProgressLabel(progress, title) {
    var head = title || "进行中";
    var percent = validPercent(progress);
    var received = progress.receivedLabel || "";
    var total = progress.totalLabel || "";
    var bits = [];
    if (received && total) bits.push(received + " / " + total);
    else if (received) bits.push("已写入 " + received);
    else if (total) bits.push("0 B / " + total);
    if (percent != null) bits.push(Math.round(percent) + "%");
    if (bits.length) return head + " · " + bits.join(" · ");
    return head.slice(-1) === "…" ? head : head + "…";
  }

  /* 持久进度槽：单一 .row-sub + .progress > i 原地切换文本/确定态/不确定流动态，不销毁或重挂节点。 */
  function createProgressSlot() {
    var textEl = V3.dom.el("div", { className: "row-sub", hidden: true });
    var fillEl = V3.dom.el("i");
    var barEl = V3.dom.el("div", { className: "progress", role: "progressbar", hidden: true }, [fillEl]);

    function resetBar() {
      barEl.hidden = true;
      barEl.classList.remove("indeterminate");
      barEl.removeAttribute("aria-label");
      barEl.removeAttribute("aria-valuemin");
      barEl.removeAttribute("aria-valuemax");
      barEl.removeAttribute("aria-valuenow");
      barEl.removeAttribute("aria-valuetext");
      if (fillEl.style.width) fillEl.style.width = "";
    }

    function showText(text, isError) {
      var str = text || "";
      var cls = "row-sub" + (isError ? " err" : "");
      if (textEl.className !== cls) textEl.className = cls;
      if (textEl.textContent !== str) textEl.textContent = str;
      textEl.hidden = !str;
      resetBar();
    }

    function showProgress(progress, title) {
      var label = formatProgressLabel(progress, title);
      var percent = validPercent(progress);
      if (textEl.className !== "row-sub") textEl.className = "row-sub";
      if (textEl.textContent !== label) textEl.textContent = label;
      textEl.hidden = false;
      barEl.hidden = false;
      barEl.setAttribute("aria-label", label);
      if (percent != null) {
        barEl.classList.remove("indeterminate");
        barEl.removeAttribute("aria-valuetext");
        barEl.setAttribute("aria-valuemin", "0");
        barEl.setAttribute("aria-valuemax", "100");
        barEl.setAttribute("aria-valuenow", String(Math.round(percent)));
        fillEl.style.width = percent + "%";
      } else {
        barEl.classList.add("indeterminate");
        barEl.removeAttribute("aria-valuemin");
        barEl.removeAttribute("aria-valuemax");
        barEl.removeAttribute("aria-valuenow");
        barEl.setAttribute("aria-valuetext", "进度未知");
        if (fillEl.style.width) fillEl.style.width = "";
      }
    }

    return {
      textEl: textEl,
      barEl: barEl,
      fillEl: fillEl,
      showText: showText,
      showProgress: showProgress
    };
  }

  /* ---------- 两个模型行（无动作按钮，缺什么由一键装齐补齐） ---------- */
  function applyModelBadge(el, status) {
    if (status === "ready") setBadge(el, "ok", "已就绪");
    else if (status === "downloading") setBadge(el, "warn", "下载中");
    else if (status === "failed") setBadge(el, "danger", "失败");
    else if (status === "missing") setBadge(el, "warn", "未就绪");
    else setBadge(el, "", "未读取");
  }

  function updateAsrBadge(el, asr, connected) {
    if (!connected) { applyModelBadge(el, "unknown"); return; }
    if (asr.status === "ready" && asr.sidecarRunning === true) { applyModelBadge(el, "ready"); return; }
    if (asr.status === "ready") { setBadge(el, "warn", "未启动"); return; }
    applyModelBadge(el, asr.status);
  }

  function updateOllamaBadge(el, ollama, connected) {
    if (!connected) { applyModelBadge(el, "unknown"); return; }
    if (ollama.modelStatus === "downloading") { applyModelBadge(el, "downloading"); return; }
    if (ollama.modelStatus === "failed") { applyModelBadge(el, "failed"); return; }
    if (ollama.installed === false) { applyModelBadge(el, "missing"); return; }
    if (ollama.running === false) { setBadge(el, "warn", "未运行"); return; }
    if (ollama.modelStatus === "ready" && ollama.running === true) { applyModelBadge(el, "ready"); return; }
    applyModelBadge(el, ollama.modelStatus);
  }

  function updateAsrSub(slot, asr, connected) {
    if (!connected) { slot.showText("尚未读取状态", false); return; }
    if (asr.status === "downloading") {
      if (asr.progress) slot.showProgress(asr.progress, "下载中");
      else slot.showText("下载中…", false);
      return;
    }
    if (asr.status === "failed") { slot.showText(asr.message || "下载或加载失败", true); return; }
    if (asr.status === "missing") { slot.showText(asr.message || "尚未下载", false); return; }
    if (asr.status === "ready") {
      var bits = [asr.sidecarRunning === true ? "已下载" : (asr.message || "识别服务未启动")];
      if (asr.sizeLabel) bits.push(asr.sizeLabel);
      if (asr.deviceLabel) bits.push(asr.deviceLabel);
      slot.showText(bits.join(" · "), false);
      return;
    }
    slot.showText("检测中…", false);
  }

  function updateOllamaSub(slot, ollama, connected) {
    if (!connected) { slot.showText("尚未读取状态", false); return; }
    if (ollama.modelStatus === "downloading") {
      if (ollama.progress) slot.showProgress(ollama.progress, "下载中");
      else slot.showText("下载中…", false);
      return;
    }
    if (ollama.installed === false) { slot.showText(ollama.message || "未安装 Ollama，纠错与翻译不可用", true); return; }
    if (ollama.running === false) { slot.showText(ollama.message || "Ollama 已安装但未运行，润色与截图翻译不可用", true); return; }
    if (ollama.modelStatus === "failed") { slot.showText(ollama.message || "模型不可用", true); return; }
    if (ollama.modelStatus === "missing") { slot.showText(ollama.message || "模型尚未下载", false); return; }
    if (ollama.modelStatus === "ready" && ollama.running === true) {
      var bits = [ollama.version ? ("Ollama " + ollama.version + " 运行中") : "Ollama 运行中"];
      bits.push(ollama.sizeLabel ? ("模型已下载 " + ollama.sizeLabel) : "模型已就绪");
      slot.showText(bits.join(" · "), false);
      return;
    }
    slot.showText("检测中…", false);
  }

  /* ---------- 一键装齐五步 ---------- */
  function updateStepSub(slot, step) {
    if (step.state === "running") {
      if (step.progress) slot.showProgress(step.progress, step.message || "进行中");
      else slot.showText(step.message || "进行中…", false);
      return;
    }
    if (step.state === "failed") { slot.showText(step.message || "失败", true); return; }
    slot.showText(step.message || "", false);
  }

  function installButtonText(installAll) {
    if (installAll && installAll.state === "running") return "安装中…";
    if (installAll && installAll.state === "failed") return "从此步重试";
    return "一键装齐";
  }

  /* ---------- 看护组件 ---------- */
  function componentSubText(item) {
    if (item.state === "backing_off") {
      return "连续失败 " + item.failures + " 次，稍后自动重启" + (item.reason ? "：" + item.reason : "");
    }
    if (item.state === "stopped") return item.reason || "已停止自动重启";
    if (item.state === "not_installed") return item.reason || "尚未安装，可用「一键装齐」安装";
    return "";
  }

  function createModelsView(options) {
    var invoke = options.invoke;

    var asrSubSlot = createProgressSlot();
    var asrBadgeEl = badge("", "");
    var ollamaSubSlot = createProgressSlot();
    var ollamaBadgeEl = badge("", "");
    var aboutEl = V3.dom.el("p", { className: "setup-about", hidden: true });

    var stepsEmpty = V3.dom.el("div", { className: "empty" });
    var stepsEl = V3.dom.el("div", { className: "rows" }, [stepsEmpty]);
    var stepRows = {};
    var installBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary",
      dataset: { role: "install-all" },
      disabled: true,
      text: "一键装齐",
      onClick: function () { invoke("onInstallAll"); }
    });

    var componentsEmpty = V3.dom.el("div", { className: "empty" });
    var autostartSwitch = V3.dom.el("button", {
      type: "button",
      className: "switch",
      role: "switch",
      "aria-checked": "mixed",
      "aria-label": "开机自启",
      disabled: true,
      onClick: function () {
        invoke("onToggleAutostart", autostartSwitch.getAttribute("aria-checked") !== "true");
      }
    });
    var autostartRow = V3.dom.el("div", { className: "row" }, [
      V3.dom.el("div", { className: "row-main" }, [
        V3.dom.el("div", { className: "row-title", text: "开机自启" }),
        sub("登录系统后自动启动本软件，后台服务随之运行")
      ]),
      autostartSwitch
    ]);
    var serviceRowsEl = V3.dom.el("div", { className: "rows" }, [componentsEmpty, autostartRow]);
    var componentRows = {};

    function modelRow(title, modelName, subSlot, badgeEl) {
      return V3.dom.el("div", { className: "row" }, [
        V3.dom.el("div", { className: "row-main" }, [
          V3.dom.el("div", { className: "row-title" }, [
            document.createTextNode(title + " "),
            V3.dom.el("span", { className: "mono muted", text: modelName })
          ]),
          subSlot.textEl,
          subSlot.barEl
        ]),
        badgeEl
      ]);
    }

    /* 三个分区直接挂进设置面板（.setup-pane 以 gap 排布分区间距）。 */
    var sections = [
      V3.dom.el("div", { className: "setup-section" }, [
        V3.dom.el("h2", { text: "两个本地模型与服务" }),
        V3.dom.el("div", { className: "rows" }, [
          modelRow("语音识别", V3.labels.FIXED_MODELS.asr, asrSubSlot, asrBadgeEl),
          modelRow("纠错与翻译", V3.labels.FIXED_MODELS.ollama, ollamaSubSlot, ollamaBadgeEl)
        ]),
        V3.dom.el("p", {
          className: "hint",
          text: "模型固定，不提供切换。缺少的组件用下方「一键装齐」安装。没有进度数据时不会显示百分比。"
        })
      ]),
      V3.dom.el("div", { className: "setup-section" }, [
        V3.dom.el("h2", { text: "一键装齐" }),
        V3.dom.el("p", {
          className: "hint",
          text: "按顺序安装 Ollama 程序、纠错与翻译模型、语音识别运行环境与模型。已具备的步骤自动跳过；某步失败即停在该步，可从该步重试。"
        }),
        stepsEl,
        V3.dom.el("div", { className: "actions" }, [installBtn])
      ]),
      V3.dom.el("div", { className: "setup-section" }, [
        V3.dom.el("h2", { text: "后台服务" }),
        V3.dom.el("p", { className: "hint", text: "后台服务由本软件启动并看护；已停止的服务可在此重试。" }),
        serviceRowsEl,
        aboutEl
      ])
    ];

    function ensureStepRow(kind, anchor) {
      if (stepRows[kind]) return stepRows[kind];
      var parts = {
        slot: createProgressSlot(),
        badge: badge("", "")
      };
      parts.row = V3.dom.el("div", { className: "row", dataset: { step: kind } }, [
        V3.dom.el("div", { className: "row-main" }, [
          V3.dom.el("div", { className: "row-title", text: labelOf("INSTALL_STEP", kind) }),
          parts.slot.textEl,
          parts.slot.barEl
        ]),
        parts.badge
      ]);
      stepsEl.insertBefore(parts.row, anchor || stepsEmpty);
      stepRows[kind] = parts;
      return parts;
    }

    function componentRow(name) {
      if (componentRows[name]) return componentRows[name];
      var parts = {
        sub: sub(""),
        badge: badge("", ""),
        retry: V3.dom.el("button", {
          type: "button",
          className: "btn",
          hidden: true,
          text: "重试",
          onClick: function () { invoke("onRetryComponent", name); }
        })
      };
      parts.row = V3.dom.el("div", { className: "row", dataset: { component: name } }, [
        V3.dom.el("div", { className: "row-main" }, [
          V3.dom.el("div", { className: "row-title", text: labelOf("COMPONENT", name) }),
          parts.sub
        ]),
        parts.badge,
        parts.retry
      ]);
      serviceRowsEl.insertBefore(parts.row, autostartRow);
      componentRows[name] = parts;
      return parts;
    }

    function updateInstall(installAll, connected) {
      var steps = (installAll && Array.isArray(installAll.steps)) ? installAll.steps : [];
      stepsEmpty.hidden = steps.length > 0;
      stepsEmpty.textContent = connected ? "未读到安装进度。" : "尚未读取安装状态。";
      var seen = {};
      for (var i = 0; i < steps.length; i += 1) {
        var step = steps[i];
        var parts = stepRows[step.kind];
        if (!parts) {
          var anchor = stepsEmpty;
          for (var j = i + 1; j < steps.length; j += 1) {
            if (stepRows[steps[j].kind]) {
              anchor = stepRows[steps[j].kind].row;
              break;
            }
          }
          parts = ensureStepRow(step.kind, anchor);
        }
        seen[step.kind] = true;
        parts.row.hidden = false;
        updateStepSub(parts.slot, step);
        setBadge(parts.badge, STEP_BADGE[step.state], labelOf("INSTALL_STEP_STATE", step.state));
      }
      Object.keys(stepRows).forEach(function (kind) {
        if (!seen[kind]) {
          stepRows[kind].row.hidden = true;
          stepRows[kind].slot.showText("", false);
        }
      });
      installBtn.textContent = installButtonText(installAll);
      installBtn.disabled = !connected || !installAll || installAll.state === "running";
    }

    function updateComponents(list, connected, installAll) {
      componentsEmpty.hidden = !!(list && list.length);
      componentsEmpty.textContent = list ? "看护层尚未上报服务状态。" : "尚未读取服务状态。";
      /* 一键装齐运行中可能正在替换 ASR 运行环境，隐藏并禁用 asr 重试以防误点撞锁。 */
      var installRunning = !!(installAll && installAll.state === "running");
      var seen = {};
      (list || []).forEach(function (item) {
        var parts = componentRow(item.name);
        var text = componentSubText(item);
        var blockAsrRetry = item.name === "asr" && installRunning;
        seen[item.name] = true;
        parts.row.hidden = false;
        setBadge(parts.badge, COMPONENT_BADGE[item.state], labelOf("COMPONENT_STATE", item.state));
        parts.sub.className = "row-sub" + (item.state === "stopped" ? " err" : "");
        parts.sub.textContent = text;
        parts.sub.hidden = !text;
        parts.retry.hidden = item.state !== "stopped" || blockAsrRetry;
        parts.retry.disabled = !connected || blockAsrRetry;
      });
      Object.keys(componentRows).forEach(function (name) {
        if (!seen[name]) componentRows[name].row.hidden = true;
      });
    }

    function update(state) {
      var connected = isConnected(state);
      updateAsrSub(asrSubSlot, state.asr || {}, connected);
      updateAsrBadge(asrBadgeEl, state.asr || {}, connected);
      updateOllamaSub(ollamaSubSlot, state.ollama || {}, connected);
      updateOllamaBadge(ollamaBadgeEl, state.ollama || {}, connected);
      aboutEl.hidden = !state.aboutText;
      aboutEl.textContent = state.aboutText || "";
      updateInstall(state.installAll, connected);
      updateComponents(state.components, connected, state.installAll);
      var auto = state.autostart_enabled;
      var known = auto === true || auto === false;
      autostartSwitch.setAttribute("aria-checked", known ? String(auto) : "mixed");
      autostartSwitch.disabled = !connected || !known;
    }

    return { sections: sections, update: update };
  }

  V3.setupModels = { create: createModelsView };
})(window.V3 = window.V3 || {});

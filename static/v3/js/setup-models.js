/* 设置「模型与服务」子标签纯 UI：两个模型的就绪状态、一键装齐五步进度与单按钮、看护组件状态与「重试」、开机自启开关。
 * 持久 DOM 原地更新：一键装齐进行中每秒重读状态时，按钮与开关不被销毁重建，点击不会被吞掉。
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

  function progressNode(progress, title) {
    if (!progress) return null;
    var percent = typeof progress.percent === "number" ? progress.percent : null;
    var received = progress.receivedLabel || "";
    var total = progress.totalLabel || "";
    var bits = [];
    if (received || total) bits.push((received || "?") + " / " + (total || "?"));
    if (percent != null) bits.push(Math.round(percent) + "%");
    var label = bits.length ? title + " " + bits.join(" · ") : title + "…";
    var bar = V3.dom.el("div", { className: "progress", role: "progressbar", "aria-label": label });
    if (percent != null) {
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(Math.round(percent)));
      bar.appendChild(V3.dom.el("i", { style: "width:" + Math.max(0, Math.min(100, percent)) + "%" }));
    } else {
      bar.setAttribute("aria-valuetext", "进度未知");
      bar.appendChild(V3.dom.el("i", { style: "width:0" }));
    }
    return V3.dom.el("div", {}, [
      V3.dom.el("div", { className: "row-sub", text: label }),
      bar
    ]);
  }

  function sub(text, isError) {
    return V3.dom.el("div", { className: "row-sub" + (isError ? " err" : ""), text: text });
  }

  /* ---------- 两个模型行（无动作按钮，缺什么由一键装齐补齐） ---------- */
  function modelBadge(status) {
    if (status === "ready") return badge("ok", "已就绪");
    if (status === "downloading") return badge("warn", "下载中");
    if (status === "failed") return badge("danger", "失败");
    if (status === "missing") return badge("warn", "未就绪");
    return badge("", "未读取");
  }

  function asrBadge(asr, connected) {
    if (!connected) return modelBadge("unknown");
    if (asr.status === "ready" && asr.sidecarRunning === true) return modelBadge("ready");
    if (asr.status === "ready") return badge("warn", "未启动");
    return modelBadge(asr.status);
  }

  function ollamaBadge(ollama, connected) {
    if (!connected) return modelBadge("unknown");
    if (ollama.modelStatus === "downloading") return modelBadge("downloading");
    if (ollama.modelStatus === "failed") return modelBadge("failed");
    if (ollama.installed === false) return modelBadge("missing");
    if (ollama.running === false) return badge("warn", "未运行");
    if (ollama.modelStatus === "ready" && ollama.running === true) return modelBadge("ready");
    return modelBadge(ollama.modelStatus);
  }

  function asrSub(asr, connected) {
    if (!connected) return sub("尚未读取状态");
    if (asr.status === "downloading") return progressNode(asr.progress, "下载中") || sub("下载中…");
    if (asr.status === "failed") return sub(asr.message || "下载或加载失败", true);
    if (asr.status === "missing") return sub(asr.message || "尚未下载");
    if (asr.status === "ready") {
      var bits = [asr.sidecarRunning === true ? "已下载" : (asr.message || "识别服务未启动")];
      if (asr.sizeLabel) bits.push(asr.sizeLabel);
      if (asr.deviceLabel) bits.push(asr.deviceLabel);
      return sub(bits.join(" · "));
    }
    return sub("检测中…");
  }

  function ollamaSub(ollama, connected) {
    if (!connected) return sub("尚未读取状态");
    if (ollama.modelStatus === "downloading") return progressNode(ollama.progress, "下载中") || sub("下载中…");
    if (ollama.installed === false) return sub(ollama.message || "未安装 Ollama，纠错与翻译不可用", true);
    if (ollama.running === false) return sub(ollama.message || "Ollama 已安装但未运行，润色与截图翻译不可用", true);
    if (ollama.modelStatus === "failed") return sub(ollama.message || "模型不可用", true);
    if (ollama.modelStatus === "missing") return sub(ollama.message || "模型尚未下载");
    if (ollama.modelStatus === "ready" && ollama.running === true) {
      var bits = [ollama.version ? ("Ollama " + ollama.version + " 运行中") : "Ollama 运行中"];
      bits.push(ollama.sizeLabel ? ("模型已下载 " + ollama.sizeLabel) : "模型已就绪");
      return sub(bits.join(" · "));
    }
    return sub("检测中…");
  }

  /* ---------- 一键装齐五步 ---------- */
  function stepSub(step) {
    if (step.state === "running") {
      return progressNode(step.progress, step.message || "进行中") || sub(step.message || "进行中…");
    }
    if (step.state === "failed") return sub(step.message || "失败", true);
    return step.message ? sub(step.message) : null;
  }

  function stepRow(step) {
    return V3.dom.el("div", { className: "row", dataset: { step: step.kind } }, [
      V3.dom.el("div", { className: "row-main" }, [
        V3.dom.el("div", { className: "row-title", text: labelOf("INSTALL_STEP", step.kind) }),
        stepSub(step)
      ]),
      badge(STEP_BADGE[step.state], labelOf("INSTALL_STEP_STATE", step.state))
    ]);
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

    function slot(node) { return { node: node }; }
    function put(target, node) {
      target.node.parentNode.replaceChild(node, target.node);
      target.node = node;
    }

    var asrSubSlot = slot(sub(""));
    var asrBadgeSlot = slot(badge("", ""));
    var ollamaSubSlot = slot(sub(""));
    var ollamaBadgeSlot = slot(badge("", ""));
    var aboutEl = V3.dom.el("p", { className: "setup-about", hidden: true });

    var stepsEl = V3.dom.el("div", { className: "rows" });
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

    function modelRow(title, modelName, subSlot, badgeSlot) {
      return V3.dom.el("div", { className: "row" }, [
        V3.dom.el("div", { className: "row-main" }, [
          V3.dom.el("div", { className: "row-title" }, [
            document.createTextNode(title + " "),
            V3.dom.el("span", { className: "mono muted", text: modelName })
          ]),
          subSlot.node
        ]),
        badgeSlot.node
      ]);
    }

    /* 三个分区直接挂进设置面板（.setup-pane 以 gap 排布分区间距）。 */
    var sections = [
      V3.dom.el("div", { className: "setup-section" }, [
        V3.dom.el("h2", { text: "两个本地模型与服务" }),
        V3.dom.el("div", { className: "rows" }, [
          modelRow("语音识别", V3.labels.FIXED_MODELS.asr, asrSubSlot, asrBadgeSlot),
          modelRow("纠错与翻译", V3.labels.FIXED_MODELS.ollama, ollamaSubSlot, ollamaBadgeSlot)
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
      V3.dom.empty(stepsEl);
      if (!installAll || !installAll.steps.length) {
        stepsEl.appendChild(V3.dom.el("div", { className: "empty", text: connected ? "未读到安装进度。" : "尚未读取安装状态。" }));
      } else {
        installAll.steps.forEach(function (step) { stepsEl.appendChild(stepRow(step)); });
      }
      installBtn.textContent = installButtonText(installAll);
      installBtn.disabled = !connected || !installAll || installAll.state === "running";
    }

    function updateComponents(list, connected) {
      componentsEmpty.hidden = !!(list && list.length);
      componentsEmpty.textContent = list ? "看护层尚未上报服务状态。" : "尚未读取服务状态。";
      var seen = {};
      (list || []).forEach(function (item) {
        var parts = componentRow(item.name);
        var text = componentSubText(item);
        seen[item.name] = true;
        parts.row.hidden = false;
        parts.badge.className = "badge" + (COMPONENT_BADGE[item.state] ? " " + COMPONENT_BADGE[item.state] : "");
        parts.badge.textContent = labelOf("COMPONENT_STATE", item.state);
        parts.sub.className = "row-sub" + (item.state === "stopped" ? " err" : "");
        parts.sub.textContent = text;
        parts.sub.hidden = !text;
        parts.retry.hidden = item.state !== "stopped";
        parts.retry.disabled = !connected;
      });
      Object.keys(componentRows).forEach(function (name) {
        if (!seen[name]) componentRows[name].row.hidden = true;
      });
    }

    function update(state) {
      var connected = isConnected(state);
      put(asrSubSlot, asrSub(state.asr || {}, connected));
      put(asrBadgeSlot, asrBadge(state.asr || {}, connected));
      put(ollamaSubSlot, ollamaSub(state.ollama || {}, connected));
      put(ollamaBadgeSlot, ollamaBadge(state.ollama || {}, connected));
      aboutEl.hidden = !state.aboutText;
      aboutEl.textContent = state.aboutText || "";
      updateInstall(state.installAll, connected);
      updateComponents(state.components, connected);
      var auto = state.autostart_enabled;
      var known = auto === true || auto === false;
      autostartSwitch.setAttribute("aria-checked", known ? String(auto) : "mixed");
      autostartSwitch.disabled = !connected || !known;
    }

    return { sections: sections, update: update };
  }

  V3.setupModels = { create: createModelsView };
})(window.V3 = window.V3 || {});

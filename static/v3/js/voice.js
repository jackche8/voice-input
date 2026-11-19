/* 手机录音页纯 UI：非流式。只提供录音控制与状态展示，不录音、不上传、不入队。 */
(function (V3) {
  "use strict";

  function call(hooks, name, payload) {
    const fn = hooks && hooks[name];
    if (typeof fn === "function") fn(payload);
  }

  function connected(state) {
    return state.connection === "connected" && state.tokenValid !== false;
  }

  function phaseStatus(state) {
    if (state.statusText) {
      return { tone: state.statusTone || "", text: state.statusText };
    }
    if (state.connection === "disconnected") {
      return { tone: "", text: "未连接电脑。当前为纯界面，尚未接到业务层。" };
    }
    if (state.connection === "loading") {
      return { tone: "", text: "正在连接…" };
    }
    if (state.connection === "error") {
      return { tone: "err", text: V3.labels.formatError(state.error, state.message || "无法连接") };
    }
    if (state.tokenValid === false) {
      return { tone: "err", text: "访问令牌无效或已更新，请在电脑工作台「手机扫码语音」重新扫码进入。" };
    }
    const phase = state.phase || "idle";
    if (phase === "recording") {
      return { tone: "", text: "录音中" + (state.elapsed_label ? " " + state.elapsed_label : "") + " · 松开结束" };
    }
    if (phase === "uploading") return { tone: "", text: "正在提交录音…" };
    if (phase === "recognizing") return { tone: "", text: "识别中…" };
    if (phase === "polishing") return { tone: "", text: "润色中…" };
    if (phase === "waiting") return { tone: "", text: "等待电脑处理…" };
    if (phase === "queued" || state.job_state === "queued") return { tone: "", text: "已提交，等待处理" };
    if (phase === "claimed" || state.job_state === "claimed") return { tone: "", text: "电脑正在处理" };
    if (phase === "typed") return { tone: "ok", text: "已输入到电脑" };
    if (phase === "done") return { tone: "ok", text: "处理已完成" };
    if (phase === "polish_failed") {
      return { tone: "err", text: V3.labels.formatError(state.error || "polish_failed", state.message || "润色失败。原文未输入，可直接发送原文。") };
    }
    if (phase === "failed") {
      return { tone: "err", text: V3.labels.formatError(state.error, state.message || "处理失败") };
    }
    if (phase === "delivery_unknown" || state.job_state === "delivery_unknown") {
      return { tone: "warn", text: "投递结果未知，文字可能已经输入，请勿当成已经成功。" };
    }
    if (state.useFileUpload) return { tone: "", text: state.fileUploadHint || "当前不能使用网页录音，请用下方按钮选择录音文件。" };
    return { tone: "", text: "就绪。按住按钮说话，松开后提交整段录音。" };
  }

  function mountVoice(root, options) {
    const opts = options || {};
    const hooks = opts.hooks || {};
    let state = V3.defaults.merge(V3.defaults.voiceState(), opts.initialState);
    const confirm = V3.mountConfirm(root);
    let pressed = false;

    V3.ensureIcons();
    V3.dom.empty(root);

    const live = V3.dom.el("div", { className: "sr-only", "aria-live": "polite", id: "v3-voice-live" });
    const headBadge = V3.dom.el("span", { className: "badge" });
    const banner = V3.dom.el("div", { className: "banner danger", hidden: true, role: "alert" });
    const card = V3.dom.el("div", { className: "ph-card" });
    const histBox = V3.dom.el("div");
    const histSection = V3.dom.el("div", { className: "ph-hist" }, [
      V3.dom.el("div", { className: "ph-hist-head" }, [
        V3.dom.el("h2", { text: "最近处理记录 · 点一下再次发送" }),
        V3.dom.el("button", {
          type: "button",
          className: "linkbtn",
          text: "刷新",
          onClick: function () { call(hooks, "onRefreshHistory"); }
        })
      ]),
      histBox
    ]);

    const page = V3.dom.el("div", { className: "ph" }, [
      V3.dom.el("div", { className: "ph-head" }, [
        V3.dom.el("h1", { text: "语音录入" }),
        headBadge
      ]),
      banner,
      card,
      histSection,
      live
    ]);
    root.appendChild(page);

    function canRecord() {
      return connected(state) && state.micEnabled !== false && !state.useFileUpload &&
        state.phase !== "uploading" && state.phase !== "recognizing" &&
        state.phase !== "polishing" && state.phase !== "waiting" &&
        state.phase !== "queued" && state.phase !== "claimed";
    }

    function startRecord(ev) {
      if (!canRecord()) return;
      pressed = true;
      renderCard();
      call(hooks, "onRecordStart", ev && ev.type ? { via: ev.type } : {});
    }
    function endRecord(ev) {
      if (!pressed) return;
      pressed = false;
      renderCard();
      call(hooks, "onRecordEnd", ev && ev.type ? { via: ev.type } : {});
    }
    function cancelRecord() {
      if (!pressed) return;
      pressed = false;
      renderCard();
      call(hooks, "onRecordCancel");
    }

    function confirmResend(record, text, reason) {
      const unknown = reason === "delivery_unknown" || (record && record._delivery === "delivery_unknown");
      confirm.open({
        title: unknown ? "投递结果未知" : "再次发送",
        warning: unknown
          ? "此次文字可能已经输入。如果再次发送，可能会造成重复输入。确认框只记录你的选择，不会自行排队发送。"
          : "确认后只把选择交给业务层，界面不会自行入队。",
        text: text || "",
        confirmLabel: unknown ? "仍要发送" : "发送",
        cancelLabel: "取消",
        onConfirm: function () {
          call(hooks, "onConfirmResendChoice", {
            confirm: true,
            record: record,
            text: text || "",
            job_id: record && record.job_id,
            reason: reason || "resend"
          });
        },
        onCancel: function () {
          call(hooks, "onConfirmResendChoice", {
            confirm: false,
            record: record,
            text: text || "",
            job_id: record && record.job_id,
            reason: reason || "resend"
          });
        }
      });
    }

    function sendOriginal() {
      const text = state.raw_text || state.text || "";
      call(hooks, "onSendOriginal", {
        text: text,
        job_id: state.job_id,
        polish: state.polish
      });
    }

    /* 挂载时一次性创建卡片内固定节点（特别是 .mic 按钮），避免 setState 刷新计时时销毁重建丢失 PointerCapture。 */
    const uploadHint = V3.dom.el("p", {
      className: "hint muted",
      style: "font-size:var(--fs-sm);line-height:1.6"
    });
    const fileInput = V3.dom.el("input", {
      type: "file",
      accept: "audio/*",
      capture: "microphone",
      hidden: true,
      "aria-label": "选择录音文件"
    });
    fileInput.addEventListener("change", function () {
      const file = fileInput.files && fileInput.files[0];
      call(hooks, "onPickAudio", file ? { file: file, name: file.name, size: file.size, type: file.type } : { file: null });
      fileInput.value = "";
    });
    const uploadWrap = V3.dom.el("div", { hidden: true }, [
      uploadHint,
      V3.dom.el("label", { className: "upload" }, [
        V3.dom.icon("upload"),
        document.createTextNode("选择录音文件"),
        fileInput
      ])
    ]);

    const micLabelEl = V3.dom.el("span", { text: "按住说话" });
    const micBtn = V3.dom.el("button", {
      type: "button",
      className: "mic",
      "aria-pressed": "false",
      "aria-label": "按住说话"
    }, [
      V3.dom.icon("mic"),
      micLabelEl
    ]);
    micBtn.addEventListener("contextmenu", function (ev) {
      ev.preventDefault();
    });
    micBtn.addEventListener("pointerdown", function (ev) {
      if (ev.button != null && ev.button !== 0) return;
      ev.preventDefault();
      try { micBtn.setPointerCapture(ev.pointerId); } catch (err) {}
      startRecord(ev);
    });
    micBtn.addEventListener("pointerup", function (ev) {
      ev.preventDefault();
      try {
        if (micBtn.hasPointerCapture && micBtn.hasPointerCapture(ev.pointerId)) {
          micBtn.releasePointerCapture(ev.pointerId);
        }
      } catch (err) {}
      endRecord(ev);
    });
    micBtn.addEventListener("pointercancel", function () { cancelRecord(); });
    micBtn.addEventListener("keydown", function (ev) {
      if (ev.key === " " || ev.key === "Enter") {
        ev.preventDefault();
        if (!pressed) startRecord(ev);
      }
    });
    micBtn.addEventListener("keyup", function (ev) {
      if (ev.key === " " || ev.key === "Enter") {
        ev.preventDefault();
        endRecord(ev);
      }
    });

    const statusEl = V3.dom.el("div", { className: "ph-status", id: "v3-ph-status" });
    const resultSlot = V3.dom.el("div", { hidden: true });
    const keyKbd = V3.dom.el("kbd", { text: "空格" });
    const keyRow = V3.dom.el("div", { className: "ph-key" }, [
      document.createTextNode("或按住 "),
      keyKbd,
      document.createTextNode(" 说话 "),
      V3.dom.el("button", {
        type: "button",
        className: "linkbtn",
        text: "改快捷键",
        onClick: function () { call(hooks, "onStartChangeHotkey"); }
      })
    ]);

    card.appendChild(uploadWrap);
    card.appendChild(micBtn);
    card.appendChild(statusEl);
    card.appendChild(resultSlot);
    card.appendChild(keyRow);

    function renderCard() {
      const ok = connected(state);
      if (state.tokenValid === false) {
        banner.hidden = false;
        banner.textContent = "访问令牌无效或已更新，请在电脑工作台「手机扫码语音」重新扫码进入。";
      } else if (state.connection === "error" && (state.message || state.error)) {
        banner.hidden = false;
        if (typeof state.error === "string" && state.error && !Object.prototype.hasOwnProperty.call(V3.labels.ERROR_CODE, state.error)) {
          banner.textContent = (state.message && String(state.message) !== state.error)
            ? state.error + "：" + String(state.message)
            : state.error;
        } else {
          banner.textContent = V3.labels.formatError(state.error, state.message, state.details);
        }
      } else {
        banner.hidden = true;
      }

      if (ok) {
        headBadge.className = "badge ok";
        headBadge.textContent = "已连接电脑";
      } else if (state.connection === "loading") {
        headBadge.className = "badge";
        headBadge.textContent = "连接中";
      } else {
        headBadge.className = "badge danger";
        headBadge.textContent = "未连接";
      }

      const useUpload = !!state.useFileUpload;
      uploadWrap.hidden = !useUpload;
      micBtn.hidden = useUpload;
      keyRow.hidden = useUpload;

      if (useUpload) {
        uploadHint.textContent = state.fileUploadHint || "当前不是安全连接，浏览器不允许网页录音。请用下面按钮选择系统录音文件。选择后只通知业务层，本页不会自行上传。";
        fileInput.disabled = !ok;
      } else {
        const recording = state.phase === "recording" || pressed;
        const busy = state.phase === "uploading" || state.phase === "recognizing" ||
          state.phase === "polishing" || state.phase === "waiting" ||
          state.phase === "queued" || state.phase === "claimed";
        const nextClass = "mic" + (recording ? " rec" : "") + (pressed && !recording ? " pressed" : "");
        if (micBtn.className !== nextClass) micBtn.className = nextClass;
        const nextDisabled = !canRecord() && !recording;
        if (micBtn.disabled !== nextDisabled) micBtn.disabled = nextDisabled;
        micBtn.setAttribute("aria-pressed", recording ? "true" : "false");
        micBtn.setAttribute("aria-label", recording ? "松开结束录音" : "按住说话");
        const nextMicText = recording
          ? (state.elapsed_label ? "松开结束 " + state.elapsed_label : "松开结束")
          : (busy ? "处理中" : "按住说话");
        if (micLabelEl.textContent !== nextMicText) micLabelEl.textContent = nextMicText;
        keyKbd.textContent = state.capturingHotkey ? "请按新键" : (state.recordHotkeyLabel || "空格");
      }

      const st = phaseStatus(state);
      const nextStatusClass = "ph-status" + (st.tone ? " " + st.tone : "");
      if (statusEl.className !== nextStatusClass) statusEl.className = nextStatusClass;
      if (statusEl.textContent !== st.text) statusEl.textContent = st.text;
      if (live.textContent !== st.text) live.textContent = st.text;

      V3.dom.empty(resultSlot);
      if (state.phase === "typed" || state.phase === "done") {
        resultSlot.appendChild(resultBlock(state.text, {
          badge: V3.labels.polish(state.polish),
          badgeClass: V3.labels.polishBadgeClass(state.polish),
          jobState: state.job_state
        }));
      } else if (state.phase === "polish_failed") {
        resultSlot.appendChild(resultBlock(state.raw_text || state.text, {
          title: "识别原文（未输入）",
          badge: "润色失败",
          badgeClass: "danger",
          action: { label: "发送原文", onClick: sendOriginal }
        }));
      } else if (state.phase === "delivery_unknown" || state.job_state === "delivery_unknown") {
        resultSlot.appendChild(resultBlock(state.text || state.raw_text, {
          badge: "投递结果未知",
          badgeClass: "warn",
          action: {
            label: "确认处理",
            onClick: function () {
              confirmResend({ job_id: state.job_id, final_text: state.text, raw_text: state.raw_text }, state.text || state.raw_text, "delivery_unknown");
            }
          }
        }));
      } else if (state.phase === "failed" && (state.raw_text || state.text)) {
        resultSlot.appendChild(resultBlock(state.raw_text || state.text, {
          title: "识别原文",
          badge: "未完成",
          badgeClass: "danger"
        }));
      }
      resultSlot.hidden = !resultSlot.firstChild;
    }

    function resultBlock(text, meta) {
      const info = meta || {};
      const node = V3.dom.el("div", { className: "ph-result" });
      if (info.title) node.appendChild(V3.dom.el("div", { className: "label", text: info.title }));
      node.appendChild(document.createTextNode(text || "（无文本）"));
      const row = V3.dom.el("div", { className: "meta" });
      if (info.badge) row.appendChild(V3.dom.el("span", { className: "badge " + (info.badgeClass || ""), text: info.badge }));
      if (info.action) {
        row.appendChild(V3.dom.el("button", {
          type: "button",
          className: "btn primary",
          text: info.action.label,
          onClick: info.action.onClick
        }));
      }
      node.appendChild(row);
      return node;
    }

    function renderHistory() {
      const enableSelect = connected(state);
      V3.history.renderVoiceItems(histBox, state.history, {
        disabled: !enableSelect,
        emptyText: connected(state) ? "还没有处理记录" : "尚未读取处理记录。",
        onSelect: function (record, text) {
          confirmResend(record, text, "resend");
        }
      });
    }

    function isHotkey(ev) {
      const code = state.recordHotkeyCode || "Space";
      if (state.capturingHotkey) return false;
      if (ev.code && ev.code === code) return true;
      if (code === "Space" && ev.key === " ") return true;
      return ev.key && ev.key === code;
    }

    function onKeyDown(ev) {
      if (state.capturingHotkey) {
        ev.preventDefault();
        const label = ev.key === " " ? "空格" : (ev.key.length === 1 ? ev.key.toUpperCase() : ev.key);
        call(hooks, "onChangeHotkey", { code: ev.code || ev.key, label: label });
        return;
      }
      if (ev.repeat) return;
      if (ev.target && /input|textarea|select|button/i.test(ev.target.tagName) && ev.target.closest(".mic") == null) {
        return;
      }
      if (isHotkey(ev)) {
        ev.preventDefault();
        startRecord(ev);
      }
    }
    function onKeyUp(ev) {
      if (isHotkey(ev)) {
        ev.preventDefault();
        endRecord(ev);
      }
    }

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);

    function renderAll() {
      renderCard();
      renderHistory();
      const refresh = histSection.querySelector(".linkbtn");
      if (refresh) refresh.disabled = state.connection === "disconnected" && state.tokenValid !== true;
    }

    renderAll();

    return {
      setState: function (patch) {
        state = V3.defaults.merge(state, patch);
        renderAll();
      },
      getState: function () { return state; },
      confirm: confirm,
      destroy: function () {
        document.removeEventListener("keydown", onKeyDown);
        document.removeEventListener("keyup", onKeyUp);
        confirm.destroy();
        V3.dom.empty(root);
      }
    };
  }

  V3.mountVoice = mountVoice;
})(window.V3 = window.V3 || {});

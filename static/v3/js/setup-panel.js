/* 方案 C 内置设置面板纯 UI（6 个子标签：模型与服务 / 语音与润色 / 截图翻译 / 快捷键 / 开放接口 / 系统检测）。
 * 严守纯展示边界：不发起网络请求、不读写剪贴板或存储、不硬编码后端路径，所有动作经 hooks 回调通知业务层。
 * 子标签栏、「模型与服务」（setup-models.js）及语音/快捷键表单采用持久 DOM 原地更新，确保轮询或 blur 绝不吞点击、绝不丢输入草稿与焦点。
 */
(function (V3) {
  "use strict";

  var HOTKEY_ROWS = [
    { name: "hotkey_voice", label: "语音输入快捷键", title: "语音输入（按住说话）", sub: "" },
    { name: "hotkey_screenshot", label: "截图翻译快捷键", title: "截图翻译", sub: "红十字准星框选区域，译文半透明贴回原位" },
    { name: "hotkey_ocr", label: "纯 OCR 识别快捷键", title: "纯 OCR 识别", sub: "红十字准星框选区域，识别文字原位显示，菜单复制" },
    { name: "hotkey_repeat", label: "重复上次输入快捷键", title: "重复上次输入", sub: "润色失败或焦点切走时，用它补发" },
    { name: "hotkey_annotate", label: "截屏标注快捷键", title: "截屏标注", sub: "红十字准星框选区域，冻结标注后复制或保存" }
  ];

  function getTabs() {
    return (V3.labels && V3.labels.SETUP_TABS) || [];
  }

  function hasTab(id) {
    return getTabs().some(function (item) { return item.id === id; });
  }

  function call(hooks, name, payload) {
    var fn = hooks && hooks[name];
    if (typeof fn !== "function") return undefined;
    var res = fn(payload);
    if (res && typeof res.then === "function") {
      res.then(undefined, function () {});
    }
    return res;
  }

  function modelReady(model) {
    return !!(model && model.status === "ready" && model.sidecarRunning === true);
  }

  function ollamaReady(ollama) {
    return !!(ollama && ollama.modelStatus === "ready" && ollama.running === true);
  }

  function isConnected(state) {
    return state.connection === "connected";
  }

  function currentHotkey(state, name) {
    var drafts = state.hotkeyDrafts;
    if (drafts && Object.prototype.hasOwnProperty.call(drafts, name) && drafts[name] != null) {
      return drafts[name];
    }
    return state[name] || "";
  }

  function headerNotice(state) {
    state = state || {};
    if (state.connection === "disconnected") {
      return { tone: "muted", text: "未连接本地服务。当前为纯界面，尚未接到业务层。" };
    }
    if (state.connection === "loading") {
      return { tone: "muted", text: "正在读取状态…" };
    }
    if (state.connection === "error") {
      return { tone: "danger", text: "无法读取状态" };
    }
    var asr = state.asr || {};
    var ollama = state.ollama || {};
    if (
      asr.status === "failed" ||
      ollama.modelStatus === "failed" ||
      ollama.installed === false ||
      ollama.running === false ||
      asr.status === "missing" ||
      ollama.modelStatus === "missing" ||
      asr.status === "downloading" ||
      ollama.modelStatus === "downloading" ||
      (asr.status === "ready" && asr.sidecarRunning !== true)
    ) {
      return { tone: "warn", text: "有事项需要处理，完成前部分功能不可用" };
    }
    if (modelReady(asr) && ollamaReady(ollama)) {
      var voice = state.hotkey_voice;
      var shot = state.hotkey_screenshot;
      var ocr = state.hotkey_ocr;
      var annotate = state.hotkey_annotate;
      if (voice && shot && ocr && annotate) {
        return { tone: "muted", text: "全部就绪 · 按住 " + voice + " 说话，" + shot + " 截图翻译，" + ocr + " 纯 OCR 识别，" + annotate + " 截屏标注" };
      }
      if (voice && shot && ocr) {
        return { tone: "muted", text: "全部就绪 · 按住 " + voice + " 说话，" + shot + " 截图翻译，" + ocr + " 纯 OCR 识别" };
      }
      if (voice && shot) {
        return { tone: "muted", text: "全部就绪 · 按住 " + voice + " 说话，" + shot + " 截图翻译" };
      }
      return { tone: "muted", text: "全部就绪" };
    }
    return { tone: "muted", text: "已连接，模型状态尚未完整" };
  }

  function statusDotClass(state) {
    state = state || {};
    if (state.connection === "error") return "err";
    if (state.connection === "disconnected" || state.connection === "loading") return "";
    var asr = state.asr || {};
    var ollama = state.ollama || {};
    if (modelReady(asr) && ollamaReady(ollama)) return "ok";
    if (
      asr.status === "failed" ||
      ollama.modelStatus === "failed" ||
      ollama.installed === false ||
      ollama.running === false ||
      (asr.status === "ready" && asr.sidecarRunning !== true)
    ) {
      return "err";
    }
    if (
      asr.status === "missing" ||
      ollama.modelStatus === "missing" ||
      asr.status === "downloading" ||
      ollama.modelStatus === "downloading"
    ) {
      return "warn";
    }
    return "";
  }

  function createSetupPanel(container, options) {
    var opts = options || {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var patchState = typeof opts.patchState === "function" ? opts.patchState : function () {};
    var hooks = opts.hooks || {};
    var initTab = (getState() && getState().setupTab) || "models";
    var tab = hasTab(initTab) ? initTab : "models";
    var mountedTab = null;
    var ignoreHotkeyBlur = false;
    var lastSaveError = "";
    var destroyed = false;

    /* ---------- 持久子标签栏（原地更新，绝不销毁重建按钮） ---------- */
    var tabsEl = V3.dom.el("div", {
      className: "setup-tabs",
      role: "tablist",
      "aria-label": "设置分类"
    });
    var tabButtons = {};
    var modelsTabDot = V3.dom.el("span", { className: "dot", "aria-hidden": "true", hidden: true });
    getTabs().forEach(function (item) {
      var children = [document.createTextNode(item.label)];
      if (item.id === "models") children.push(modelsTabDot);
      var btn = V3.dom.el("button", {
        type: "button",
        role: "tab",
        "aria-selected": "false",
        dataset: { tab: item.id },
        onClick: function () { setTab(item.id); }
      }, children);
      tabButtons[item.id] = btn;
      tabsEl.appendChild(btn);
    });

    var paneEl = V3.dom.el("div", {
      className: "setup-pane",
      role: "tabpanel"
    });

    function onTabsKeyDown(ev) {
      var buttons = Array.prototype.slice.call(tabsEl.querySelectorAll("button[data-tab]"));
      var idx = buttons.indexOf(document.activeElement);
      if (idx < 0) return;
      var next = idx;
      if (ev.key === "ArrowRight" || ev.key === "ArrowDown") next = (idx + 1) % buttons.length;
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") next = (idx - 1 + buttons.length) % buttons.length;
      else if (ev.key === "Home") next = 0;
      else if (ev.key === "End") next = buttons.length - 1;
      else return;
      ev.preventDefault();
      buttons[next].focus();
    }

    tabsEl.addEventListener("keydown", onTabsKeyDown);
    V3.dom.empty(container);
    container.appendChild(tabsEl);
    container.appendChild(paneEl);

    /* ---------- 持久「模型与服务」视图（一键装齐进行中重读状态时不重建按钮） ---------- */
    var modelsView = V3.setupModels.create({
      invoke: function (name, payload) { return call(hooks, name, payload); }
    });

    /* ---------- 持久「语音与润色」视图（保护未保存关键词草稿与焦点） ---------- */
    var polishSwitch = V3.dom.el("button", {
      type: "button",
      className: "switch",
      role: "switch",
      "aria-checked": "mixed",
      "aria-label": "松开后纠错润色",
      disabled: true,
      onClick: function () {
        var cur = getState();
        if (!isConnected(cur)) return;
        call(hooks, "onTogglePolish", cur.polish_enabled !== true);
      }
    });
    var keywordsTextarea = V3.dom.el("textarea", {
      className: "field",
      rows: "3",
      disabled: true,
      "aria-label": "关键词",
      onInput: function () {
        patchState({ keywordsDraft: keywordsTextarea.value, keywordsSaved: false });
      }
    });
    var keywordsSavedEl = V3.dom.el("span", { className: "saved", text: "已保存", hidden: true });
    var keywordsSaveBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary",
      disabled: true,
      text: "保存",
      onClick: function () {
        call(hooks, "onSaveKeywords", keywordsTextarea.value);
      }
    });
    var voiceSection = V3.dom.el("div", { className: "setup-section" }, [
      V3.dom.el("h2", { text: "松开后的处理" }),
      V3.dom.el("div", { className: "rows" }, [
        V3.dom.el("div", { className: "row" }, [
          V3.dom.el("div", { className: "row-main" }, [
            V3.dom.el("div", { className: "row-title", text: "松开后纠错润色" }),
            V3.dom.el("div", {
              className: "row-sub",
              text: "只改错别字、同音词、专有名词、标点和口头禅，不改原意和语序。润色失败时不输入，可按重复键发送原文。"
            })
          ]),
          polishSwitch
        ]),
        V3.dom.el("div", { className: "row", style: "display:block" }, [
          V3.dom.el("div", { className: "row-title", text: "关键词" }),
          V3.dom.el("p", { className: "hint", text: "润色时按这里的写法修正专有名词。用逗号、顿号、空格或换行分隔。" }),
          keywordsTextarea,
          V3.dom.el("div", { className: "actions" }, [keywordsSavedEl, keywordsSaveBtn])
        ])
      ])
    ]);

    function updateVoice(state) {
      var connected = isConnected(state);
      var enabled = state.polish_enabled;
      var known = enabled === true || enabled === false;
      polishSwitch.setAttribute("aria-checked", known ? String(enabled) : "mixed");
      polishSwitch.disabled = !connected;
      keywordsTextarea.disabled = !connected;
      keywordsSaveBtn.disabled = !connected;
      keywordsSavedEl.hidden = !state.keywordsSaved;
      var savedText = Array.isArray(state.custom_keywords)
        ? state.custom_keywords.join("、")
        : (state.custom_keywords || "");
      var displayVal = state.keywordsDraft != null ? String(state.keywordsDraft) : savedText;
      if (keywordsTextarea.value !== displayVal) {
        keywordsTextarea.value = displayVal;
      }
    }

    /* ---------- 持久「快捷键」视图（原地更新，绝不在 blur/轮询时卸载控件） ---------- */
    function endHotkeyCapture(restoreSaved) {
      var cur = getState();
      var name = cur.hotkeyCapturing;
      var patch = { hotkeyCapturing: null, hotkeyCaptureError: "" };
      if (restoreSaved && name) {
        var drafts = Object.assign({}, cur.hotkeyDrafts || {});
        drafts[name] = null;
        var hasRemaining = Object.keys(drafts).some(function (k) { return drafts[k] != null; });
        patch.hotkeyDrafts = hasRemaining ? drafts : null;
        patch.hotkeyCaptureWarning = "";
        patch.hotkeyCaptureHint = "";
      }
      patchState(patch);
    }

    var keyInputs = {};
    var keyRowEls = HOTKEY_ROWS.map(function (spec) {
      var name = spec.name;
      var input = V3.dom.el("input", {
        className: "field key",
        type: "text",
        "aria-label": spec.label,
        spellcheck: "false",
        autocomplete: "off",
        readonly: true,
        disabled: true,
        "aria-describedby": "v3-hotkey-hint",
        onFocus: function () {
          var cur = getState();
          if (!isConnected(cur) || ignoreHotkeyBlur) return;
          if (cur.hotkeyCapturing === name && cur.hotkeyFocus === name) return;
          patchState({
            hotkeyCapturing: name,
            hotkeyFocus: name,
            hotkeyCaptureError: "",
            hotkeysSaved: false
          });
        },
        onBlur: function () {
          if (ignoreHotkeyBlur) return;
          var cur = getState();
          if (cur.hotkeyCapturing === name) {
            patchState({ hotkeyCapturing: null, hotkeyFocus: name });
          }
        },
        onKeyDown: function (ev) {
          var cur = getState();
          if (!isConnected(cur)) return;
          if (cur.hotkeyCapturing && cur.hotkeyCapturing !== name) return;
          var convert = V3.hotkey && typeof V3.hotkey.fromKeyboardEvent === "function"
            ? V3.hotkey.fromKeyboardEvent(ev)
            : { error: "快捷键转换模块未加载", consumed: false };
          if (convert.consumed) ev.preventDefault();
          if (convert.cancel) {
            endHotkeyCapture(true);
            return;
          }
          if (convert.navigate) {
            patchState({ hotkeyCapturing: null, hotkeyFocus: name });
            return;
          }
          if (convert.pending) {
            if (cur.hotkeyCapturing !== name) {
              patchState({ hotkeyCapturing: name, hotkeyFocus: name, hotkeyCaptureError: "" });
            }
            return;
          }
          if (convert.error) {
            patchState({ hotkeyCaptureError: convert.error, hotkeyFocus: name, hotkeyCapturing: name });
            return;
          }
          if (convert.value) {
            var drafts = Object.assign({}, cur.hotkeyDrafts || {});
            drafts[name] = convert.value;
            patchState({
              hotkeyDrafts: drafts,
              hotkeyCapturing: name,
              hotkeyFocus: name,
              hotkeyCaptureError: "",
              hotkeyCaptureWarning: convert.warning || "",
              hotkeyCaptureHint: convert.hint || "",
              hotkeysSaved: false,
              hotkeySaveError: ""
            });
          }
        }
      });
      input.dataset.hotkey = name;
      keyInputs[name] = input;
      var mainNode = spec.sub
        ? V3.dom.el("div", { className: "row-main" }, [
            V3.dom.el("div", { className: "row-title", text: spec.title }),
            V3.dom.el("div", { className: "row-sub", text: spec.sub })
          ])
        : V3.dom.el("div", { className: "row-main row-title", text: spec.title });
      return V3.dom.el("div", { className: "row" }, [mainNode, input]);
    });

    var keysMessagesEl = V3.dom.el("div", { hidden: true });
    var keysHintEl = V3.dom.el("span", {
      className: "hint",
      id: "v3-hotkey-hint",
      style: "margin:0 auto 0 0"
    });
    var keysSavedEl = V3.dom.el("span", { className: "saved", text: "已保存", hidden: true });
    var keysSaveBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary",
      disabled: true,
      text: "保存快捷键",
      onClick: function () {
        lastSaveError = "";
        var cur = getState();
        call(hooks, "onSaveHotkeys", {
          hotkey_voice: currentHotkey(cur, "hotkey_voice"),
          hotkey_screenshot: currentHotkey(cur, "hotkey_screenshot"),
          hotkey_ocr: currentHotkey(cur, "hotkey_ocr"),
          hotkey_repeat: currentHotkey(cur, "hotkey_repeat"),
          hotkey_annotate: currentHotkey(cur, "hotkey_annotate")
        });
      }
    });
    var keysSection = V3.dom.el("div", { className: "setup-section" }, [
      V3.dom.el("h2", { text: "五个全局快捷键" }),
      V3.dom.el("div", { className: "rows" }, keyRowEls),
      keysMessagesEl,
      V3.dom.el("div", { className: "actions" }, [keysHintEl, keysSavedEl, keysSaveBtn])
    ]);

    function updateKeys(state, isSetupVisible) {
      var connected = isConnected(state);
      HOTKEY_ROWS.forEach(function (spec) {
        var input = keyInputs[spec.name];
        input.disabled = !connected;
        var nextVal = currentHotkey(state, spec.name);
        if (input.value !== nextVal) input.value = nextVal;
        if (state.hotkeyCapturing === spec.name) input.setAttribute("data-capturing", "true");
        else input.removeAttribute("data-capturing");
      });

      V3.dom.empty(keysMessagesEl);
      var msgCount = 0;
      if (state.hotkeyCaptureWarning) {
        keysMessagesEl.appendChild(V3.dom.el("div", {
          className: "banner warn",
          role: "status",
          style: "margin-top:var(--sp-2)",
          text: state.hotkeyCaptureWarning
        }));
        msgCount += 1;
      }
      if (state.hotkeyCaptureError) {
        keysMessagesEl.appendChild(V3.dom.el("div", { className: "row-sub err", text: state.hotkeyCaptureError }));
        msgCount += 1;
      }
      if (state.hotkeySaveError) {
        keysMessagesEl.appendChild(V3.dom.el("div", { className: "row-sub err", text: state.hotkeySaveError }));
        msgCount += 1;
      }
      keysMessagesEl.hidden = msgCount === 0;

      var hintBits = [
        "修改快捷键后需重启应用生效。",
        "点选输入框后按下修饰键加主键录入。Esc 恢复原值，离开输入框结束录入且不自动保存。单独 Tab 用于移出焦点；Ctrl+Tab 等带修饰键的组合会被录入。部分系统或浏览器保留的组合不会送达此页，无法在这里录入，也不会被保存为已成功。"
      ];
      if (state.hotkeyCaptureHint) hintBits.push(state.hotkeyCaptureHint);
      keysHintEl.textContent = hintBits.join(" ");
      keysSavedEl.hidden = !state.hotkeysSaved;
      keysSaveBtn.disabled = !connected;

      var saveErr = state.hotkeySaveError || "";
      if (saveErr && saveErr !== lastSaveError) {
        lastSaveError = saveErr;
        if (isSetupVisible && state.hotkeyFocus && keyInputs[state.hotkeyFocus]) {
          var active = typeof document !== "undefined" ? document.activeElement : null;
          if (!(active && active.getAttribute && active.getAttribute("data-hotkey"))) {
            ignoreHotkeyBlur = true;
            keyInputs[state.hotkeyFocus].focus();
            ignoreHotkeyBlur = false;
          }
        }
      } else if (!saveErr) {
        lastSaveError = "";
      }
    }

    /* ---------- 其余子标签视图（截图 / 接口 / 系统） ---------- */
    function renderShot(state) {
      var optionsList = state.targetLangOptions && state.targetLangOptions.length
        ? state.targetLangOptions
        : V3.labels.TARGET_LANG_OPTIONS;
      var select = V3.dom.el("select", {
        className: "field",
        "aria-label": "翻译成",
        disabled: !isConnected(state),
        onChange: function (ev) { call(hooks, "onChangeTargetLang", ev.target.value); }
      });
      if (state.translate_target_lang == null || state.translate_target_lang === "") {
        select.appendChild(V3.dom.el("option", { value: "", selected: true, text: "尚未读取" }));
      }
      optionsList.forEach(function (item) {
        var opt = V3.dom.el("option", { value: item.value, text: item.label });
        if (item.value === state.translate_target_lang) opt.selected = true;
        select.appendChild(opt);
      });
      return [
        V3.dom.el("div", { className: "setup-section" }, [
          V3.dom.el("h2", { text: "翻译设置" }),
          V3.dom.el("div", { className: "rows" }, [
            V3.dom.el("div", { className: "row" }, [
              V3.dom.el("div", { className: "row-main" }, [
                V3.dom.el("div", { className: "row-title", text: "翻译成" }),
                V3.dom.el("div", {
                  className: "row-sub",
                  text: "本地 " + V3.labels.FIXED_MODELS.ollama + " 翻译，不联网。选「仅识别文字」时只做文字识别。"
                })
              ]),
              select
            ])
          ]),
          state.targetLangSaved
            ? V3.dom.el("div", { className: "actions" }, [V3.dom.el("span", { className: "saved", text: "已保存" })])
            : null
        ])
      ];
    }

    function renderApi(state) {
      var tokenText = state.tokenMasked || (state.tokenPresent ? "••••••••" : "尚未读取");
      var rows = [
        { name: "STT", desc: "音频 → 原始识别文本（不润色、不输入）。契约：SttRequest / SttResponse。" },
        { name: "Polish", desc: "文本 → 只纠错的润色结果。契约：PolishRequest / PolishResponse。" },
        { name: "Translate", desc: "文本 → 本地翻译结果。契约：TranslateRequest / TranslateResponse。" },
        { name: "OCR Translate", desc: "图片 → 识别文字与译文。契约：OcrTranslateResponse（text 是译文，ocr_text 是原文）。" },
        { name: "Utterance / Job", desc: "提交文本并查询任务状态。投递未知为 delivery_unknown，不得显示为成功。" },
        { name: "History", desc: "处理记录列表。契约：HistoryListResponse。" }
      ];
      var fallbackTable = V3.dom.el("table", { className: "api-table" });
      rows.forEach(function (row) {
        fallbackTable.appendChild(V3.dom.el("tr", {}, [
          V3.dom.el("td", { className: "mono", text: row.name }),
          V3.dom.el("td", { text: row.desc })
        ]));
      });
      var addr = [];
      if (state.serviceUrl) addr.push("服务地址 " + state.serviceUrl);
      if (state.localUrl) addr.push("本机 " + state.localUrl);
      var derived = state.apiPaths;
      var pathTable = null;
      function apiHint(addrBits) {
        var prefix = addrBits.length ? addrBits.join("，") + "。" : "服务地址尚未读取。";
        if (!derived) return prefix + "尚未读取接口路径。";
        if (!derived.length) return prefix + "当前目录没有可展示的接口路径。";
        return prefix + "下列路径从当前 OpenAPI 快照派生。";
      }
      if (derived && derived.length) {
        pathTable = V3.dom.el("table", { className: "api-table" });
        derived.forEach(function (op) {
          pathTable.appendChild(V3.dom.el("tr", {}, [
            V3.dom.el("td", { className: "mono", text: (op.method || "") + " " + (op.path || "") }),
            V3.dom.el("td", { text: (op.access === "lan_token" ? "需令牌" : "仅本机") + (op.operationId ? " · " + op.operationId : "") })
          ]));
        });
      }
      var tokenCopied = state.copyKind === "token" && state.copyStatus ? state.copyStatus : "";
      return [
        V3.dom.el("div", { className: "setup-section" }, [
          V3.dom.el("h2", { text: "供其他项目调用" }),
          V3.dom.el("p", { className: "hint", text: apiHint(addr) }),
          V3.dom.el("div", { className: "rows" }, [
            V3.dom.el("div", { className: "row" }, [
              V3.dom.el("div", { className: "row-main" }, [
                V3.dom.el("div", { className: "row-title", text: "访问令牌" }),
                V3.dom.el("div", { className: "row-sub mono", text: tokenText })
              ]),
              tokenCopied ? V3.dom.el("span", { className: "saved", text: tokenCopied }) : null,
              V3.dom.el("button", {
                type: "button",
                className: "btn",
                disabled: !state.tokenPresent,
                onClick: function () { call(hooks, "onCopy", { kind: "token" }); }
              }, [V3.dom.icon("copy"), document.createTextNode("复制")])
            ]),
            V3.dom.el("div", { className: "row", style: "display:block" }, [pathTable || fallbackTable])
          ])
        ])
      ];
    }

    function renderSys(state) {
      var list = state.capabilities;
      var rows = V3.dom.el("div", { className: "rows" });
      if (!list) {
        rows.appendChild(V3.dom.el("div", { className: "empty", text: "尚未读取平台能力。" }));
      } else if (!list.length) {
        rows.appendChild(V3.dom.el("div", { className: "empty", text: "当前平台没有可展示的检测项。" }));
      } else {
        list.forEach(function (item) {
          var ok = item.available === true;
          var known = item.available === true || item.available === false;
          var failDetail = item.message || item.detail || "不可用";
          rows.appendChild(V3.dom.el("div", { className: "row" }, [
            V3.dom.el("span", { className: "dot " + (known ? (ok ? "ok" : "err") : ""), "aria-hidden": "true" }),
            V3.dom.el("div", { className: "row-main", text: item.name || "未命名能力" }),
            V3.dom.el("span", {
              className: ok ? "muted" : "",
              style: known && !ok ? "color:var(--c-danger)" : "",
              text: known ? (ok ? "可用" : failDetail) : "未检测"
            })
          ]));
          if (!ok && item.installCommand) {
            rows.appendChild(V3.dom.el("div", { className: "row" }, [
              V3.dom.el("div", { className: "row-main row-sub", text: "安装命令" }),
              V3.dom.el("span", { className: "mono", style: "font-size:var(--fs-xs)", text: item.installCommand })
            ]));
          }
        });
      }
      return [
        V3.dom.el("div", { className: "setup-section" }, [
          V3.dom.el("h2", { text: "平台能力" }),
          rows,
          V3.dom.el("p", {
            className: "hint",
            text: "列表项由业务层按当前平台实时检测注入，页面不写死。"
          })
        ])
      ];
    }

    function renderTabs(state) {
      var dotCls = statusDotClass(state);
      if (dotCls) {
        modelsTabDot.hidden = false;
        modelsTabDot.className = "dot " + dotCls;
      } else {
        modelsTabDot.hidden = true;
        modelsTabDot.className = "dot";
      }
      Object.keys(tabButtons).forEach(function (id) {
        var btn = tabButtons[id];
        var current = id === tab;
        btn.className = current ? "on" : "";
        btn.setAttribute("aria-selected", current ? "true" : "false");
      });
    }

    function renderPane(state) {
      var isSetupVisible = state.activePanel === "setup" && !container.hidden;
      if (tab === "models") {
        if (mountedTab !== "models") {
          ignoreHotkeyBlur = true;
          V3.dom.empty(paneEl);
          modelsView.sections.forEach(function (node) { paneEl.appendChild(node); });
          mountedTab = "models";
          ignoreHotkeyBlur = false;
        }
        modelsView.update(state);
        return;
      }
      if (tab === "voice") {
        if (mountedTab !== "voice") {
          V3.dom.empty(paneEl);
          paneEl.appendChild(voiceSection);
          mountedTab = "voice";
        }
        updateVoice(state);
        return;
      }
      if (tab === "keys") {
        var switchedToKeys = mountedTab !== "keys";
        if (switchedToKeys) {
          ignoreHotkeyBlur = true;
          V3.dom.empty(paneEl);
          paneEl.appendChild(keysSection);
          mountedTab = "keys";
          ignoreHotkeyBlur = false;
        }
        updateKeys(state, isSetupVisible);
        if (switchedToKeys && isSetupVisible && state.hotkeyCapturing && keyInputs[state.hotkeyCapturing]) {
          ignoreHotkeyBlur = true;
          keyInputs[state.hotkeyCapturing].focus();
          ignoreHotkeyBlur = false;
        }
        return;
      }
      ignoreHotkeyBlur = true;
      V3.dom.empty(paneEl);
      mountedTab = tab;
      var map = {
        shot: renderShot,
        api: renderApi,
        sys: renderSys
      };
      map[tab](state).forEach(function (node) {
        if (node) paneEl.appendChild(node);
      });
      ignoreHotkeyBlur = false;
    }

    function render(nextState) {
      if (destroyed) return;
      var state = nextState || getState() || {};
      if (state.setupTab && hasTab(state.setupTab)) {
        tab = state.setupTab;
      }
      renderTabs(state);
      renderPane(state);
    }

    function setTab(id) {
      if (destroyed || !hasTab(id)) return;
      tab = id;
      var cur = getState() || {};
      if (cur.setupTab !== id) {
        patchState({ setupTab: id });
      } else {
        render(cur);
      }
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      tabsEl.removeEventListener("keydown", onTabsKeyDown);
      mountedTab = null;
      V3.dom.empty(container);
    }

    return {
      render: render,
      setTab: setTab,
      getTab: function () { return tab; },
      destroy: destroy
    };
  }

  V3.setupPanel = {
    create: createSetupPanel,
    headerNotice: headerNotice,
    statusDotClass: statusDotClass
  };
})(window.V3 = window.V3 || {});

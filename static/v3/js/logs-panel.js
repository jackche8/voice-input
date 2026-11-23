/* 运行日志纯 UI（模板 C：顶部持久上限胶囊 + 两排 9 份来源胶囊 + 全宽零 innerHTML 日志区 + 底部操作条）。
 * 严守纯展示边界：不发起网络请求、不读写剪贴板或存储，所有动作经 hooks 回调通知业务层。
 */
(function (V3) {
  "use strict";

  var GROUP_ORDER = ["runtime", "setup"];

  function call(hooks, name, payload) {
    var fn = hooks && hooks[name];
    if (typeof fn !== "function") return undefined;
    var res = fn(payload);
    if (res && typeof res.then === "function") {
      res.then(undefined, function () {});
    }
    return res;
  }

  function formatBytes(b) {
    if (typeof b !== "number" || !isFinite(b) || b <= 0) return "0 B";
    if (b < 1024) return Math.round(b) + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
    return (b / (1024 * 1024)).toFixed(2) + " MB";
  }

  function formatError(errCode, message, fallback) {
    if (errCode && Object.prototype.hasOwnProperty.call(V3.labels.ERROR_CODE, errCode)) {
      return V3.labels.formatError(errCode, message);
    }
    if (errCode && message && String(errCode) !== String(message)) {
      return String(errCode) + "：" + String(message);
    }
    if (message) return String(message);
    if (errCode) return String(errCode);
    return fallback || "日志读取失败";
  }

  function dotClassForSource(src) {
    if (!src || !src.exists || !src.line_count) return "";
    if (src.has_error) return "err";
    if (src.has_warn) return "warn";
    return "ok";
  }

  function levelClassForEntry(entry) {
    if (!entry) return "log-line";
    if (entry.level === "error") return "log-line lvl-err";
    if (entry.level === "warn") return "log-line lvl-warn";
    return "log-line";
  }

  /* 零 innerHTML 单遍原始文本渲染：仅使用 createTextNode + span.log-redact + mark.log-hit，
   * 搜索 span / class / < / & 永不污染标签结构或转义实体。
   */
  function appendHighlightedText(parent, rawText, kwLower) {
    if (!rawText) return;
    var hasRedact = rawText.indexOf("***") !== -1;
    var hasKw = !!kwLower;
    if (!hasRedact && !hasKw) {
      parent.appendChild(document.createTextNode(rawText));
      return;
    }
    if (!hasKw) {
      var pos = 0;
      while (pos < rawText.length) {
        var idx = rawText.indexOf("***", pos);
        if (idx === -1) {
          parent.appendChild(document.createTextNode(rawText.slice(pos)));
          break;
        }
        if (idx > pos) {
          parent.appendChild(document.createTextNode(rawText.slice(pos, idx)));
        }
        parent.appendChild(V3.dom.el("span", { className: "log-redact" }, [document.createTextNode("***")]));
        pos = idx + 3;
      }
      return;
    }
    var lower = rawText.toLowerCase();
    var kwLen = kwLower.length;
    if (!hasRedact) {
      var kpos = 0;
      while (kpos < rawText.length) {
        var kidx = lower.indexOf(kwLower, kpos);
        if (kidx === -1) {
          parent.appendChild(document.createTextNode(rawText.slice(kpos)));
          break;
        }
        if (kidx > kpos) {
          parent.appendChild(document.createTextNode(rawText.slice(kpos, kidx)));
        }
        parent.appendChild(V3.dom.el("mark", { className: "log-hit" }, [
          document.createTextNode(rawText.slice(kidx, kidx + kwLen))
        ]));
        kpos = kidx + kwLen;
      }
      return;
    }

    var redacts = [];
    var rpos = 0;
    while (rpos < rawText.length) {
      var ridx = rawText.indexOf("***", rpos);
      if (ridx === -1) break;
      redacts.push([ridx, ridx + 3]);
      rpos = ridx + 3;
    }
    var hits = [];
    var hpos = 0;
    while (hpos < lower.length) {
      var hidx = lower.indexOf(kwLower, hpos);
      if (hidx === -1) break;
      hits.push([hidx, hidx + kwLen]);
      hpos = hidx + kwLen;
    }
    var pts = [0, rawText.length];
    var i;
    for (i = 0; i < redacts.length; i += 1) pts.push(redacts[i][0], redacts[i][1]);
    for (i = 0; i < hits.length; i += 1) pts.push(hits[i][0], hits[i][1]);
    pts.sort(function (a, b) { return a - b; });

    var ri = 0;
    var hi = 0;
    for (i = 0; i < pts.length - 1; i += 1) {
      var a = pts[i];
      var b = pts[i + 1];
      if (a === b) continue;
      while (ri < redacts.length && redacts[ri][1] <= a) ri += 1;
      while (hi < hits.length && hits[hi][1] <= a) hi += 1;
      var inRedact = ri < redacts.length && redacts[ri][0] <= a && b <= redacts[ri][1];
      var inHit = hi < hits.length && hits[hi][0] <= a && b <= hits[hi][1];
      var slice = rawText.slice(a, b);
      if (inRedact && inHit) {
        parent.appendChild(V3.dom.el("span", { className: "log-redact" }, [
          V3.dom.el("mark", { className: "log-hit" }, [document.createTextNode(slice)])
        ]));
      } else if (inRedact) {
        parent.appendChild(V3.dom.el("span", { className: "log-redact" }, [document.createTextNode(slice)]));
      } else if (inHit) {
        parent.appendChild(V3.dom.el("mark", { className: "log-hit" }, [document.createTextNode(slice)]));
      } else {
        parent.appendChild(document.createTextNode(slice));
      }
    }
  }

  function createLogsPanel(container, options) {
    var opts = options || {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var hooks = opts.hooks || {};
    var confirmDialog = opts.confirm || null;

    var keyword = "";
    var wrap = true;
    var lastCapSig = "";
    var capButtons = {};
    var lastPillsSig = "";
    var pillNodes = {};
    var lastViewportKey = null;
    var lastMatchedEntries = [];

    /* ---------- 持久顶栏保留上限胶囊（挂载于 home.js headActionsEl，仅 logs 面板持久化） ---------- */
    var capSegEl = V3.dom.el("div", {
      className: "cap-seg",
      role: "group",
      "aria-label": "单文件日志保留行数上限",
      dataset: { role: "log-cap-seg" }
    });
    var capSavedEl = V3.dom.el("span", {
      className: "saved",
      dataset: { role: "log-cap-saved" },
      text: "已保存",
      hidden: true
    });
    var capErrorEl = V3.dom.el("span", {
      className: "copy-err",
      dataset: { role: "log-cap-error" },
      role: "alert",
      hidden: true
    });
    var headActionsEl = V3.dom.el("div", {
      className: "cap-seg-wrap",
      dataset: { role: "logs-head-actions" }
    }, [
      V3.dom.el("span", { text: "每文件保留" }),
      capSegEl,
      capSavedEl,
      capErrorEl
    ]);

    function ensureCapButtons(choices) {
      var list = (Array.isArray(choices) && choices.length)
        ? choices
        : (V3.labels.LOG_MAX_LINES_OPTIONS || [500, 2000, 5000]);
      var sig = list.join(",");
      if (sig === lastCapSig) return;
      V3.dom.empty(capSegEl);
      capButtons = {};
      list.forEach(function (val) {
        var num = Number(val);
        var btn = V3.dom.el("button", {
          type: "button",
          dataset: { cap: String(num) },
          "aria-pressed": "false",
          disabled: true,
          text: num + " 行",
          onClick: function () {
            var curState = getState();
            var curLogs = (curState && curState.logs) || {};
            var curCap = curLogs.log_max_lines != null ? curLogs.log_max_lines : curState.log_max_lines;
            if (btn.disabled || curCap === num) return;
            /* 非乐观更新：仅向业务层上报意图，待回读确认后切换激活态。 */
            call(hooks, "onChangeLogMaxLines", { log_max_lines: num });
          }
        });
        capButtons[String(num)] = btn;
        capSegEl.appendChild(btn);
      });
      lastCapSig = sig;
    }

    ensureCapButtons(null);

    /* ---------- 横向来源胶囊条（持久按钮原地更新，避免 1s 轮询打断点击或焦点） ---------- */
    var sourcePillsEl = V3.dom.el("div", {
      className: "source-pills",
      role: "tablist",
      "aria-label": "日志文件列表",
      dataset: { role: "log-source-pills" },
      hidden: true
    });

    sourcePillsEl.addEventListener("keydown", function (ev) {
      var list = Array.prototype.slice.call(sourcePillsEl.querySelectorAll("button[data-file]"));
      var idx = list.indexOf(document.activeElement);
      if (idx < 0) return;
      var next = idx;
      if (ev.key === "ArrowRight" || ev.key === "ArrowDown") next = (idx + 1) % list.length;
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") next = (idx - 1 + list.length) % list.length;
      else if (ev.key === "Home") next = 0;
      else if (ev.key === "End") next = list.length - 1;
      else return;
      ev.preventDefault();
      list[next].focus();
    });

    function ensureSourcePills(sources) {
      if (!Array.isArray(sources) || !sources.length) {
        sourcePillsEl.hidden = true;
        if (lastPillsSig !== "") {
          V3.dom.empty(sourcePillsEl);
          pillNodes = {};
          lastPillsSig = "";
        }
        return;
      }
      sourcePillsEl.hidden = false;
      var sig = sources.map(function (s) { return (s.group || "") + ":" + (s.name || ""); }).join("|");
      if (sig === lastPillsSig) return;

      V3.dom.empty(sourcePillsEl);
      pillNodes = {};
      var groups = {};
      var groupKeys = GROUP_ORDER.slice();
      sources.forEach(function (src) {
        var g = src.group || "runtime";
        if (!groups[g]) {
          groups[g] = [];
          if (groupKeys.indexOf(g) === -1) groupKeys.push(g);
        }
        groups[g].push(src);
      });
      groupKeys.forEach(function (g) {
        var items = groups[g];
        if (!items || !items.length) return;
        var tagText = V3.labels.labelOf(V3.labels.LOG_GROUPS || {}, g, g);
        var rowEl = V3.dom.el("div", {
          className: "source-pills-row",
          dataset: { logGroup: g }
        }, [
          V3.dom.el("span", { className: "source-pills-tag", text: tagText })
        ]);
        items.forEach(function (src) {
          var name = String(src.name || "");
          var dotEl = V3.dom.el("span", { className: "dot", "aria-hidden": "true" });
          var countEl = V3.dom.el("span", { className: "pill-count", text: "0" });
          var btn = V3.dom.el("button", {
            type: "button",
            className: "source-pill",
            role: "tab",
            "aria-selected": "false",
            title: name,
            dataset: { file: name },
            onClick: function () {
              call(hooks, "onSelectLogSource", { name: name });
            }
          }, [
            dotEl,
            V3.dom.el("span", { text: name }),
            countEl
          ]);
          pillNodes[name] = { btn: btn, dot: dotEl, count: countEl };
          rowEl.appendChild(btn);
        });
        sourcePillsEl.appendChild(rowEl);
      });
      lastPillsSig = sig;
    }

    /* ---------- 盒头状态栏：关键词搜索 + 遥测元信息 + 自动换行开关 ---------- */
    var filterInput = V3.dom.el("input", {
      type: "search",
      className: "field",
      placeholder: "关键词筛选当前日志（支持过滤 ERROR / WARN / 路径）…",
      "aria-label": "关键词筛选当前日志",
      dataset: { role: "log-filter" }
    });
    filterInput.addEventListener("input", function () {
      keyword = filterInput.value;
      render(getState());
    });

    var metaEl = V3.dom.el("span", { className: "mono", dataset: { role: "log-meta" } });
    var wrapBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm",
      dataset: { role: "log-toggle-wrap" },
      "aria-pressed": "true",
      text: "自动换行：开",
      onClick: function () {
        wrap = !wrap;
        syncWrapState();
      }
    });

    var viewportEl = V3.dom.el("div", {
      className: "log-viewport",
      role: "tabpanel",
      "aria-label": "日志内容",
      dataset: { role: "log-viewport" }
    });

    /* ---------- 底部操作条：刷新 / 清空（确认框）/ 复制反馈（绿/红分离）/ 复制当前 / 复制汇总 ---------- */
    var refreshBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm",
      dataset: { role: "logs-refresh" },
      onClick: function () {
        var curLogs = (getState() && getState().logs) || {};
        call(hooks, "onRefreshLogs", { name: curLogs.activeFile || null });
      }
    }, [V3.dom.icon("refresh", "ic ic-sm"), document.createTextNode("刷新")]);

    var clearBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm danger-outline",
      dataset: { role: "logs-clear" },
      disabled: true,
      onClick: function () {
        var curLogs = (getState() && getState().logs) || {};
        var name = curLogs.activeFile;
        if (!name || clearBtn.disabled) return;
        if (confirmDialog && typeof confirmDialog.open === "function") {
          confirmDialog.open({
            title: "清空当前日志文件？",
            warning: "将清空 " + name + " 中的现有记录，此操作不可恢复，不会影响正在运行的服务进程。",
            cancelLabel: "取消",
            confirmLabel: "确认清空",
            onConfirm: function () {
              call(hooks, "onClearCurrentLog", { name: name });
            }
          });
        } else {
          call(hooks, "onClearCurrentLog", { name: name });
        }
      }
    }, [V3.dom.icon("trash", "ic ic-sm"), document.createTextNode("清空当前日志")]);

    var copyFeedbackEl = V3.dom.el("span", {
      className: "saved",
      dataset: { role: "logs-copy-feedback" },
      hidden: true
    });

    var copyCurrentBtn = V3.dom.el("button", {
      type: "button",
      className: "btn btn-sm",
      dataset: { role: "logs-copy-current" },
      disabled: true,
      onClick: function () {
        var curLogs = (getState() && getState().logs) || {};
        if (copyCurrentBtn.disabled || !lastMatchedEntries.length) return;
        var text = lastMatchedEntries.map(function (entry) {
          return entry && entry.text != null ? String(entry.text) : "";
        }).join("\n");
        call(hooks, "onCopyCurrentLog", {
          name: curLogs.activeFile || null,
          text: text,
          keyword: keyword.trim()
        });
      }
    }, [V3.dom.icon("copy", "ic ic-sm"), document.createTextNode("复制当前日志")]);

    var copySummaryBtn = V3.dom.el("button", {
      type: "button",
      className: "btn primary btn-sm",
      dataset: { role: "logs-copy-summary" },
      disabled: true,
      onClick: function () {
        if (copySummaryBtn.disabled) return;
        call(hooks, "onCopyDiagnosticSummary");
      }
    }, [V3.dom.icon("copy", "ic ic-sm"), document.createTextNode("复制排障汇总")]);

    var studioBoxEl = V3.dom.el("div", { className: "studio-box" }, [
      V3.dom.el("div", { className: "box-head" }, [
        V3.dom.el("div", { className: "c-status-bar" }, [
          V3.dom.el("div", { className: "c-search-wrap" }, [
            V3.dom.icon("search", "ic ic-sm"),
            filterInput
          ]),
          V3.dom.el("div", { className: "c-telemetry" }, [
            metaEl,
            wrapBtn
          ])
        ])
      ]),
      viewportEl,
      V3.dom.el("div", { className: "box-foot" }, [
        V3.dom.el("div", { className: "c-foot-left" }, [refreshBtn, clearBtn]),
        V3.dom.el("div", { className: "c-foot-right" }, [copyFeedbackEl, copyCurrentBtn, copySummaryBtn])
      ])
    ]);

    V3.dom.empty(container);
    container.appendChild(sourcePillsEl);
    container.appendChild(studioBoxEl);

    function syncWrapState() {
      viewportEl.classList.toggle("nowrap", !wrap);
      wrapBtn.textContent = wrap ? "自动换行：开" : "自动换行：关";
      wrapBtn.setAttribute("aria-pressed", String(wrap));
    }

    function computeMatched(entries, kwLower) {
      if (!Array.isArray(entries) || !entries.length) return [];
      if (!kwLower) return entries;
      return entries.filter(function (entry) {
        var t = entry && entry.text != null ? String(entry.text) : "";
        return t.toLowerCase().indexOf(kwLower) !== -1;
      });
    }

    function renderViewport(key, matched, kwTrim, kwLower) {
      V3.dom.empty(viewportEl);

      if (key.phase === "loading") {
        var capNote = key.loadingCap != null ? ("（保留上限 " + key.loadingCap + " 行）") : "";
        var loadLabel = key.activeFile
          ? ("正在读取 " + key.activeFile + capNote + "…")
          : "正在读取运行日志…";
        viewportEl.appendChild(V3.dom.el("div", { className: "log-loading-state" }, [
          V3.dom.el("div", { className: "box-loading", role: "status", "aria-live": "polite" }, [
            V3.dom.el("span", { text: loadLabel }),
            V3.dom.el("div", { className: "progress indeterminate", "aria-hidden": "true" }, [V3.dom.el("i")])
          ]),
          V3.dom.el("div", { className: "log-skeleton", "aria-hidden": "true" }, [
            V3.dom.el("div", { className: "log-skel-bar log-skel-w1" }),
            V3.dom.el("div", { className: "log-skel-bar log-skel-w2" }),
            V3.dom.el("div", { className: "log-skel-bar log-skel-w3" }),
            V3.dom.el("div", { className: "log-skel-bar log-skel-w4" })
          ])
        ]));
        return;
      }

      if (key.phase === "error") {
        viewportEl.appendChild(V3.dom.el("div", { className: "box-error", role: "alert" }, [
          V3.dom.el("div", { className: "box-error-main" }, [
            V3.dom.icon("alert"),
            V3.dom.el("span", { text: formatError(key.error, key.message, "日志读取失败") })
          ]),
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "logs-retry" },
            text: "重试读取",
            onClick: function () {
              call(hooks, "onRefreshLogs", { name: key.activeFile || null });
            }
          })
        ]));
        return;
      }

      if (key.idleUnloaded) {
        viewportEl.appendChild(V3.dom.el("div", { className: "box-empty" }, [
          V3.dom.el("div", {}, [
            V3.dom.el("strong", { text: "尚未读取运行日志" }),
            V3.dom.el("br"),
            document.createTextNode(key.connected ? "点击刷新以读取本地运行日志。" : "尚未连接本地服务，无法读取运行日志。")
          ]),
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "logs-empty-refresh" },
            onClick: function () {
              call(hooks, "onRefreshLogs", { name: key.activeFile || null });
            }
          }, [V3.dom.icon("refresh", "ic ic-sm"), document.createTextNode("刷新日志")])
        ]));
        return;
      }

      if (key.phase === "empty" || !key.entries || !key.entries.length) {
        var emptyName = key.activeFile || "当前文件";
        viewportEl.appendChild(V3.dom.el("div", { className: "box-empty" }, [
          V3.dom.el("div", {}, [
            V3.dom.el("strong", { text: emptyName + " 当前无日志记录" }),
            V3.dom.el("br"),
            document.createTextNode("该组件尚未输出日志，或刚刚执行过清空操作。")
          ]),
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "logs-empty-refresh" },
            onClick: function () {
              call(hooks, "onRefreshLogs", { name: key.activeFile || null });
            }
          }, [V3.dom.icon("refresh", "ic ic-sm"), document.createTextNode("刷新日志")])
        ]));
        return;
      }

      if (!matched.length) {
        var noMatchName = key.activeFile || "当前日志";
        viewportEl.appendChild(V3.dom.el("div", { className: "box-empty" }, [
          V3.dom.el("div", { text: "未在 " + noMatchName + " 中找到包含「" + kwTrim + "」的日志行。" }),
          V3.dom.el("button", {
            type: "button",
            className: "btn btn-sm",
            dataset: { role: "logs-clear-filter" },
            text: "清除关键词",
            onClick: function () {
              keyword = "";
              filterInput.value = "";
              render(getState());
            }
          })
        ]));
        return;
      }

      var linesWrap = V3.dom.el("div", { className: "log-lines", dataset: { role: "log-lines" } });
      var frag = document.createDocumentFragment();
      for (var i = 0; i < matched.length; i += 1) {
        var entry = matched[i] || {};
        var lineNo = entry.line_no != null ? String(entry.line_no) : String(i + 1);
        var msgEl = V3.dom.el("span", { className: "log-msg" });
        appendHighlightedText(msgEl, entry.text != null ? String(entry.text) : "", kwLower);
        var lineEl = V3.dom.el("div", { className: levelClassForEntry(entry) }, [
          V3.dom.el("span", { className: "log-ln", text: lineNo }),
          msgEl
        ]);
        frag.appendChild(lineEl);
      }
      linesWrap.appendChild(frag);
      viewportEl.appendChild(linesWrap);
    }

    function sameViewportKey(a, b) {
      return !!(a && b
        && a.activeFile === b.activeFile
        && a.entries === b.entries
        && a.keyword === b.keyword
        && a.phase === b.phase
        && a.error === b.error
        && a.message === b.message
        && a.loadingCap === b.loadingCap
        && a.idleUnloaded === b.idleUnloaded
        && a.connected === b.connected);
    }

    function render(state) {
      state = state || {};
      var connected = state.connection === "connected";
      var logs = state.logs || {};
      var phase = logs.phase || "idle";
      var sources = Array.isArray(logs.sources) ? logs.sources : [];
      var entries = Array.isArray(logs.entries) ? logs.entries : [];
      var activeFile = logs.activeFile || null;
      var effectiveCap = logs.log_max_lines != null
        ? logs.log_max_lines
        : (state.log_max_lines != null ? state.log_max_lines : null);
      var displayMaxLines = logs.max_lines != null ? logs.max_lines : effectiveCap;
      var isLoading = phase === "loading";
      var isError = phase === "error";
      var isEmpty = phase === "empty";
      var idleUnloaded = !isLoading && !isError && !isEmpty && !activeFile && !sources.length && !entries.length;

      /* 1. 更新持久顶栏保留上限胶囊 */
      ensureCapButtons(logs.allowed_max_lines);
      Object.keys(capButtons).forEach(function (k) {
        var btn = capButtons[k];
        var active = effectiveCap != null && Number(k) === Number(effectiveCap);
        btn.classList.toggle("on", active);
        btn.setAttribute("aria-pressed", String(active));
        btn.disabled = !connected || effectiveCap == null || logs.savingCap === true || isLoading;
      });
      if (logs.capError) {
        capSavedEl.hidden = true;
        capErrorEl.hidden = false;
        capErrorEl.textContent = String(logs.capError);
      } else if (logs.capSaved) {
        capErrorEl.hidden = true;
        capErrorEl.textContent = "";
        capSavedEl.hidden = false;
      } else {
        capSavedEl.hidden = true;
        capErrorEl.hidden = true;
        capErrorEl.textContent = "";
      }

      /* 2. 更新来源胶囊条 */
      ensureSourcePills(sources);
      sources.forEach(function (src) {
        var node = pillNodes[src.name];
        if (!node) return;
        var isOn = src.name === activeFile;
        node.btn.classList.toggle("on", isOn);
        node.btn.setAttribute("aria-selected", String(isOn));
        var count = (isOn && isEmpty) ? 0 : (src.exists ? (src.line_count || 0) : 0);
        var dot = (isOn && isEmpty) ? "" : dotClassForSource(src);
        var dotCls = "dot" + (dot ? " " + dot : "");
        if (node.dot.className !== dotCls) node.dot.className = dotCls;
        var countStr = String(count);
        if (node.count.textContent !== countStr) node.count.textContent = countStr;
      });

      /* 3. 自动换行态（仅切 class，绝不重绘 .log-lines） */
      syncWrapState();

      /* 4. 筛选与记忆化视口渲染 */
      var kwTrim = keyword.trim();
      var kwLower = kwTrim.toLowerCase();
      var matched = (isLoading || isError || isEmpty || idleUnloaded) ? [] : computeMatched(entries, kwLower);
      lastMatchedEntries = matched;

      var nextKey = {
        activeFile: activeFile,
        entries: entries,
        keyword: kwTrim,
        phase: phase,
        error: logs.error || null,
        message: logs.message || "",
        loadingCap: isLoading ? effectiveCap : null,
        idleUnloaded: idleUnloaded,
        connected: idleUnloaded ? connected : null
      };
      if (!sameViewportKey(lastViewportKey, nextKey)) {
        renderViewport(nextKey, matched, kwTrim, kwLower);
        lastViewportKey = nextKey;
      }

      /* 5. 状态条元信息 */
      if (isLoading) {
        metaEl.textContent = (activeFile || "运行日志") + " · 读取中…";
      } else if (isError) {
        metaEl.textContent = (activeFile || "运行日志") + " · 读取失败";
      } else if (idleUnloaded) {
        metaEl.textContent = "未读取";
      } else {
        var fileName = activeFile || "当前文件";
        var capPart = displayMaxLines != null ? (" / " + displayMaxLines) : "";
        var bytePart = formatBytes(isEmpty ? 0 : logs.bytes);
        var truncPart = (!isEmpty && logs.truncated) ? " · 已截断" : "";
        if (isEmpty || !entries.length) {
          metaEl.textContent = fileName + " · 0" + capPart + " 行 · " + bytePart + " · 已脱敏";
        } else if (kwTrim) {
          metaEl.textContent = fileName + " · 匹配 " + matched.length + " / " + entries.length + " 行 · " + bytePart + truncPart + " · 已脱敏";
        } else {
          metaEl.textContent = fileName + " · " + entries.length + capPart + " 行 · " + bytePart + truncPart + " · 已脱敏";
        }
      }

      /* 6. 底部操作按钮与复制反馈（copyOk 绿 / copyError 红严格分离） */
      refreshBtn.disabled = isLoading || state.connection === "loading";
      var hasContent = !isEmpty && (entries.length > 0 || (logs.exists === true && logs.bytes > 0));
      clearBtn.disabled = !connected || !activeFile || isLoading || isError || logs.clearing === true || !hasContent;
      copyCurrentBtn.disabled = isLoading || isError || isEmpty || !matched.length;
      copySummaryBtn.disabled = !connected || isLoading;

      if (logs.copyError) {
        copyFeedbackEl.hidden = false;
        copyFeedbackEl.className = "copy-err";
        copyFeedbackEl.setAttribute("role", "alert");
        copyFeedbackEl.textContent = String(logs.copyError);
      } else if (logs.copyOk) {
        copyFeedbackEl.hidden = false;
        copyFeedbackEl.className = "saved";
        copyFeedbackEl.setAttribute("role", "status");
        copyFeedbackEl.textContent = String(logs.copyOk);
      } else {
        copyFeedbackEl.hidden = true;
        copyFeedbackEl.textContent = "";
        copyFeedbackEl.removeAttribute("role");
      }
    }

    function destroy() {
      lastViewportKey = null;
      lastMatchedEntries = [];
      capButtons = {};
      pillNodes = {};
    }

    return {
      headActionsEl: headActionsEl,
      render: render,
      destroy: destroy
    };
  }

  V3.logsPanel = {
    create: createLogsPanel,
    formatBytes: formatBytes
  };
})(window.V3 = window.V3 || {});

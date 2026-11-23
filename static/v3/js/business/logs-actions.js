/* 运行日志与关于页面业务动作：懒加载、单调请求 ID 防竞态、非乐观上限持久化、确认清空与异步脱敏复制。
 * 所有接口经由 OpenAPI catalog 按冻结 operationId 派生，不手写路径，不使用 Web Storage。
 */
(function (root) {
  "use strict";

  var OP = {
    listLogs: "list_logs_api_v1_logs_get",
    readLog: "read_log_api_v1_logs__name__get",
    clearLog: "clear_log_api_v1_logs__name__delete",
    diagnosticSummary: "diagnostic_summary_api_v1_logs_diagnostic_summary_get",
    about: "get_about_api_v1_about_get"
  };

  function formatErr(err, fallback) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError && err && (err.error || err.message)) {
      return labels.formatError(err.error, err.message, err.details);
    }
    return (err && (err.message || err.error)) || fallback || "请求失败";
  }

  function formatAboutCopyText(about) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    var ver = String(about.version || "").trim();
    var verTag = ver ? (/^v/i.test(ver) ? ver : ("v" + ver)) : "";
    var osLabel = labels ? labels.labelOf(labels.OS_NAME || {}, about.os_name, "") : (about.os_name || "");
    var platformBits = [];
    if (osLabel) platformBits.push(osLabel);
    if (about.platform_detail) platformBits.push(String(about.platform_detail));
    if (about.session_type) platformBits.push(String(about.session_type));
    var modeLabel = labels ? labels.labelOf(labels.RUN_MODE || {}, about.run_mode, "") : (about.run_mode || "");
    var pyLine = about.python_version ? ("Python " + String(about.python_version)) : "";
    if (pyLine && modeLabel) pyLine += "（" + modeLabel + "）";
    var variantText;
    if (typeof V3 !== "undefined" && V3.aboutPanel && typeof V3.aboutPanel.asrVariantBadge === "function") {
      variantText = V3.aboutPanel.asrVariantBadge(about.asr_variant).text;
    } else if (about.asr_variant === "corrupt") {
      variantText = "清单损坏";
    } else if (about.asr_variant == null || about.asr_variant === "") {
      variantText = "未安装";
    } else {
      variantText = String(about.asr_variant);
    }
    if (about.asr_detail) variantText += " · " + String(about.asr_detail);
    var models = [];
    if (about.asr_model) models.push(String(about.asr_model));
    if (about.ollama_model) {
      models.push(about.ollama_version ? (about.ollama_model + "（Ollama " + about.ollama_version + "）") : String(about.ollama_model));
    }
    var lines = [
      (about.app_name || "Voice Studio") + (verTag ? (" " + verTag) : ""),
      "操作系统与平台：" + (platformBits.join(" · ") || "—"),
      "Python 运行环境：" + (pyLine || "—"),
      "ASR 运行变体：" + variantText,
      "本地模型栈：" + (models.join(" · ") || "—"),
      "本地数据目录：" + (about.data_root || "—"),
      "本地日志目录：" + (about.logs_dir || "—"),
      "本地配置目录：" + (about.config_root || "—"),
      "许可协议：" + (about.license_title || "—")
    ];
    return lines.join("\n");
  }

  function updateSourceEntry(sources, res) {
    if (!Array.isArray(sources) || !res || !res.name) return null;
    var changed = false;
    var next = sources.map(function (item) {
      if (!item || item.name !== res.name) return item;
      changed = true;
      return {
        name: res.name,
        group: res.group || item.group,
        exists: !!res.exists,
        line_count: res.line_count != null ? res.line_count : 0,
        bytes: res.bytes != null ? res.bytes : 0,
        truncated: !!res.truncated,
        has_error: !!res.has_error,
        has_warn: !!res.has_warn,
        updated_at: res.updated_at != null ? res.updated_at : null
      };
    });
    return changed ? next : null;
  }

  function createLogsActions(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var clipboard = opts.clipboard || root.createClipboard(opts);
    var setupActions = opts.setupActions;
    var setState = typeof opts.setState === "function" ? opts.setState : function () {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var isDestroyed = typeof opts.isDestroyed === "function" ? opts.isDestroyed : function () { return false; };
    var nonLocalMsgFn = typeof opts.nonLocalMessage === "function"
      ? opts.nonLocalMessage
      : function () { return "本地工作台仅允许在本机访问，已阻止在当前环境发出请求。"; };

    var logListReqId = 0;
    var logReadReqId = 0;
    var logCapReqId = 0;
    var logClearReqId = 0;
    var aboutReqId = 0;

    function canCallLocal() {
      return !!(context && typeof context.canCallLocal === "function" && context.canCallLocal());
    }

    function readLog(name, readOpts) {
      if (isDestroyed() || !name) return Promise.resolve(null);
      var fileName = String(name);
      if (!canCallLocal()) {
        setState({
          logs: {
            phase: "error",
            activeFile: fileName,
            entries: [],
            error: "forbidden",
            message: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++logReadReqId;
      var keepFeedback = !!(readOpts && readOpts.preserveFeedback);
      var loadingPatch = {
        phase: "loading",
        activeFile: fileName,
        error: null,
        message: ""
      };
      if (!keepFeedback) {
        loadingPatch.copyOk = "";
        loadingPatch.copyError = "";
      }
      setState({ logs: loadingPatch });

      var callArgs = { pathParams: { name: fileName } };
      if (readOpts && readOpts.lines != null) {
        callArgs.query = { lines: readOpts.lines };
      }
      return client.call(OP.readLog, callArgs).then(function (res) {
        if (isDestroyed() || reqId !== logReadReqId) return null;
        var curLogs = (getState().logs) || {};
        if (curLogs.activeFile && curLogs.activeFile !== fileName) return null;
        var entries = Array.isArray(res && res.entries) ? res.entries : [];
        var patch = {
          phase: entries.length > 0 ? "ready" : "empty",
          activeFile: (res && res.name) || fileName,
          entries: entries,
          exists: !!(res && res.exists),
          max_lines: (res && typeof res.max_lines === "number") ? res.max_lines : null,
          line_count: (res && typeof res.line_count === "number") ? res.line_count : entries.length,
          bytes: (res && typeof res.bytes === "number") ? res.bytes : 0,
          truncated: !!(res && res.truncated),
          updated_at: (res && res.updated_at != null) ? res.updated_at : null,
          error: null,
          message: ""
        };
        var refreshedSources = updateSourceEntry(curLogs.sources, res);
        if (refreshedSources) patch.sources = refreshedSources;
        setState({ logs: patch });
        return res;
      }, function (err) {
        if (isDestroyed() || reqId !== logReadReqId) return null;
        var curLogs = (getState().logs) || {};
        if (curLogs.activeFile && curLogs.activeFile !== fileName) return null;
        setState({
          logs: {
            phase: "error",
            activeFile: fileName,
            entries: [],
            error: (err && err.error) || "logs_io",
            message: (err && err.message) || ""
          }
        });
        return null;
      });
    }

    function loadLogs(targetName, loadOpts) {
      if (isDestroyed()) return Promise.resolve(null);
      if (!canCallLocal()) {
        setState({
          logs: {
            phase: "error",
            entries: [],
            error: "forbidden",
            message: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var listReqId = ++logListReqId;
      var readSeqAtStart = logReadReqId;
      var curLogs = (getState().logs) || {};
      var preferred = targetName ? String(targetName) : (curLogs.activeFile || null);
      var keepFeedback = !!(loadOpts && loadOpts.preserveFeedback);
      var startPatch = {
        phase: "loading",
        error: null,
        message: ""
      };
      if (!keepFeedback) {
        startPatch.copyOk = "";
        startPatch.copyError = "";
      }
      setState({ logs: startPatch });

      return client.call(OP.listLogs).then(function (listRes) {
        if (isDestroyed() || listReqId !== logListReqId) return null;
        var sources = Array.isArray(listRes && listRes.sources) ? listRes.sources : [];
        var cap = (listRes && typeof listRes.log_max_lines === "number") ? listRes.log_max_lines : null;
        var allowed = (listRes && Array.isArray(listRes.allowed_max_lines)) ? listRes.allowed_max_lines : null;
        var hasPreferred = !!(preferred && sources.some(function (s) { return s && s.name === preferred; }));
        var nextFile = hasPreferred ? preferred : (sources.length ? sources[0].name : preferred);
        var statePatch = {
          logs: {
            sources: sources,
            allowed_max_lines: allowed
          }
        };
        if (cap != null) {
          statePatch.log_max_lines = cap;
          statePatch.logs.log_max_lines = cap;
        }
        /* 若在 listLogs 在途期间用户已显式切源，保留较新的切源请求，不抢回旧文件。 */
        if (logReadReqId !== readSeqAtStart) {
          setState(statePatch);
          return listRes;
        }
        if (nextFile) {
          statePatch.logs.activeFile = nextFile;
          setState(statePatch);
          return readLog(nextFile, { preserveFeedback: keepFeedback }).then(function () {
            return listRes;
          });
        }
        statePatch.logs.phase = "empty";
        statePatch.logs.activeFile = null;
        statePatch.logs.entries = [];
        statePatch.logs.bytes = 0;
        statePatch.logs.line_count = 0;
        setState(statePatch);
        return listRes;
      }, function (err) {
        if (isDestroyed() || listReqId !== logListReqId) return null;
        setState({
          logs: {
            phase: "error",
            entries: [],
            error: (err && err.error) || "logs_io",
            message: (err && err.message) || ""
          }
        });
        return null;
      });
    }

    function selectLogSource(payload) {
      var name = (payload && typeof payload === "object") ? payload.name : payload;
      if (!name) return Promise.resolve(null);
      return readLog(String(name));
    }

    function refreshLogs(payload) {
      var name = (payload && typeof payload === "object") ? payload.name : payload;
      return loadLogs(name || (getState().logs || {}).activeFile || null);
    }

    function changeLogMaxLines(payload) {
      if (isDestroyed()) return Promise.resolve(null);
      var raw = (payload && typeof payload === "object") ? payload.log_max_lines : payload;
      var nextCap = Number(raw);
      if (!isFinite(nextCap)) return Promise.resolve(null);
      if (!canCallLocal()) {
        setState({
          logs: {
            savingCap: false,
            capSaved: false,
            capError: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++logCapReqId;
      var isCurrent = function () { return !isDestroyed() && reqId === logCapReqId; };
      /* 非乐观更新：保存期间仅禁用按钮，待服务端回读确认后才更新 log_max_lines 并立刻重读当前日志。 */
      setState({
        logs: {
          savingCap: true,
          capSaved: false,
          capError: ""
        }
      });
      return setupActions.saveConfig(
        { log_max_lines: nextCap },
        {},
        {
          isCurrent: isCurrent,
          onError: function (errPatch, err, notice) {
            errPatch.logs = {
              savingCap: false,
              capSaved: false,
              capError: notice
            };
          }
        }
      ).then(function (config) {
        if (!isCurrent()) return null;
        var savedCap = (config && typeof config.log_max_lines === "number") ? config.log_max_lines : nextCap;
        setState({
          log_max_lines: savedCap,
          logs: {
            log_max_lines: savedCap,
            savingCap: false,
            capSaved: true,
            capError: ""
          }
        });
        var active = (getState().logs || {}).activeFile || null;
        return loadLogs(active).then(function () {
          return config;
        });
      }, function () {
        return null;
      });
    }

    function clearCurrentLog(payload) {
      if (isDestroyed()) return Promise.resolve(null);
      var curLogs = (getState().logs) || {};
      var name = (payload && typeof payload === "object" && payload.name)
        ? String(payload.name)
        : (typeof payload === "string" && payload ? payload : curLogs.activeFile);
      if (!name) return Promise.resolve(null);
      if (!canCallLocal()) {
        setState({
          logs: {
            clearing: false,
            copyOk: "",
            copyError: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++logClearReqId;
      /* 递增 logReadReqId 防止清空前发出的迟到读日志响应把刚清空的视图覆盖回旧行。 */
      logReadReqId += 1;
      setState({
        logs: {
          clearing: true,
          copyOk: "",
          copyError: ""
        }
      });
      return client.call(OP.clearLog, { pathParams: { name: name } }).then(function (res) {
        if (isDestroyed() || reqId !== logClearReqId) return null;
        setState({
          logs: {
            clearing: false,
            copyOk: "已清空 " + name,
            copyError: ""
          }
        });
        var activeNow = (getState().logs || {}).activeFile || name;
        return loadLogs(activeNow, { preserveFeedback: true }).then(function () {
          return res;
        });
      }, function (err) {
        if (isDestroyed() || reqId !== logClearReqId) return null;
        setState({
          logs: {
            clearing: false,
            copyOk: "",
            copyError: formatErr(err, "清空日志失败")
          }
        });
        return null;
      });
    }

    function copyCurrentLog(payload) {
      if (isDestroyed()) return Promise.resolve(false);
      var curLogs = (getState().logs) || {};
      var name = (payload && typeof payload === "object" && payload.name)
        ? String(payload.name)
        : (typeof payload === "string" && payload ? payload : (curLogs.activeFile || "当前日志"));
      var text;
      if (payload && typeof payload === "object" && typeof payload.text === "string") {
        text = payload.text;
      } else {
        var entries = Array.isArray(curLogs.entries) ? curLogs.entries : [];
        text = entries.map(function (e) { return e && e.text != null ? String(e.text) : ""; }).join("\n");
      }
      if (!text) {
        setState({ logs: { copyOk: "", copyError: "没有可复制的日志内容" } });
        return Promise.resolve(false);
      }
      return clipboard.writeText(text).then(function () {
        if (!isDestroyed()) {
          setState({ logs: { copyOk: "已复制 " + name, copyError: "" } });
        }
        return true;
      }, function (err) {
        if (!isDestroyed()) {
          setState({ logs: { copyOk: "", copyError: formatErr(err, "复制失败") } });
        }
        return false;
      });
    }

    function copyDiagnosticSummary() {
      if (isDestroyed()) return Promise.resolve(false);
      if (!canCallLocal()) {
        setState({ logs: { copyOk: "", copyError: nonLocalMsgFn() } });
        return Promise.resolve(false);
      }
      var fetcher = function () {
        return client.call(OP.diagnosticSummary).then(function (res) {
          var text = res && typeof res.text === "string" ? res.text : "";
          if (!text) throw new Error("排障汇总为空");
          return text;
        });
      };
      var writePromise = typeof clipboard.writeAsyncText === "function"
        ? clipboard.writeAsyncText(fetcher)
        : fetcher().then(function (text) { return clipboard.writeText(text); });
      return writePromise.then(function () {
        if (!isDestroyed()) {
          setState({ logs: { copyOk: "已复制排障汇总（已脱敏）", copyError: "" } });
        }
        return true;
      }, function (err) {
        if (!isDestroyed()) {
          setState({ logs: { copyOk: "", copyError: formatErr(err, "复制排障汇总失败") } });
        }
        return false;
      });
    }

    function loadAbout() {
      if (isDestroyed()) return Promise.resolve(null);
      if (!canCallLocal()) {
        setState({
          about: {
            phase: "error",
            error: "forbidden",
            message: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++aboutReqId;
      setState({
        about: {
          phase: "loading",
          copyOk: "",
          copyError: "",
          error: null,
          message: ""
        }
      });
      return client.call(OP.about).then(function (res) {
        if (isDestroyed() || reqId !== aboutReqId) return null;
        res = res || {};
        setState({
          about: {
            phase: "ready",
            app_name: res.app_name || "",
            version: res.version || "",
            summary: res.summary || "",
            os_name: res.os_name || null,
            platform_detail: res.platform_detail || "",
            session_type: res.session_type || "",
            python_version: res.python_version || "",
            run_mode: res.run_mode || null,
            asr_variant: res.asr_variant != null ? res.asr_variant : null,
            asr_detail: res.asr_detail || "",
            asr_model: res.asr_model || "",
            ollama_model: res.ollama_model || "",
            ollama_version: res.ollama_version || "",
            data_root: res.data_root || "",
            logs_dir: res.logs_dir || "",
            config_root: res.config_root || "",
            license_title: res.license_title || "",
            license_note: res.license_note || "",
            links: res.links || null,
            copyOk: "",
            copyError: "",
            error: null,
            message: ""
          }
        });
        return res;
      }, function (err) {
        if (isDestroyed() || reqId !== aboutReqId) return null;
        setState({
          about: {
            phase: "error",
            error: (err && err.error) || "internal",
            message: (err && err.message) || ""
          }
        });
        return null;
      });
    }

    function copyAboutInfo() {
      if (isDestroyed()) return Promise.resolve(false);
      var about = (getState().about) || {};
      if (!about.app_name && !about.version) {
        setState({ about: { copyOk: "", copyError: "尚未读取关于信息" } });
        return Promise.resolve(false);
      }
      var text = formatAboutCopyText(about);
      return clipboard.writeText(text).then(function () {
        if (!isDestroyed()) {
          setState({ about: { copyOk: "已复制环境信息", copyError: "" } });
        }
        return true;
      }, function (err) {
        if (!isDestroyed()) {
          setState({ about: { copyOk: "", copyError: formatErr(err, "复制失败") } });
        }
        return false;
      });
    }

    function onSelectPanel(panel) {
      if (panel === "logs") {
        return loadLogs((getState().logs || {}).activeFile || null);
      }
      if (panel === "about") {
        return loadAbout();
      }
      return undefined;
    }

    function destroy() {
      logListReqId += 1;
      logReadReqId += 1;
      logCapReqId += 1;
      logClearReqId += 1;
      aboutReqId += 1;
    }

    var hooks = {
      onSelectLogSource: function (payload) { return selectLogSource(payload); },
      onRefreshLogs: function (payload) { return refreshLogs(payload); },
      onChangeLogMaxLines: function (payload) { return changeLogMaxLines(payload); },
      onClearCurrentLog: function (payload) { return clearCurrentLog(payload); },
      onCopyCurrentLog: function (payload) { return copyCurrentLog(payload); },
      onCopyDiagnosticSummary: function () { return copyDiagnosticSummary(); },
      onRefreshAbout: function () { return loadAbout(); },
      onCopyAboutInfo: function () { return copyAboutInfo(); }
    };

    return {
      loadLogs: loadLogs,
      readLog: readLog,
      loadAbout: loadAbout,
      onSelectPanel: onSelectPanel,
      destroy: destroy,
      hooks: hooks
    };
  }

  root.createLogsActions = createLogsActions;
  root.LOGS_ACTION_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

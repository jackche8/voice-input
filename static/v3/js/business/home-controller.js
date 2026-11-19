/* 主页（方案 C 侧栏工作台）业务控制器：文本翻译、图片 OCR/翻译、历史、局域网扫码与设置聚合。
 * 所有接口经由 OpenAPI catalog 按 operationId 派生，不手写路径、不使用 Web Storage、不直接操作 DOM。
 * 明文局域网网址与令牌仅驻留控制器私有闭包，绝不写入视图 state。
 */
(function (root) {
  "use strict";

  var OP = {
    translate: "translate_api_v1_translate_post",
    ocrTranslate: "ocr_translate_api_v1_ocr_translate_post",
    configGet: "get_config_api_v1_config_get",
    status: "status_api_v1_status_get",
    check: "setup_check_api_setup_check_get",
    history: "history_api_v1_history_get",
    lanAccess: "lan_access_api_v1_lan_access_get"
  };

  var HISTORY_PAGE_SIZE = 20;

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) {
      return labels.formatError(err && err.error, err && err.message, err && err.details);
    }
    return (err && (err.message || err.error)) || "请求失败";
  }

  function nonLocalMessage(context) {
    if (!context) return "缺少访问上下文";
    if (context.kind === "file") {
      return "file:// 不能作为成功路径。请通过同源 HTTPS 在本机打开工作台。";
    }
    return "本地工作台仅允许在本机访问，已阻止在当前环境发出请求。";
  }

  function createHomeController(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var catalog = opts.catalog;
    var clipboard = opts.clipboard || root.createClipboard(opts);
    var nowFn = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var locObj = opts.location || (typeof location !== "undefined" ? location : null);
    var histObj = opts.history || (typeof history !== "undefined" ? history : null);
    var view = null;
    var destroyed = false;
    var lanToken = null;
    var lanUrls = [];
    var loadReqId = 0;
    var translateReqId = 0;
    var ocrReqId = 0;

    function setState(patch) {
      if (destroyed) return;
      if (view && typeof view.setState === "function") view.setState(patch);
    }

    function getState() {
      return view && typeof view.getState === "function" ? view.getState() : {};
    }

    var setupActions = root.createSetupActions({
      client: client,
      context: context,
      setState: setState,
      getState: getState,
      isDestroyed: function () { return destroyed; },
      pollInterval: opts.pollInterval,
      setTimeout: opts.setTimeout,
      clearTimeout: opts.clearTimeout
    });

    function storeLan(mapped) {
      lanToken = mapped && mapped.token ? mapped.token : null;
      lanUrls = [];
      var entries = (mapped && mapped.entries) || [];
      return entries.map(function (entry, index) {
        lanUrls[index] = entry.url || "";
        return {
          ip: entry.ip || "",
          urlMasked: entry.urlMasked || "",
          qrSvg: entry.qrSvg || ""
        };
      });
    }

    function rejectNonLocal() {
      var msg = nonLocalMessage(context);
      lanToken = null;
      lanUrls = [];
      setState({
        connection: "error",
        connectionError: msg,
        error: null,
        message: "",
        statusNotice: msg,
        lanEntries: null,
        lanMessage: msg,
        tokenMasked: "",
        tokenPresent: false,
        capabilities: null,
        apiPaths: root.mapApiPaths(catalog)
      });
      return msg;
    }

    function loadAll() {
      if (destroyed) return Promise.resolve();
      var reqId = ++loadReqId;
      setupActions.stopPoll();
      if (!context || !context.canCallLocal()) {
        rejectNonLocal();
        return Promise.resolve();
      }
      setState({
        connection: "loading",
        connectionError: null,
        error: null,
        message: "",
        history: { loading: true, message: null }
      });
      return Promise.all([
        client.call(OP.check),
        client.call(OP.status),
        client.call(OP.configGet),
        client.call(OP.history, { query: { limit: HISTORY_PAGE_SIZE, offset: 0 } }),
        client.call(OP.lanAccess)
      ]).then(function (results) {
        if (destroyed || reqId !== loadReqId) return;
        var mapped = root.mapReadiness(results[0], results[1]);
        var patch = {
          connection: "connected",
          connectionError: null,
          error: null,
          message: "",
          statusNotice: mapped.statusNotice,
          unmetGates: mapped.gates,
          asr: mapped.asr,
          ollama: mapped.ollama,
          capabilities: mapped.capabilities,
          apiPaths: root.mapApiPaths(catalog),
          aboutText: "本地运行。组件状态来自设置检查，不把检查成功或注入标记当成已就绪。",
          history: root.applyHistory(results[3]),
          keywordsSaved: false,
          targetLangSaved: false,
          hotkeysSaved: false
        };
        if (opts.pageOrigin) patch.serviceUrl = String(opts.pageOrigin);
        root.applyConfig(patch, results[2]);
        if (results[4]) {
          var lanMapped = root.mapLanAccess(results[4]);
          patch.lanEntries = storeLan(lanMapped);
          patch.lanMessage = lanMapped.message || "";
          patch.tokenMasked = lanMapped.tokenMasked;
          patch.tokenPresent = lanMapped.tokenPresent;
        }
        setState(patch);
        setupActions.maybeResumePoll(mapped);
      }, function (err) {
        if (destroyed || reqId !== loadReqId) return;
        var msg = formatErr(err);
        setState({
          connection: "error",
          connectionError: msg,
          error: (err && err.error) ? err.error : null,
          message: (err && err.message) || "",
          statusNotice: msg,
          history: {
            records: [],
            total: null,
            skipped_malformed: 0,
            loading: false,
            message: msg
          }
        });
      });
    }

    function translateText(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      var rawInput = payload.text != null ? String(payload.text) : "";
      var text = rawInput.trim();
      var state = getState();
      var targetLang = String(
        payload.target_lang ||
        (state.translate && state.translate.target_lang) ||
        state.translate_target_lang ||
        "zh"
      );
      if (!text) {
        setState({
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "error",
            target_lang: targetLang,
            text: "",
            ocr_text: "",
            elapsedMs: null,
            error: "invalid_request",
            message: "请先输入或粘贴需要翻译的文字。",
            details: null,
            copyStatus: null,
            copyKind: null
          }
        });
        return Promise.resolve();
      }
      if (targetLang === "raw") {
        setState({
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "result",
            sourceText: rawInput,
            target_lang: "raw",
            text: text,
            ocr_text: "",
            elapsedMs: 0,
            error: null,
            message: "",
            details: null,
            copyStatus: null,
            copyKind: null
          }
        });
        return Promise.resolve({ ok: true, text: text, target_lang: "raw" });
      }
      if (!context || !context.canCallLocal()) {
        var msg = nonLocalMessage(context);
        setState({
          connection: "error",
          connectionError: msg,
          translate: {
            phase: "error",
            target_lang: targetLang,
            text: "",
            ocr_text: "",
            elapsedMs: null,
            error: "forbidden",
            message: msg,
            details: null,
            copyStatus: null,
            copyKind: null
          }
        });
        return Promise.resolve();
      }
      var reqId = ++translateReqId;
      var startedAt = nowFn();
      setState({
        copyStatus: null,
        copyKind: null,
        translate: {
          phase: "loading",
          sourceText: rawInput,
          target_lang: targetLang,
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
      return client.call(OP.translate, {
        body: { text: text, target_lang: targetLang }
      }).then(function (res) {
        if (destroyed || reqId !== translateReqId) return res;
        var elapsed = Math.max(0, nowFn() - startedAt);
        setState({
          connection: "connected",
          translate: {
            phase: "result",
            target_lang: (res && res.target_lang) || targetLang,
            text: (res && res.text) || "",
            ocr_text: "",
            elapsedMs: elapsed,
            error: null,
            message: "",
            details: null,
            copyStatus: null,
            copyKind: null
          }
        });
        return res;
      }, function (err) {
        if (destroyed || reqId !== translateReqId) return;
        setState({
          connection: err && err.blocked ? "error" : (getState().connection || "connected"),
          translate: {
            phase: "error",
            target_lang: targetLang,
            text: "",
            ocr_text: "",
            elapsedMs: null,
            error: (err && err.error) || "translate_failed",
            message: (err && err.message) || "",
            details: (err && err.details) != null ? err.details : null,
            copyStatus: null,
            copyKind: null
          }
        });
      });
    }

    function resolveImageSlot(payload) {
      if (payload.panel === "ocr" || payload.panel === "translate") return payload.panel;
      if (payload.mode === "ocr") return "ocr";
      if (getState().activePanel === "ocr") return "ocr";
      return "translate";
    }

    function submitImage(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      var slot = resolveImageSlot(payload);
      var file = payload.file;
      var state = getState();
      var targetLang = String(
        payload.target_lang ||
        (slot === "ocr"
          ? ((state.ocr && state.ocr.target_lang) || "raw")
          : ((state.translate && state.translate.target_lang) || state.translate_target_lang || "zh"))
      );

      function setSlotError(errObj, blocked) {
        var patch = { copyStatus: null, copyKind: null };
        if (blocked) patch.connection = "error";
        if (slot === "ocr") {
          patch.ocr = {
            phase: "error",
            mode: targetLang === "raw" ? "raw" : "translate",
            target_lang: targetLang,
            ocr_text: "",
            text: "",
            error: (errObj && errObj.error) || "ocr_failed",
            message: (errObj && errObj.message) || "",
            details: (errObj && errObj.details) != null ? errObj.details : null,
            copyStatus: null,
            copyKind: null
          };
        } else {
          patch.translate = {
            phase: "error",
            target_lang: targetLang,
            text: "",
            ocr_text: "",
            elapsedMs: null,
            error: (errObj && errObj.error) || "ocr_failed",
            message: (errObj && errObj.message) || "",
            details: (errObj && errObj.details) != null ? errObj.details : null,
            copyStatus: null,
            copyKind: null
          };
        }
        setState(patch);
      }

      if (!context || !context.canCallLocal()) {
        var msg = nonLocalMessage(context);
        setSlotError({ error: "forbidden", message: msg }, true);
        return Promise.resolve();
      }
      if (!file) {
        setSlotError({ error: "invalid_request", message: "没有可提交的图片文件。" }, false);
        return Promise.resolve();
      }

      var reqId = slot === "ocr" ? (++ocrReqId) : (++translateReqId);
      var startedAt = nowFn();
      if (slot === "ocr") {
        setState({
          copyStatus: null,
          copyKind: null,
          ocr: {
            phase: "loading",
            mode: targetLang === "raw" ? "raw" : "translate",
            target_lang: targetLang,
            ocr_text: "",
            text: "",
            error: null,
            message: "",
            details: null,
            copyStatus: null,
            copyKind: null
          }
        });
      } else {
        setState({
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "loading",
            target_lang: targetLang,
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
      }

      var form = new FormData();
      form.append("target_lang", targetLang);
      var filename = (file && file.name) || "clipboard-image.png";
      if (file instanceof Blob || (typeof File !== "undefined" && file instanceof File)) {
        form.append("image", file, filename);
      } else {
        form.append("image", file);
      }

      return client.call(OP.ocrTranslate, { body: form }).then(function (res) {
        var currentId = slot === "ocr" ? ocrReqId : translateReqId;
        if (destroyed || reqId !== currentId) return res;
        var elapsed = Math.max(0, nowFn() - startedAt);
        var usedLang = (res && res.target_lang) || targetLang;
        if (slot === "ocr") {
          var rawOcr = (res && res.ocr_text) || (usedLang === "raw" && res && res.text) || "";
          setState({
            connection: "connected",
            ocr: {
              phase: "result",
              mode: usedLang === "raw" ? "raw" : "translate",
              target_lang: usedLang,
              ocr_text: rawOcr,
              text: usedLang === "raw" ? "" : ((res && res.text) || ""),
              error: null,
              message: "",
              details: null,
              copyStatus: null,
              copyKind: null
            }
          });
        } else {
          setState({
            connection: "connected",
            translate: {
              phase: "result",
              target_lang: usedLang,
              text: (res && res.text) || "",
              ocr_text: (res && res.ocr_text) || "",
              elapsedMs: elapsed,
              error: null,
              message: "",
              details: null,
              copyStatus: null,
              copyKind: null
            }
          });
        }
        return res;
      }, function (err) {
        var currentId = slot === "ocr" ? ocrReqId : translateReqId;
        if (destroyed || reqId !== currentId) return;
        /* 失败时严禁用 ocr_text 冒充译文，统一清空 text 与 ocr_text 并如实呈现错误体。 */
        setSlotError(err, !!(err && err.blocked));
      });
    }

    function applyCopyFeedback(kind, statusText) {
      var patch = { copyKind: kind, copyStatus: statusText };
      if (kind === "translation" || kind === "translate_ocr" || kind === "source") {
        patch.translate = { copyKind: kind, copyStatus: statusText };
      } else if (kind === "ocr" || kind === "ocr_translation") {
        patch.ocr = { copyKind: kind, copyStatus: statusText };
      }
      setState(patch);
    }

    function copy(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      var kind = payload.kind || "text";
      var text = "";
      if (kind === "lan_url") {
        text = payload.index != null ? (lanUrls[payload.index] || "") : "";
      } else if (kind === "token") {
        text = lanToken || "";
        if (!text) {
          applyCopyFeedback("token", "当前没有可复制的访问令牌。");
          return Promise.resolve();
        }
      } else if (payload.text != null) {
        text = String(payload.text);
      } else if (payload.record) {
        text = String(payload.record.final_text || payload.record.raw_text || "");
      }
      if (!text) {
        applyCopyFeedback(kind, "没有可复制的内容。");
        return Promise.resolve();
      }
      return clipboard.writeText(text).then(function () {
        if (!destroyed) applyCopyFeedback(kind, "已复制");
      }, function (err) {
        if (!destroyed) applyCopyFeedback(kind, (err && err.message) || "复制失败");
      });
    }

    function refreshHistory() {
      if (destroyed) return Promise.resolve();
      if (!context || !context.canCallLocal()) {
        rejectNonLocal();
        return Promise.resolve();
      }
      setState({ history: { loading: true, message: null } });
      return client.call(OP.history, { query: { limit: HISTORY_PAGE_SIZE, offset: 0 } }).then(function (payload) {
        if (!destroyed) setState({ history: root.applyHistory(payload) });
      }, function (err) {
        if (!destroyed) setState({ history: { loading: false, message: formatErr(err) } });
      });
    }

    function loadMoreHistory() {
      if (destroyed) return Promise.resolve();
      if (!context || !context.canCallLocal()) {
        rejectNonLocal();
        return Promise.resolve();
      }
      var hist = (getState().history) || {};
      var existing = Array.isArray(hist.records) ? hist.records : [];
      setState({ history: { loading: true } });
      return client.call(OP.history, { query: { limit: HISTORY_PAGE_SIZE, offset: existing.length } }).then(function (payload) {
        if (destroyed) return;
        var more = root.applyHistory(payload);
        setState({
          history: {
            records: existing.concat(more.records),
            total: more.total,
            skipped_malformed: (hist.skipped_malformed || 0) + (more.skipped_malformed || 0),
            loading: false,
            message: null
          }
        });
      }, function (err) {
        if (!destroyed) setState({ history: { loading: false, message: formatErr(err) } });
      });
    }

    function refreshLan() {
      if (destroyed) return Promise.resolve();
      if (!context || !context.canCallLocal()) {
        rejectNonLocal();
        return Promise.resolve();
      }
      return client.call(OP.lanAccess).then(function (lan) {
        if (destroyed) return;
        var lanMapped = root.mapLanAccess(lan);
        setState({
          lanEntries: storeLan(lanMapped),
          lanMessage: lanMapped.message || "",
          tokenMasked: lanMapped.tokenMasked,
          tokenPresent: lanMapped.tokenPresent
        });
      }, function (err) {
        if (!destroyed) setState({ lanMessage: formatErr(err) });
      });
    }

    function syncUrlForPanel(panel) {
      if (!context || context.kind !== "local") return;
      if (!locObj || !histObj || typeof histObj.replaceState !== "function") return;
      var currentPath = String(locObj.pathname || "");
      if (currentPath !== "/" && currentPath !== "/setup" && currentPath !== "/setup/") return;
      var targetPath = panel === "setup" ? "/setup" : "/";
      var search = String(locObj.search || "");
      var currentHash = String(locObj.hash || "");
      var targetHash = currentHash === "#setup" ? "" : currentHash;
      if (currentPath === targetPath && currentHash === targetHash) return;
      try {
        histObj.replaceState(histObj.state, "", targetPath + search + targetHash);
      } catch (err) {}
    }

    function destroy() {
      destroyed = true;
      loadReqId += 1;
      translateReqId += 1;
      ocrReqId += 1;
      setupActions.stopPoll();
    }

    var hooks = Object.assign({}, setupActions.hooks, {
      onTranslateText: function (payload) { return translateText(payload); },
      onSubmitImage: function (payload) { return submitImage(payload); },
      onCopy: function (payload) { return copy(payload); },
      onRefreshHistory: function () { return refreshHistory(); },
      onLoadMoreHistory: function () { return loadMoreHistory(); },
      onRefreshLan: function () { return refreshLan(); },
      onRefreshStatus: function () { return loadAll(); },
      onToggleTheme: function (payload) {
        var next = payload && payload.theme === "light" ? "light" : "dark";
        if (getState().theme !== next) setState({ theme: next });
      },
      onSelectPanel: function (payload) {
        var panel = payload && payload.panel;
        if (!panel) return;
        if (getState().activePanel !== panel) {
          setState({ activePanel: panel });
        }
        syncUrlForPanel(panel);
      }
    });

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: function () { return loadAll(); },
      loadAll: loadAll,
      saveConfig: setupActions.saveConfig,
      startTask: setupActions.startTask,
      destroy: destroy,
      _debug: {
        getLanToken: function () { return lanToken; },
        getLanUrls: function () { return lanUrls.slice(); },
        isDestroyed: function () { return destroyed; },
        polling: function () { return setupActions.getPollingTaskId(); }
      }
    };
  }

  root.createHomeController = createHomeController;
  root.HOME_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

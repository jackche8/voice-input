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
    check: "setup_check_api_setup_check_get",
    history: "history_api_v1_history_get",
    lanAccess: "lan_access_api_v1_lan_access_get",
    audioInputs: "list_audio_inputs_api_v1_audio_inputs_get"
  };

  var HISTORY_PAGE_SIZE = 20;
  var DEFAULT_TRANSLATE_DEBOUNCE_MS = 1000;

  function scheduleTimer(fn, ms, opts) {
    var start = (opts && opts.setTimeout) || (typeof setTimeout === "function" ? setTimeout : null);
    if (!start) {
      fn();
      return 0;
    }
    return start(fn, ms);
  }

  function unscheduleTimer(id, opts) {
    var stop = (opts && opts.clearTimeout) || (typeof clearTimeout === "function" ? clearTimeout : null);
    if (stop && id != null) stop(id);
  }

  function defaultPairParts(pair) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    var map = labels && labels.LANG_PAIR_PARTS;
    if (map && Array.isArray(map[pair]) && map[pair].length === 2) {
      return [map[pair][0], map[pair][1]];
    }
    var split = String(pair || "zh-en").split("-");
    if (split.length === 2 && split[0] && split[1]) return [split[0], split[1]];
    return ["zh", "en"];
  }

  function readManualOverride(state) {
    var ov = state && state.translate && state.translate.manualOverride;
    if (ov && ov.source_lang && ov.target_lang) {
      return { source_lang: String(ov.source_lang), target_lang: String(ov.target_lang) };
    }
    return null;
  }

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
    var persistentNotice = null;
    var lanToken = null;
    var lanUrls = [];
    var loadReqId = 0;
    var translateReqId = 0;
    var ocrReqId = 0;
    var themeReqId = 0;
    var audioInputsReqId = 0;
    var persistedTheme = "light";
    var persistedThemeKnown = false;
    var debounceTimer = null;
    var debounceMs = opts.debounceMs != null ? opts.debounceMs : DEFAULT_TRANSLATE_DEBOUNCE_MS;

    function cancelDebounce() {
      if (debounceTimer != null) {
        unscheduleTimer(debounceTimer, opts);
        debounceTimer = null;
      }
    }

    function setState(patch) {
      if (destroyed) return;
      var effective = patch;
      if (
        persistentNotice
        && patch
        && typeof patch === "object"
        && Object.prototype.hasOwnProperty.call(patch, "statusNotice")
        && patch.statusNotice === null
      ) {
        effective = Object.assign({}, patch, { statusNotice: persistentNotice });
      }
      if (view && typeof view.setState === "function") view.setState(effective);
    }

    function setPersistentNotice(msg) {
      if (destroyed) return;
      var normalized = (msg != null && String(msg).trim()) ? String(msg) : null;
      persistentNotice = normalized;
      setState({ statusNotice: normalized });
    }

    function getState() {
      return view && typeof view.getState === "function" ? view.getState() : {};
    }

    var setupActions = root.createSetupActions({
      client: client,
      context: context,
      setState: setState,
      isDestroyed: function () { return destroyed; },
      pollInterval: opts.pollInterval,
      setTimeout: opts.setTimeout,
      clearTimeout: opts.clearTimeout,
      onCancelDebounce: cancelDebounce,
      onInvalidateTranslate: function () {
        cancelDebounce();
        translateReqId += 1;
      }
    });

    var logsActions = typeof root.createLogsActions === "function"
      ? root.createLogsActions({
          client: client,
          context: context,
          clipboard: clipboard,
          setupActions: setupActions,
          setState: setState,
          getState: getState,
          isDestroyed: function () { return destroyed; },
          nonLocalMessage: function () { return nonLocalMessage(context); }
        })
      : null;

    var ocrActions = typeof root.createOcrActions === "function"
      ? root.createOcrActions({
          client: client,
          context: context,
          setupActions: setupActions,
          setState: setState,
          getState: getState,
          isDestroyed: function () { return destroyed; },
          nonLocalMessage: function () { return nonLocalMessage(context); },
          onInvalidateOcr: function () { ocrReqId += 1; },
          previewDebounceMs: opts.previewDebounceMs,
          setTimeout: opts.setTimeout,
          clearTimeout: opts.clearTimeout
        })
      : null;

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
        audioInputs: null,
        audioInputsLoading: false,
        audioInputError: msg,
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
      var audioReqId = ++audioInputsReqId;
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
        audioInputsLoading: true,
        history: { loading: true, message: null }
      });
      var lanPromise = client.call(OP.lanAccess).then(function (data) {
        return { ok: true, data: data };
      }, function (err) {
        return { ok: false, error: err };
      });
      var audioInputsPromise = client.call(OP.audioInputs).then(function (data) {
        return { ok: true, data: data };
      }, function (err) {
        return { ok: false, error: err };
      });
      return Promise.all([
        client.call(OP.check),
        client.call(OP.configGet),
        client.call(OP.history, { query: { limit: HISTORY_PAGE_SIZE, offset: 0 } }),
        lanPromise,
        audioInputsPromise
      ]).then(function (results) {
        if (destroyed || reqId !== loadReqId) return;
        var mapped = root.mapReadiness(results[0]);
        var patch = Object.assign({
          connection: "connected",
          connectionError: null,
          error: null,
          message: "",
          apiPaths: root.mapApiPaths(catalog),
          history: root.applyHistory(results[2]),
          keywordsSaved: false,
          translateSettingsSaved: false,
          hotkeysSaved: false,
          audioInputSaved: false
        }, root.readinessPatch(mapped));
        if (opts.pageOrigin) patch.serviceUrl = String(opts.pageOrigin);
        root.applyConfig(patch, results[1]);
        if (setupActions.hasInFlightThemeWrites && setupActions.hasInFlightThemeWrites()) {
          delete patch.theme;
        } else if (patch.theme === "light" || patch.theme === "dark") {
          persistedTheme = patch.theme;
          persistedThemeKnown = true;
        }
        if (patch.translate_lang_pair) {
          patch.translate = { lang_pair: patch.translate_lang_pair };
          patch.ocr = { lang_pair: patch.translate_lang_pair };
        }
        var lanResult = results[3];
        if (lanResult && lanResult.ok) {
          if (lanResult.data) {
            var lanMapped = root.mapLanAccess(lanResult.data);
            patch.lanEntries = storeLan(lanMapped);
            patch.lanMessage = lanMapped.message || "";
            patch.tokenMasked = lanMapped.tokenMasked;
            patch.tokenPresent = lanMapped.tokenPresent;
          }
        } else if (lanResult) {
          lanToken = null;
          lanUrls = [];
          patch.lanEntries = [];
          patch.lanMessage = formatErr(lanResult.error);
          patch.tokenMasked = "";
          patch.tokenPresent = false;
        }
        var audioResult = results[4];
        if (audioReqId === audioInputsReqId) {
          patch.audioInputsLoading = false;
          if (audioResult && audioResult.ok) {
            patch.audioInputs = root.mapAudioInputs(audioResult.data);
            patch.audioInputError = "";
          } else if (audioResult) {
            patch.audioInputs = null;
            patch.audioInputError = formatErr(audioResult.error);
          }
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
          audioInputsLoading: false,
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

    function currentLangPair(state, explicitPair) {
      return String(
        explicitPair ||
        (state && state.translate && state.translate.lang_pair) ||
        (state && state.translate_lang_pair) ||
        "zh-en"
      );
    }

    function translateText(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      cancelDebounce();
      var rawInput = payload.text != null ? String(payload.text) : "";
      var text = rawInput.trim();
      var state = getState();
      var langPair = currentLangPair(state, payload.lang_pair);
      var override = Object.prototype.hasOwnProperty.call(payload, "manualOverride")
        ? payload.manualOverride
        : ((payload.source_lang && payload.target_lang)
            ? { source_lang: String(payload.source_lang), target_lang: String(payload.target_lang) }
            : readManualOverride(state));
      if (!text) {
        setState({
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "error",
            lang_pair: langPair,
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            chunks: null,
            debouncePending: false,
            manualOverride: override,
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
      if (!context || !context.canCallLocal()) {
        var msg = nonLocalMessage(context);
        setState({
          connection: "error",
          connectionError: msg,
          translate: {
            phase: "error",
            lang_pair: langPair,
            source_lang: override ? override.source_lang : null,
            target_lang: override ? override.target_lang : null,
            direction_mode: override ? "manual" : null,
            chunks: null,
            debouncePending: false,
            manualOverride: override,
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
          lang_pair: langPair,
          source_lang: override ? override.source_lang : null,
          target_lang: override ? override.target_lang : null,
          direction_mode: override ? "manual" : null,
          chunks: null,
          debouncePending: false,
          manualOverride: override,
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
      var reqBody = { text: text, lang_pair: langPair };
      if (override) {
        reqBody.source_lang = override.source_lang;
        reqBody.target_lang = override.target_lang;
      }
      return client.call(OP.translate, {
        body: reqBody
      }).then(function (res) {
        if (destroyed || reqId !== translateReqId) return res;
        var elapsed = Math.max(0, nowFn() - startedAt);
        setState({
          connection: "connected",
          translate: {
            phase: "result",
            lang_pair: (res && res.lang_pair) || langPair,
            source_lang: (res && res.source_lang) || null,
            target_lang: (res && res.target_lang) || null,
            direction_mode: (res && res.direction_mode) || null,
            chunks: (res && typeof res.chunks === "number") ? res.chunks : 1,
            debouncePending: false,
            manualOverride: override,
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
            lang_pair: langPair,
            source_lang: override ? override.source_lang : null,
            target_lang: override ? override.target_lang : null,
            direction_mode: override ? "manual" : null,
            chunks: null,
            debouncePending: false,
            manualOverride: override,
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

    function handleTranslateInput(payload) {
      if (destroyed) return;
      payload = payload || {};
      var rawInput = payload.text != null ? String(payload.text) : "";
      var trimmed = rawInput.trim();
      cancelDebounce();
      translateReqId += 1;

      if (!trimmed) {
        setState({
          copyStatus: null,
          copyKind: null,
          translate: {
            phase: "empty",
            sourceText: rawInput,
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            chunks: null,
            debouncePending: false,
            manualOverride: null,
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
        return;
      }

      var state = getState();
      var autoEnabled = state.translate_auto_enabled === true;
      var wasLoading = !!(state.translate && state.translate.phase === "loading");
      var patch = {
        sourceText: rawInput,
        debouncePending: autoEnabled
      };
      if (wasLoading) {
        patch.phase = (state.translate && state.translate.text) ? "result" : "empty";
      }
      setState({ translate: patch });

      if (autoEnabled) {
        var scheduledReqId = translateReqId;
        debounceTimer = scheduleTimer(function () {
          debounceTimer = null;
          if (destroyed || scheduledReqId !== translateReqId) return;
          var latestState = getState();
          if (latestState.translate_auto_enabled !== true) {
            setState({ translate: { debouncePending: false } });
            return;
          }
          var latestRaw = (latestState.translate && latestState.translate.sourceText != null)
            ? String(latestState.translate.sourceText)
            : rawInput;
          if (!latestRaw.trim()) {
            setState({ translate: { debouncePending: false } });
            return;
          }
          translateText({
            text: latestRaw,
            lang_pair: currentLangPair(latestState),
            trigger: "auto"
          });
        }, debounceMs, opts);
      }
    }

    function swapDirection(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      cancelDebounce();
      var state = getState();
      var tr = state.translate || {};
      var rawInput = payload.text != null ? String(payload.text) : String(tr.sourceText || "");
      var trimmed = rawInput.trim();
      if (!trimmed || tr.phase === "loading") return Promise.resolve();

      var langPair = currentLangPair(state, payload.lang_pair);
      var currentLock = readManualOverride(state);
      var nextOverride = null;
      if (!currentLock) {
        if (tr.source_lang && tr.target_lang) {
          nextOverride = {
            source_lang: String(tr.target_lang),
            target_lang: String(tr.source_lang)
          };
        } else {
          var parts = defaultPairParts(langPair);
          nextOverride = {
            source_lang: parts[1],
            target_lang: parts[0]
          };
        }
      }
      return translateText({
        text: rawInput,
        lang_pair: langPair,
        manualOverride: nextOverride,
        trigger: "manual"
      });
    }

    function clearTranslate() {
      if (destroyed) return;
      cancelDebounce();
      translateReqId += 1;
      setState({
        copyStatus: null,
        copyKind: null,
        translate: {
          phase: "empty",
          sourceText: "",
          source_lang: null,
          target_lang: null,
          direction_mode: null,
          chunks: null,
          debouncePending: false,
          manualOverride: null,
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

    function resolveImageSlot(payload) {
      if (payload.panel === "ocr" || payload.panel === "translate") return payload.panel;
      if (getState().activePanel === "ocr") return "ocr";
      return "translate";
    }

    function submitImage(payload) {
      if (destroyed) return Promise.resolve();
      payload = payload || {};
      var slot = resolveImageSlot(payload);
      var file = payload.file;
      var state = getState();
      if (slot === "translate") {
        cancelDebounce();
      }
      var mode = slot === "translate"
        ? "translate"
        : (payload.mode === "translate"
            ? "translate"
            : (payload.mode === "raw"
                ? "raw"
                : ((state.ocr && state.ocr.mode === "translate") ? "translate" : "raw")));
      var langPair = mode === "translate"
        ? String(
            payload.lang_pair ||
            (slot === "ocr"
              ? ((state.ocr && state.ocr.lang_pair) || state.translate_lang_pair || "zh-en")
              : currentLangPair(state))
          )
        : null;

      var hasDraftParams = slot === "ocr" && payload.preprocess_params != null;
      var runKind = hasDraftParams ? "draft" : "chain";

      function setSlotError(errObj, blocked) {
        var patch = { copyStatus: null, copyKind: null };
        if (blocked) patch.connection = "error";
        if (slot === "ocr") {
          patch.ocr = {
            phase: "error",
            mode: mode,
            lang_pair: langPair || ((state.ocr && state.ocr.lang_pair) || state.translate_lang_pair || "zh-en"),
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            ocr_text: "",
            text: "",
            ocr_meta: null,
            lastRunKind: runKind,
            error: (errObj && errObj.error) || "ocr_failed",
            message: (errObj && errObj.message) || "",
            details: (errObj && errObj.details) != null ? errObj.details : null,
            copyStatus: null,
            copyKind: null
          };
        } else {
          patch.translate = {
            phase: "error",
            lang_pair: langPair || "zh-en",
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            chunks: null,
            debouncePending: false,
            manualOverride: null,
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
            mode: mode,
            lang_pair: langPair || ((state.ocr && state.ocr.lang_pair) || state.translate_lang_pair || "zh-en"),
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            ocr_text: "",
            text: "",
            ocr_meta: null,
            lastRunKind: runKind,
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
            lang_pair: langPair || "zh-en",
            source_lang: null,
            target_lang: null,
            direction_mode: null,
            chunks: null,
            debouncePending: false,
            manualOverride: null,
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
      form.append("mode", mode);
      if (mode === "translate" && langPair) {
        form.append("lang_pair", langPair);
      }
      if (hasDraftParams) {
        form.append(
          "preprocess_params",
          typeof payload.preprocess_params === "string"
            ? payload.preprocess_params
            : JSON.stringify(payload.preprocess_params)
        );
      }
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
        var usedMode = (res && res.mode) || mode;
        if (slot === "ocr") {
          var rawOcr = (res && res.ocr_text) || (usedMode === "raw" && res && res.text) || "";
          setState({
            connection: "connected",
            ocr: {
              phase: "result",
              mode: usedMode,
              lang_pair: (res && res.lang_pair) || langPair || ((getState().ocr && getState().ocr.lang_pair) || "zh-en"),
              source_lang: (res && res.source_lang) || null,
              target_lang: (res && res.target_lang) || null,
              direction_mode: (res && res.direction_mode) || null,
              ocr_text: rawOcr,
              text: usedMode === "raw" ? "" : ((res && res.text) || ""),
              ocr_meta: (res && res.ocr_meta) != null ? res.ocr_meta : null,
              lastRunKind: runKind,
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
              lang_pair: (res && res.lang_pair) || langPair || "zh-en",
              source_lang: (res && res.source_lang) || null,
              target_lang: (res && res.target_lang) || null,
              direction_mode: (res && res.direction_mode) || null,
              chunks: (res && typeof res.chunks === "number") ? res.chunks : 1,
              debouncePending: false,
              manualOverride: null,
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

    function refreshAudioInputs() {
      if (destroyed) return Promise.resolve(null);
      if (!context || !context.canCallLocal()) {
        rejectNonLocal();
        return Promise.resolve(null);
      }
      var reqId = ++audioInputsReqId;
      setState({
        audioInputsLoading: true,
        audioInputError: "",
        audioInputSaved: false
      });
      return client.call(OP.audioInputs).then(function (data) {
        if (destroyed || reqId !== audioInputsReqId) return null;
        var mapped = root.mapAudioInputs(data);
        setState({
          audioInputs: mapped,
          audioInputsLoading: false,
          audioInputError: ""
        });
        return mapped;
      }, function (err) {
        if (destroyed || reqId !== audioInputsReqId) return null;
        setState({
          audioInputs: null,
          audioInputsLoading: false,
          audioInputError: formatErr(err)
        });
        return null;
      });
    }

    function toggleTheme(payload) {
      if (destroyed) return Promise.resolve(null);
      var curState = getState();
      var next = payload && payload.theme === "dark" ? "dark" : "light";
      var fallbackPrev = (curState.theme === "light" || curState.theme === "dark") && curState.theme !== next
        ? curState.theme
        : (next === "dark" ? "light" : "dark");
      var reqId = ++themeReqId;
      if (curState.theme !== next) {
        setState({ theme: next });
      }
      if (!context || !context.canCallLocal() || curState.connection !== "connected") {
        return Promise.resolve(null);
      }
      var isCurrent = function () { return !destroyed && reqId === themeReqId; };
      return setupActions.saveConfig(
        { ui_theme: next },
        {},
        {
          isCurrent: isCurrent,
          onError: function (errPatch) {
            errPatch.theme = persistedThemeKnown ? persistedTheme : fallbackPrev;
          }
        }
      ).then(function (config) {
        if (isCurrent() && config && (config.ui_theme === "light" || config.ui_theme === "dark")) {
          persistedTheme = config.ui_theme;
          persistedThemeKnown = true;
        }
        return config;
      }, function () {
        return null;
      });
    }

    function destroy() {
      destroyed = true;
      cancelDebounce();
      loadReqId += 1;
      translateReqId += 1;
      ocrReqId += 1;
      themeReqId += 1;
      audioInputsReqId += 1;
      setupActions.stopPoll();
      if (logsActions && typeof logsActions.destroy === "function") {
        logsActions.destroy();
      }
      if (ocrActions && typeof ocrActions.destroy === "function") {
        ocrActions.destroy();
      }
    }

    var hooks = Object.assign({}, setupActions.hooks, logsActions ? logsActions.hooks : {}, ocrActions ? ocrActions.hooks : {}, {
      onTranslateInput: function (payload) { return handleTranslateInput(payload); },
      onTranslateText: function (payload) { return translateText(payload); },
      onSwapDirection: function (payload) { return swapDirection(payload); },
      onClearTranslate: function () { return clearTranslate(); },
      onSubmitImage: function (payload) { return submitImage(payload); },
      onCopy: function (payload) { return copy(payload); },
      onRefreshHistory: function () { return refreshHistory(); },
      onLoadMoreHistory: function () { return loadMoreHistory(); },
      onRefreshLan: function () { return refreshLan(); },
      onRefreshAudioInputs: function () { return refreshAudioInputs(); },
      onRefreshStatus: function () { return loadAll(); },
      onToggleTheme: function (payload) { return toggleTheme(payload); },
      onSelectPanel: function (payload) {
        var panel = payload && payload.panel;
        if (!panel) return undefined;
        if (getState().activePanel !== panel) {
          setState({ activePanel: panel });
        }
        syncUrlForPanel(panel);
        if (logsActions && typeof logsActions.onSelectPanel === "function") {
          return logsActions.onSelectPanel(panel);
        }
        return undefined;
      }
    });

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: function () { return loadAll(); },
      loadAll: loadAll,
      saveConfig: setupActions.saveConfig,
      setPersistentNotice: setPersistentNotice,
      destroy: destroy,
      _debug: {
        getLanToken: function () { return lanToken; },
        getLanUrls: function () { return lanUrls.slice(); },
        isDestroyed: function () { return destroyed; },
        polling: function () { return setupActions.isPolling(); },
        debouncePending: function () { return debounceTimer != null; },
        previewDebouncePending: function () { return !!(ocrActions && ocrActions._debug && ocrActions._debug.previewDebouncePending()); },
        getOcrReqId: function () { return ocrReqId; }
      }
    };
  }

  root.createHomeController = createHomeController;
  root.HOME_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

/* 设置面板业务动作：一键装齐、组件重试、进行中重读设置检查与配置持久化。
 * 所有接口经由 OpenAPI catalog 按 operationId 派生，不手写接口路径，不使用 Web Storage。
 * 进度与组件状态只来自 setup/check；前端不推算某步是否完成。
 */
(function (root) {
  "use strict";

  var OP = {
    check: "setup_check_api_setup_check_get",
    installAll: "setup_install_all_api_setup_install_all_post",
    componentRetry: "retry_component_api_v1_components__name__retry_post",
    configPost: "post_config_api_v1_config_post"
  };

  /* 一键装齐每秒写一次步骤进度，设置检查每次都实时探测；重读不必比这更密。 */
  var DEFAULT_POLL_MS = 1000;

  /* 进行中 = 一键装齐在跑，或有组件正在启动 / 等待重启；进行中才继续重读。 */
  var BUSY_COMPONENT_STATES = ["starting", "backing_off"];

  function isBusy(mapped) {
    if (!mapped) return false;
    if (mapped.installAll && mapped.installAll.state === "running") return true;
    return (mapped.components || []).some(function (item) {
      return BUSY_COMPONENT_STATES.indexOf(item.state) >= 0;
    });
  }

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) {
      return labels.formatError(err && err.error, err && err.message, err && err.details);
    }
    return (err && (err.message || err.error)) || "请求失败";
  }

  function schedule(fn, ms, opts) {
    var start = (opts && opts.setTimeout) || (typeof setTimeout === "function" ? setTimeout : null);
    if (!start) {
      fn();
      return 0;
    }
    return start(fn, ms);
  }

  function unschedule(id, opts) {
    var stop = (opts && opts.clearTimeout) || (typeof clearTimeout === "function" ? clearTimeout : null);
    if (stop && id != null) stop(id);
  }

  function createSetupActions(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var setState = typeof opts.setState === "function" ? opts.setState : function () {};
    var isDestroyed = typeof opts.isDestroyed === "function" ? opts.isDestroyed : function () { return false; };
    var pollInterval = opts.pollInterval != null ? opts.pollInterval : DEFAULT_POLL_MS;
    var pollTimer = null;
    var pollGeneration = 0;

    function stopPoll() {
      unschedule(pollTimer, opts);
      pollTimer = null;
      pollGeneration += 1;
    }

    function isPolling() {
      return pollTimer != null;
    }

    function refreshCheck() {
      if (isDestroyed() || !context || !context.canCallLocal()) return Promise.resolve(null);
      return client.call(OP.check).then(function (check) {
        if (isDestroyed()) return null;
        var mapped = root.mapReadiness(check);
        setState(Object.assign({ connection: "connected" }, root.readinessPatch(mapped)));
        return mapped;
      }, function (err) {
        if (!isDestroyed()) setState({ statusNotice: formatErr(err) });
        return null;
      });
    }

    function schedulePoll() {
      if (isDestroyed() || pollTimer != null) return;
      var gen = pollGeneration;
      pollTimer = schedule(function () {
        pollTimer = null;
        if (isDestroyed() || gen !== pollGeneration) return;
        refreshCheck().then(function (mapped) {
          if (gen === pollGeneration && isBusy(mapped)) schedulePoll();
        });
      }, pollInterval, opts);
    }

    function maybeResumePoll(mapped) {
      if (isBusy(mapped)) schedulePoll();
    }

    function installAll() {
      if (isDestroyed()) return Promise.resolve(null);
      return client.call(OP.installAll).then(function (status) {
        if (isDestroyed()) return status;
        setState({ installAll: root.mapInstallAll(status), connection: "connected" });
        schedulePoll();
        return status;
      }, function (err) {
        if (!isDestroyed()) setState({ statusNotice: formatErr(err), connection: "connected" });
        return null;
      });
    }

    function retryComponent(name) {
      if (isDestroyed()) return Promise.resolve(null);
      return client.call(OP.componentRetry, { pathParams: { name: name } }).then(function (accepted) {
        if (isDestroyed()) return accepted;
        setState({ statusNotice: "已请求重试，等待看护层重新拉起", connection: "connected" });
        schedulePoll();
        return accepted;
      }, function (err) {
        if (!isDestroyed()) setState({ statusNotice: formatErr(err), connection: "connected" });
        return null;
      });
    }

    function saveConfig(patch, flags) {
      var body = root.pickLegalConfig(patch);
      return client.call(OP.configPost, { body: body }).then(function (config) {
        if (isDestroyed()) return config;
        var next = Object.assign({
          connection: "connected",
          statusNotice: null
        }, flags || {});
        root.applyConfig(next, config);
        setState(next);
        return config;
      }, function (err) {
        if (!isDestroyed()) {
          var notice = formatErr(err);
          var errPatch = { statusNotice: notice, connection: "connected" };
          if (flags && Object.prototype.hasOwnProperty.call(flags, "hotkeysSaved")) {
            errPatch.hotkeySaveError = notice;
          }
          setState(errPatch);
        }
        throw err;
      });
    }

    var hooks = {
      onInstallAll: function () { return installAll(); },
      onRetryComponent: function (name) { return retryComponent(name); },
      onToggleAutostart: function (enabled) {
        return saveConfig({ autostart_enabled: !!enabled }, {});
      },
      onTogglePolish: function (enabled) {
        return saveConfig({ polish_enabled: !!enabled }, {});
      },
      onSaveKeywords: function (value) {
        return saveConfig({ custom_keywords: root.parseKeywords(value) }, { keywordsSaved: true, keywordsDraft: null });
      },
      onChangeTargetLang: function (value) {
        return saveConfig({ translate_target_lang: value }, { targetLangSaved: true });
      },
      onSaveHotkeys: function (keys) {
        var patch = {
          hotkey_voice: keys && keys.hotkey_voice,
          hotkey_screenshot: keys && keys.hotkey_screenshot,
          hotkey_ocr: keys && keys.hotkey_ocr,
          hotkey_repeat: keys && keys.hotkey_repeat,
          hotkey_annotate: keys && keys.hotkey_annotate
        };
        if (!keys || keys.hotkey_ocr === undefined) delete patch.hotkey_ocr;
        if (!keys || keys.hotkey_annotate === undefined) delete patch.hotkey_annotate;
        return saveConfig(patch, {
          hotkeysSaved: true,
          hotkeyDrafts: null,
          hotkeyCapturing: null,
          hotkeyCaptureError: "",
          hotkeyCaptureWarning: "",
          hotkeyCaptureHint: "",
          hotkeySaveError: ""
        });
      }
    };

    return {
      installAll: installAll,
      retryComponent: retryComponent,
      maybeResumePoll: maybeResumePoll,
      refreshCheck: refreshCheck,
      stopPoll: stopPoll,
      isPolling: isPolling,
      saveConfig: saveConfig,
      hooks: hooks
    };
  }

  root.createSetupActions = createSetupActions;
  root.SETUP_ACTION_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

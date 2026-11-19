/* 设置面板业务动作：任务启动/轮询/取消、失败叠加与配置持久化。
 * 所有接口经由 OpenAPI catalog 按 operationId 派生，不手写接口路径，不使用 Web Storage。
 */
(function (root) {
  "use strict";

  var OP = {
    check: "setup_check_api_setup_check_get",
    taskStart: "setup_task_start_api_setup_tasks_post",
    taskGet: "setup_task_get_api_setup_tasks__task_id__get",
    status: "status_api_v1_status_get",
    configPost: "post_config_api_v1_config_post"
  };

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
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var isDestroyed = typeof opts.isDestroyed === "function" ? opts.isDestroyed : function () { return false; };
    var pollInterval = opts.pollInterval != null ? opts.pollInterval : 500;
    var pollTimer = null;
    var pollTaskId = null;
    var pollGeneration = 0;

    function stopPoll() {
      unschedule(pollTimer, opts);
      pollTimer = null;
      pollTaskId = null;
      pollGeneration += 1;
    }

    function getPollingTaskId() {
      return pollTaskId;
    }

    function applyTask(task) {
      if (isDestroyed() || !task) return;
      var state = getState();
      var next = root.applyTaskToModels(state.asr, state.ollama, task);
      var notice = task.state === "failed"
        ? (task.message || (task.error ? String(task.error) : "任务失败"))
        : (task.message || state.statusNotice);
      setState({
        asr: next.asr,
        ollama: next.ollama,
        statusNotice: notice,
        connection: "connected"
      });
    }

    function overlayTaskFailure(mapped, task) {
      if (!mapped || !task || task.state !== "failed") return mapped;
      var message = task.message || (task.error ? String(task.error) : "任务失败");
      var kind = task.kind;
      if (kind === "asr_start") {
        mapped.asr = Object.assign({}, mapped.asr, {
          progress: null,
          message: message,
          action: kind,
          taskId: task.task_id || mapped.asr.taskId
        });
      } else if (kind === "asr_download") {
        if (mapped.asr.status !== "failed" && mapped.asr.status !== "ready") {
          mapped.asr = Object.assign({}, mapped.asr, {
            status: "failed",
            progress: null,
            message: message,
            action: kind,
            taskId: task.task_id || mapped.asr.taskId
          });
        } else {
          mapped.asr = Object.assign({}, mapped.asr, {
            progress: null,
            message: message,
            action: kind,
            taskId: task.task_id || mapped.asr.taskId
          });
        }
      } else if (kind === "ollama_install" || kind === "ollama_start" || kind === "ollama_pull") {
        var ol = Object.assign({}, mapped.ollama, {
          progress: null,
          message: message,
          action: kind,
          taskId: task.task_id || mapped.ollama.taskId
        });
        if (kind === "ollama_pull" && ol.modelStatus !== "ready") ol.modelStatus = "failed";
        if (kind === "ollama_install" && ol.installed !== true) ol.installed = false;
        if (kind === "ollama_start" && ol.running !== true) ol.running = false;
        mapped.ollama = ol;
      }
      mapped.statusNotice = message;
      return mapped;
    }

    function refreshCheck(afterTask) {
      if (isDestroyed() || !context || !context.canCallLocal()) return Promise.resolve();
      return Promise.all([
        client.call(OP.check),
        client.call(OP.status)
      ]).then(function (results) {
        if (isDestroyed()) return;
        var mapped = overlayTaskFailure(root.mapReadiness(results[0], results[1]), afterTask);
        setState({
          asr: mapped.asr,
          ollama: mapped.ollama,
          unmetGates: mapped.gates,
          capabilities: mapped.capabilities,
          statusNotice: mapped.statusNotice,
          connection: "connected"
        });
      }, function (err) {
        if (isDestroyed()) return;
        setState({ statusNotice: formatErr(err) });
      });
    }

    function pollTask(taskId) {
      if (isDestroyed() || !taskId) return Promise.resolve();
      if (pollTaskId === taskId && pollTimer != null) return Promise.resolve();
      unschedule(pollTimer, opts);
      pollTimer = null;
      pollTaskId = taskId;
      var gen = pollGeneration;
      return new Promise(function (resolve) {
        pollTimer = schedule(function () {
          pollTimer = null;
          if (isDestroyed() || gen !== pollGeneration || pollTaskId !== taskId) {
            resolve();
            return;
          }
          client.call(OP.taskGet, { pathParams: { task_id: taskId } }).then(function (task) {
            if (isDestroyed() || gen !== pollGeneration) {
              resolve(task);
              return;
            }
            applyTask(task);
            if (task && task.state === "running") {
              pollTaskId = null;
              return pollTask(task.task_id).then(resolve);
            }
            pollTaskId = null;
            return refreshCheck(task).then(function () { resolve(task); });
          }, function (err) {
            if (isDestroyed() || gen !== pollGeneration) {
              resolve();
              return;
            }
            pollTaskId = null;
            setState({ statusNotice: formatErr(err), connection: "connected" });
            resolve();
          });
        }, pollInterval, opts);
      });
    }

    function maybeResumePoll(mapped) {
      if (isDestroyed()) return;
      var asrId = mapped && mapped.asr && mapped.asr.taskId && (mapped.asr.status === "downloading" || mapped.asr.action === "asr_start")
        ? mapped.asr.taskId : null;
      var olId = mapped && mapped.ollama && mapped.ollama.taskId && (mapped.ollama.modelStatus === "downloading" || mapped.ollama.action === "ollama_install" || mapped.ollama.action === "ollama_start")
        ? mapped.ollama.taskId : null;
      var taskId = asrId || olId;
      if (taskId) pollTask(taskId);
    }

    function startTask(kind) {
      if (isDestroyed()) return Promise.resolve();
      setState({ statusNotice: "正在启动任务…" });
      return client.call(OP.taskStart, { body: { kind: kind } }).then(function (task) {
        if (isDestroyed()) return task;
        applyTask(task);
        if (task && task.state === "running") return pollTask(task.task_id).then(function () { return task; });
        if (task && (task.state === "succeeded" || task.state === "failed")) {
          return refreshCheck(task).then(function () { return task; });
        }
        return task;
      }, function (err) {
        if (isDestroyed()) return;
        var message = formatErr(err);
        var state = getState();
        if (kind === "asr_download") {
          setState({
            asr: Object.assign({}, state.asr || {}, { status: "failed", progress: null, message: message, action: kind }),
            statusNotice: message,
            connection: "connected"
          });
        } else if (kind === "asr_start") {
          setState({
            asr: Object.assign({}, state.asr || {}, { progress: null, message: message, action: kind }),
            statusNotice: message,
            connection: "connected"
          });
        } else {
          var ol = Object.assign({}, state.ollama || {}, { progress: null, message: message, action: kind });
          if (kind === "ollama_pull") ol.modelStatus = "failed";
          setState({ ollama: ol, statusNotice: message, connection: "connected" });
        }
      });
    }

    function noticeFromGates(state) {
      var gates = (state && (state.unmetGates || state.unmet_gates)) || [];
      return gates.length ? ("未达上线条件：" + gates.join("；")) : null;
    }

    function saveConfig(patch, flags) {
      var body = root.pickLegalConfig(patch);
      return client.call(OP.configPost, { body: body }).then(function (config) {
        if (isDestroyed()) return config;
        var next = Object.assign({
          connection: "connected",
          statusNotice: noticeFromGates(getState())
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
      onDownloadAsr: function () { return startTask("asr_download"); },
      onStartAsr: function () { return startTask("asr_start"); },
      onInstallOllama: function () { return startTask("ollama_install"); },
      onStartOllama: function () { return startTask("ollama_start"); },
      onDownloadOllamaModel: function () { return startTask("ollama_pull"); },
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
          hotkey_repeat: keys && keys.hotkey_repeat
        };
        if (!keys || keys.hotkey_ocr === undefined) delete patch.hotkey_ocr;
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
      startTask: startTask,
      pollTask: pollTask,
      maybeResumePoll: maybeResumePoll,
      refreshCheck: refreshCheck,
      overlayTaskFailure: overlayTaskFailure,
      stopPoll: stopPoll,
      getPollingTaskId: getPollingTaskId,
      saveConfig: saveConfig,
      hooks: hooks
    };
  }

  root.createSetupActions = createSetupActions;
  root.SETUP_ACTION_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

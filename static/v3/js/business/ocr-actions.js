/* 图片 OCR 预处理预览与全局预设持久化业务动作：
 * 180ms 滑块防抖（新图/点按钮 0ms 立即请求，切换新图时同步递增 ocrReqId 丢弃旧图在途 OCR 响应）、
 * 单调 previewReqId 抗竞态、非乐观预设持久化（savingPresets 在途写锁，失败保草稿并回显后端可读错误）。
 * 所有接口经由 OpenAPI catalog 按冻结 operationId 派生，不手写路径，不使用 Web Storage。
 */
(function (root) {
  "use strict";

  var OP = {
    preview: "ocr_preview_api_v1_ocr_preview_post"
  };

  var DEFAULT_PREVIEW_DEBOUNCE_MS = 180;

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

  function createOcrActions(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var setupActions = opts.setupActions;
    var setState = typeof opts.setState === "function" ? opts.setState : function () {};
    var getState = typeof opts.getState === "function" ? opts.getState : function () { return {}; };
    var isDestroyed = typeof opts.isDestroyed === "function" ? opts.isDestroyed : function () { return false; };
    var onInvalidateOcr = typeof opts.onInvalidateOcr === "function" ? opts.onInvalidateOcr : function () {};
    var nonLocalMsgFn = typeof opts.nonLocalMessage === "function"
      ? opts.nonLocalMessage
      : function () { return "本地工作台仅允许在本机访问，已阻止在当前环境发出请求。"; };
    var previewDebounceMs = opts.previewDebounceMs != null ? opts.previewDebounceMs : DEFAULT_PREVIEW_DEBOUNCE_MS;

    var previewReqId = 0;
    var saveReqId = 0;
    var previewTimer = null;
    var pendingPreviewResolve = null;
    var lastPreviewFile = null;
    var savingInFlight = false;

    function canCallLocal() {
      return !!(context && typeof context.canCallLocal === "function" && context.canCallLocal());
    }

    function cancelPendingPreview() {
      if (previewTimer != null) {
        unscheduleTimer(previewTimer, opts);
        previewTimer = null;
      }
      if (pendingPreviewResolve) {
        var resolvePending = pendingPreviewResolve;
        pendingPreviewResolve = null;
        resolvePending(null);
      }
    }

    function executePreview(reqId, file, params) {
      if (isDestroyed() || reqId !== previewReqId) return Promise.resolve(null);
      setState({
        ocr: {
          previewPhase: "loading",
          previewErrorCode: null,
          previewError: ""
        }
      });

      var form = new FormData();
      var filename = (file && file.name) || "clipboard-image.png";
      if (file instanceof Blob || (typeof File !== "undefined" && file instanceof File)) {
        form.append("image", file, filename);
      } else {
        form.append("image", file);
      }
      if (params != null) {
        form.append("preprocess_params", typeof params === "string" ? params : JSON.stringify(params));
      }

      return client.call(OP.preview, { body: form }).then(function (res) {
        if (isDestroyed() || reqId !== previewReqId) return null;
        var b64 = (res && typeof res.image_base64 === "string") ? res.image_base64 : "";
        if (!b64) {
          setState({
            connection: "connected",
            ocr: {
              previewPhase: "error",
              previewErrorCode: "internal",
              previewError: "后端未返回预览图数据"
            }
          });
          return null;
        }
        var mediaType = (res && res.media_type) || "image/png";
        setState({
          connection: "connected",
          ocr: {
            processedPreviewUrl: "data:" + mediaType + ";base64," + b64,
            previewPhase: "ready",
            previewErrorCode: null,
            previewError: "",
            imageWidth: res.image_width != null ? res.image_width : null,
            imageHeight: res.image_height != null ? res.image_height : null,
            previewWidth: res.preview_width != null ? res.preview_width : null,
            previewHeight: res.preview_height != null ? res.preview_height : null,
            otsuThreshold: res.otsu_threshold != null ? res.otsu_threshold : null
          }
        });
        return res;
      }, function (err) {
        if (isDestroyed() || reqId !== previewReqId) return null;
        setState({
          connection: err && err.blocked ? "error" : (getState().connection || "connected"),
          ocr: {
            previewPhase: "error",
            previewErrorCode: (err && err.error) || "ocr_failed",
            previewError: (err && err.message) || ""
          }
        });
        return null;
      });
    }

    function requestPreprocessPreview(payload) {
      if (isDestroyed()) return Promise.resolve(null);
      payload = payload || {};
      var file = payload.file;
      if (!file) {
        cancelPendingPreview();
        previewReqId += 1;
        lastPreviewFile = null;
        return Promise.resolve(null);
      }
      var isNewFile = file !== lastPreviewFile;
      if (isNewFile) {
        lastPreviewFile = file;
        /* 切换新图时同步递增 ocrReqId，确保上一张图片的在途 OCR 响应不会覆写新图视图。 */
        onInvalidateOcr();
      }
      cancelPendingPreview();
      if (!canCallLocal()) {
        previewReqId += 1;
        setState({
          ocr: {
            previewPhase: "error",
            previewErrorCode: "forbidden",
            previewError: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++previewReqId;
      var immediate = !!payload.immediate || isNewFile;
      if (immediate) {
        return executePreview(reqId, file, payload.params);
      }
      return new Promise(function (resolve) {
        pendingPreviewResolve = resolve;
        previewTimer = scheduleTimer(function () {
          previewTimer = null;
          var finish = pendingPreviewResolve;
          pendingPreviewResolve = null;
          if (isDestroyed() || reqId !== previewReqId) {
            if (finish) finish(null);
            return;
          }
          executePreview(reqId, file, payload.params).then(
            function (val) { if (finish) finish(val); },
            function () { if (finish) finish(null); }
          );
        }, previewDebounceMs, opts);
      });
    }

    function saveOcrPresets(payload) {
      if (isDestroyed() || savingInFlight) return Promise.resolve(null);
      payload = payload || {};
      var presets = Array.isArray(payload.presets) ? payload.presets : null;
      if (!presets) return Promise.resolve(null);
      if (!canCallLocal()) {
        setState({
          ocr: {
            savingPresets: false,
            presetsSaved: false,
            presetsSaveError: nonLocalMsgFn()
          }
        });
        return Promise.resolve(null);
      }
      var reqId = ++saveReqId;
      var isCurrent = function () { return !isDestroyed() && reqId === saveReqId; };
      savingInFlight = true;
      /* 非乐观更新：写锁期间不改写 ocr_preprocess_presets，仅待服务端回读确认后统一刷新。 */
      setState({
        ocr: {
          savingPresets: true,
          presetsSaved: false,
          presetsSaveError: ""
        }
      });
      return setupActions.saveConfig(
        { ocr_preprocess_presets: presets },
        {
          ocr: {
            savingPresets: false,
            presetsSaved: true,
            presetsSaveError: ""
          }
        },
        {
          isCurrent: isCurrent,
          onError: function (errPatch, _err, notice) {
            errPatch.ocr = {
              savingPresets: false,
              presetsSaved: false,
              presetsSaveError: notice
            };
          }
        }
      ).then(function (config) {
        if (reqId === saveReqId) savingInFlight = false;
        return isCurrent() ? config : null;
      }, function () {
        if (reqId === saveReqId) savingInFlight = false;
        return null;
      });
    }

    function clearOcr() {
      cancelPendingPreview();
      previewReqId += 1;
      lastPreviewFile = null;
      onInvalidateOcr();
      if (isDestroyed()) return;
      setState({
        copyStatus: null,
        copyKind: null,
        ocr: {
          phase: "empty",
          fileName: "",
          previewUrl: "",
          processedPreviewUrl: "",
          previewPhase: "idle",
          previewErrorCode: null,
          previewError: "",
          imageWidth: null,
          imageHeight: null,
          previewWidth: null,
          previewHeight: null,
          otsuThreshold: null,
          source_lang: null,
          target_lang: null,
          direction_mode: null,
          ocr_text: "",
          text: "",
          ocr_meta: null,
          lastRunKind: null,
          error: null,
          message: "",
          details: null,
          copyStatus: null,
          copyKind: null
        }
      });
    }

    function destroy() {
      cancelPendingPreview();
      previewReqId += 1;
      saveReqId += 1;
      savingInFlight = false;
      lastPreviewFile = null;
    }

    var hooks = {
      onRequestPreprocessPreview: function (payload) { return requestPreprocessPreview(payload); },
      onSaveOcrPresets: function (payload) { return saveOcrPresets(payload); },
      onClearOcr: function () { return clearOcr(); }
    };

    return {
      requestPreprocessPreview: requestPreprocessPreview,
      saveOcrPresets: saveOcrPresets,
      clearOcr: clearOcr,
      destroy: destroy,
      hooks: hooks,
      _debug: {
        previewDebouncePending: function () { return previewTimer != null; },
        savingInFlight: function () { return savingInFlight; }
      }
    };
  }

  root.createOcrActions = createOcrActions;
  root.OCR_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});

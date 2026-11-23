/* 纯 UI 默认视图状态：未连接、无数据。不把缺省值写成用户已选或已成功。
 *
 * 配置字段与 AppConfig 对齐：
 *   ui_theme, audio_input_device, polish_enabled, custom_keywords, translate_lang_pair, translate_auto_enabled,
 *   hotkey_voice, hotkey_screenshot, hotkey_ocr, hotkey_repeat, hotkey_annotate, autostart_enabled
 * 历史记录字段与 HistoryRecord / HistoryListResponse 对齐：
 *   timestamp, raw_text, final_text, language, mode, source, polish, job_id,
 *   records, total, skipped_malformed
 * 字幕只呈现 stable_text / tail_text（及各产品事件已有字段）。
 * 错误体与 ErrorBody 对齐：ok=false, error, message, details
 *
 * 下列为界面状态标记（apiPaths 由业务层从 OpenAPI 快照派生后注入，缺省为尚未读取）：
 *   connection, asr/ollama 就绪与进度、installAll（一键装齐）、components（看护组件）、lanEntries、capabilities、apiPaths、aboutText 等。
 * 缺省不得显示为已就绪或已下载成功。
 */
(function (V3) {
  "use strict";

  function homeState() {
    return {
      connection: "disconnected",
      connectionError: null,
      error: null,
      message: "",
      statusNotice: null,
      aboutText: "",
      theme: "light",
      activePanel: "translate",
      setupTab: "models",
      asr: {
        name: V3.labels.FIXED_MODELS.asr,
        status: "unknown",
        sidecarRunning: null,
        sizeLabel: null,
        deviceLabel: null,
        progress: null,
        message: null
      },
      ollama: {
        modelName: V3.labels.FIXED_MODELS.ollama,
        installed: null,
        running: null,
        version: null,
        modelStatus: "unknown",
        sizeLabel: null,
        progress: null,
        message: null
      },
      installAll: null,
      components: null,
      autostart_enabled: null,
      audio_input_device: null,
      audioInputs: null,
      audioInputsLoading: false,
      audioInputError: "",
      audioInputSaved: false,
      polish_enabled: null,
      custom_keywords: [],
      keywordsDraft: null,
      keywordsSaved: false,
      translate_lang_pair: null,
      translate_auto_enabled: null,
      langPairOptions: null,
      translateSettingsSaved: false,
      hotkey_voice: "",
      hotkey_screenshot: "",
      hotkey_ocr: "",
      hotkey_repeat: "",
      hotkey_annotate: "",
      hotkeyDrafts: null,
      hotkeyCapturing: null,
      hotkeyFocus: null,
      hotkeyCaptureError: "",
      hotkeyCaptureWarning: "",
      hotkeyCaptureHint: "",
      hotkeySaveError: "",
      hotkeysSaved: false,
      translate: {
        phase: "empty",
        sourceText: "",
        lang_pair: "zh-en",
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
      },
      ocr: {
        phase: "empty",
        mode: "raw",
        lang_pair: "zh-en",
        source_lang: null,
        target_lang: null,
        direction_mode: null,
        fileName: "",
        previewUrl: "",
        ocr_text: "",
        text: "",
        error: null,
        message: "",
        details: null,
        copyStatus: null,
        copyKind: null
      },
      history: {
        records: [],
        total: null,
        skipped_malformed: 0,
        loading: false,
        message: null
      },
      lanEntries: null,
      lanMessage: "",
      tokenMasked: "",
      tokenPresent: false,
      serviceUrl: "",
      localUrl: "",
      capabilities: null,
      apiPaths: null,
      copyKind: null,
      copyStatus: null
    };
  }

  function voiceState() {
    return {
      connection: "disconnected",
      tokenValid: null,
      phase: "idle",
      statusText: "",
      statusTone: "",
      elapsed_label: "",
      job_id: null,
      job_state: null,
      text: "",
      raw_text: "",
      polish: null,
      error: null,
      message: "",
      details: null,
      useFileUpload: false,
      fileUploadHint: "",
      recordHotkeyCode: "Space",
      recordHotkeyLabel: "空格",
      capturingHotkey: false,
      history: {
        records: [],
        total: null,
        skipped_malformed: 0,
        loading: false,
        message: null
      },
      micEnabled: false
    };
  }

  function merge(base, patch) {
    if (!patch) return base;
    const out = Object.assign({}, base);
    Object.keys(patch).forEach(function (key) {
      const value = patch[key];
      const prev = base[key];
      if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        prev &&
        typeof prev === "object" &&
        !Array.isArray(prev)
      ) {
        out[key] = merge(prev, value);
      } else {
        out[key] = value;
      }
    });
    return out;
  }

  V3.defaults = {
    homeState: homeState,
    voiceState: voiceState,
    merge: merge
  };
})(window.V3 = window.V3 || {});

/* 把 setup/check、config、history、一键装齐进度、看护组件状态、局域网入口映到设置页状态。
 * 组件就绪只看 asr.status / ollama.model_status 等真实字段，不以 check.ok 冒充全部就绪。
 * 进度只展示服务端给出的 percent/字节/stage，不推算假百分比。
 */
(function (root) {
  "use strict";

  var LEGAL_CONFIG_KEYS = [
    "polish_enabled",
    "custom_keywords",
    "translate_lang_pair",
    "translate_auto_enabled",
    "hotkey_voice",
    "hotkey_screenshot",
    "hotkey_ocr",
    "hotkey_repeat",
    "hotkey_annotate",
    "autostart_enabled",
    "ui_theme",
    "audio_input_device",
    "log_max_lines"
  ];

  function pickLegalConfig(patch) {
    var out = {};
    if (!patch || typeof patch !== "object") return out;
    LEGAL_CONFIG_KEYS.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(patch, key)) out[key] = patch[key];
    });
    return out;
  }

  function parseKeywords(value) {
    if (Array.isArray(value)) {
      return value.map(function (v) { return String(v).trim(); }).filter(Boolean);
    }
    return String(value || "").split(/[,，、\s]+/).map(function (v) { return v.trim(); }).filter(Boolean);
  }

  function applyConfig(state, config) {
    if (!config || typeof config !== "object") return state;
    state.polish_enabled = config.polish_enabled;
    state.custom_keywords = Array.isArray(config.custom_keywords) ? config.custom_keywords.slice() : [];
    state.translate_lang_pair = config.translate_lang_pair != null ? config.translate_lang_pair : null;
    state.translate_auto_enabled = typeof config.translate_auto_enabled === "boolean" ? config.translate_auto_enabled : null;
    state.hotkey_voice = config.hotkey_voice || "";
    state.hotkey_screenshot = config.hotkey_screenshot || "";
    state.hotkey_ocr = config.hotkey_ocr || "";
    state.hotkey_repeat = config.hotkey_repeat || "";
    state.hotkey_annotate = config.hotkey_annotate || "";
    state.autostart_enabled = config.autostart_enabled;
    if (config.ui_theme === "light" || config.ui_theme === "dark") {
      state.theme = config.ui_theme;
    }
    if (config.audio_input_device != null) {
      state.audio_input_device = String(config.audio_input_device);
    }
    if (typeof config.log_max_lines === "number" && isFinite(config.log_max_lines)) {
      state.log_max_lines = config.log_max_lines;
    }
    return state;
  }

  function mapAudioInputs(raw) {
    if (!raw || typeof raw !== "object") return null;
    var list = Array.isArray(raw.devices) ? raw.devices : [];
    var devices = [];
    list.forEach(function (item) {
      if (!item || !item.name) return;
      devices.push({
        name: String(item.name),
        hostapi: item.hostapi != null ? String(item.hostapi) : "",
        isDefault: item.is_default === true
      });
    });
    return {
      defaultDevice: (raw.default_device != null && raw.default_device !== "") ? String(raw.default_device) : null,
      devices: devices
    };
  }

  function formatBytes(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return null;
    if (n < 1024) return String(Math.round(n)) + " B";
    if (n < 1024 * 1024) {
      var kb = n / 1024;
      return (Math.round(kb * 10) / 10).toString() + " KB";
    }
    if (n < 1024 * 1024 * 1024) {
      var mb = n / (1024 * 1024);
      return (Math.round(mb * 10) / 10).toString() + " MB";
    }
    var gb = n / (1024 * 1024 * 1024);
    return gb.toFixed(2) + " GB";
  }

  function mapProgress(progress) {
    if (!progress || typeof progress !== "object") return null;
    var percent = (typeof progress.percent === "number" && isFinite(progress.percent)) ? progress.percent : null;
    var receivedLabel = formatBytes(progress.completed_bytes);
    var totalLabel = formatBytes(progress.total_bytes);
    var stage = progress.stage != null && progress.stage !== "" ? String(progress.stage) : null;
    return {
      percent: percent,
      receivedLabel: receivedLabel,
      totalLabel: totalLabel,
      stage: stage
    };
  }

  function mapAsr(asr) {
    asr = asr || {};
    var status = asr.status || null;
    return {
      name: asr.model_name || (typeof V3 !== "undefined" && V3.labels && V3.labels.FIXED_MODELS.asr) || "Qwen3-ASR-1.7B",
      status: status,
      sizeLabel: asr.size_label != null ? asr.size_label : null,
      deviceLabel: asr.device_label != null ? asr.device_label : null,
      progress: status === "downloading" ? mapProgress(asr.progress) : (asr.progress ? mapProgress(asr.progress) : null),
      message: asr.message || "",
      weightsPresent: asr.weights_present,
      sidecarRunning: asr.sidecar_running
    };
  }

  function mapOllama(ollama) {
    ollama = ollama || {};
    var modelStatus = ollama.model_status || null;
    return {
      modelName: ollama.model_name || (typeof V3 !== "undefined" && V3.labels && V3.labels.FIXED_MODELS.ollama) || "qwen3.5:4b",
      installed: typeof ollama.installed === "boolean" ? ollama.installed : null,
      running: typeof ollama.running === "boolean" ? ollama.running : null,
      version: ollama.version != null ? ollama.version : null,
      modelStatus: modelStatus,
      sizeLabel: ollama.size_label != null ? ollama.size_label : null,
      progress: modelStatus === "downloading" ? mapProgress(ollama.progress) : (ollama.progress ? mapProgress(ollama.progress) : null),
      message: ollama.message || ""
    };
  }

  /* InstallAllStatus：五步固定顺序原样保留，进度只做字节换算。 */
  function mapInstallAll(status) {
    if (!status || typeof status !== "object") return null;
    return {
      state: status.state,
      current: status.current != null ? status.current : null,
      steps: (Array.isArray(status.steps) ? status.steps : []).map(function (step) {
        return {
          kind: step.kind,
          state: step.state,
          message: step.message || "",
          progress: mapProgress(step.progress)
        };
      })
    };
  }

  function mapComponents(list) {
    if (!Array.isArray(list)) return null;
    return list.map(function (item) {
      return {
        name: item.name,
        state: item.state,
        reason: item.reason || "",
        failures: item.failures || 0
      };
    });
  }

  function mapCapabilities(list) {
    if (!Array.isArray(list)) return null;
    return list.map(function (item) {
      item = item || {};
      return {
        id: item.id,
        name: item.label || item.id || "未命名能力",
        label: item.label,
        available: item.available,
        required: item.required,
        detail: item.detail != null ? item.detail : "",
        message: item.detail != null ? item.detail : ""
      };
    });
  }

  function mapLanAccess(lan) {
    lan = lan || {};
    var entries = Array.isArray(lan.entries) ? lan.entries.map(function (entry) {
      entry = entry || {};
      return {
        ip: entry.ip || "",
        url: entry.url || "",
        urlMasked: entry.url_masked || "",
        qrSvg: entry.qr_svg || ""
      };
    }) : [];
    return {
      port: lan.port,
      token: lan.token || "",
      tokenMasked: lan.token_masked || "",
      tokenPresent: !!(lan.token),
      message: lan.message || "",
      entries: entries
    };
  }

  function mapReadiness(check) {
    var asr = mapAsr(check && check.asr);
    var ollama = mapOllama(check && check.ollama);
    var capabilities = mapCapabilities(check && check.capabilities);
    var asrMsg = asr.message || "";
    return {
      asr: asr,
      ollama: ollama,
      capabilities: capabilities,
      installAll: mapInstallAll(check && check.install_all),
      components: mapComponents(check && check.components),
      sessionType: check && check.session_type ? check.session_type : null,
      asrMessage: asrMsg
    };
  }

  /* mapReadiness 的结果 → 视图状态补丁；首次加载与进行中重读共用这一份。读到最新状态即清掉上一次的操作提示。 */
  function readinessPatch(mapped) {
    return {
      statusNotice: null,
      asr: mapped.asr,
      ollama: mapped.ollama,
      capabilities: mapped.capabilities,
      installAll: mapped.installAll,
      components: mapped.components
    };
  }

  function applyHistory(payload) {
    payload = payload || {};
    return {
      records: Array.isArray(payload.records) ? payload.records : [],
      total: payload.total != null ? payload.total : 0,
      skipped_malformed: payload.skipped_malformed || 0,
      loading: false,
      message: null
    };
  }

  function publicLanEntries(entries) {
    return (entries || []).map(function (entry) {
      return {
        ip: entry.ip || "",
        urlMasked: entry.urlMasked || entry.url_masked || "",
        qrSvg: entry.qrSvg || entry.qr_svg || ""
      };
    });
  }

  function mapApiPaths(catalog) {
    if (!catalog || typeof catalog.all !== "function") return null;
    return catalog.all().slice().sort(function (a, b) {
      var p = String(a.path).localeCompare(String(b.path));
      return p !== 0 ? p : String(a.method).localeCompare(String(b.method));
    }).map(function (op) {
      return {
        method: op.method,
        path: op.path,
        operationId: op.operationId,
        access: op.access
      };
    });
  }

  root.LEGAL_CONFIG_KEYS = LEGAL_CONFIG_KEYS;
  root.pickLegalConfig = pickLegalConfig;
  root.parseKeywords = parseKeywords;
  root.applyConfig = applyConfig;
  root.mapAudioInputs = mapAudioInputs;
  root.formatBytes = formatBytes;
  root.mapProgress = mapProgress;
  root.mapAsr = mapAsr;
  root.mapOllama = mapOllama;
  root.mapInstallAll = mapInstallAll;
  root.mapComponents = mapComponents;
  root.mapCapabilities = mapCapabilities;
  root.mapLanAccess = mapLanAccess;
  root.mapReadiness = mapReadiness;
  root.readinessPatch = readinessPatch;
  root.applyHistory = applyHistory;
  root.publicLanEntries = publicLanEntries;
  root.mapApiPaths = mapApiPaths;
})(window.V3Business = window.V3Business || {});

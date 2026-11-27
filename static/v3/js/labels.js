/* 契约枚举与错误码的中文展示。字段名与 _contracts.py / OpenAPI 一致。 */
(function (V3) {
  "use strict";

  const ERROR_CODE = {
    invalid_request: "请求无效",
    not_found: "未找到",
    conflict: "冲突",
    unauthorized: "未授权",
    forbidden: "禁止访问",
    timeout: "超时",
    dependency_failed: "依赖失败",
    asr_unavailable: "语音识别不可用",
    polish_failed: "润色失败",
    translate_failed: "翻译失败",
    ocr_failed: "文字识别失败",
    ocr_language_missing: "缺少识别语言包",
    history_io: "历史记录读写失败",
    jobs_io: "任务读写失败",
    delivery_failed: "投递失败",
    config_io: "配置读写失败",
    logs_io: "日志读写失败",
    internal: "内部错误"
  };

  const JOB_STATE = {
    queued: "已排队",
    claimed: "处理中",
    done: "已完成",
    failed: "失败",
    delivery_unknown: "投递结果未知",
    unknown: "任务不存在"
  };

  const UTTERANCE_SOURCE = {
    desktop: "本机",
    web: "手机",
    resend: "重发"
  };

  const HISTORY_POLISH = {
    on: "已润色",
    off: "未润色",
    failed: "润色失败",
    skipped: "已跳过"
  };

  const PRODUCT_EVENT = {
    idle: "就绪",
    recording: "录音中",
    partial: "识别中",
    polishing: "润色中",
    typed: "已输入",
    error: "出错",
    busy: "已排队",
    history_write_failed: "历史未写入",
    delivery_unknown: "投递结果未知",
    component_command: "服务命令",
    config_changed: "配置已更新"
  };

  const SESSION_CLOSED_REASON = {
    finished: "已结束",
    cancelled: "已取消",
    error: "出错",
    timeout: "超时",
    disconnected: "连接已断开"
  };

  /* 一键装齐五步（InstallStepKind，固定顺序）与步状态（InstallStepState）。 */
  const INSTALL_STEP = {
    ollama_install: "安装 Ollama 程序",
    wait_ollama: "等待 Ollama 运行",
    ollama_pull: "下载纠错与翻译模型",
    asr_runtime_install: "安装语音识别运行环境",
    asr_download: "下载语音识别模型"
  };

  const INSTALL_STEP_STATE = {
    pending: "待执行",
    running: "进行中",
    skipped: "已具备",
    done: "已完成",
    failed: "失败"
  };

  /* 看护层组件（ComponentName）与组件状态（ComponentState）。 */
  const COMPONENT = {
    ollama: "Ollama 纠错/翻译服务",
    asr: "语音识别服务",
    server: "本地服务"
  };

  const COMPONENT_STATE = {
    not_installed: "未安装",
    starting: "启动中",
    running: "运行中",
    backing_off: "等待重启",
    stopped: "已停止"
  };

  const SETUP_TABS = [
    { id: "models", label: "模型与服务" },
    { id: "voice", label: "语音与润色" },
    { id: "shot", label: "截图翻译" },
    { id: "keys", label: "快捷键" },
    { id: "api", label: "开放接口" },
    { id: "sys", label: "系统检测" }
  ];

  /* 翻译语言对字典与语种简称（先支持中⇄英，结构预留扩展其他语言对）。 */
  const LANG_PAIR_OPTIONS = [
    { value: "zh-en", label: "中 ⇄ 英" }
  ];

  const LANG_PAIR_PARTS = {
    "zh-en": ["zh", "en"]
  };

  const LANG_SHORT_NAME = {
    zh: "中",
    en: "英"
  };

  const FIXED_MODELS = {
    asr: "Qwen3-ASR-1.7B",
    ollama: "qwen3.5:4b"
  };

  const HOTKEY_LABELS = {
    hotkey_voice: "语音输入",
    hotkey_screenshot: "截图翻译",
    hotkey_ocr: "纯 OCR 识别",
    hotkey_repeat: "重复上次输入",
    hotkey_annotate: "截屏标注"
  };

  const HOME_NAVS = [
    { id: "translate", label: "翻译框 · 主页" },
    { id: "ocr", label: "图片 OCR 识别" },
    { id: "history", label: "语音与翻译历史" },
    { id: "lan", label: "手机扫码语音" },
    { id: "setup", label: "模型与热键设置" },
    { id: "logs", label: "运行日志" },
    { id: "about", label: "关于" }
  ];

  const LOG_MAX_LINES_OPTIONS = [500, 2000, 5000];

  const LOG_GROUPS = {
    runtime: "运行服务",
    setup: "安装过程"
  };

  const RUN_MODE = {
    frozen: "打包运行",
    deb: "安装包运行",
    source: "源码运行"
  };

  const OS_NAME = {
    linux: "Linux",
    windows: "Windows",
    macos: "macOS"
  };

  /* OCR 预处理预设契约枚举展示标签（起步模板参数仅从后端 state.ocrStarterTemplates 注入，此处严禁复制）。 */
  const OCR_PREPROCESS_MAX_ENABLED = 3;

  const OCR_BINARIZE_MODE = {
    off: "关闭",
    otsu: "自动 (Otsu)",
    manual: "手动"
  };

  const OCR_BINARIZE_SHORT = {
    off: "关闭",
    otsu: "自动",
    manual: "手动"
  };

  const OCR_RUN_MODE = {
    original: "原图直识",
    preset_chain: "启用预设链路",
    draft_params: "当前调参试识别"
  };

  function labelOf(map, value, fallback) {
    if (value == null || value === "") return fallback || "";
    if (Object.prototype.hasOwnProperty.call(map, value)) return map[value];
    return fallback != null ? fallback : String(value);
  }

  function errorCode(code) {
    return labelOf(ERROR_CODE, code, code ? String(code) : "错误");
  }

  function formatError(error, message, details) {
    const parts = [];
    if (error) parts.push(errorCode(error));
    if (message) parts.push(String(message));
    const jobId = details && details.job_id;
    if (jobId) parts.push("任务 " + jobId);
    return parts.join("：") || "发生错误";
  }

  /* 统一界面报错格式化（支持错误码字符串、{error,message,details} 对象或后端原文，供 home.js 与 OCR 面板共用）。 */
  function formatUiError(errCodeOrText, message, details, fallback) {
    if (typeof errCodeOrText === "string" && errCodeOrText) {
      if (Object.prototype.hasOwnProperty.call(ERROR_CODE, errCodeOrText)) {
        return formatError(errCodeOrText, message, details);
      }
      return (message && String(message) !== errCodeOrText)
        ? (errCodeOrText + "：" + String(message))
        : errCodeOrText;
    }
    if (errCodeOrText && typeof errCodeOrText === "object") {
      const innerErr = errCodeOrText.error;
      const innerMsg = errCodeOrText.message != null && errCodeOrText.message !== "" ? errCodeOrText.message : message;
      const innerDetails = errCodeOrText.details || details;
      if (typeof innerErr === "string" && innerErr) {
        if (Object.prototype.hasOwnProperty.call(ERROR_CODE, innerErr)) {
          return formatError(innerErr, innerMsg, innerDetails);
        }
        return (innerMsg && String(innerMsg) !== innerErr)
          ? (innerErr + "：" + String(innerMsg))
          : innerErr;
      }
      if (innerMsg) return formatError(null, innerMsg, innerDetails);
    }
    if (message) return String(message);
    return fallback || "发生错误";
  }

  function source(value) {
    return labelOf(UTTERANCE_SOURCE, value, "");
  }

  function polish(value) {
    return labelOf(HISTORY_POLISH, value, "");
  }

  function jobState(value) {
    return labelOf(JOB_STATE, value, value ? String(value) : "");
  }

  function productEvent(value) {
    return labelOf(PRODUCT_EVENT, value, "");
  }

  function langPairLabel(value, options) {
    if (value == null || value === "") return "";
    const list = options && options.length ? options : LANG_PAIR_OPTIONS;
    for (let i = 0; i < list.length; i++) {
      if (list[i].value === value) return list[i].label;
    }
    return String(value);
  }

  function directionLabel(sourceLang, targetLang) {
    if (!sourceLang || !targetLang) return "";
    const src = labelOf(LANG_SHORT_NAME, sourceLang, String(sourceLang));
    const tgt = labelOf(LANG_SHORT_NAME, targetLang, String(targetLang));
    return src + " → " + tgt;
  }

  function polishBadgeClass(value) {
    if (value === "on") return "accent";
    if (value === "failed") return "danger";
    if (value === "skipped" || value === "off") return "";
    return "";
  }

  function hotkeyLabel(name) {
    return labelOf(HOTKEY_LABELS, name, name ? String(name) : "");
  }

  function ocrBinarizeMode(value) {
    return labelOf(OCR_BINARIZE_MODE, value, value ? String(value) : "");
  }

  function ocrBinarizeShort(value) {
    return labelOf(OCR_BINARIZE_SHORT, value, value ? String(value) : "");
  }

  function ocrRunMode(value) {
    return labelOf(OCR_RUN_MODE, value, value ? String(value) : "");
  }

  function formatHotkey(value) {
    if (!value) return "";
    const parts = String(value).toLowerCase().split("+").map(function (s) { return s.trim(); }).filter(Boolean);
    if (!parts.length) return "";
    const modMap = { ctrl: "Ctrl", alt: "Alt", shift: "Shift", super: "Super" };
    const mods = parts.slice(0, -1).map(function (m) {
      return modMap[m] || (m.charAt(0).toUpperCase() + m.slice(1));
    });
    let key = parts[parts.length - 1];
    if (key === "grave") key = "`";
    else if (key.length === 1) key = key.toUpperCase();
    else if (/^f([1-9]|1[0-2])$/.test(key)) key = key.toUpperCase();
    else key = key.charAt(0).toUpperCase() + key.slice(1);
    return mods.concat([key]).join("+");
  }

  V3.labels = {
    ERROR_CODE: ERROR_CODE,
    JOB_STATE: JOB_STATE,
    UTTERANCE_SOURCE: UTTERANCE_SOURCE,
    HISTORY_POLISH: HISTORY_POLISH,
    PRODUCT_EVENT: PRODUCT_EVENT,
    SESSION_CLOSED_REASON: SESSION_CLOSED_REASON,
    INSTALL_STEP: INSTALL_STEP,
    INSTALL_STEP_STATE: INSTALL_STEP_STATE,
    COMPONENT: COMPONENT,
    COMPONENT_STATE: COMPONENT_STATE,
    SETUP_TABS: SETUP_TABS,
    LANG_PAIR_OPTIONS: LANG_PAIR_OPTIONS,
    LANG_PAIR_PARTS: LANG_PAIR_PARTS,
    LANG_SHORT_NAME: LANG_SHORT_NAME,
    FIXED_MODELS: FIXED_MODELS,
    HOTKEY_LABELS: HOTKEY_LABELS,
    HOME_NAVS: HOME_NAVS,
    LOG_MAX_LINES_OPTIONS: LOG_MAX_LINES_OPTIONS,
    LOG_GROUPS: LOG_GROUPS,
    RUN_MODE: RUN_MODE,
    OS_NAME: OS_NAME,
    OCR_PREPROCESS_MAX_ENABLED: OCR_PREPROCESS_MAX_ENABLED,
    OCR_BINARIZE_MODE: OCR_BINARIZE_MODE,
    OCR_BINARIZE_SHORT: OCR_BINARIZE_SHORT,
    OCR_RUN_MODE: OCR_RUN_MODE,
    labelOf: labelOf,
    errorCode: errorCode,
    formatError: formatError,
    formatUiError: formatUiError,
    source: source,
    polish: polish,
    jobState: jobState,
    productEvent: productEvent,
    langPairLabel: langPairLabel,
    directionLabel: directionLabel,
    polishBadgeClass: polishBadgeClass,
    hotkeyLabel: hotkeyLabel,
    ocrBinarizeMode: ocrBinarizeMode,
    ocrBinarizeShort: ocrBinarizeShort,
    ocrRunMode: ocrRunMode,
    formatHotkey: formatHotkey
  };
})(window.V3 = window.V3 || {});

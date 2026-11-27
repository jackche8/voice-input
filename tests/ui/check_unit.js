#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");

const repo = path.resolve(__dirname, "../..");
const v3js = path.join(repo, "static/v3/js");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];
function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

const jsFiles = fs.readdirSync(v3js).filter((n) => n.endsWith(".js")).map((n) => path.join(v3js, n));
if (!jsFiles.some((f) => path.basename(f) === "home.js") || !jsFiles.some((f) => path.basename(f) === "setup-panel.js")) {
  fail("缺少 static/v3/js/home.js 或 static/v3/js/setup-panel.js");
}
for (const file of jsFiles) {
  try {
    execFileSync("node", ["--check", file], { stdio: "pipe" });
    pass("syntax " + path.basename(file));
  } catch (err) {
    fail("syntax " + path.basename(file) + ": " + (err.stderr || err.message).toString());
  }
}

const context = { window: {}, console: console };
context.window.V3 = {};
vm.createContext(context);
for (const name of ["dom.js", "labels.js", "icons.js", "defaults.js", "hotkey-capture.js", "ocr-presets.js", "ocr-panel.js", "logs-panel.js", "about-panel.js"]) {
  vm.runInContext(fs.readFileSync(path.join(v3js, name), "utf8"), context, { filename: name });
}
const V3 = context.window.V3 || context.V3;

if (V3.labels.FIXED_MODELS.asr !== "Qwen3-ASR-1.7B") fail("固定识别模型名不正确");
else pass("固定识别模型为 Qwen3-ASR-1.7B");
if (V3.labels.FIXED_MODELS.ollama !== "qwen3.5:4b") fail("固定纠错模型名不正确");
else pass("固定纠错模型为 qwen3.5:4b");

const setupTabIds = Array.isArray(V3.labels.SETUP_TABS) ? V3.labels.SETUP_TABS.map((t) => t.id) : [];
if (
  setupTabIds.length !== 6 ||
  setupTabIds.join(",") !== "models,voice,shot,keys,api,sys" ||
  setupTabIds.includes("lan") ||
  setupTabIds.includes("hist")
) {
  fail("SETUP_TABS 应为 6 项且不含重复 lan/hist：" + JSON.stringify(setupTabIds));
} else pass("SETUP_TABS 六项子标签齐全且无 lan/hist 重复");
if (Object.prototype.hasOwnProperty.call(V3.labels, "SETUP_MENUS")) {
  fail("labels.js 仍导出已废弃的 SETUP_MENUS");
} else pass("labels.js 已清理废弃导出 SETUP_MENUS");

if (
  Object.prototype.hasOwnProperty.call(V3.labels, "TARGET_LANG_OPTIONS") ||
  Object.prototype.hasOwnProperty.call(V3.labels, "targetLang")
) {
  fail("labels.js 仍导出已废弃的 TARGET_LANG_OPTIONS 或 targetLang");
} else pass("labels.js 已清理废弃导出 TARGET_LANG_OPTIONS 与 targetLang");

if (
  !Array.isArray(V3.labels.LANG_PAIR_OPTIONS) ||
  V3.labels.LANG_PAIR_OPTIONS.length !== 1 ||
  V3.labels.LANG_PAIR_OPTIONS[0].value !== "zh-en" ||
  V3.labels.LANG_PAIR_OPTIONS[0].label !== "中 ⇄ 英" ||
  !V3.labels.LANG_PAIR_PARTS ||
  !Array.isArray(V3.labels.LANG_PAIR_PARTS["zh-en"]) ||
  V3.labels.LANG_PAIR_PARTS["zh-en"].join(",") !== "zh,en" ||
  !V3.labels.LANG_SHORT_NAME ||
  V3.labels.LANG_SHORT_NAME.zh !== "中" ||
  V3.labels.LANG_SHORT_NAME.en !== "英"
) {
  fail("labels.js 语言对常量不正确");
} else pass("labels.js 导出 LANG_PAIR_OPTIONS / LANG_PAIR_PARTS / LANG_SHORT_NAME");

if (
  V3.labels.langPairLabel("zh-en") !== "中 ⇄ 英" ||
  V3.labels.langPairLabel("zh-ja") !== "zh-ja" ||
  V3.labels.langPairLabel(null) !== ""
) {
  fail("langPairLabel 行为不正确");
} else pass("langPairLabel 正常映射并原样回退未知语言对");

if (
  V3.labels.directionLabel("zh", "en") !== "中 → 英" ||
  V3.labels.directionLabel("en", "zh") !== "英 → 中" ||
  V3.labels.directionLabel("ja", "zh") !== "ja → 中" ||
  V3.labels.directionLabel(null, "en") !== ""
) {
  fail("directionLabel 行为不正确");
} else pass("directionLabel 正常格式化方向且对未知语种不吞码");

const iconsSrc = fs.readFileSync(path.join(v3js, "icons.js"), "utf8");
const requiredIcons = [
  "i-swap", "i-lock", "i-file-text", "i-info", "i-search", "i-external",
  "i-sliders", "i-plus", "i-chevron-up", "i-chevron-down", "i-edit", "i-play", "i-rotate-ccw"
];
const missingIcons = requiredIcons.filter((id) => !iconsSrc.includes('id="' + id + '"'));
if (missingIcons.length || (iconsSrc.match(/id="i-trash"/g) || []).length !== 1) {
  fail("icons.js 缺少图标（" + missingIcons.join(", ") + "）或重复定义 i-trash");
} else pass("icons.js 包含全部新增图标（含 7 枚 OCR 预设矢量图标）并复用唯一 i-trash");

if (V3.labels.source("desktop") !== "本机" || V3.labels.source("web") !== "手机" || V3.labels.source("resend") !== "重发") {
  fail("来源标签不正确");
} else pass("UtteranceSource 中文标签正确");

if (V3.labels.polish("failed") !== "润色失败" || V3.labels.polish("on") !== "已润色") fail("润色标签不正确");
else pass("HistoryPolish 中文标签正确");

if (V3.labels.errorCode("ocr_language_missing") !== "缺少识别语言包") fail("错误码标签缺失");
else pass("错误码中文标签可用");

if (V3.labels.formatError("translate_failed", "本地模型无响应") !== "翻译失败：本地模型无响应") {
  fail("错误格式化不正确：" + V3.labels.formatError("translate_failed", "本地模型无响应"));
} else pass("错误展示包含错误码与后端原文");

if (
  typeof V3.labels.formatUiError !== "function" ||
  V3.labels.formatUiError("ocr_failed", "未检测到文字") !== "文字识别失败：未检测到文字" ||
  V3.labels.formatUiError({ error: "delivery_failed", message: "目标丢失", details: { job_id: "j-1" } }) !== "投递失败：目标丢失：任务 j-1" ||
  V3.labels.formatUiError("自定义错误", "明细") !== "自定义错误：明细" ||
  V3.labels.formatUiError(null, "", null, "回退文案") !== "回退文案"
) {
  fail("labels.formatUiError 单源报错格式化行为不正确");
} else pass("labels.formatUiError 正确处理错误码、错误对象、自定义文本与回退文案");

if (V3.labels.productEvent("delivery_unknown") !== "投递结果未知") fail("delivery_unknown 标签不正确");
else pass("delivery_unknown 标签正确");

if (V3.labels.errorCode("delivery_failed") !== "投递失败") fail("delivery_failed 标签不正确");
else pass("delivery_failed 中文标签正确");
if (V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }) !== "投递失败：输入目标已消失：任务 job-delivery-1") {
  fail("delivery_failed 未展示 details.job_id：" + V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }));
} else pass("delivery_failed 展示错误码、原文和 details.job_id");

if (V3.labels.hotkeyLabel("hotkey_ocr") !== "纯 OCR 识别") fail("hotkey_ocr 标签不正确：" + V3.labels.hotkeyLabel("hotkey_ocr"));
else pass("hotkey_ocr 中文标签为 纯 OCR 识别");
if (V3.labels.hotkeyLabel("hotkey_annotate") !== "截屏标注") fail("hotkey_annotate 标签不正确：" + V3.labels.hotkeyLabel("hotkey_annotate"));
else pass("hotkey_annotate 中文标签为 截屏标注");
if (V3.labels.formatHotkey("ctrl+alt+o") !== "Ctrl+Alt+O" || V3.labels.formatHotkey("ctrl+alt+a") !== "Ctrl+Alt+A") fail("formatHotkey 不正确：" + V3.labels.formatHotkey("ctrl+alt+o") + " / " + V3.labels.formatHotkey("ctrl+alt+a"));
else pass("formatHotkey 格式化正确");
const homeNavIds = Array.isArray(V3.labels.HOME_NAVS) ? V3.labels.HOME_NAVS.map((n) => n.id) : [];
if (homeNavIds.length !== 7 || homeNavIds.join(",") !== "translate,ocr,history,lan,setup,logs,about") {
  fail("HOME_NAVS 应为 7 项（含 setup/logs/about）：" + JSON.stringify(homeNavIds));
} else pass("方案 C 主页七项导航齐全（含 setup / logs / about）");

if (V3.labels.errorCode("logs_io") !== "日志读写失败") {
  fail("logs_io 错误码中文标签不正确：" + V3.labels.errorCode("logs_io"));
} else pass("logs_io 中文标签为 日志读写失败");

if (
  !Array.isArray(V3.labels.LOG_MAX_LINES_OPTIONS) ||
  V3.labels.LOG_MAX_LINES_OPTIONS.join(",") !== "500,2000,5000" ||
  !V3.labels.LOG_GROUPS ||
  V3.labels.LOG_GROUPS.runtime !== "运行服务" ||
  V3.labels.LOG_GROUPS.setup !== "安装过程"
) {
  fail("LOG_MAX_LINES_OPTIONS 或 LOG_GROUPS 常量不正确");
} else pass("labels.js 导出 LOG_MAX_LINES_OPTIONS=[500,2000,5000] 与 LOG_GROUPS");

if (
  !V3.aboutPanel ||
  typeof V3.aboutPanel.asrVariantBadge !== "function" ||
  V3.aboutPanel.asrVariantBadge("cuda").tone !== "ok" ||
  V3.aboutPanel.asrVariantBadge("cuda").text !== "cuda" ||
  V3.aboutPanel.asrVariantBadge("mps").tone !== "ok" ||
  V3.aboutPanel.asrVariantBadge("mps").text !== "mps" ||
  V3.aboutPanel.asrVariantBadge("cpu").tone !== "ok" ||
  V3.aboutPanel.asrVariantBadge("cpu").text !== "cpu" ||
  V3.aboutPanel.asrVariantBadge(null).tone !== "warn" ||
  V3.aboutPanel.asrVariantBadge(null).text !== "未安装" ||
  V3.aboutPanel.asrVariantBadge("corrupt").tone !== "danger" ||
  V3.aboutPanel.asrVariantBadge("corrupt").text !== "清单损坏" ||
  V3.aboutPanel.asrVariantBadge("future_npu").text !== "future_npu"
) {
  fail("aboutPanel.asrVariantBadge 五态映射或未知透传不正确");
} else pass("aboutPanel.asrVariantBadge 五态徽章映射正确（cuda/mps/cpu=ok, null=warn未安装, corrupt=danger清单损坏）");

if (
  V3.labels.OCR_PREPROCESS_MAX_ENABLED !== 3 ||
  V3.labels.ocrBinarizeMode("off") !== "关闭" ||
  V3.labels.ocrBinarizeMode("otsu") !== "自动 (Otsu)" ||
  V3.labels.ocrBinarizeMode("manual") !== "手动" ||
  V3.labels.ocrBinarizeMode("adaptive_future") !== "adaptive_future" ||
  V3.labels.ocrRunMode("preset_chain") !== "启用预设链路" ||
  Object.prototype.hasOwnProperty.call(V3.labels, "OCR_STARTER_TEMPLATES") ||
  Object.prototype.hasOwnProperty.call(V3.labels, "OCR_PREPROCESS_STARTER_TEMPLATES")
) {
  fail("labels.js OCR 枚举映射不正确或违规导出了起步模板副本");
} else pass("labels.js OCR 枚举映射与未知透传正确且未复制起步模板参数");

if (
  !V3.ocrPresets ||
  typeof V3.ocrPresets.cloneParams !== "function" ||
  typeof V3.ocrPresets.generatePresetId !== "function" ||
  typeof V3.ocrPresets.extractImageFile !== "function" ||
  typeof V3.ocrPresets.paramChipLabels !== "function" ||
  typeof V3.ocrPresets.formatAttemptLabel !== "function" ||
  typeof V3.ocrPresets.summarizeOcrMeta !== "function"
) {
  fail("ocr-presets.js 未导出必需纯函数（cloneParams / generatePresetId / extractImageFile / paramChipLabels / formatAttemptLabel / summarizeOcrMeta）");
} else {
  const clamped = V3.ocrPresets.cloneParams({ invert: 1, contrast: 9.9, brightness: 0.1, sharpen: -2, binarize_mode: "bogus", threshold: 999 });
  if (
    clamped.invert !== true ||
    clamped.contrast !== 3.0 ||
    clamped.brightness !== 0.5 ||
    clamped.sharpen !== 0.0 ||
    clamped.binarize_mode !== "off" ||
    clamped.threshold !== 255
  ) {
    fail("ocrPresets.cloneParams 越界裁剪或枚举归一化失败：" + JSON.stringify(clamped));
  } else pass("ocrPresets.cloneParams 正常裁剪越界数值并归一化未知二值化模式");

  const id1 = V3.ocrPresets.generatePresetId([]);
  const id2 = V3.ocrPresets.generatePresetId([{ id: id1 }]);
  if (!id1 || !id2 || id1 === id2) {
    fail("ocrPresets.generatePresetId 未生成无碰撞 ID：" + id1 + " / " + id2);
  } else pass("ocrPresets.generatePresetId 生成非空且与已有列表无碰撞 ID");

  const fakeImg = { name: "shot.png", type: "image/png" };
  const fakeTxt = { name: "note.txt", type: "text/plain" };
  if (
    V3.ocrPresets.extractImageFile({ items: [{ kind: "file", type: "image/png", getAsFile: () => fakeImg }] }) !== fakeImg ||
    V3.ocrPresets.extractImageFile({ files: [fakeTxt, fakeImg] }) !== fakeImg ||
    V3.ocrPresets.extractImageFile({ files: [fakeTxt] }) !== null
  ) {
    fail("ocrPresets.extractImageFile 提取图片文件行为不正确");
  } else pass("ocrPresets.extractImageFile 正确从 items/files 提取图片并忽略非图片");

  const attRank1 = V3.ocrPresets.formatAttemptLabel({ label: "深底反色", rank: 1, confidence: 34, accepted: false });
  const attRank3 = V3.ocrPresets.formatAttemptLabel({ label: "强黑白字幕", rank: 3, confidence: 91, accepted: true });
  const attOrig = V3.ocrPresets.formatAttemptLabel({ label: "默认原图", rank: null, confidence: 78, accepted: true });
  if (
    attRank1 !== "#1 深底反色 34%" ||
    attRank3 !== "#3 强黑白字幕 91% ✓" ||
    attOrig !== "默认原图 78% ✓"
  ) {
    fail("ocrPresets.formatAttemptLabel 未按真实线格式（rank 非空拼 #rank、rank=null 不加 #）格式化：" + JSON.stringify({ attRank1, attRank3, attOrig }));
  } else pass("ocrPresets.formatAttemptLabel 按真实后端线格式拼接 #rank 前缀（rank=null 不加 #）");

  const sOrig = V3.ocrPresets.summarizeOcrMeta({ run_mode: "original", confidence: 88, elapsed_ms: 140, early_stopped: false, fallback_to_original: false, attempts: [{ label: "默认原图", rank: null, confidence: 88, accepted: true }] });
  const sDraft = V3.ocrPresets.summarizeOcrMeta({ run_mode: "draft_params", confidence: 94, elapsed_ms: 210, early_stopped: false, fallback_to_original: false, attempts: [] });
  const sHit = V3.ocrPresets.summarizeOcrMeta({ run_mode: "preset_chain", matched_preset_name: "深底反色", matched_rank: 2, confidence: 91, elapsed_ms: 380, early_stopped: true, fallback_to_original: false, attempts: [{ label: "暗色终端", rank: 1, confidence: 40, accepted: false }, { label: "深底反色", rank: 2, confidence: 91, accepted: true }] });
  const sFbOrig = V3.ocrPresets.summarizeOcrMeta({ run_mode: "preset_chain", matched_preset_name: "默认原图", matched_rank: null, confidence: 76, elapsed_ms: 520, early_stopped: false, fallback_to_original: true, attempts: [{ label: "暗色终端", rank: 1, confidence: 40, accepted: false }, { label: "低对比灰字", rank: 2, confidence: 62, accepted: false }, { label: "默认原图", rank: null, confidence: 76, accepted: true }] });
  const sFbPreset = V3.ocrPresets.summarizeOcrMeta({ run_mode: "preset_chain", matched_preset_name: "低对比增强", matched_rank: 1, confidence: 73, elapsed_ms: 410, early_stopped: false, fallback_to_original: true, attempts: [{ label: "低对比增强", rank: 1, confidence: 73, accepted: true }, { label: "默认原图", rank: null, confidence: 55, accepted: false }] });
  if (
    sOrig.badgeTone !== "ok" || !sOrig.badgeText.includes("原图直识") ||
    sDraft.badgeTone !== "ok" || !sDraft.badgeText.includes("当前参数试识别") ||
    sHit.badgeTone !== "ok" || !sHit.badgeText.includes("命中 #2") || !sHit.summaryText.includes("早停") ||
    sFbOrig.badgeTone !== "warn" || !sFbOrig.summaryText.includes("已尝试 2 条预设") ||
    sFbPreset.badgeTone !== "warn" || !sFbPreset.badgeText.includes("择优 #1") || !sFbPreset.summaryText.includes("已尝试 1 条预设")
  ) {
    fail("ocrPresets.summarizeOcrMeta 五态真值表或动态尝试数文案不正确：" + JSON.stringify({ sOrig, sDraft, sHit, sFbOrig, sFbPreset }));
  } else pass("ocrPresets.summarizeOcrMeta 覆盖 OcrRunMeta 五态真值表且动态展示实际尝试预设数");
}

const home = V3.defaults.homeState();
if (
  home.connection !== "disconnected" ||
  home.theme !== "light" ||
  home.activePanel !== "translate" ||
  home.setupTab !== "models" ||
  Object.prototype.hasOwnProperty.call(home, "aboutText") ||
  home.log_max_lines !== null ||
  !home.logs ||
  home.logs.phase !== "idle" ||
  home.logs.log_max_lines !== null ||
  home.logs.activeFile !== null ||
  !Array.isArray(home.logs.sources) ||
  home.logs.sources.length !== 0 ||
  !Array.isArray(home.logs.entries) ||
  home.logs.entries.length !== 0 ||
  home.logs.copyOk !== "" ||
  home.logs.copyError !== "" ||
  !home.about ||
  home.about.phase !== "idle" ||
  home.about.version !== "" ||
  home.about.asr_variant !== null ||
  home.about.links !== null ||
  home.about.copyOk !== "" ||
  home.about.copyError !== "" ||
  home.asr.status !== "unknown" ||
  home.asr.sidecarRunning === true ||
  home.ollama.modelStatus !== "unknown" ||
  home.audio_input_device !== null ||
  home.audioInputs !== null ||
  home.audioInputsLoading !== false ||
  home.audioInputError !== "" ||
  home.audioInputSaved !== false ||
  home.polish_enabled !== null ||
  home.keywordsDraft !== null ||
  home.translate_lang_pair !== null ||
  home.translate_auto_enabled !== null ||
  !Array.isArray(home.ocr_preprocess_presets) ||
  home.ocr_preprocess_presets.length !== 0 ||
  home.ocrStarterTemplates !== null ||
  home.langPairOptions !== null ||
  home.translateSettingsSaved !== false ||
  Object.prototype.hasOwnProperty.call(home, "translate_target_lang") ||
  Object.prototype.hasOwnProperty.call(home, "targetLangOptions") ||
  Object.prototype.hasOwnProperty.call(home, "targetLangSaved") ||
  home.hotkey_voice !== "" ||
  home.hotkey_ocr !== "" ||
  home.hotkey_annotate !== "" ||
  Object.prototype.hasOwnProperty.call(home, "hotkeyHint") ||
  home.capabilities !== null ||
  home.apiPaths !== null ||
  home.history.records.length !== 0 ||
  home.translate.phase !== "empty" ||
  home.translate.lang_pair !== "zh-en" ||
  home.translate.source_lang !== null ||
  home.translate.target_lang !== null ||
  home.translate.direction_mode !== null ||
  home.translate.chunks !== null ||
  home.translate.debouncePending !== false ||
  home.translate.manualOverride !== null ||
  home.translate.text !== "" ||
  home.ocr.phase !== "empty" ||
  home.ocr.mode !== "raw" ||
  home.ocr.lang_pair !== "zh-en" ||
  home.ocr.source_lang !== null ||
  home.ocr.target_lang !== null ||
  home.ocr.direction_mode !== null ||
  home.ocr.processedPreviewUrl !== "" ||
  home.ocr.previewPhase !== "idle" ||
  home.ocr.previewErrorCode !== null ||
  home.ocr.previewError !== "" ||
  home.ocr.ocr_meta !== null ||
  home.ocr.lastRunKind !== null ||
  home.ocr.savingPresets !== false ||
  home.ocr.presetsSaveError !== "" ||
  home.ocr.presetsSaved !== false ||
  home.ocr.ocr_text !== "" ||
  home.lanEntries !== null ||
  home.tokenPresent !== false
) {
  fail("homeState 默认状态不合法：" + JSON.stringify(home));
} else pass("homeState 统一桌面默认状态合法（默认亮色、含音频输入与语言对/自动开关契约字段且无旧键/成功示例/明文令牌）");

function ev(init) {
  return Object.assign({ ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, key: "", code: "" }, init || {});
}
if (!V3.hotkey || typeof V3.hotkey.fromKeyboardEvent !== "function") fail("未加载 V3.hotkey.fromKeyboardEvent");
else {
  const rec = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "z", code: "KeyZ" }));
  if (rec.value === "ctrl+alt+z" && rec.consumed === true) pass("Ctrl+Alt+Z 转为 ctrl+alt+z 且 consumed");
  else fail("完整组合转换失败：" + JSON.stringify(rec));

  const pend = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, key: "Control", code: "ControlLeft" }));
  if (pend.pending === true && pend.consumed === false && !pend.value) pass("仅修饰键不结束录入、不消费");
  else fail("仅修饰键转换失败：" + JSON.stringify(pend));

  const esc = V3.hotkey.fromKeyboardEvent(ev({ key: "Escape", code: "Escape" }));
  if (esc.cancel === true && esc.consumed === true) pass("Escape 取消并消费");
  else fail("Escape 转换失败：" + JSON.stringify(esc));

  const tab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab" }));
  if (tab.navigate === true && tab.consumed === false) pass("单独 Tab 用于导航、不消费");
  else fail("单独 Tab 转换失败：" + JSON.stringify(tab));

  const shiftTab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab", shiftKey: true }));
  if (shiftTab.navigate === true && shiftTab.consumed === false) pass("Shift+Tab 无 ctrl/alt/super 时用于导航");
  else fail("Shift+Tab 转换失败：" + JSON.stringify(shiftTab));

  const ctrlTab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab", ctrlKey: true }));
  if (ctrlTab.value === "ctrl+tab" && ctrlTab.consumed === true) pass("Ctrl+Tab 录为 ctrl+tab 并消费");
  else fail("Ctrl+Tab 转换失败：" + JSON.stringify(ctrlTab));

  const needMod = V3.hotkey.fromKeyboardEvent(ev({ key: "a", code: "KeyA" }));
  if (needMod.error && needMod.consumed === false && !needMod.value) pass("无修饰键的主键报错且不消费、不伪造成功");
  else fail("无修饰键转换失败：" + JSON.stringify(needMod));

  const unsup = V3.hotkey.fromKeyboardEvent(ev({ key: "ArrowLeft", code: "ArrowLeft", ctrlKey: true }));
  if (unsup.error && /不支持/.test(unsup.error) && unsup.consumed === false && !unsup.value) pass("不支持按键报错且不伪造成功");
  else fail("不支持按键转换失败：" + JSON.stringify(unsup));

  const layout = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "q", code: "KeyA" }));
  if (layout.value === "ctrl+alt+a" && layout.hint && /键帽/.test(layout.hint)) pass("非 US 布局按物理 code 记录并提示键帽差异");
  else fail("布局提示转换失败：" + JSON.stringify(layout));

  const superA = V3.hotkey.fromKeyboardEvent(ev({ metaKey: true, key: "a", code: "KeyA" }));
  if (superA.value === "super+a" && superA.consumed === true) pass("Meta 转为 super");
  else fail("super 转换失败：" + JSON.stringify(superA));

  const shiftOnly = V3.hotkey.fromKeyboardEvent(ev({ shiftKey: true, key: "O", code: "KeyO" }));
  if (shiftOnly.value === "shift+o" && shiftOnly.consumed === true && shiftOnly.warning && /Shift/.test(shiftOnly.warning)) {
    pass("单独 Shift+Key 给出占用大写/上档警告");
  } else fail("Shift+Key 警告缺失：" + JSON.stringify(shiftOnly));

  const ocrKey = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "o", code: "KeyO" }));
  if (ocrKey.value === "ctrl+alt+o" && ocrKey.consumed === true && !ocrKey.warning) {
    pass("Ctrl+Alt+O 录为 ctrl+alt+o 且无警告");
  } else fail("Ctrl+Alt+O 转换失败：" + JSON.stringify(ocrKey));

  const annotateKey = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "a", code: "KeyA" }));
  if (annotateKey.value === "ctrl+alt+a" && annotateKey.consumed === true && !annotateKey.warning && !annotateKey.hint) {
    pass("Ctrl+Alt+A 录为 ctrl+alt+a 且无警告");
  } else fail("Ctrl+Alt+A 转换失败：" + JSON.stringify(annotateKey));
}

if (
  Object.prototype.hasOwnProperty.call(V3.defaults, "setupState")
  || Object.prototype.hasOwnProperty.call(V3.defaults, "statusState")
  || Object.prototype.hasOwnProperty.call(V3.defaults, "translateState")
) {
  fail("defaults.js 仍导出已废弃的 setupState / statusState / translateState");
} else pass("defaults.js 已清理废弃导出 setupState、statusState 与 translateState");

const merged = V3.defaults.merge(home, { asr: { status: "failed", message: "下载中断" } });
if (merged.asr.status !== "failed" || merged.asr.name !== "Qwen3-ASR-1.7B") fail("状态合并不正确");
if (home.asr.status !== "unknown") fail("合并污染了默认对象");
else pass("状态合并保留固定模型名且不污染默认值");

const mergedHome = V3.defaults.merge(home, { translate: { phase: "result", text: "你好" } });
if (mergedHome.translate.phase !== "result" || home.translate.phase !== "empty") fail("homeState 合并污染了默认对象");
else pass("homeState 深度合并保留默认字段且不污染默认值");

const report = { passes, failures };
fs.writeFileSync(path.join(logDir, "check_unit.json"), JSON.stringify(report, null, 2));
console.log("单元检查通过 " + passes.length + "，失败 " + failures.length);
for (const m of failures) console.log("FAIL " + m);
for (const m of passes) console.log("PASS " + m);
if (failures.length) process.exit(1);

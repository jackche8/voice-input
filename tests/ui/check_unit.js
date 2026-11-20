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
for (const name of ["dom.js", "labels.js", "icons.js", "defaults.js", "hotkey-capture.js"]) {
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

if (V3.labels.productEvent("delivery_unknown") !== "投递结果未知") fail("delivery_unknown 标签不正确");
else pass("delivery_unknown 标签正确");

if (V3.labels.errorCode("delivery_failed") !== "投递失败") fail("delivery_failed 标签不正确");
else pass("delivery_failed 中文标签正确");
if (V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }) !== "投递失败：输入目标已消失：任务 job-delivery-1") {
  fail("delivery_failed 未展示 details.job_id：" + V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }));
} else pass("delivery_failed 展示错误码、原文和 details.job_id");

if (V3.labels.hotkeyLabel("hotkey_ocr") !== "纯 OCR 识别") fail("hotkey_ocr 标签不正确：" + V3.labels.hotkeyLabel("hotkey_ocr"));
else pass("hotkey_ocr 中文标签为 纯 OCR 识别");
if (V3.labels.formatHotkey("ctrl+alt+o") !== "Ctrl+Alt+O") fail("formatHotkey 不正确：" + V3.labels.formatHotkey("ctrl+alt+o"));
else pass("formatHotkey 格式化正确");
const homeNavIds = Array.isArray(V3.labels.HOME_NAVS) ? V3.labels.HOME_NAVS.map((n) => n.id) : [];
if (homeNavIds.length !== 5 || homeNavIds.join(",") !== "translate,ocr,history,lan,setup") {
  fail("HOME_NAVS 应为 5 项（含 setup）：" + JSON.stringify(homeNavIds));
} else pass("方案 C 主页五项导航齐全（含 setup）");

const home = V3.defaults.homeState();
if (
  home.connection !== "disconnected" ||
  home.activePanel !== "translate" ||
  home.setupTab !== "models" ||
  home.asr.status !== "unknown" ||
  home.asr.sidecarRunning === true ||
  home.ollama.modelStatus !== "unknown" ||
  home.polish_enabled !== null ||
  home.keywordsDraft !== null ||
  home.translate_target_lang !== null ||
  home.hotkey_voice !== "" ||
  home.hotkey_ocr !== "" ||
  Object.prototype.hasOwnProperty.call(home, "hotkeyHint") ||
  home.capabilities !== null ||
  home.apiPaths !== null ||
  home.history.records.length !== 0 ||
  home.translate.phase !== "empty" ||
  home.translate.text !== "" ||
  home.ocr.phase !== "empty" ||
  home.ocr.ocr_text !== "" ||
  home.lanEntries !== null ||
  home.tokenPresent !== false
) {
  fail("homeState 默认状态不合法：" + JSON.stringify(home));
} else pass("homeState 统一桌面默认状态合法（未连接、含全部设置字段且无成功示例/明文令牌）");

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

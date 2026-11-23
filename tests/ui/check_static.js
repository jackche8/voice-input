#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "../..");
const v3 = path.join(repo, "static/v3");
const openapiPath = path.join(repo, "static/v3/openapi.json");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];

function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

function walk(dir, acc) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
}

const files = walk(v3, []);
function isBusinessFile(file) {
  const rel = path.relative(v3, file).split(path.sep).join("/");
  return rel === "js/business" || rel.indexOf("js/business/") === 0;
}
function isGeneratedSnapshot(file) {
  const rel = path.relative(v3, file).split(path.sep).join("/");
  return rel === "openapi.json";
}
const pureFiles = files.filter((f) => !isBusinessFile(f) && !isGeneratedSnapshot(f));
const jsFiles = files.filter((f) => f.endsWith(".js"));
const cssFiles = files.filter((f) => f.endsWith(".css"));
const htmlFiles = files.filter((f) => f.endsWith(".html"));
const prodText = files.map((f) => fs.readFileSync(f, "utf8")).join("\n");

const expectedHtml = ["index.html", "voice.html"];
for (const name of expectedHtml) {
  if (fs.existsSync(path.join(v3, name))) pass("存在 " + name);
  else fail("缺少 " + name);
}
const removedFiles = [
  "overlay-status.html",
  "overlay-translate.html",
  "js/overlay-status.js",
  "js/overlay-translate.js",
  "css/overlay.css",
  "js/business/status-controller.js",
  "js/business/translate-controller.js",
  "js/business/claim.js",
  "setup.html",
  "css/setup.css",
  "js/setup.js",
  "js/business/setup-controller.js"
];
const leftoverRemoved = removedFiles.filter((rel) => fs.existsSync(path.join(v3, rel)));
if (leftoverRemoved.length) fail("仍存在已废弃的 Web 浮层或旧独立设置页文件：" + leftoverRemoved.join(", "));
else pass("已清理全部废弃 Web 浮层与旧独立设置页文件");

const forbidden = [
  [/\bfetch\s*\(/, "fetch("],
  [/\bEventSource\s*\(/, "EventSource("],
  [/\bWebSocket\s*\(/, "WebSocket("],
  [/\bXMLHttpRequest\b/, "XMLHttpRequest"],
  [/navigator\.clipboard/, "navigator.clipboard"],
  [/document\.execCommand\s*\(\s*['"]copy['"]/, "execCommand(copy)"],
  [/\balert\s*\(/, "alert("],
  [/\bwindow\.confirm\s*\(/, "window.confirm("],
  [/\blocalStorage\b/, "localStorage"],
  [/\bsessionStorage\b/, "sessionStorage"],
  [/\beval\s*\(/, "eval("],
  [/new\s+Function\s*\(/, "new Function"]
];

for (const file of pureFiles) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  for (const [re, label] of forbidden) {
    if (re.test(text)) fail(rel + " 含有纯 UI 不允许的 " + label);
  }
  if (/\/api\//.test(text)) fail(rel + " 含有 /api/ 路径，纯 UI 不得写死可调用地址");
}

const businessJs = jsFiles.filter((f) => isBusinessFile(f));
for (const file of businessJs) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  if (/\blocalStorage\b/.test(text) || /\bsessionStorage\b/.test(text)) {
    fail(rel + " 不得使用 Web Storage（避免保存令牌）");
  }
  if (/\/api\/v1\/|\/api\/setup\//.test(text)) {
    fail(rel + " 不得手写 /api/v1/ 或 /api/setup/ 路径");
  }
}

if (!failures.some((m) => m.includes("fetch") || m.includes("EventSource") || m.includes("clipboard") || m.includes("/api/"))) {
  pass("纯 UI 目录无 fetch / EventSource / 剪贴板 / /api/ 路径；业务层单独存放");
}

const htmlJoined = htmlFiles.map((f) => fs.readFileSync(f, "utf8")).join("\n");
const htmlBanned = [
  "全部就绪",
  "今天下午三点",
  "192.168.",
  "3.5 GB",
  "42%",
  "已下载",
  "Qwen3-ASR-0.6B",
  "baidu",
  "Whisper"
];
for (const needle of htmlBanned) {
  if (htmlJoined.toLowerCase().includes(needle.toLowerCase())) {
    fail("生产 HTML 含有不应作为默认内容的文本：" + needle);
  }
}
if (!failures.some((m) => m.includes("生产 HTML"))) pass("生产 HTML 未写死成功示例或已删除的能力");

for (const name of expectedHtml) {
  const text = fs.readFileSync(path.join(v3, name), "utf8");
  if (!/css\/theme\.css/.test(text)) fail(name + " 未引用共享 theme.css");
  if (!/js\/defaults\.js/.test(text)) fail(name + " 未引用共享 defaults.js");
  if (!/__V3_HOOKS__/.test(text)) fail(name + " 未提供 hooks 注入点");
  if (!/<html[^>]*data-theme=["']dark["']/.test(text)) fail(name + ' 未声明统一 Studio C 默认 data-theme="dark"');
}
pass("两页（index / voice）均引用共享主题与默认状态、声明 data-theme=dark 并预留 hooks 注入");

const faviconFailures = [];
for (const name of expectedHtml) {
  const text = fs.readFileSync(path.join(v3, name), "utf8");
  const headMatch = text.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i);
  const head = headMatch ? headMatch[1].replace(/<!--[\s\S]*?-->/g, "") : "";
  const iconLinks = head.match(/<link\b[^>]*\brel=["']icon["'][^>]*>/gi) || [];
  const hasSvgIcon = iconLinks.some(
    (tag) => /\btype=["']image\/svg\+xml["']/i.test(tag) && /\bhref=["']\/favicon\.svg["']/i.test(tag)
  );
  const hasIcoIcon = iconLinks.some(
    (tag) => /\btype=["']image\/x-icon["']/i.test(tag) && /\bhref=["']\/favicon\.ico["']/i.test(tag) && /\bsizes=["']any["']/i.test(tag)
  );
  if (!hasSvgIcon) faviconFailures.push(name + ' <head> 缺少 <link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
  if (!hasIcoIcon) faviconFailures.push(name + ' <head> 缺少 <link rel="icon" type="image/x-icon" href="/favicon.ico" sizes="any" />');
}
if (faviconFailures.length) {
  for (const msg of faviconFailures) fail(msg);
} else {
  pass("两页（index / voice）<head> 均已声明 /favicon.svg 与 /favicon.ico 图标引用");
}

const theme = fs.readFileSync(path.join(v3, "css/theme.css"), "utf8");
const requiredVars = [
  "--c-bg", "--c-accent", "--c-accent-solid", "--c-accent-solid-hover", "--c-danger-solid",
  "--qr-bg", "--qr-fg", "--hud-bg", "--hud-text", "--hud-unstable", "--font-sans", "--sp-3", "--radius-md"
];
for (const v of requiredVars) {
  if (!theme.includes(v)) fail("theme.css 缺少变量 " + v);
}
if (/#0f766e|#115e59|#e6f4f2/i.test(theme)) {
  fail("theme.css 仍残留旧蓝绿主题色（#0f766e 等）");
} else pass("theme.css 含全部必需令牌且已清理旧蓝绿主题色");

/* WCAG 2.1 对比度静态核验（深浅双主题主按钮 / 录音按钮 / 二维码静区均需 ≥ 4.5:1） */
function hexToRgb(hex) {
  const clean = String(hex || "").trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(clean)) return null;
  return [
    parseInt(clean.slice(0, 2), 16),
    parseInt(clean.slice(2, 4), 16),
    parseInt(clean.slice(4, 6), 16)
  ];
}
function relLuminance(rgb) {
  const chan = rgb.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2];
}
function contrastRatio(hexA, hexB) {
  const a = hexToRgb(hexA);
  const b = hexToRgb(hexB);
  if (!a || !b) return 0;
  const l1 = relLuminance(a);
  const l2 = relLuminance(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}
function parseThemeBlock(cssText, selectorRe) {
  const m = cssText.match(selectorRe);
  const vars = {};
  if (!m) return vars;
  const re = /(--[a-z0-9-]+)\s*:\s*([^;]+);/gi;
  let kv;
  while ((kv = re.exec(m[1])) !== null) vars[kv[1]] = kv[2].trim();
  return vars;
}
const themeBlocks = [
  [":root", parseThemeBlock(theme, /:root\s*\{([^}]+)\}/)],
  ['[data-theme="light"]', parseThemeBlock(theme, /\[data-theme="light"\]\s*\{([^}]+)\}/)],
  ['[data-theme="dark"]', parseThemeBlock(theme, /\[data-theme="dark"\]\s*\{([^}]+)\}/)]
];
for (const [label, vars] of themeBlocks) {
  const pairs = [
    ["--c-on-accent vs --c-accent-solid", vars["--c-on-accent"], vars["--c-accent-solid"]],
    ["--c-on-accent vs --c-accent-solid-hover", vars["--c-on-accent"], vars["--c-accent-solid-hover"]],
    ["--c-on-accent vs --c-danger-solid", vars["--c-on-accent"], vars["--c-danger-solid"]],
    ["#000000 vs --qr-bg", "#000000", vars["--qr-bg"]],
    ["--qr-fg vs --qr-bg", vars["--qr-fg"], vars["--qr-bg"]]
  ];
  for (const [name, fg, bg] of pairs) {
    const ratio = contrastRatio(fg, bg);
    if (ratio < 4.5) {
      fail("theme.css " + label + " 对比度不足 4.5:1（" + name + " = " + ratio.toFixed(2) + ":1）");
    }
  }
}
const studioCss = fs.readFileSync(path.join(v3, "css/studio.css"), "utf8");
const voiceCss = fs.readFileSync(path.join(v3, "css/voice.css"), "utf8");
if (!/\.qr\s*\{[^}]*background:\s*var\(--qr-bg\)[^}]*color:\s*var\(--qr-fg\)/s.test(studioCss)) {
  fail("studio.css .qr 未使用 --qr-bg 与 --qr-fg 高对比静区令牌");
}
if (!/\.btn\.primary\s*\{[^}]*background:\s*var\(--c-accent-solid\)/s.test(theme)) {
  fail("theme.css .btn.primary 未使用 --c-accent-solid");
}
if (!/\.mic\s*\{[^}]*background:\s*var\(--c-accent-solid\)/s.test(voiceCss) || !/\.mic\.rec\s*\{[^}]*background:\s*var\(--c-danger-solid\)/s.test(voiceCss) || !/\.upload\s*\{[^}]*background:\s*var\(--c-accent-solid\)/s.test(voiceCss)) {
  fail("voice.css .mic / .mic.rec / .upload 未切换为实心高对比令牌");
}
if (!failures.some((m) => m.includes("对比度") || m.includes("--qr-bg") || m.includes("--c-accent-solid"))) {
  pass("theme.css / studio.css / voice.css 双主题按钮与二维码静区 WCAG 对比度全部 ≥ 4.5:1");
}

/* 暗/亮双主题 color-scheme 与原生 select / option / optgroup 主题变量配色核验（先剥注释再解析顶层规则块） */
function stripCssComments(cssText) {
  return String(cssText || "").replace(/\/\*[\s\S]*?\*\//g, "");
}
function parseTopLevelCssRules(cssText) {
  const clean = stripCssComments(cssText);
  const rules = [];
  let i = 0;
  const n = clean.length;
  while (i < n) {
    const open = clean.indexOf("{", i);
    if (open === -1) break;
    const prelude = clean.slice(i, open).trim();
    let depth = 1;
    let j = open + 1;
    while (j < n && depth > 0) {
      if (clean[j] === "{") depth += 1;
      else if (clean[j] === "}") depth -= 1;
      j += 1;
    }
    const body = clean.slice(open + 1, j - 1);
    i = j;
    if (!prelude || prelude.startsWith("@")) continue;
    const selectors = prelude.split(",").map((s) => s.trim().replace(/\s+/g, " ")).filter(Boolean);
    const decls = {};
    for (const part of body.split(";")) {
      const colon = part.indexOf(":");
      if (colon === -1) continue;
      const prop = part.slice(0, colon).trim().toLowerCase();
      const val = part.slice(colon + 1).trim();
      if (prop && val) decls[prop] = val;
    }
    rules.push({ selectors, decls });
  }
  return rules;
}
function mergedDeclsForSelector(rules, selector) {
  const target = selector.trim().replace(/\s+/g, " ");
  const out = {};
  for (const r of rules) {
    if (r.selectors.includes(target)) Object.assign(out, r.decls);
  }
  return out;
}
function isThemeTokenRef(val) {
  return /^var\(--[a-z0-9-]+\)$/.test(String(val || "").trim());
}

const themeRules = parseTopLevelCssRules(theme);
const rootDecls = mergedDeclsForSelector(themeRules, ":root");
const lightDecls = mergedDeclsForSelector(themeRules, '[data-theme="light"]');
const darkDecls = mergedDeclsForSelector(themeRules, '[data-theme="dark"]');
if (rootDecls["color-scheme"] !== "light") {
  fail("theme.css :root 未声明 color-scheme: light");
} else {
  pass("theme.css :root 已声明 color-scheme: light");
}
if (lightDecls["color-scheme"] !== "light") {
  fail('theme.css [data-theme="light"] 未声明 color-scheme: light');
} else {
  pass('theme.css [data-theme="light"] 已声明 color-scheme: light');
}
if (darkDecls["color-scheme"] !== "dark") {
  fail('theme.css [data-theme="dark"] 未声明 color-scheme: dark');
} else {
  pass('theme.css [data-theme="dark"] 已声明 color-scheme: dark');
}

const selectDecls = mergedDeclsForSelector(themeRules, "select");
const selectBg = selectDecls["background-color"] || selectDecls["background"];
const selectFg = selectDecls["color"];
if (
  selectDecls["color-scheme"] !== "inherit"
  || selectBg !== "var(--c-surface)"
  || selectFg !== "var(--c-text)"
) {
  fail("theme.css select 未统一声明 color-scheme: inherit 与主题变量配色（var(--c-surface) / var(--c-text)）");
} else {
  pass("theme.css select 已统一声明 color-scheme: inherit 与主题变量配色");
}

const selectDisDecls = mergedDeclsForSelector(themeRules, "select:disabled");
const selectDisBg = selectDisDecls["background-color"] || selectDisDecls["background"];
const selectDisFg = selectDisDecls["color"];
if (!isThemeTokenRef(selectDisBg) || !isThemeTokenRef(selectDisFg) || selectDisBg === selectDisFg) {
  fail("theme.css select:disabled 未使用主题变量设置可读禁用态配色");
} else {
  pass("theme.css select:disabled 已使用主题变量设置可读禁用态配色");
}

const selectOptDecls = mergedDeclsForSelector(themeRules, "select option");
const selectOptGroupDecls = mergedDeclsForSelector(themeRules, "select optgroup");
const optBg = selectOptDecls["background-color"] || selectOptDecls["background"];
const optFg = selectOptDecls["color"];
const optGrpBg = selectOptGroupDecls["background-color"] || selectOptGroupDecls["background"];
const optGrpFg = selectOptGroupDecls["color"];
if (
  optBg !== "var(--c-surface)" || optFg !== "var(--c-text)"
  || optGrpBg !== "var(--c-surface)" || optGrpFg !== "var(--c-text)"
) {
  fail("theme.css select option / select optgroup 未使用主题变量配色（var(--c-surface) / var(--c-text)）");
} else {
  pass("theme.css select option / select optgroup 已统一使用主题变量配色");
}

const selectOptDisDecls = mergedDeclsForSelector(themeRules, "select option:disabled");
if (!isThemeTokenRef(selectOptDisDecls["color"])) {
  fail("theme.css select option:disabled 未使用主题变量配色");
} else {
  pass("theme.css select option:disabled 已使用主题变量配色");
}

const studioRules = parseTopLevelCssRules(studioCss);
const boxHeadSelectDecls = mergedDeclsForSelector(studioRules, ".box-head select");
if ("background" in boxHeadSelectDecls || "background-color" in boxHeadSelectDecls || "color" in boxHeadSelectDecls) {
  fail("studio.css .box-head select 存在与 theme.css 重复的背景或文字色声明");
} else {
  pass("studio.css .box-head select 仅保留盒头局部布局，背景/文字色统一由 theme.css 单源提供");
}

/* 设置页与工作台长报错防溢出 CSS 规则核验（复用顶层 CSS 规则解析器） */
const overflowGuardChecks = [
  { file: "theme.css", rules: themeRules, selector: ".row-sub", expect: { "overflow-wrap": "anywhere" } },
  { file: "theme.css", rules: themeRules, selector: ".banner", expect: { "overflow-wrap": "anywhere" } },
  { file: "theme.css", rules: themeRules, selector: ".badge", expect: { "flex-shrink": "0" } },
  { file: "studio.css", rules: studioRules, selector: ".box-error", expect: { "overflow-wrap": "anywhere" } },
  { file: "studio.css", rules: studioRules, selector: ".box-error > span", expect: { "min-width": "0", flex: "1" } },
  { file: "studio.css", rules: studioRules, selector: ".empty", expect: { "overflow-wrap": "anywhere" } },
  { file: "studio.css", rules: studioRules, selector: ".studio-panel", expect: { "min-width": "0" } },
  { file: "studio.css", rules: studioRules, selector: ".setup-section", expect: { "min-width": "0" } },
  {
    file: "studio.css",
    rules: studioRules,
    selector: ".setup-pane .row > span:not(.badge):not(.dot):not(.saved)",
    expect: { "min-width": "0", "max-width": "65%", "overflow-wrap": "anywhere", "text-align": "right" }
  }
];
const overflowGuardErrors = [];
for (const item of overflowGuardChecks) {
  const decls = mergedDeclsForSelector(item.rules, item.selector);
  for (const [prop, expected] of Object.entries(item.expect)) {
    if (decls[prop] !== expected) {
      overflowGuardErrors.push(item.file + " " + item.selector + " 缺少 " + prop + ": " + expected + "（实际：" + (decls[prop] || "未声明") + "）");
    }
  }
}
if (overflowGuardErrors.length) {
  for (const msg of overflowGuardErrors) fail(msg);
} else {
  pass("theme.css / studio.css 长报错防溢出规则齐全（overflow-wrap / min-width / flex-shrink / max-width）");
}

for (const css of cssFiles) {
  const rel = path.relative(repo, css);
  if (path.basename(css) === "theme.css") continue;
  const text = fs.readFileSync(css, "utf8");
  const hex = text.match(/#[0-9a-fA-F]{3,8}/g) || [];
  const leftover = hex.filter((h) => !/var\(/.test(h));
  if (leftover.length) {
    fail(rel + " 含有未走主题变量的十六进制颜色：" + leftover.join(", "));
  }
}

const setupPanelJs = fs.readFileSync(path.join(v3, "js/setup-panel.js"), "utf8");
for (const tab of ["models", "voice", "shot", "keys", "api", "sys"]) {
  if (!setupPanelJs.includes(tab + ":") && !setupPanelJs.includes('"' + tab + '"') && !setupPanelJs.includes("'" + tab + "'")) {
    fail("setup-panel.js 未覆盖子标签 " + tab);
  }
}
if (setupPanelJs.includes("baidu") || setupPanelJs.includes("whisper_model")) fail("setup-panel.js 仍出现已删除的百度或 Whisper 切换");
if (setupPanelJs.includes("Qwen3-ASR-0.6B")) fail("setup-panel.js 出现低配模型切换");
pass("设置面板覆盖 6 个子标签，且无百度/Whisper/0.6B 切换");
if (setupPanelJs.includes("hotkeyHint") || setupPanelJs.includes("以后端返回的提示为准") || setupPanelJs.includes("已生效")) {
  fail("setup-panel.js 仍含空承诺或已生效文案");
} else pass("快捷键区已去掉空承诺和已生效");
if (!setupPanelJs.includes("sidecarRunning === true")) {
  fail("setup-panel.js 未按 sidecarRunning 判定识别就绪");
} else pass("setup-panel.js 已接线 sidecar 就绪判定");

const labels = fs.readFileSync(path.join(v3, "js/labels.js"), "utf8");
const openapi = JSON.parse(fs.readFileSync(openapiPath, "utf8"));
const schemas = openapi.components.schemas;

function collectEnums(schema, into) {
  if (!schema || typeof schema !== "object") return;
  if (Array.isArray(schema.enum)) schema.enum.forEach((v) => into.add(String(v)));
  Object.values(schema).forEach((v) => {
    if (v && typeof v === "object") collectEnums(v, into);
  });
}

const errorEnums = new Set(schemas.ErrorCode.enum);
const jobEnums = new Set(schemas.JobState.enum);
const sourceEnums = new Set(schemas.UtteranceSource.enum);
const polishEnums = new Set(schemas.HistoryPolish.enum);
const eventEnums = new Set();
collectEnums(schemas.ProductSSEEventEnvelope, eventEnums);
// also ProductEvent types from const event fields
for (const name of Object.keys(schemas)) {
  if (name.indexOf("Product") === 0 && schemas[name].properties && schemas[name].properties.event) {
    const ev = schemas[name].properties.event;
    if (ev.const) eventEnums.add(ev.const);
  }
}

function hasAll(label, values) {
  const missing = [...values].filter((v) => !labels.includes(v));
  if (missing.length) fail(label + " 缺少：" + missing.join(", "));
  else pass(label + " 已覆盖");
}
hasAll("ErrorCode", errorEnums);
hasAll("JobState", jobEnums);
hasAll("UtteranceSource", sourceEnums);
hasAll("HistoryPolish", polishEnums);
hasAll("ProductEventType", eventEnums);

for (const field of ["stable_text", "tail_text", "raw_text", "final_text", "ocr_text", "polish_enabled", "custom_keywords", "translate_lang_pair", "translate_auto_enabled", "lang_pair", "source_lang", "direction_mode", "hotkey_voice", "hotkey_screenshot", "hotkey_ocr", "hotkey_repeat", "hotkey_annotate", "job_id", "delivery_unknown"]) {
  if (!prodText.includes(field)) fail("生产代码未使用契约字段 " + field);
}
pass("关键契约字段（含 translate_lang_pair / translate_auto_enabled / 方向字段与五热键）已在界面层出现");

/* 反向守卫：static/v3/ 全量生产源码与生成快照严禁残留已删除旧字段 translate_target_lang */
function stripSourceComments(filePath, raw) {
  if (filePath.endsWith(".json")) return raw;
  if (filePath.endsWith(".html")) return raw.replace(/<!--[\s\S]*?-->/g, "");
  return raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/.*$/gm, "$1");
}
const legacyTargetLangHits = [];
for (const file of files) {
  const code = stripSourceComments(file, fs.readFileSync(file, "utf8"));
  if (/\btranslate_target_lang\b/.test(code)) {
    legacyTargetLangHits.push(path.relative(repo, file));
  }
}
if (legacyTargetLangHits.length) {
  fail("static/v3/ 生产源码仍残留已废弃字段 translate_target_lang：" + legacyTargetLangHits.join(", "));
} else {
  pass("static/v3/ 生产源码已彻底清除已废弃字段 translate_target_lang（零出现反向守卫）");
}

const indexHtml = fs.readFileSync(path.join(v3, "index.html"), "utf8");
if (!/css\/studio\.css/.test(indexHtml) || !/js\/setup-panel\.js/.test(indexHtml) || !/js\/home\.js/.test(indexHtml) || !/js\/business\/setup-actions\.js/.test(indexHtml) || !/js\/business\/home-controller\.js/.test(indexHtml) || !/data-page=["']home["']/.test(indexHtml)) {
  fail("index.html 未正确挂载方案 C 统一工作台脚本与样式");
} else pass("index.html 已加载 studio.css / setup-panel.js / home.js / setup-actions.js / home-controller.js / bootstrap(home)");
if (!/hotkey-capture\.js[\s\S]*js\/setup-panel\.js[\s\S]*js\/home\.js/.test(indexHtml)) {
  fail("index.html 未按 hotkey-capture.js → setup-panel.js → home.js 顺序加载");
} else pass("index.html 先加载 hotkey-capture.js 再加载 setup-panel.js 与 home.js");
const redundantHomeScripts = [
  "js/confirm.js",
  "js/business/job-id.js",
  "js/business/sse.js",
  "js/business/audio.js",
  "js/business/platform.js"
];
const leftoverHomeScripts = redundantHomeScripts.filter((src) => indexHtml.includes(src));
if (leftoverHomeScripts.length) fail("index.html 仍加载未使用的冗余脚本：" + leftoverHomeScripts.join(", "));
else pass("index.html 已清理未使用的 5 个冗余脚本");

const homeJs = fs.readFileSync(path.join(v3, "js/home.js"), "utf8");
if (
  !homeJs.includes("onTranslateInput") ||
  !homeJs.includes("onTranslateText") ||
  !homeJs.includes("onSwapDirection") ||
  !homeJs.includes("onClearTranslate") ||
  !homeJs.includes("onChangeLangPair") ||
  !homeJs.includes("onSubmitImage") ||
  !homeJs.includes("onCopy") ||
  !homeJs.includes("onToggleTheme")
) {
  fail("home.js 缺少翻译框（输入/提交/互换/清空/语言对）、图片 OCR、复制或主题切换回调");
} else pass("home.js 提供翻译框完整回调（输入/提交/互换/清空/语言对）、图片 OCR、复制与主题切换回调");
if (/\/setup/.test(homeJs)) {
  fail("home.js 仍含 /setup 外链跳转");
} else pass("home.js 无 /setup 外链跳转，设置为页内面板");

if (!homeJs.includes("sanitizeLanPatch") || !homeJs.includes("urlMasked") || /entry\.url\b/.test(homeJs)) {
  fail("home.js 未严格阻止明文 LAN 令牌/URL 进入 DOM");
} else pass("home.js 仅渲染脱敏 urlMasked，明文令牌不入 DOM");

const homeErrMatch = homeJs.match(/phase === "error"\)\s*\{[\s\S]*?(?=\}\s*else\b)/g) || [];
if (homeErrMatch.length < 2 || homeErrMatch.some((seg) => /\bocr_text\b/.test(seg))) {
  fail("home.js 失败分支缺失或可能把 ocr_text 当译文");
} else pass("home.js 翻译与 OCR 失败分支均如实呈现错误，不拿 ocr_text 冒充译文");

const voiceJs = fs.readFileSync(path.join(v3, "js/voice.js"), "utf8");
if (!voiceJs.includes("onRecordStart") || !voiceJs.includes("onRecordEnd") || !voiceJs.includes("onPickAudio") || !voiceJs.includes("onSendOriginal")) {
  fail("语音页缺少录音控制或发送原文回调");
} else pass("语音页提供录音控制、选文件和发送原文回调");
if (!voiceJs.includes("可能已经输入")) fail("语音页缺少投递未知提示");
else pass("语音页包含投递未知提示");
if (voiceJs.includes("设置 → 手机录入")) fail("voice.js 仍含过期引导文案「设置 → 手机录入」");
else pass("voice.js 已对齐电脑工作台「手机扫码语音」引导文案");
const voiceCtlJs = fs.readFileSync(path.join(v3, "js/business/voice-controller.js"), "utf8");
if (voiceCtlJs.includes("电脑设置页")) fail("voice-controller.js 仍含过期文案「电脑设置页」");
else pass("voice-controller.js 已对齐电脑工作台「手机扫码语音」引导文案");

const histJs = fs.readFileSync(path.join(v3, "js/history.js"), "utf8");
const histCode = histJs.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
if (histCode.includes("已输入")) fail("历史组件把处理记录写成已输入");
else pass("历史组件未谎称已输入");

if (/paths 目前为空|路径尚未发布/.test(setupPanelJs)) fail("开放接口页仍声称 paths 为空或尚未发布");
else if (!/apiPaths/.test(setupPanelJs) || !/尚未读取接口路径/.test(setupPanelJs)) fail("开放接口页未改为从 apiPaths 派生真实路径");
else pass("开放接口页按派生 apiPaths 列出路径，缺省为尚未读取");

const report = { passes, failures, files: files.map((f) => path.relative(repo, f)) };
const out = path.join(logDir, "check_static.json");
fs.writeFileSync(out, JSON.stringify(report, null, 2));
console.log("静态检查通过 " + passes.length + "，失败 " + failures.length);
for (const m of failures) console.log("FAIL " + m);
for (const m of passes) console.log("PASS " + m);
if (failures.length) process.exit(1);

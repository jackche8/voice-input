#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync, spawn, spawnSync } = require("child_process");

const repo = path.resolve(__dirname, "../..");
const v3js = path.join(repo, "static/v3/js");
const bizDir = path.join(v3js, "business");
const snapshotPath = path.join(repo, "static/v3/openapi.json");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];
function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

const businessFiles = fs.readdirSync(bizDir).filter((n) => n.endsWith(".js")).map((n) => path.join(bizDir, n));
for (const file of businessFiles) {
  try {
    execFileSync(process.execPath, ["--check", file], { stdio: "pipe" });
    pass("syntax " + path.basename(file));
  } catch (err) {
    fail("syntax " + path.basename(file) + ": " + (err.stderr || err.message).toString());
  }
}

if (!fs.existsSync(snapshotPath)) {
  fail("缺少唯一生成快照 static/v3/openapi.json");
}
if (fs.existsSync(path.join(bizDir, "openapi.snapshot.json"))) {
  fail("应删除第二份手写快照 static/v3/js/business/openapi.snapshot.json");
} else pass("已删除业务目录第二份 OpenAPI 快照");

const snapshot = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));

function operationSet(doc) {
  const ids = [];
  Object.keys(doc.paths || {}).forEach((p) => {
    Object.keys(doc.paths[p]).forEach((method) => {
      const op = doc.paths[p][method];
      if (op && op.operationId) ids.push(op.operationId);
    });
  });
  return ids.sort();
}

const snapIds = operationSet(snapshot);
const snapPaths = Object.keys(snapshot.paths || {});
if (snapPaths.length < 24) fail("唯一快照 paths 应不少于 24，实际 " + snapPaths.length);
else pass("唯一生成快照 static/v3/openapi.json 有 " + snapPaths.length + " 条路径");
if (snapIds.length < 25) fail("唯一快照 operationId 应不少于 25，实际 " + snapIds.length);
else pass("唯一快照 operationId 集合为 " + snapIds.length + " 个");

const requiredOps = [
  "setup_check_api_setup_check_get",
  "setup_install_all_api_setup_install_all_post",
  "retry_component_api_v1_components__name__retry_post",
  "lan_access_api_v1_lan_access_get",
  "list_audio_inputs_api_v1_audio_inputs_get",
  "list_logs_api_v1_logs_get",
  "diagnostic_summary_api_v1_logs_diagnostic_summary_get",
  "read_log_api_v1_logs__name__get",
  "clear_log_api_v1_logs__name__delete",
  "get_about_api_v1_about_get",
  "get_config_api_v1_config_get",
  "post_config_api_v1_config_post",
  "history_api_v1_history_get",
  "translate_api_v1_translate_post",
  "ocr_preview_api_v1_ocr_preview_post",
  "ocr_translate_api_v1_ocr_translate_post",
  "from_audio_api_v1_utterances_from_audio_post",
  "resend_api_v1_utterances_resend_post",
  "query_api_v1_jobs__job_id__get",
  "platform_event_api_v1_events_platform_post"
];
const missingOps = requiredOps.filter((id) => snapIds.indexOf(id) < 0);
if (missingOps.length) fail("快照缺少前端所需 operationId：" + missingOps.join(", "));
else pass("快照含 home/setup/voice 全部必需 operationId（含 translate 与 ocr-translate）");
const removedOps = [
  "setup_set_config_api_setup_set_config_post",
  "setup_unsupported_pull_model_api_setup_pull_model_post",
  "setup_unsupported_install_ollama_api_setup_install_ollama_post",
  "setup_unsupported_start_ollama_api_setup_start_ollama_post"
];
const stillPresent = removedOps.filter((id) => snapIds.indexOf(id) >= 0);
if (stillPresent.length) fail("快照仍含已删除 operationId：" + stillPresent.join(", "));
else pass("快照已去掉 set_config 与旧 setup 未实现接口");

const bootstrapJs = fs.readFileSync(path.join(bizDir, "bootstrap.js"), "utf8");
if (!bootstrapJs.includes('"/openapi.snapshot.json"') || bootstrapJs.includes("js/business/openapi.snapshot.json")) {
  fail("bootstrap 未改为请求 /openapi.snapshot.json");
} else pass("bootstrap 默认快照地址为 /openapi.snapshot.json");

for (const file of businessFiles) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  if (/localStorage|sessionStorage|indexedDB|document\.cookie/.test(text)) fail(rel + " 使用了 Web Storage 或 Cookie");
  if (/\/api\/v1\/|\/api\/setup\//.test(text)) fail(rel + " 手写了 /api/ 路径，应只从 OpenAPI 派生");
}
if (!failures.some((m) => m.includes("手写了") || m.includes("Web Storage"))) {
  pass("业务 JS 未手写 /api 路径，也未使用 Web Storage");
}

function jsonRes(status, body) {
  const text = arguments.length < 2 ? "" : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status: status,
    text: function () { return Promise.resolve(text); },
    json: function () { return Promise.resolve(body); }
  };
}

function createWorld() {
  const calls = [];
  const world = {
    calls: calls,
    routes: [],
    fetch: function (url, init) {
      const rec = { url: String(url), method: (init && init.method) || "GET", headers: (init && init.headers) || {}, body: init && init.body, init: init || {}, referrerPolicy: init && init.referrerPolicy };
      calls.push(rec);
      const route = world.routes.find(function (r) {
        if (r.method && r.method !== rec.method) return false;
        if (typeof r.match === "function") return r.match(rec.url, rec);
        return rec.url.indexOf(r.path) >= 0;
      });
      if (!route) return Promise.resolve(jsonRes(404, { ok: false, error: "not_found", message: "no fake route " + rec.url }));
      return Promise.resolve(route.handler(rec, jsonRes));
    }
  };
  return world;
}

function FakeEventSource(url) {
  this.url = url;
  this.readyState = 0;
  this.onopen = null;
  this.onerror = null;
  this.onmessage = null;
  this._listeners = {};
  FakeEventSource.instances.push(this);
}
FakeEventSource.instances = [];
FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;
FakeEventSource.prototype.addEventListener = function (type, fn) {
  this._listeners[type] = this._listeners[type] || [];
  this._listeners[type].push(fn);
};
FakeEventSource.prototype.close = function () { this.readyState = 2; this.closed = true; };
FakeEventSource.prototype.open = function () {
  this.readyState = 1;
  if (this.onopen) this.onopen();
};
FakeEventSource.prototype.emit = function (type, data) {
  const ev = { type: type, data: typeof data === "string" ? data : JSON.stringify(data) };
  (this._listeners[type] || []).forEach(function (fn) { fn(ev); });
  if (type === "message" && this.onmessage) this.onmessage(ev);
};

function FakeTrack() { this.stopped = 0; this.stop = function () { this.stopped += 1; }; }
function FakeStream() {
  this._tracks = [new FakeTrack(), new FakeTrack()];
  this.getTracks = function () { return this._tracks; };
}
function createGumQueue() {
  const calls = [];
  return {
    calls: calls,
    mediaDevices: {
      getUserMedia: function () {
        let resolveFn, rejectFn;
        const stream = new FakeStream();
        const promise = new Promise(function (res, rej) { resolveFn = res; rejectFn = rej; });
        calls.push({
          stream: stream,
          resolve: function () { resolveFn(stream); },
          reject: function (err) { rejectFn(err || new Error("gum failed")); }
        });
        return promise;
      }
    }
  };
}
function tracksLive(stream) {
  return stream.getTracks().some(function (tr) { return tr.stopped === 0; });
}
function tracksStopped(stream) {
  return stream.getTracks().every(function (tr) { return tr.stopped > 0; });
}
function FakeMediaRecorder(stream, opts) {
  this.stream = stream;
  this.mimeType = (opts && opts.mimeType) || "audio/webm";
  this.state = "inactive";
  this.ondataavailable = null;
  this.onstop = null;
  this.onerror = null;
  FakeMediaRecorder.instances.push(this);
}
FakeMediaRecorder.instances = [];
FakeMediaRecorder.isTypeSupported = function (t) { return String(t).indexOf("audio/webm") === 0; };
FakeMediaRecorder.prototype.start = function () { this.state = "recording"; };
FakeMediaRecorder.prototype.stop = function () {
  this.state = "inactive";
  if (this.ondataavailable) {
    this.ondataavailable({ data: new Blob([new Uint8Array([1, 2, 3, 4])], { type: this.mimeType }) });
  }
  if (this.onstop) this.onstop();
};

function loadBusiness(extra) {
  const context = {
    window: {},
    console: console,
    fetch: extra && extra.fetch,
    EventSource: FakeEventSource,
    MediaRecorder: FakeMediaRecorder,
    FormData: FormData,
    Blob: Blob,
    File: typeof File !== "undefined" ? File : Blob,
    URLSearchParams: URLSearchParams,
    crypto: crypto,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    Promise: Promise,
    Object: Object,
    Array: Array,
    JSON: JSON,
    Error: Error,
    Uint8Array: Uint8Array
  };
  context.window = context.window;
  context.globalThis = context;
  context.navigator = {
    clipboard: extra && extra.clipboard,
    mediaDevices: extra && extra.mediaDevices
  };
  context.document = extra && extra.document;
  context.location = extra && extra.location;
  if (extra && extra.ClipboardItem) context.ClipboardItem = extra.ClipboardItem;
  context.window.V3 = {};
  context.window.V3Business = {};
  vm.createContext(context);
  ["dom.js", "labels.js", "icons.js", "defaults.js"].forEach(function (name) {
    vm.runInContext(fs.readFileSync(path.join(v3js, name), "utf8"), context, { filename: name });
  });
  const order = [
    "catalog.js", "context.js", "job-id.js", "http.js", "sse.js", "clipboard.js",
    "audio.js", "platform.js", "map-setup.js", "setup-actions.js", "logs-actions.js", "ocr-actions.js", "home-controller.js",
    "voice-controller.js", "bootstrap.js"
  ];
  order.forEach(function (name) {
    vm.runInContext(fs.readFileSync(path.join(bizDir, name), "utf8"), context, { filename: name });
  });
  context.V3 = context.window.V3;
  context.V3Business = context.window.V3Business;
  return context;
}

function fakeView(initial) {
  let state = Object.assign({}, initial || {});
  return {
    setState: function (patch) {
      const next = Object.assign({}, state);
      Object.keys(patch || {}).forEach(function (k) {
        const v = patch[k];
        if (v && typeof v === "object" && !Array.isArray(v) && state[k] && typeof state[k] === "object" && !Array.isArray(state[k])) {
          next[k] = Object.assign({}, state[k], v);
        } else next[k] = v;
      });
      state = next;
    },
    getState: function () { return state; }
  };
}

function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

async function waitFor(predicate, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 2000);
  while (!predicate()) {
    if (Date.now() > deadline) return false;
    await wait(5);
  }
  return true;
}

async function runUnit() {
  const catalogCtx = loadBusiness();
  const B = catalogCtx.window.V3Business;
  const catalog = B.createCatalog(snapshot);
  if (catalog.size !== snapIds.length) fail("catalog 数量 " + catalog.size + " != " + snapIds.length);
  else pass("catalog 从唯一快照派生 " + catalog.size + " 个 operation");
  const fromAudio = catalog.operation("from_audio_api_v1_utterances_from_audio_post");
  if (fromAudio.method !== "POST" || fromAudio.path !== "/api/v1/utterances/from-audio" || fromAudio.access !== "lan_token") {
    fail("from-audio 派生错误 " + JSON.stringify(fromAudio));
  } else pass("from-audio 路径/方法/权限由 OpenAPI 派生");
  const statusOp = catalog.operation("status_api_v1_status_get");
  if (statusOp.access !== "local" || statusOp.method !== "GET") fail("status 权限派生错误");
  else pass("status 默认无 VoiceToken，按本机接口处理");
  const eventsOp = catalog.operation("product_events_api_v1_events_get");
  if (eventsOp.path !== "/api/v1/events" || eventsOp.access !== "local") fail("events 派生错误");
  else pass("产品事件路径由 OpenAPI 派生且为本机接口");
  const platformOp = catalog.operation("platform_event_api_v1_events_platform_post");
  if (platformOp.method !== "POST" || platformOp.path !== "/api/v1/events/platform" || platformOp.access !== "local") {
    fail("platform 事件派生错误 " + JSON.stringify(platformOp));
  } else pass("平台事件 POST /api/v1/events/platform 由 OpenAPI 派生且仅本机");
  const translateOp = catalog.operation("translate_api_v1_translate_post");
  if (translateOp.method !== "POST" || translateOp.path !== "/api/v1/translate") {
    fail("translate 派生错误 " + JSON.stringify(translateOp));
  } else pass("translate POST /api/v1/translate 由 OpenAPI 派生");
  const ocrOp = catalog.operation("ocr_translate_api_v1_ocr_translate_post");
  if (ocrOp.method !== "POST" || ocrOp.path !== "/api/v1/ocr-translate" || ocrOp.access !== "local") {
    fail("ocr-translate 派生错误 " + JSON.stringify(ocrOp));
  } else pass("ocr-translate POST /api/v1/ocr-translate 由 OpenAPI 派生且仅本机");
  const ocrPreviewOp = catalog.operation("ocr_preview_api_v1_ocr_preview_post");
  if (ocrPreviewOp.method !== "POST" || ocrPreviewOp.path !== "/api/v1/ocr/preview" || ocrPreviewOp.access !== "local") {
    fail("ocr-preview 派生错误 " + JSON.stringify(ocrPreviewOp));
  } else pass("ocr-preview POST /api/v1/ocr/preview 由 OpenAPI 派生且仅本机");
  const audioInputsOp = catalog.operation("list_audio_inputs_api_v1_audio_inputs_get");
  if (audioInputsOp.method !== "GET" || audioInputsOp.path !== "/api/v1/audio-inputs" || audioInputsOp.access !== "local") {
    fail("audio-inputs 派生错误 " + JSON.stringify(audioInputsOp));
  } else pass("audio-inputs GET /api/v1/audio-inputs 由 OpenAPI 派生且仅本机");

  const fileCtx = B.createContext({ protocol: "file:", hostname: "", origin: "file://", href: "file:///tmp/voice.html", search: "" });
  if (fileCtx.kind !== "file" || fileCtx.allowFetch) fail("file:// 上下文未关闭请求");
  else pass("file:// 上下文不允许成功请求");

  const localCtx = B.createContext({ protocol: "https:", hostname: "127.0.0.1", origin: "https://127.0.0.1:17945", href: "https://127.0.0.1:17945/setup", search: "" });
  if (localCtx.kind !== "local" || !localCtx.canCallLocal()) fail("本机上下文不正确");
  else pass("本机 https 上下文可调用仅本机接口");

  const lanCtx = B.createContext({ protocol: "https:", hostname: "192.168.1.8", origin: "https://192.168.1.8:17945", href: "https://192.168.1.8:17945/static/v3/voice.html?token=secret-fixture-not-for-log", search: "?token=secret-fixture-not-for-log" });
  if (lanCtx.kind !== "lan" || lanCtx.canCallLocal() || !lanCtx.tokenPresent) fail("局域网上下文不正确");
  else pass("局域网上下文带内存令牌，且不能调用仅本机接口");

  const world = createWorld();
  const httpLan = B.createHttp({ catalog: catalog, context: lanCtx, fetch: world.fetch, origin: "https://192.168.1.8:17945" });
  world.routes.push({
    path: "/api/v1/config",
    handler: function () { return jsonRes(200, { polish_enabled: true }); }
  });
  try {
    await httpLan.call("get_config_api_v1_config_get");
    fail("局域网不应发出 config 请求");
  } catch (err) {
    if (err.blocked && world.calls.length === 0) pass("局域网调用仅本机接口在客户端被阻止，未发出 fetch");
    else fail("局域网阻止行为异常：" + (err && err.message) + " calls=" + world.calls.length);
  }
  try {
    await httpLan.call("list_audio_inputs_api_v1_audio_inputs_get");
    fail("局域网不应发出 audio-inputs 请求");
  } catch (err) {
    if (err.blocked && world.calls.length === 0) pass("局域网调用 audio-inputs 在客户端被阻止，未发出 fetch");
    else fail("局域网 audio-inputs 阻止行为异常：" + (err && err.message) + " calls=" + world.calls.length);
  }

  world.calls.length = 0;
  world.routes = [{
    path: "/api/v1/history",
    method: "GET",
    handler: function (rec) {
      if (!rec.headers["X-Voice-Token"]) return jsonRes(403, { ok: false, error: "forbidden", message: "缺少令牌" });
      return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    }
  }];
  const hist = await httpLan.call("history_api_v1_history_get", { query: { limit: 5, offset: 0 } });
  if (hist.total === 0 && world.calls[0].headers["X-Voice-Token"] && world.calls[0].url.indexOf("token=") < 0) {
    pass("局域网 history 使用请求头令牌，不把令牌写进 URL");
  } else fail("局域网 history 授权方式不符合既有设计");
  if (world.calls[0].init && world.calls[0].init.referrerPolicy === "no-referrer") {
    pass("业务 fetch 使用 referrerPolicy=no-referrer");
  } else fail("业务 fetch 未设置 no-referrer");

  const httpFile = B.createHttp({ catalog: catalog, context: fileCtx, fetch: world.fetch, origin: "" });
  world.calls.length = 0;
  try {
    await httpFile.call("history_api_v1_history_get");
    fail("file:// 不应成功调用接口");
  } catch (err) {
    if (err.blocked && world.calls.length === 0) pass("file:// 无成功请求路径");
    else fail("file:// 阻止失败");
  }

  function setupCheckBody(overrides) {
    const base = {
      ok: true,
      session_type: "local",
      asr: {
        model_name: "Qwen/Qwen3-ASR-1.7B",
        status: "missing",
        weights_present: false,
        sidecar_running: false,
        size_label: null,
        device_label: null,
        progress: null,
        message: "尚未下载识别模型"
      },
      ollama: {
        model_name: "qwen3.5:4b",
        installed: false,
        version: null,
        running: false,
        model_status: "missing",
        size_label: null,
        progress: null,
        message: "尚未安装 Ollama"
      },
      capabilities: [
        { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
      ],
      components: [
        componentBody("ollama", "not_installed", { reason: "未找到 Ollama 程序" }),
        componentBody("asr", "not_installed", { reason: "未找到识别运行环境" }),
        componentBody("server", "running")
      ],
      install_all: installAllBody("idle", [])
    };
    return Object.assign(base, overrides || {});
  }

  const STEP_KINDS = ["ollama_install", "wait_ollama", "ollama_pull", "asr_runtime_install", "asr_download"];
  function installAllBody(state, stepStates, extra) {
    const stepExtra = (extra && extra.steps) || {};
    return {
      state: state,
      current: (extra && extra.current) || null,
      steps: STEP_KINDS.map(function (kind, i) {
        return Object.assign({ kind: kind, state: stepStates[i] || "pending", message: "", progress: null, task_id: null }, stepExtra[kind] || {});
      })
    };
  }
  function componentBody(name, state, extra) {
    return Object.assign({ name: name, state: state, reason: null, failures: 0, retry_at: null, pid: null, since: "2026-11-20T00:00:00+00:00" }, extra || {});
  }

  /* setup controller: real schema mapping, config POST, install-all, component retry, LAN */
  const setupWorld = createWorld();
  setupWorld.check = setupCheckBody();
  setupWorld.checkReads = 0;
  setupWorld.installPosts = 0;
  setupWorld.retried = [];
  setupWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () {
      setupWorld.checkReads += 1;
      if (typeof setupWorld.onCheck === "function") setupWorld.check = setupWorld.onCheck(setupWorld.check, setupWorld.checkReads);
      return jsonRes(200, setupWorld.check);
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, { polish_enabled: false, custom_keywords: ["Alpha"], translate_lang_pair: "zh-en", translate_auto_enabled: true, hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r", hotkey_annotate: "ctrl+alt+a", autostart_enabled: true, ui_theme: "light", audio_input_device: "", ollama_model: "qwen3.5:4b" });
    } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      setupWorld.lastConfig = body;
      if (Object.prototype.hasOwnProperty.call(body, "baidu_appkey") || Object.prototype.hasOwnProperty.call(body, "ollama_model") || Object.prototype.hasOwnProperty.call(body, "translate_target_lang")) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "未知或只读字段" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_lang_pair: "zh-en", translate_auto_enabled: true, hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r", hotkey_annotate: "ctrl+alt+a", autostart_enabled: true, ui_theme: "light", audio_input_device: "", ollama_model: "qwen3.5:4b" }, body));
    } },
    { path: "/api/v1/audio-inputs", method: "GET", handler: function () {
      return jsonRes(200, {
        ok: true,
        default_device: "USB Mic",
        devices: [
          { name: "USB Mic", hostapi: "PulseAudio", is_default: true },
          { name: "Headset Mic", hostapi: "PulseAudio", is_default: false }
        ]
      });
    } },
    { path: "/api/v1/history", method: "GET", handler: function () {
      return jsonRes(200, { records: [{ timestamp: "t", raw_text: "a", final_text: "a", job_id: "j1", source: "web", polish: "off" }], total: 1, skipped_malformed: 0 });
    } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () {
      setupWorld.lanCalls = (setupWorld.lanCalls || 0) + 1;
      return jsonRes(200, {
        port: 8443,
        token: "secret-lan-token-do-not-log",
        token_masked: "sec****log",
        entries: [{
          ip: "192.0.2.8",
          url: "https://192.0.2.8:8443/voice?token=secret-lan-token-do-not-log",
          url_masked: "https://192.0.2.8:8443/voice?token=sec****log",
          qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        }],
        message: ""
      });
    } },
    { path: "/api/setup/set_config", method: "POST", handler: function () {
      setupWorld.setConfigCalled = true;
      return jsonRes(404, { ok: false, error: "not_found", message: "set_config 已删除" });
    } },
    { path: "/api/setup/install-all", method: "POST", handler: function (rec) {
      setupWorld.installPosts += 1;
      setupWorld.installBodies = (setupWorld.installBodies || []).concat([rec.body == null ? null : rec.body]);
      if (setupWorld.failInstall) return jsonRes(403, { ok: false, error: "forbidden", message: setupWorld.failInstall });
      setupWorld.check = setupCheckBody({ install_all: installAllBody("running", ["running"], { current: "ollama_install" }) });
      return jsonRes(202, setupWorld.check.install_all);
    } },
    { match: function (url) { return /\/api\/v1\/components\/[^/]+\/retry$/.test(String(url).split("?")[0]); }, method: "POST", handler: function (rec) {
      const name = decodeURIComponent(rec.url.split("?")[0].split("/").slice(-2)[0]);
      setupWorld.retried.push(name);
      if (["ollama", "asr", "server"].indexOf(name) < 0) return jsonRes(404, { ok: false, error: "not_found", message: "没有组件 '" + name + "'" });
      return jsonRes(202, { ok: true });
    } }
  ];
  const setupCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const setupView = fakeView();
  setupCtl.bind(setupView);
  await setupCtl.start();
  const st = setupView.getState();
  if (st.connection === "connected" && st.asr && st.asr.status === "missing" && st.ollama.modelStatus === "missing" && !st.asr.progress && st.history.records.length === 1 && st.polish_enabled === false && st.autostart_enabled === true) {
    pass("统一工作台设置已连接，按 asr.status/model_status 映射，不用 injected 猜就绪，也无假进度；读到 autostart_enabled");
  } else fail("设置就绪映射错误：" + JSON.stringify({ connection: st.connection, asr: st.asr, ollama: st.ollama, autostart: st.autostart_enabled }));
  if (
    st.installAll && st.installAll.state === "idle" && st.installAll.current === null
    && st.installAll.steps.map(function (x) { return x.kind + ":" + x.state; }).join(",") === STEP_KINDS.map(function (k) { return k + ":pending"; }).join(",")
    && st.components && st.components.map(function (c) { return c.name + ":" + c.state; }).join(",") === "ollama:not_installed,asr:not_installed,server:running"
    && st.components[0].reason === "未找到 Ollama 程序"
    && setupCtl._debug.polling() === false
  ) {
    pass("加载时映射 install_all（五步固定顺序）与 components；空闲时不重读");
  } else fail("install_all/components 映射错误：" + JSON.stringify({ installAll: st.installAll, components: st.components, polling: setupCtl._debug.polling() }));
  if (st.capabilities && st.capabilities[0] && st.capabilities[0].available === false && /剪贴板/.test(st.capabilities[0].name) && /无法写入剪贴板/.test(st.capabilities[0].message)) {
    pass("平台能力按真实 capabilities 呈现不可用原因");
  } else fail("能力映射错误：" + JSON.stringify(st.capabilities));
  if (Array.isArray(st.apiPaths) && st.apiPaths.some(function (op) { return op.path === "/api/setup/check" && op.method === "GET"; }) && st.apiPaths.length === snapIds.length) {
    pass("renderApi 数据为派生真实 paths，不是空表");
  } else fail("apiPaths 未派生真实路径：" + JSON.stringify(st.apiPaths && st.apiPaths.slice(0, 3)));
  if (st.tokenMasked === "sec****log" && st.tokenPresent === true && st.lanEntries && st.lanEntries[0] && st.lanEntries[0].urlMasked.indexOf("token=sec****log") >= 0 && !st.lanEntries[0].url && JSON.stringify(st).indexOf("secret-lan-token-do-not-log") < 0) {
    pass("局域网入口只把遮罩写入视图状态，原始令牌不进 setState");
  } else fail("LAN 状态泄露或映射错误");
  const copied = [];
  const copyCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: { writeText: function (text) { copied.push(text); return Promise.resolve(); } },
    pollInterval: 10
  });
  const copyView = fakeView();
  copyCtl.bind(copyView);
  await copyCtl.start();
  await copyCtl.hooks.onCopy({ kind: "token" });
  await copyCtl.hooks.onCopy({ kind: "lan_url", index: 0 });
  if (copied[0] === "secret-lan-token-do-not-log" && copied[1].indexOf("token=secret-lan-token-do-not-log") >= 0 && JSON.stringify(copyView.getState()).indexOf("secret-lan-token-do-not-log") < 0) {
    pass("令牌与入口网址仅在用户点击复制时写出，不进入 DOM 状态");
  } else fail("复制路径不正确：" + JSON.stringify(copied));
  copyCtl.destroy();

  setupView.setState({ keywordsDraft: "未保存关键词草稿" });
  await setupCtl.loadAll();
  if (setupView.getState().keywordsDraft === "未保存关键词草稿") {
    pass("loadAll 不覆盖未保存的 keywordsDraft");
  } else fail("loadAll 覆盖了 keywordsDraft：" + JSON.stringify(setupView.getState().keywordsDraft));

  await setupCtl.hooks.onSaveKeywords("Alpha，Beta  Gamma");
  const saved = setupWorld.lastConfig;
  if (setupWorld.setConfigCalled) fail("保存配置仍调用了已删除的 set_config");
  if (
    saved &&
    Array.isArray(saved.custom_keywords) &&
    saved.custom_keywords.join(",") === "Alpha,Beta,Gamma" &&
    setupView.getState().keywordsDraft === null &&
    setupView.getState().keywordsSaved === true &&
    !Object.prototype.hasOwnProperty.call(saved, "baidu_appkey") &&
    !Object.prototype.hasOwnProperty.call(saved, "whisper_model") &&
    !Object.prototype.hasOwnProperty.call(saved, "ollama_model")
  ) {
    pass("保存只经 config POST 提交合法字段，并在成功时清空 keywordsDraft");
  } else fail("保存字段不合法：" + JSON.stringify(saved));

  setupView.setState({ hotkeyCaptureWarning: "仅使用 Shift 修饰警告" });
  await setupCtl.hooks.onSaveHotkeys({
    hotkey_voice: "ctrl+alt+z",
    hotkey_screenshot: "ctrl+alt+x",
    hotkey_ocr: "ctrl+alt+p",
    hotkey_repeat: "ctrl+alt+r",
    hotkey_annotate: "ctrl+alt+b"
  });
  const savedKeys = setupWorld.lastConfig;
  const afterKeys = setupView.getState();
  if (
    st.hotkey_ocr === "ctrl+alt+o" &&
    st.hotkey_annotate === "ctrl+alt+a" &&
    savedKeys &&
    savedKeys.hotkey_ocr === "ctrl+alt+p" &&
    savedKeys.hotkey_annotate === "ctrl+alt+b" &&
    afterKeys.hotkey_ocr === "ctrl+alt+p" &&
    afterKeys.hotkey_annotate === "ctrl+alt+b" &&
    afterKeys.hotkeysSaved === true &&
    afterKeys.hotkeyCaptureWarning === ""
  ) {
    pass("home-controller + setup-actions 读取并保存第四/五热键 hotkey_ocr 与 hotkey_annotate，保存成功时清空 hotkeyCaptureWarning");
  } else fail("home-controller hotkey_ocr/hotkey_annotate 保存异常：" + JSON.stringify({ initOcr: st.hotkey_ocr, initAnnotate: st.hotkey_annotate, savedKeys: savedKeys, afterOcr: afterKeys.hotkey_ocr, afterAnnotate: afterKeys.hotkey_annotate, warn: afterKeys.hotkeyCaptureWarning }));

  /* 一键装齐：POST 一次（无请求体）→ 进行中按 setup/check 重读 → 结束即停；模型就绪仍只看 check.asr/ollama */
  setupWorld.onCheck = function (check, n) {
    if (check.install_all.state !== "running") return check;
    if (check.install_all.current === "ollama_install") {
      return setupCheckBody({ install_all: installAllBody("running", ["done", "done", "running"], {
        current: "ollama_pull",
        steps: { ollama_pull: { message: "下载中", progress: { completed_bytes: 5, total_bytes: null, percent: null, stage: "pull" } } }
      }) });
    }
    return setupCheckBody({ install_all: installAllBody("done", ["skipped", "done", "done", "done", "done"]) });
  };
  await setupCtl.hooks.onInstallAll();
  const afterPost = setupView.getState();
  if (setupWorld.installPosts === 1 && setupWorld.installBodies[0] == null && afterPost.installAll.state === "running" && afterPost.installAll.current === "ollama_install" && setupCtl._debug.polling() === true) {
    pass("一键装齐只 POST 一次 install-all（无请求体），202 状态立即进视图并开始重读");
  } else fail("install-all 请求或 202 映射错误：" + JSON.stringify({ posts: setupWorld.installPosts, bodies: setupWorld.installBodies, installAll: afterPost.installAll }));
  await waitFor(function () { const ia = setupView.getState().installAll; return ia.steps[2].state === "running"; });
  const stepProgress = setupView.getState().installAll.steps[2].progress;
  if (stepProgress && stepProgress.percent == null && stepProgress.receivedLabel === "5 B" && stepProgress.totalLabel == null && setupView.getState().installAll.steps[2].message === "下载中") {
    pass("步骤进度只换算服务端字节，未给 percent 时不编造百分比");
  } else fail("步骤进度映射错误：" + JSON.stringify(setupView.getState().installAll.steps[2]));
  const doneOk = await waitFor(function () { return setupView.getState().installAll.state === "done" && setupCtl._debug.polling() === false; });
  const afterDone = setupView.getState();
  if (doneOk && afterDone.installAll.steps.map(function (x) { return x.state; }).join(",") === "skipped,done,done,done,done" && afterDone.asr.status === "missing" && setupWorld.installPosts === 1) {
    pass("装齐结束后停止重读；模型行仍按 check.asr 显示，不以装齐完成冒充就绪");
  } else fail("装齐结束映射或停读错误：" + JSON.stringify({ done: doneOk, installAll: afterDone.installAll, asr: afterDone.asr, polling: setupCtl._debug.polling() }));

  setupWorld.onCheck = function (check) {
    if (check.install_all.state !== "running") return check;
    return setupCheckBody({ install_all: installAllBody("failed", ["skipped", "done", "skipped", "failed"], {
      current: "asr_runtime_install",
      steps: { asr_runtime_install: { message: "安装识别运行环境失败：网络不可达" } }
    }) });
  };
  await setupCtl.hooks.onInstallAll();
  const failedOk = await waitFor(function () { return setupView.getState().installAll.state === "failed" && setupCtl._debug.polling() === false; });
  const afterFail = setupView.getState().installAll;
  if (failedOk && afterFail.current === "asr_runtime_install" && afterFail.steps[3].state === "failed" && afterFail.steps[3].message.indexOf("网络不可达") >= 0 && afterFail.steps[4].state === "pending" && setupWorld.installPosts === 2) {
    pass("某步失败即停在该步：失败步与原因进视图，后续步保持待执行，停止重读");
  } else fail("装齐失败映射错误：" + JSON.stringify({ ok: failedOk, installAll: afterFail, posts: setupWorld.installPosts }));
  setupWorld.onCheck = null;

  setupWorld.failInstall = "仅本机可调用";
  const beforeDenied = setupView.getState().installAll;
  await setupCtl.hooks.onInstallAll();
  if (/禁止访问/.test(setupView.getState().statusNotice || "") && /仅本机可调用/.test(setupView.getState().statusNotice || "") && setupView.getState().installAll === beforeDenied && setupCtl._debug.polling() === false) {
    pass("install-all 被拒时把后端错误写进提示，不改装齐状态、不开始重读");
  } else fail("install-all 错误未呈现：" + JSON.stringify({ notice: setupView.getState().statusNotice, polling: setupCtl._debug.polling() }));
  setupWorld.failInstall = null;

  /* 组件重试：POST /api/v1/components/{name}/retry → 202 后重读，直到不再启动中；未知组件的 404 呈现给用户 */
  setupWorld.check = setupCheckBody({ components: [
    componentBody("ollama", "running"),
    componentBody("asr", "stopped", { reason: "连续失败 5 次：CUDA out of memory", failures: 5 }),
    componentBody("server", "running")
  ] });
  await setupCtl.loadAll();
  if (setupView.getState().components[1].state === "stopped" && setupView.getState().components[1].reason.indexOf("CUDA") >= 0 && setupCtl._debug.polling() === false) {
    pass("组件已停止时映射原因，不自动重读");
  } else fail("组件 stopped 映射错误：" + JSON.stringify(setupView.getState().components));
  setupWorld.onCheck = function (check, n) {
    const asr = check.components[1];
    const next = asr.state === "stopped" ? "starting" : "running";
    return setupCheckBody({ components: [check.components[0], componentBody("asr", next), check.components[2]] });
  };
  await setupCtl.hooks.onRetryComponent("asr");
  const retryNotice = setupView.getState().statusNotice || "";
  const retryOk = await waitFor(function () { return setupView.getState().components[1].state === "running" && setupCtl._debug.polling() === false; });
  if (setupWorld.retried.join(",") === "asr" && /已请求重试/.test(retryNotice) && retryOk) {
    pass("组件重试只发 retry(name)，随后重读到启动中→运行中并停止重读");
  } else fail("组件重试流程错误：" + JSON.stringify({ retried: setupWorld.retried, notice: retryNotice, components: setupView.getState().components, polling: setupCtl._debug.polling() }));
  if (setupView.getState().statusNotice === null) pass("重读到最新状态后清掉上一次的操作提示");
  else fail("重读后残留操作提示：" + JSON.stringify(setupView.getState().statusNotice));
  setupWorld.onCheck = null;
  await setupCtl.hooks.onRetryComponent("nope");
  if (/未找到/.test(setupView.getState().statusNotice || "") && /nope/.test(setupView.getState().statusNotice || "")) {
    pass("未知组件重试的 404 原样呈现");
  } else fail("组件重试错误未呈现：" + JSON.stringify(setupView.getState().statusNotice));
  setupWorld.check = setupCheckBody();

  /* 开机自启：只提交 autostart_enabled，回读结果进视图 */
  setupWorld.lastConfig = null;
  await setupCtl.hooks.onToggleAutostart(false);
  if (setupWorld.lastConfig && JSON.stringify(setupWorld.lastConfig) === JSON.stringify({ autostart_enabled: false }) && setupView.getState().autostart_enabled === false) {
    pass("开机自启开关只经 config POST 提交 autostart_enabled 并回读");
  } else fail("开机自启保存错误：" + JSON.stringify({ sent: setupWorld.lastConfig, state: setupView.getState().autostart_enabled }));

  /* destroy 清掉待执行的重读，之后不再请求（假定时器，不靠等待时长） */
  const desTimers = [];
  const desWorld = createWorld();
  desWorld.routes = setupWorld.routes.filter(function (r) { return r.path !== "/api/setup/check" && r.path !== "/api/setup/install-all"; });
  desWorld.reads = 0;
  desWorld.routes.unshift(
    { path: "/api/setup/check", method: "GET", handler: function () {
      desWorld.reads += 1;
      return jsonRes(200, setupCheckBody({ install_all: installAllBody("running", ["running"], { current: "ollama_install" }) }));
    } },
    { path: "/api/setup/install-all", method: "POST", handler: function () {
      return jsonRes(202, installAllBody("running", ["running"], { current: "ollama_install" }));
    } }
  );
  const setupDes = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: desWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    setTimeout: function (fn) { desTimers.push(fn); return desTimers.length; },
    clearTimeout: function (id) { desTimers[id - 1] = null; }
  });
  const desView = fakeView();
  setupDes.bind(desView);
  await setupDes.start();
  const readsAfterStart = desWorld.reads;
  const pendingAfterStart = desTimers.filter(Boolean).length;
  const firstTimer = desTimers.findIndex(Boolean);
  if (firstTimer >= 0) {
    const fireFirst = desTimers[firstTimer];
    desTimers[firstTimer] = null;
    fireFirst();
    await waitFor(function () { return desWorld.reads === readsAfterStart + 1 && desTimers.filter(Boolean).length === 1; });
  }
  setupDes.destroy();
  const pendingAfterDestroy = desTimers.filter(Boolean).length;
  if (pendingAfterStart === 1 && desWorld.reads === readsAfterStart + 1 && pendingAfterDestroy === 0 && setupDes._debug.polling() === false) {
    pass("装齐进行中打开页面即续读；destroy 清掉待执行的重读，不再请求");
  } else fail("destroy 未停重读：" + JSON.stringify({ pendingAfterStart: pendingAfterStart, reads: desWorld.reads, readsAfterStart: readsAfterStart, pendingAfterDestroy: pendingAfterDestroy }));

  /* LAN setup must not call local endpoints */
  const lanSetupWorld = createWorld();
  const lanSetup = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanSetupWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    catalog: catalog
  });
  const lanSetupView = fakeView();
  lanSetup.bind(lanSetupView);
  await lanSetup.start();
  if (lanSetupWorld.calls.length === 0 && lanSetupView.getState().connection === "error") pass("统一工作台在局域网不硬调 config/status");
  else fail("统一工作台在局域网发出了请求：" + lanSetupWorld.calls.length);

  /* home-controller: 方案 C 主页全链路（状态/四热键/翻译/图片OCR/错误不伪造译文/LAN 脱敏复制/destroy/非本机阻止） */
  const homeWorld = createWorld();
  homeWorld.translateFail = false;
  homeWorld.ocrFail = false;
  homeWorld.historyOffsetCalls = [];
  homeWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () {
      return jsonRes(200, setupCheckBody({
        asr: Object.assign({}, setupCheckBody().asr, { status: "ready", weights_present: true, sidecar_running: true, message: "" }),
        ollama: Object.assign({}, setupCheckBody().ollama, { installed: true, running: true, model_status: "ready", message: "" })
      }));
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, {
        polish_enabled: true,
        custom_keywords: ["Qwen"],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r",
        hotkey_annotate: "ctrl+alt+a",
        autostart_enabled: true,
        ui_theme: "light",
        audio_input_device: "",
        ollama_model: "qwen3.5:4b"
      });
    } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body || "{}");
      homeWorld.lastConfig = body;
      if (Object.prototype.hasOwnProperty.call(body, "translate_target_lang")) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "已删除旧字段 translate_target_lang" });
      }
      return jsonRes(200, Object.assign({
        polish_enabled: true,
        custom_keywords: ["Qwen"],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r",
        hotkey_annotate: "ctrl+alt+a",
        autostart_enabled: true,
        ui_theme: "light",
        audio_input_device: "",
        ollama_model: "qwen3.5:4b"
      }, body));
    } },
    { path: "/api/v1/audio-inputs", method: "GET", handler: function () {
      return jsonRes(200, {
        ok: true,
        default_device: "USB Mic",
        devices: [
          { name: "USB Mic", hostapi: "PulseAudio", is_default: true },
          { name: "Headset Mic", hostapi: "PulseAudio", is_default: false }
        ]
      });
    } },
    { path: "/api/v1/history", method: "GET", handler: function (rec) {
      const q = new URLSearchParams(String(rec.url).split("?")[1] || "");
      const offset = Number(q.get("offset") || 0);
      homeWorld.historyOffsetCalls.push(offset);
      if (offset === 0) {
        return jsonRes(200, {
          records: [{ timestamp: "t1", raw_text: "orig1", final_text: "final1", job_id: "h1", source: "desktop", polish: "on" }],
          total: 2,
          skipped_malformed: 0
        });
      }
      return jsonRes(200, {
        records: [{ timestamp: "t2", raw_text: "orig2", final_text: "final2", job_id: "h2", source: "web", polish: "off" }],
        total: 2,
        skipped_malformed: 0
      });
    } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () {
      return jsonRes(200, {
        port: 8443,
        token: "home-secret-lan-token-do-not-leak",
        token_masked: "hom****eak",
        entries: [{
          ip: "192.0.2.9",
          url: "https://192.0.2.9:8443/voice#token=home-secret-lan-token-do-not-leak",
          url_masked: "https://192.0.2.9:8443/voice#token=hom****eak",
          qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        }],
        message: ""
      });
    } },
    { path: "/api/v1/translate", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body || "{}");
      homeWorld.lastTranslate = body;
      homeWorld.translateBodies = (homeWorld.translateBodies || []).concat([body]);
      if (homeWorld.translateDelay) {
        return new Promise(function (resolve) {
          homeWorld.resolveDelayedTranslate = function (customRes) {
            resolve(customRes || jsonRes(200, {
              ok: true,
              text: "迟到译文",
              lang_pair: body.lang_pair || "zh-en",
              source_lang: body.source_lang || "en",
              target_lang: body.target_lang || "zh",
              direction_mode: body.source_lang ? "manual" : "auto",
              chunks: 1
            }));
          };
          homeWorld.rejectDelayedTranslate = function () {
            resolve(jsonRes(503, { ok: false, error: "translate_failed", message: "迟到错误" }));
          };
        });
      }
      if (homeWorld.translateFail) {
        return jsonRes(502, { ok: false, error: "translate_failed", message: "本地模型无响应", details: { ocr_text: "不应被当成译文" } });
      }
      const isManual = !!(body.source_lang && body.target_lang);
      const isZh = /[\u4e00-\u9fff]/.test(body.text || "");
      const src = isManual ? body.source_lang : (isZh ? "zh" : "en");
      const tgt = isManual ? body.target_lang : (src === "zh" ? "en" : "zh");
      return jsonRes(200, {
        ok: true,
        text: (src === "zh" && tgt === "en") ? ("EN:" + body.text) : "你好世界",
        lang_pair: body.lang_pair || "zh-en",
        source_lang: src,
        target_lang: tgt,
        direction_mode: isManual ? "manual" : "auto",
        chunks: (body.text || "").length > 100 ? 2 : 1
      });
    } },
    { path: "/api/v1/ocr-translate", method: "POST", handler: function (rec) {
      const fd = rec.body;
      if (!(fd instanceof FormData) || !fd.get("mode") || !fd.get("image")) {
        return jsonRes(422, { ok: false, error: "invalid_request", message: "缺少字段" });
      }
      const mode = fd.get("mode");
      const langPair = fd.get("lang_pair");
      homeWorld.lastOcrForm = { mode: mode, lang_pair: langPair, hasTargetLang: fd.get("target_lang") != null };
      if (homeWorld.ocrFail) {
        return jsonRes(502, { ok: false, error: "translate_failed", message: "翻译服务异常", details: { ocr_text: "DO_NOT_FAKE_TRANSLATION" } });
      }
      if (mode === "raw") {
        return jsonRes(200, {
          ok: true,
          text: "Pure OCR line",
          ocr_text: "Pure OCR line",
          mode: "raw",
          lang_pair: null,
          source_lang: null,
          target_lang: null,
          direction_mode: null,
          chunks: null
        });
      }
      return jsonRes(200, {
        ok: true,
        text: "版本说明 3.2",
        ocr_text: "Release notes 3.2",
        mode: "translate",
        lang_pair: langPair || "zh-en",
        source_lang: "en",
        target_lang: "zh",
        direction_mode: "auto",
        chunks: 1
      });
    } }
  ];

  const homeCopied = [];
  let fakeClock = 1000;
  const homeTimers = [];
  const homeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: homeWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: { writeText: function (t) { homeCopied.push(t); return Promise.resolve(); } },
    now: function () { fakeClock += 45; return fakeClock; },
    setTimeout: function (fn, ms) {
      const entry = { fn: fn, ms: ms, cleared: false };
      homeTimers.push(entry);
      return homeTimers.length;
    },
    clearTimeout: function (id) {
      if (homeTimers[id - 1]) homeTimers[id - 1].cleared = true;
    }
  });
  const homeView = fakeView(catalogCtx.V3.defaults.homeState());
  homeCtl.bind(homeView);
  await homeCtl.start();
  const hs = homeView.getState();
  if (
    hs.connection === "connected" &&
    hs.asr.status === "ready" &&
    hs.asr.sidecarRunning === true &&
    hs.ollama.modelStatus === "ready" &&
    hs.hotkey_ocr === "ctrl+alt+o" &&
    hs.hotkey_annotate === "ctrl+alt+a" &&
    hs.translate_lang_pair === "zh-en" &&
    hs.translate_auto_enabled === true &&
    hs.translateSettingsSaved === false &&
    hs.history.records.length === 1 &&
    hs.lanEntries &&
    hs.lanEntries[0].urlMasked.indexOf("hom****eak") >= 0 &&
    !hs.lanEntries[0].url &&
    JSON.stringify(hs).indexOf("home-secret-lan-token-do-not-leak") < 0
  ) {
    pass("home-controller start 并行加载状态/五热键/语言对与自动开关/历史/脱敏局域网入口，明文令牌不进 setState");
  } else fail("home-controller start 映射异常：" + JSON.stringify(hs));

  await homeCtl.hooks.onCopy({ kind: "lan_url", index: 0 });
  if (
    homeCopied[0] === "https://192.0.2.9:8443/voice#token=home-secret-lan-token-do-not-leak" &&
    homeView.getState().copyKind === "lan_url" &&
    homeView.getState().copyStatus === "已复制" &&
    JSON.stringify(homeView.getState()).indexOf("home-secret-lan-token-do-not-leak") < 0
  ) {
    pass("home-controller 复制局域网网址从私有闭包写出明文，state 始终零泄露");
  } else fail("home-controller LAN 复制异常：" + JSON.stringify({ copied: homeCopied, copyStatus: homeView.getState().copyStatus }));

  await homeCtl.hooks.onTranslateText({ text: "Hello world", lang_pair: "zh-en", trigger: "manual" });
  const hTr = homeView.getState().translate;
  if (
    hTr.phase === "result" &&
    hTr.text === "你好世界" &&
    hTr.ocr_text === "" &&
    hTr.lang_pair === "zh-en" &&
    hTr.source_lang === "en" &&
    hTr.target_lang === "zh" &&
    hTr.direction_mode === "auto" &&
    hTr.chunks === 1 &&
    homeWorld.lastTranslate &&
    homeWorld.lastTranslate.lang_pair === "zh-en" &&
    !("source_lang" in homeWorld.lastTranslate) &&
    !("target_lang" in homeWorld.lastTranslate) &&
    typeof hTr.elapsedMs === "number" &&
    hTr.elapsedMs >= 45
  ) {
    pass("home-controller 文本翻译走新 TranslateRequest(lang_pair) 并映射后端权威方向与耗时");
  } else fail("home-controller 文本翻译异常：" + JSON.stringify({ hTr: hTr, last: homeWorld.lastTranslate }));

  /* 1s 防抖自动翻译：连续编辑取消前序定时器、仅发 1 次最新请求 */
  homeTimers.length = 0;
  const callsBeforeDeb = homeWorld.calls.length;
  homeCtl.hooks.onTranslateInput({ text: "你" });
  homeCtl.hooks.onTranslateInput({ text: "你好中文" });
  if (
    homeTimers.length === 2 &&
    homeTimers[0].cleared === true &&
    homeTimers[1].cleared === false &&
    homeTimers[1].ms === 1000 &&
    homeView.getState().translate.debouncePending === true &&
    homeWorld.calls.length === callsBeforeDeb
  ) {
    pass("onTranslateInput 连续编辑取消前序定时器并排 1000ms 防抖，停顿前零请求");
  } else fail("1s 防抖排期异常：" + JSON.stringify({ timers: homeTimers.map(function (t) { return { ms: t.ms, cleared: t.cleared }; }), pending: homeView.getState().translate.debouncePending, deltaCalls: homeWorld.calls.length - callsBeforeDeb }));
  if (homeTimers[1] && !homeTimers[1].cleared) {
    homeTimers[1].fn();
    await waitFor(function () { return homeView.getState().translate.phase === "result" && homeView.getState().translate.text === "EN:你好中文"; });
  }
  const debRes = homeView.getState().translate;
  if (
    homeWorld.calls.length === callsBeforeDeb + 1 &&
    debRes.source_lang === "zh" &&
    debRes.target_lang === "en" &&
    debRes.direction_mode === "auto" &&
    debRes.debouncePending === false
  ) {
    pass("1000ms 防抖到期自动发起 1 次翻译并回填后端权威方向");
  } else fail("防抖触发结果异常：" + JSON.stringify({ callsDelta: homeWorld.calls.length - callsBeforeDeb, debRes: debRes }));

  /* 非空编辑竞态治理：在途旧响应与在途旧错误均因 translateReqId++ 被丢弃，并立即退出旧 loading */
  homeWorld.translateDelay = true;
  const staleOkP = homeCtl.hooks.onTranslateText({ text: "First inflight", lang_pair: "zh-en", trigger: "manual" });
  const staleLoadingBeforeEdit = homeView.getState().translate.phase === "loading";
  homeCtl.hooks.onTranslateInput({ text: "Second edited" });
  const exitedLoadingOnEdit = homeView.getState().translate.phase !== "loading";
  homeWorld.resolveDelayedTranslate();
  await staleOkP;
  if (staleLoadingBeforeEdit && exitedLoadingOnEdit && homeView.getState().translate.text !== "迟到译文" && homeView.getState().translate.debouncePending === true) {
    pass("非空编辑推进 translateReqId：立即退出旧 loading 并丢弃在途旧成功响应");
  } else fail("非空编辑旧响应竞态防护失败：" + JSON.stringify(homeView.getState().translate));

  const staleErrP = homeCtl.hooks.onTranslateText({ text: "Fail inflight", lang_pair: "zh-en", trigger: "manual" });
  homeCtl.hooks.onTranslateInput({ text: "Edited after fail inflight" });
  homeWorld.rejectDelayedTranslate();
  await staleErrP;
  if (homeView.getState().translate.phase !== "error" && !/迟到错误/.test(homeView.getState().translate.message || "")) {
    pass("非空编辑推进 translateReqId：丢弃在途旧失败错误，不污染当前编辑态");
  } else fail("非空编辑旧错误竞态防护失败：" + JSON.stringify(homeView.getState().translate));
  homeWorld.translateDelay = false;

  /* ⇄ 手动互换锁定：首击锁反转、失败保锁可见、再点恢复自动、空输入 0 请求清锁、首次无译文回退 LANG_PAIR_PARTS 反转 */
  await homeCtl.hooks.onTranslateText({ text: "中文自动", lang_pair: "zh-en", trigger: "manual" });
  await homeCtl.hooks.onSwapDirection({ text: "中文自动", lang_pair: "zh-en" });
  const lockedRes = homeView.getState().translate;
  if (
    homeWorld.lastTranslate &&
    homeWorld.lastTranslate.source_lang === "en" &&
    homeWorld.lastTranslate.target_lang === "zh" &&
    lockedRes.manualOverride &&
    lockedRes.manualOverride.source_lang === "en" &&
    lockedRes.manualOverride.target_lang === "zh" &&
    lockedRes.direction_mode === "manual"
  ) {
    pass("onSwapDirection 首击锁定反转方向并携带成对 source_lang/target_lang 重译");
  } else fail("onSwapDirection 首击锁定异常：" + JSON.stringify({ sent: homeWorld.lastTranslate, lockedRes: lockedRes }));

  homeWorld.translateFail = true;
  await homeCtl.hooks.onTranslateText({ text: "Fail while locked", lang_pair: "zh-en", trigger: "manual" });
  const hTrErr = homeView.getState().translate;
  if (
    hTrErr.phase === "error" &&
    hTrErr.error === "translate_failed" &&
    hTrErr.text === "" &&
    /无响应/.test(hTrErr.message || "") &&
    hTrErr.manualOverride &&
    hTrErr.manualOverride.source_lang === "en" &&
    hTrErr.manualOverride.target_lang === "zh"
  ) {
    pass("home-controller 翻译失败如实呈现错误、不伪造译文且保留手动锁态可见");
  } else fail("home-controller 翻译失败状态错误：" + JSON.stringify(hTrErr));
  homeWorld.translateFail = false;

  await homeCtl.hooks.onSwapDirection({ text: "中文自动", lang_pair: "zh-en" });
  const restoredRes = homeView.getState().translate;
  if (
    restoredRes.manualOverride === null &&
    restoredRes.direction_mode === "auto" &&
    !("source_lang" in homeWorld.lastTranslate) &&
    !("target_lang" in homeWorld.lastTranslate)
  ) {
    pass("onSwapDirection 已锁态再点清除 manualOverride 并恢复后端自动判定");
  } else fail("onSwapDirection 恢复自动异常：" + JSON.stringify({ sent: homeWorld.lastTranslate, restoredRes: restoredRes }));

  /* 清空输入 / 空文本：0 请求、清锁、清防抖 */
  await homeCtl.hooks.onSwapDirection({ text: "中文自动", lang_pair: "zh-en" });
  const callsBeforeBlank = homeWorld.calls.length;
  homeCtl.hooks.onTranslateInput({ text: "   " });
  const blankTr = homeView.getState().translate;
  if (
    homeWorld.calls.length === callsBeforeBlank &&
    blankTr.phase === "empty" &&
    blankTr.manualOverride === null &&
    blankTr.debouncePending === false &&
    !homeCtl._debug.debouncePending()
  ) {
    pass("空白输入 0 请求、取消防抖、清 manualOverride 并归 empty");
  } else fail("空白输入处理异常：" + JSON.stringify({ callsDelta: homeWorld.calls.length - callsBeforeBlank, blankTr: blankTr }));

  /* 首次未译出时点 ⇄ 取 LANG_PAIR_PARTS["zh-en"] 反转对（en → zh）锁定 */
  homeCtl.hooks.onTranslateInput({ text: "Fresh text before result" });
  await homeCtl.hooks.onSwapDirection({ text: "Fresh text before result", lang_pair: "zh-en" });
  if (
    homeWorld.lastTranslate.source_lang === "en" &&
    homeWorld.lastTranslate.target_lang === "zh" &&
    homeView.getState().translate.manualOverride &&
    homeView.getState().translate.manualOverride.source_lang === "en" &&
    homeView.getState().translate.manualOverride.target_lang === "zh"
  ) {
    pass("未译出首条时点击 ⇄ 回退 LANG_PAIR_PARTS 反转对（en→zh）锁定并重译");
  } else fail("无首条结果时 ⇄ 回退异常：" + JSON.stringify({ sent: homeWorld.lastTranslate, tr: homeView.getState().translate }));

  /* onClearTranslate 停防抖、推 reqId、清锁归 empty */
  homeCtl.hooks.onTranslateInput({ text: "Before clear" });
  const pendingBeforeClear = homeCtl._debug.debouncePending();
  homeCtl.hooks.onClearTranslate();
  if (
    pendingBeforeClear === true &&
    homeCtl._debug.debouncePending() === false &&
    homeView.getState().translate.phase === "empty" &&
    homeView.getState().translate.manualOverride === null
  ) {
    pass("onClearTranslate 停止防抖、清锁并复位 state.translate 为 empty");
  } else fail("onClearTranslate 异常：" + JSON.stringify({ pendingBeforeClear: pendingBeforeClear, pendingNow: homeCtl._debug.debouncePending(), tr: homeView.getState().translate }));

  /* 切语言对清锁停防抖；关闭自动翻译开关停防抖且后续输入不排定时 */
  await homeCtl.hooks.onSwapDirection({ text: "Lock before pair change", lang_pair: "zh-en" });
  homeCtl.hooks.onTranslateInput({ text: "Lock before pair change 2" });
  await homeCtl.hooks.onChangeLangPair("zh-en");
  if (
    homeWorld.lastConfig &&
    homeWorld.lastConfig.translate_lang_pair === "zh-en" &&
    homeCtl._debug.debouncePending() === false &&
    homeView.getState().translate.manualOverride === null &&
    homeView.getState().translateSettingsSaved === true
  ) {
    pass("onChangeLangPair 停防抖、清锁、提交 translate_lang_pair 并置 translateSettingsSaved");
  } else fail("onChangeLangPair 异常：" + JSON.stringify({ cfg: homeWorld.lastConfig, tr: homeView.getState().translate, saved: homeView.getState().translateSettingsSaved }));

  homeCtl.hooks.onTranslateInput({ text: "Pending before toggle off" });
  const pendingBeforeToggleOff = homeCtl._debug.debouncePending();
  await homeCtl.hooks.onToggleAutoTranslate(false);
  const pendingAfterToggleOff = homeCtl._debug.debouncePending();
  const uiPendingAfterToggleOff = homeView.getState().translate.debouncePending;
  const timersBeforeOffInput = homeTimers.length;
  homeCtl.hooks.onTranslateInput({ text: "Input while auto off" });
  if (
    pendingBeforeToggleOff === true &&
    pendingAfterToggleOff === false &&
    uiPendingAfterToggleOff === false &&
    homeView.getState().translate_auto_enabled === false &&
    homeView.getState().translate.debouncePending === false &&
    homeTimers.length === timersBeforeOffInput &&
    homeView.getState().translateSettingsSaved === true
  ) {
    pass("onToggleAutoTranslate(false) 立即停防抖清 debouncePending，关闭后输入不再排期");
  } else fail("onToggleAutoTranslate(false) 异常：" + JSON.stringify({ pendingBefore: pendingBeforeToggleOff, pendingAfterToggleOff: pendingAfterToggleOff, uiPendingAfterToggleOff: uiPendingAfterToggleOff, auto: homeView.getState().translate_auto_enabled }));
  await homeCtl.hooks.onToggleAutoTranslate(true);

  /* 图片槽位消歧：工作台粘图清锁停防抖；OCR 面板 raw/translate 均路由到 state.ocr */
  await homeCtl.hooks.onSwapDirection({ text: "Locked before image", lang_pair: "zh-en" });
  homeCtl.hooks.onTranslateInput({ text: "Locked before image 2" });
  const imgBlob = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "translate", lang_pair: "zh-en", panel: "translate" });
  const hImgTr = homeView.getState().translate;
  if (
    hImgTr.phase === "result" &&
    hImgTr.text === "版本说明 3.2" &&
    hImgTr.ocr_text === "Release notes 3.2" &&
    hImgTr.source_lang === "en" &&
    hImgTr.target_lang === "zh" &&
    hImgTr.direction_mode === "auto" &&
    hImgTr.manualOverride === null &&
    homeCtl._debug.debouncePending() === false &&
    homeWorld.lastOcrForm &&
    homeWorld.lastOcrForm.mode === "translate" &&
    homeWorld.lastOcrForm.lang_pair === "zh-en" &&
    homeWorld.lastOcrForm.hasTargetLang === false
  ) {
    pass("home-controller 翻译面板图片提交清锁停防抖、传正交表单 mode=translate&lang_pair 并更新 state.translate");
  } else fail("home-controller 图片翻译异常：" + JSON.stringify({ hImgTr: hImgTr, form: homeWorld.lastOcrForm }));

  await homeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "raw", panel: "ocr" });
  const hImgOcr = homeView.getState().ocr;
  if (
    hImgOcr.phase === "result" &&
    hImgOcr.mode === "raw" &&
    hImgOcr.ocr_text === "Pure OCR line" &&
    hImgOcr.text === "" &&
    hImgOcr.source_lang === null &&
    homeWorld.lastOcrForm &&
    homeWorld.lastOcrForm.mode === "raw" &&
    homeWorld.lastOcrForm.lang_pair === null
  ) {
    pass("home-controller OCR 面板 mode=raw 仅传 mode=raw 并更新 state.ocr（无译文）");
  } else fail("home-controller 图片纯 OCR 异常：" + JSON.stringify({ hImgOcr: hImgOcr, form: homeWorld.lastOcrForm }));

  await homeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "translate", lang_pair: "zh-en", panel: "ocr" });
  const hImgOcrTr = homeView.getState().ocr;
  if (
    hImgOcrTr.phase === "result" &&
    hImgOcrTr.mode === "translate" &&
    hImgOcrTr.ocr_text === "Release notes 3.2" &&
    hImgOcrTr.text === "版本说明 3.2" &&
    hImgOcrTr.source_lang === "en" &&
    hImgOcrTr.target_lang === "zh"
  ) {
    pass("home-controller OCR 面板提取并翻译同时写入 ocr_text、text 与后端权威方向");
  } else fail("home-controller OCR 翻译异常：" + JSON.stringify(hImgOcrTr));

  homeWorld.ocrFail = true;
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "translate", lang_pair: "zh-en", panel: "translate" });
  const hImgErr = homeView.getState().translate;
  if (hImgErr.phase === "error" && hImgErr.error === "translate_failed" && hImgErr.text === "" && hImgErr.ocr_text === "") {
    pass("home-controller 图片翻译失败严禁拿 ocr_text 冒充译文");
  } else fail("home-controller 图片失败路径异常：" + JSON.stringify(hImgErr));
  homeWorld.ocrFail = false;

  await homeCtl.hooks.onLoadMoreHistory();
  if (homeView.getState().history.records.length === 2 && homeWorld.historyOffsetCalls.indexOf(1) >= 0) {
    pass("home-controller onLoadMoreHistory 按偏移量分页追加历史");
  } else fail("home-controller 分页历史异常：" + JSON.stringify(homeView.getState().history));

  await homeCtl.hooks.onRefreshHistory();
  await homeCtl.hooks.onRefreshLan();
  await homeCtl.hooks.onCopy({ kind: "translation", text: "版本说明 3.2" });
  if (
    homeView.getState().history.records.length === 1 &&
    homeView.getState().lanEntries && homeView.getState().lanEntries.length === 1 &&
    homeCopied[homeCopied.length - 1] === "版本说明 3.2" &&
    homeView.getState().copyKind === "translation" &&
    homeView.getState().translate.copyStatus === "已复制"
  ) {
    pass("home-controller onRefreshHistory / onRefreshLan / 译文复制回调正常");
  } else fail("home-controller 刷新或复制回调异常");

  homeCtl.hooks.onToggleTheme({ theme: "light" });
  homeCtl.hooks.onSelectPanel({ panel: "ocr" });
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "translate", lang_pair: "zh-en" });
  if (homeView.getState().theme === "light" && homeView.getState().activePanel === "ocr" && homeView.getState().ocr.text === "版本说明 3.2") {
    pass("home-controller 支持主题/面板切换，未显式传 panel 时按 activePanel=ocr 路由到 state.ocr");
  } else fail("home-controller 主题或 OCR 面板路由异常");

  /* destroy 清待执行防抖定时器并防异步回调污染 */
  homeWorld.translateDelay = true;
  const inflight = homeCtl.hooks.onTranslateText({ text: "Late", lang_pair: "zh-en", trigger: "manual" });
  homeCtl.hooks.onTranslateInput({ text: "Pending debounce before destroy" });
  const pendingBeforeDestroy = homeCtl._debug.debouncePending();
  homeCtl.destroy();
  const pendingAfterHomeDestroy = homeCtl._debug.debouncePending();
  homeWorld.resolveDelayedTranslate();
  await inflight;
  if (homeCtl._debug.isDestroyed() && pendingBeforeDestroy === true && pendingAfterHomeDestroy === false && homeView.getState().translate.text !== "迟到译文") {
    pass("home-controller destroy 清掉待执行防抖定时器且迟到响应不污染已销毁视图");
  } else fail("home-controller destroy 防护失效：" + JSON.stringify({ pendingBeforeDestroy: pendingBeforeDestroy, pendingAfterHomeDestroy: pendingAfterHomeDestroy, tr: homeView.getState().translate }));
  homeWorld.translateDelay = false;

  /* 非本机（局域网 & file://）主页阻止发出 fetch */
  const lanHomeWorld = createWorld();
  const lanHomeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanHomeWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx
  });
  const lanHomeView = fakeView(catalogCtx.V3.defaults.homeState());
  lanHomeCtl.bind(lanHomeView);
  await lanHomeCtl.start();
  await lanHomeCtl.hooks.onTranslateText({ text: "hello", lang_pair: "zh-en", trigger: "manual" });
  await lanHomeCtl.hooks.onSubmitImage({ file: imgBlob, mode: "translate", lang_pair: "zh-en", panel: "translate" });
  if (lanHomeWorld.calls.length === 0 && lanHomeView.getState().connection === "error" && lanHomeView.getState().translate.phase === "error") {
    pass("home-controller 在局域网显式报错且零 fetch");
  } else fail("home-controller 局域网未阻止：" + JSON.stringify({ calls: lanHomeWorld.calls.length, st: lanHomeView.getState().connection }));
  lanHomeCtl.destroy();

  const fileHomeWorld = createWorld();
  const fileHomeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: fileCtx, fetch: fileHomeWorld.fetch, origin: "" }),
    context: fileCtx
  });
  const fileHomeView = fakeView(catalogCtx.V3.defaults.homeState());
  fileHomeCtl.bind(fileHomeView);
  await fileHomeCtl.start();
  if (fileHomeWorld.calls.length === 0 && fileHomeView.getState().connection === "error" && /file:\/\//.test(fileHomeView.getState().connectionError || "")) {
    pass("home-controller 在 file:// 显式报错且零 fetch");
  } else fail("home-controller file:// 未阻止：" + JSON.stringify({ calls: fileHomeWorld.calls.length, err: fileHomeView.getState().connectionError }));
  fileHomeCtl.destroy();

  const platWorld = createWorld();
  platWorld.events = [];
  platWorld.routes = [{ path: "/api/v1/events/platform", method: "POST", handler: function (rec) {
    platWorld.events.push(JSON.parse(rec.body));
    return jsonRes(202, { ok: true });
  } }];
  const plat = B.createPlatformReporter({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: platWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx
  });
  await plat.postBusy();
  await plat.postError({ error: "asr_unavailable", message: "开麦失败" });
  await plat.postIdle();
  let rejected = false;
  try { await plat.post({ event: "recording" }); } catch (e) { rejected = true; }
  const lanPlat = B.createPlatformReporter({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: platWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx
  });
  const beforePlat = platWorld.calls.length;
  let lanBlocked = false;
  try { await lanPlat.postBusy(); } catch (e) { lanBlocked = !!(e && e.blocked); }
  if (platWorld.events.length === 3 && platWorld.events[0].event === "busy" && platWorld.events[1].event === "error" && platWorld.events[1].error === "asr_unavailable" && platWorld.events[2].event === "idle" && rejected && lanBlocked && platWorld.calls.length === beforePlat) {
    pass("本机平台事件只上报 idle/busy/error，拒绝冒充识别事件，局域网不可调用");
  } else fail("平台事件约束不正确：" + JSON.stringify({ events: platWorld.events, rejected: rejected, lanBlocked: lanBlocked, extra: platWorld.calls.length - beforePlat }));

  /* voice: idempotent submit, confirm, 403, unknown, resource release */
  const voiceWorld = createWorld();
  let jobState = "queued";
  voiceWorld.routes = [
    { path: "/api/v1/utterances/from-audio", method: "POST", handler: function (rec) {
      const fd = rec.body;
      voiceWorld.jobIds = voiceWorld.jobIds || [];
      voiceWorld.jobIds.push(fd.get("job_id"));
      if (!fd.get("job_id")) return jsonRes(400, { ok: false, error: "invalid_request", message: "job_id 必传，服务器不会代为生成" });
      if (!fd.get("audio")) return jsonRes(400, { ok: false, error: "invalid_request", message: "必须提供音频文件" });
      return jsonRes(200, { ok: true, state: "queued", text: "识别文本", job_id: fd.get("job_id") });
    } },
    { path: "/api/v1/jobs/", method: "GET", handler: function (rec) {
      if (voiceWorld.jobUnknown) return jsonRes(200, { found: false, state: "unknown" });
      var parts = rec.url.split("?")[0].split("/");
      var jid = parts[parts.length - 1] || (voiceWorld.jobIds && voiceWorld.jobIds[0]) || "unknown";
      return jsonRes(200, { found: true, job: { job_id: jid, state: jobState, text: "识别文本", created_at: "t", updated_at: "t" } });
    } },
    { path: "/api/v1/utterances/resend", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      voiceWorld.resends = voiceWorld.resends || [];
      voiceWorld.resends.push(body);
      if (!body.confirm) {
        return jsonRes(200, { ok: false, requires_confirmation: true, original_job_id: body.original_job_id, new_job_id: body.job_id });
      }
      return jsonRes(200, { ok: true, state: "queued", job_id: body.job_id, text: body.text });
    } },
    { path: "/api/v1/history", method: "GET", handler: function (rec) {
      if (voiceWorld.forbidHistory) return jsonRes(403, { ok: false, error: "forbidden", message: "禁止访问" });
      return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    } }
  ];
  const stream = new FakeStream();
  FakeMediaRecorder.instances = [];
  const docFake = {
    hidden: false,
    listeners: {},
    addEventListener: function (t, fn) { this.listeners[t] = fn; },
    removeEventListener: function (t) { delete this.listeners[t]; }
  };
  const voiceCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: FakeMediaRecorder,
    mediaDevices: { getUserMedia: function () { return Promise.resolve(stream); } },
    document: docFake,
    setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
    clearTimeout: function (id) { clearTimeout(id); }
  });
  const voiceView = fakeView();
  voiceCtl.bind(voiceView);
  await voiceCtl.start();
  if (voiceView.getState().connection === "connected" && voiceView.getState().tokenValid === true) pass("手机页在带令牌的局域网上下文可连接");
  else fail("手机页连接状态错误：" + JSON.stringify(voiceView.getState()));
  if (voiceView.getState().micEnabled === true && voiceView.getState().useFileUpload === false) {
    pass("voice-controller start() 支持 MediaRecorder 时 micEnabled 为 true，麦克风立即可按");
  } else fail("voice-controller start() 麦克风置灰回归：" + JSON.stringify({ micEnabled: voiceView.getState().micEnabled, useFileUpload: voiceView.getState().useFileUpload }));

  const noMrView = fakeView();
  const noMrCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: { isTypeSupported: function () { return false; } }
  });
  noMrCtl.bind(noMrView);
  await noMrCtl.start();
  if (noMrView.getState().micEnabled === false && noMrView.getState().useFileUpload === true) {
    pass("voice-controller 不支持 MediaRecorder 时降级文件上传且 micEnabled=false");
  } else fail("不支持 MediaRecorder 降级状态错误：" + JSON.stringify({ micEnabled: noMrView.getState().micEnabled, useFileUpload: noMrView.getState().useFileUpload }));
  noMrCtl.destroy();

  await voiceCtl.hooks.onRecordStart();
  if (!FakeMediaRecorder.instances.length || stream.getTracks().some(function (t) { return t.stopped; })) {
    /* start shouldn't stop tracks yet */
  }
  if (voiceView.getState().phase === "recording") pass("仅在用户按下后开始录音");
  else fail("录音未进入 recording：" + voiceView.getState().phase);
  jobState = "done";
  await voiceCtl.hooks.onRecordEnd();
  await wait(20);
  const firstId = voiceWorld.jobIds && voiceWorld.jobIds[0];
  if (firstId && voiceView.getState().job_id === firstId && voiceView.getState().text === "识别文本") pass("松开后上传原音频并提交必传 job_id");
  else fail("from-audio 提交异常：" + JSON.stringify(voiceView.getState()) + " ids=" + JSON.stringify(voiceWorld.jobIds));
  if (voiceView.getState().phase === "done" && voiceView.getState().micEnabled === true) {
    pass("录音任务进入 done 终态后恢复 micEnabled=true，可直接按住说下一句");
  } else fail("done 终态未恢复 micEnabled：" + JSON.stringify({ phase: voiceView.getState().phase, micEnabled: voiceView.getState().micEnabled }));
  jobState = "queued";

  /* polish_failed 终态也必须恢复 micEnabled=true */
  const pfWorld = createWorld();
  pfWorld.routes = [
    { path: "/api/v1/utterances/from-audio", method: "POST", handler: function () {
      return jsonRes(502, { ok: false, error: "polish_failed", message: "模型润色超时", details: { raw_text: "原始口述" } });
    } }
  ];
  const pfView = fakeView();
  const pfCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: pfWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: FakeMediaRecorder
  });
  pfCtl.bind(pfView);
  await pfCtl.submitAudio({ blob: new Blob([new Uint8Array([1, 2])], { type: "audio/webm" }), filename: "a.webm" }, "from-audio");
  if (pfView.getState().phase === "polish_failed" && pfView.getState().micEnabled === true && pfView.getState().raw_text === "原始口述") {
    pass("polish_failed 终态保留原文并恢复 micEnabled=true");
  } else fail("polish_failed 未恢复 micEnabled：" + JSON.stringify(pfView.getState()));
  pfCtl.destroy();

  /* repeat same logical request keeps job_id: simulate network fail then reuse */
  const jobs = B.createJobIds();
  const idA = jobs.reuse("from-audio");
  const idA2 = jobs.reuse("from-audio");
  const idB = jobs.fresh("from-audio");
  if (idA && idA === idA2 && idB !== idA) pass("重复提交复用同一 client job_id，新一次手势换新 id");
  else fail("job_id 幂等不正确");

  /* confirm path */
  await voiceCtl.hooks.onSendOriginal({ text: "识别文本", job_id: firstId });
  const confirmBody = voiceWorld.resends && voiceWorld.resends[0];
  if (confirmBody && confirmBody.confirm === false && confirmBody.original_job_id === firstId && confirmBody.job_id !== firstId) {
    pass("发送原文调用 resend，且未自动带 confirm");
  } else fail("发送原文请求不正确：" + JSON.stringify(confirmBody));
  if (voiceView.getState().phase === "delivery_unknown") pass("ok=false requires_confirmation 呈现可能已输入，不自动入队");
  else fail("确认响应未呈现：" + voiceView.getState().phase);
  const pendingNew = confirmBody.job_id;
  await voiceCtl.hooks.onConfirmResendChoice({ confirm: true, job_id: firstId, text: "识别文本" });
  const confirmed = voiceWorld.resends[1];
  if (confirmed && confirmed.confirm === true && confirmed.original_job_id === firstId && confirmed.job_id !== firstId && confirmed.job_id !== pendingNew) {
    pass("手动确认使用新的 job_id 明确关联 original_job_id");
  } else fail("手动确认请求不正确：" + JSON.stringify(confirmed));

  /* unknown job */
  voiceWorld.jobUnknown = true;
  const unknownCtlView = fakeView();
  const unknownCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  unknownCtl.bind(unknownCtlView);
  unknownCtl.pollJob("missing-job");
  await wait(20);
  if (unknownCtlView.getState().job_state === "unknown" || unknownCtlView.getState().phase === "failed") pass("任务不存在时呈现 unknown/失败，不谎称已输入");
  else fail("unknown job 呈现错误：" + JSON.stringify(unknownCtlView.getState()));
  unknownCtl.destroy();

  /* 403 history */
  voiceWorld.forbidHistory = true;
  const v403 = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  const v403view = fakeView();
  v403.bind(v403view);
  await v403.start();
  if (v403view.getState().tokenValid === false || /禁止|forbidden/.test((v403view.getState().history && v403view.getState().history.message) || "")) {
    pass("403 时可见未授权，不假装已登录");
  } else fail("403 呈现错误：" + JSON.stringify(v403view.getState()));
  v403.destroy();

  /* cancel releases tracks */
  const stream2 = new FakeStream();
  FakeMediaRecorder.instances = [];
  const cancelCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: FakeMediaRecorder,
    mediaDevices: { getUserMedia: function () { return Promise.resolve(stream2); } },
    document: docFake
  });
  const cancelView = fakeView();
  cancelCtl.bind(cancelView);
  await cancelCtl.hooks.onRecordStart();
  await wait(5);
  await cancelCtl.hooks.onRecordCancel();
  if (stream2.getTracks().every(function (t) { return t.stopped > 0; }) && cancelView.getState().phase === "idle" && cancelView.getState().micEnabled === true) {
    pass("取消录音后停止 tracks、回到空闲并恢复 micEnabled=true");
  } else fail("取消未释放音轨或未恢复 micEnabled：" + JSON.stringify({ tracks: stream2.getTracks().map(function (t) { return t.stopped; }), micEnabled: cancelView.getState().micEnabled }));
  docFake.hidden = true;
  await cancelCtl.hooks.onRecordStart();
  await wait(5);
  if (docFake.listeners.visibilitychange) docFake.listeners.visibilitychange();
  if (stream2.getTracks().every(function (t) { return t.stopped > 0; }) && cancelView.getState().micEnabled === true) {
    pass("页面不可见时停止录音、释放音轨并保持可再次录音");
  } else fail("visibility 未释放音轨或 micEnabled 错误");
  cancelCtl.destroy();

  /* pick audio rejects video */
  const pickView = fakeView();
  const pickCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  pickCtl.bind(pickView);
  const beforePick = (voiceWorld.jobIds || []).length;
  await pickCtl.hooks.onPickAudio({ file: { type: "video/webm", name: "clip.webm" } });
  if (pickView.getState().phase === "failed" && (voiceWorld.jobIds || []).length === beforePick) pass("选文件拒绝不支持的视频容器");
  else fail("选文件限制失效");
  pickCtl.destroy();


  /* ---- B1: URL token stripped, referrer locked, no leak ---- */
  const fixtureTok = "fixture-not-for-log";
  function leak(value) {
    if (value == null) return false;
    if (typeof value === "string") return value.indexOf(fixtureTok) >= 0;
    try { return JSON.stringify(value).indexOf(fixtureTok) >= 0; } catch (err) { return false; }
  }

  const htmlDir = path.join(repo, "static/v3");
  ["index.html", "voice.html"].forEach(function (name) {
    const html = fs.readFileSync(path.join(htmlDir, name), "utf8");
    const head = html.split("</head>")[0] || "";
    const refIdx = head.search(/<meta[^>]*name=["']referrer["'][^>]*content=["']no-referrer["']/i);
    const altRef = head.search(/<meta[^>]*content=["']no-referrer["'][^>]*name=["']referrer["']/i);
    const pos = refIdx >= 0 ? refIdx : altRef;
    const cssIdx = head.indexOf('rel="stylesheet"');
    const ctxIdx = head.indexOf("js/business/context.js");
    const adoptIdx = head.indexOf("adoptTokenFromLocation");
    if (pos >= 0 && pos < (ctxIdx >= 0 ? ctxIdx : cssIdx) && ctxIdx >= 0 && ctxIdx < adoptIdx && adoptIdx < cssIdx) {
      pass(name + " 在任何外部样式前声明 no-referrer，并先加载 context.js 再剥离令牌");
    } else fail(name + " 的 Referrer-Policy / 令牌剥离脚本顺序不正确");
  });

  /* #token= fragment 优先读取并立即从地址栏剥离（保留其余 hash 与 query，不落地 Web Storage） */
  const hashLoc = {
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    pathname: "/voice",
    search: "?foo=1",
    hash: "#a=1&token=" + fixtureTok + "&b=2",
    href: "https://192.168.1.8/voice?foo=1#a=1&token=" + fixtureTok + "&b=2"
  };
  const hashHistCalls = [];
  const hashHistory = {
    state: { page: "voice-hash" },
    replaceState: function (state, title, url) {
      hashHistCalls.push({ state: state, title: title, url: String(url) });
      const qIndex = String(url).indexOf("?");
      const hIndex = String(url).indexOf("#");
      hashLoc.search = qIndex >= 0 ? String(url).slice(qIndex, hIndex >= 0 ? hIndex : undefined) : "";
      hashLoc.hash = hIndex >= 0 ? String(url).slice(hIndex) : "";
      hashLoc.href = hashLoc.origin + url;
    }
  };
  const adoptHashBiz = loadBusiness();
  const adoptedHash = adoptHashBiz.window.V3Business.adoptTokenFromLocation({ location: hashLoc, history: hashHistory });
  const ctxAfterHash = adoptHashBiz.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: hashLoc.href,
    search: hashLoc.search,
    hash: hashLoc.hash
  });
  if (
    hashHistCalls.length === 1 &&
    !leak(hashHistCalls[0].url) &&
    hashHistCalls[0].url === "/voice?foo=1#a=1&b=2" &&
    adoptedHash.stripped &&
    adoptedHash.token === fixtureTok &&
    ctxAfterHash.tokenPresent &&
    ctxAfterHash.getToken() === fixtureTok &&
    !ctxAfterHash.tokenAdoptError
  ) {
    pass("context.js 支持 #token= 片段提取并立即 replaceState 剥离，仅存内存不留地址栏");
  } else fail("#token= 片段提取或剥离失败：" + JSON.stringify({ calls: hashHistCalls, adopted: adoptedHash, url: hashLoc.href }));

  /* 纯 #token=xxx 剥离后不残留空 #，且 #token= 优先级高于 ?token= 并双清 */
  const bothLoc = {
    pathname: "/voice",
    search: "?token=legacy-query-token",
    hash: "#token=" + fixtureTok,
    href: "https://192.168.1.8/voice?token=legacy-query-token#token=" + fixtureTok
  };
  const bothHistCalls = [];
  const adoptBothBiz = loadBusiness();
  const adoptedBoth = adoptBothBiz.window.V3Business.adoptTokenFromLocation({
    location: bothLoc,
    history: { state: null, replaceState: function (s, t, u) { bothHistCalls.push(String(u)); } }
  });
  if (adoptedBoth.token === fixtureTok && bothHistCalls.length === 1 && bothHistCalls[0] === "/voice") {
    pass("#token= 优先于 ?token=，且 replaceState 同步剥离两处令牌");
  } else fail("#token= 优先级或双清异常：" + JSON.stringify({ token: adoptedBoth.token, urls: bothHistCalls }));

  const directHashBiz = loadBusiness();
  const directHashCtx = directHashBiz.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: "https://192.168.1.8/voice#token=" + fixtureTok,
    search: "?token=legacy-fallback",
    hash: "#token=" + fixtureTok
  });
  if (directHashCtx.tokenPresent && directHashCtx.getToken() === fixtureTok) {
    pass("未 adopt 时 createContext 同样优先读取 hash 中 #token=");
  } else fail("createContext 直接读取 hash 令牌失败");

  const loc = {
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    pathname: "/static/v3/voice.html",
    search: "?foo=1&token=" + fixtureTok + "&bar=2",
    hash: "#mic",
    href: "https://192.168.1.8/static/v3/voice.html?foo=1&token=" + fixtureTok + "&bar=2#mic"
  };
  const histCalls = [];
  const urlHistory = {
    state: { page: "voice" },
    replaceState: function (state, title, url) {
      histCalls.push({ state: state, title: title, url: String(url) });
      const qIndex = String(url).indexOf("?");
      const hIndex = String(url).indexOf("#");
      loc.search = qIndex >= 0 ? String(url).slice(qIndex, hIndex >= 0 ? hIndex : undefined) : "";
      loc.hash = hIndex >= 0 ? String(url).slice(hIndex) : "";
      loc.href = loc.origin + url;
    }
  };
  const adoptFresh = loadBusiness();
  const adopted = adoptFresh.window.V3Business.adoptTokenFromLocation({ location: loc, history: urlHistory });
  const ctxAfter = adoptFresh.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: loc.href,
    search: loc.search
  });
  if (histCalls.length !== 1) fail("应只 replaceState 一次，实际 " + histCalls.length);
  else if (leak(histCalls[0].url) || /(?:\?|&)token=/.test(histCalls[0].url)) fail("replaceState 目标仍含令牌参数");
  else if (histCalls[0].url.indexOf("/static/v3/voice.html") < 0 || histCalls[0].url.indexOf("foo=1") < 0 || histCalls[0].url.indexOf("bar=2") < 0 || histCalls[0].url.indexOf("#mic") < 0) {
    fail("replaceState 未保留其他查询参数或 hash");
  } else if (histCalls[0].state && histCalls[0].state.page !== "voice") fail("replaceState 不应丢掉原有 history.state");
  else pass("读入令牌后立即 replaceState 去掉 token，保留其他参数和 hash");
  if (adopted.stripped && adopted.token && ctxAfter.tokenPresent && ctxAfter.getToken() && ctxAfter.getToken().length > 0 && !ctxAfter.tokenAdoptError) {
    pass("令牌留在内存，createContext 不再依赖地址栏查询串");
  } else fail("令牌未进入内存或剥离后上下文丢失");
  if (leak(ctxAfter.tokenAdoptError) || leak(ctxAfter.href) || leak(ctxAfter.kind)) fail("上下文字段泄露了夹具令牌");
  else pass("上下文公开字段不包含令牌值");

  const failLoc = { pathname: "/static/v3/voice.html", search: "?token=" + fixtureTok, hash: "", href: "https://192.168.1.8/static/v3/voice.html?token=" + fixtureTok };
  const adoptFail = loadBusiness();
  const failed = adoptFail.window.V3Business.adoptTokenFromLocation({
    location: failLoc,
    history: { replaceState: function () { throw new Error("replace blocked"); } }
  });
  const failCtx = adoptFail.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: failLoc.href,
    search: failLoc.search
  });
  if (failed.failed && failCtx.tokenAdoptError && !failCtx.tokenPresent && !failCtx.getToken()) {
    if (leak(failed.failed) || leak(failCtx.tokenAdoptError)) fail("剥离失败信息泄露了令牌");
    else pass("无法 replaceState 时令牌作废，并给出不含令牌的明确失败");
  } else fail("剥离失败时未安全失败");

  const schemaWorld = createWorld();
  schemaWorld.routes.push({
    path: "/openapi.snapshot.json",
    handler: function () { return jsonRes(200, snapshot); }
  });
  const bootBiz = loadBusiness();
  bootBiz.window.V3Business.adoptTokenFromLocation({ location: loc, history: urlHistory });
  await bootBiz.window.V3Business.loadOpenApi({ fetch: schemaWorld.fetch }, { kind: "lan", allowFetch: true, origin: "https://192.168.1.8" }, schemaWorld.fetch);
  if (schemaWorld.calls[0] && String(schemaWorld.calls[0].url).indexOf("/openapi.snapshot.json") >= 0) pass("局域网加载默认 /openapi.snapshot.json，不请求本机 /openapi.json");
  else fail("局域网快照地址不正确：" + (schemaWorld.calls[0] && schemaWorld.calls[0].url));
  if (schemaWorld.calls[0] && schemaWorld.calls[0].referrerPolicy === "no-referrer") pass("加载 OpenAPI/快照的 fetch 也使用 no-referrer");
  else fail("schema fetch 未设置 no-referrer");

  /* ---- B2: getUserMedia pending vs pointerup/cancel/visibility/destroy ---- */
  function voiceWithGum(gum, extraDoc, extraWin) {
    const ctl = B.createVoiceController({
      client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
      context: lanCtx,
      jobIds: B.createJobIds(),
      MediaRecorder: FakeMediaRecorder,
      mediaDevices: gum.mediaDevices,
      document: extraDoc || { hidden: false, addEventListener: function () {}, removeEventListener: function () {} },
      window: extraWin,
      setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
      clearTimeout: function (id) { clearTimeout(id); }
    });
    const view = fakeView();
    ctl.bind(view);
    return { ctl: ctl, view: view };
  }

  FakeMediaRecorder.instances = [];
  const gumEnd = createGumQueue();
  const endPair = voiceWithGum(gumEnd);
  const fromBefore = (voiceWorld.jobIds || []).length;
  endPair.ctl.hooks.onRecordStart();
  await wait(5);
  if (FakeMediaRecorder.instances.length !== 0) fail("权限未完成时不应创建 MediaRecorder");
  await endPair.ctl.hooks.onRecordEnd();
  await gumEnd.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length !== 0) fail("pointerup 发生在 getUserMedia 待决时，完成后仍启动了 MediaRecorder");
  else if (!tracksStopped(gumEnd.calls[0].stream)) fail("pointerup 待决完成后音轨未停止");
  else if (endPair.view.getState().phase === "recording" || (voiceWorld.jobIds || []).length !== fromBefore) {
    fail("待决结束后不应继续录音或上传");
  } else pass("pointerup 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  endPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumCancel = createGumQueue();
  const cancelPair = voiceWithGum(gumCancel);
  cancelPair.ctl.hooks.onRecordStart();
  await wait(5);
  cancelPair.ctl.hooks.onRecordCancel();
  await gumCancel.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumCancel.calls[0].stream) && cancelPair.view.getState().phase === "idle") {
    pass("pointercancel 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("cancel 待决完成后仍有录音或活音轨");
  cancelPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumVis = createGumQueue();
  const visDoc = { hidden: false, listeners: {}, addEventListener: function (n, fn) { this.listeners[n] = fn; }, removeEventListener: function (n) { delete this.listeners[n]; } };
  const visPair = voiceWithGum(gumVis, visDoc);
  await visPair.ctl.start();
  visPair.ctl.hooks.onRecordStart();
  await wait(5);
  visDoc.hidden = true;
  if (visDoc.listeners.visibilitychange) visDoc.listeners.visibilitychange();
  await gumVis.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumVis.calls[0].stream) && visPair.view.getState().phase === "idle") {
    pass("visibilitychange 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("visibility 待决完成后仍有录音或活音轨");
  visPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumHide = createGumQueue();
  const hideWin = { listeners: {}, addEventListener: function (n, fn) { this.listeners[n] = fn; }, removeEventListener: function (n) { delete this.listeners[n]; } };
  const hidePair = voiceWithGum(gumHide, { hidden: false, addEventListener: function () {}, removeEventListener: function () {} }, hideWin);
  await hidePair.ctl.start();
  hidePair.ctl.hooks.onRecordStart();
  await wait(5);
  if (hideWin.listeners.pagehide) hideWin.listeners.pagehide();
  await gumHide.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumHide.calls[0].stream)) {
    pass("pagehide 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("pagehide 待决完成后仍有录音或活音轨");
  hidePair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumDes = createGumQueue();
  const desPair = voiceWithGum(gumDes);
  desPair.ctl.hooks.onRecordStart();
  await wait(5);
  desPair.ctl.destroy();
  await gumDes.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumDes.calls[0].stream)) {
    pass("destroy 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("destroy 待决完成后仍有录音或活音轨");

  FakeMediaRecorder.instances = [];
  const gumOld = createGumQueue();
  const oldPair = voiceWithGum(gumOld);
  oldPair.ctl.hooks.onRecordStart();
  await wait(5);
  oldPair.ctl.hooks.onRecordStart();
  await wait(5);
  if (gumOld.calls.length !== 2) fail("两次 start 应各发起一次 getUserMedia，实际 " + gumOld.calls.length);
  await gumOld.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length !== 0) fail("旧 getUserMedia 在新录音之后 resolve 时不应启动录音器");
  else if (!tracksStopped(gumOld.calls[0].stream)) fail("被取代的旧音轨未停止");
  await gumOld.calls[1].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 1 && tracksLive(gumOld.calls[1].stream) && tracksStopped(gumOld.calls[0].stream) && oldPair.view.getState().phase === "recording") {
    pass("旧 getUserMedia 晚于新录音 resolve 时只保留新会话，旧轨道已停止");
  } else fail("新旧录音代次处理不正确");
  oldPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumOk = createGumQueue();
  const okPair = voiceWithGum(gumOk);
  const startP = okPair.ctl.hooks.onRecordStart();
  await wait(5);
  await gumOk.calls[0].resolve();
  await startP;
  if (FakeMediaRecorder.instances.length === 1 && tracksLive(gumOk.calls[0].stream) && okPair.view.getState().phase === "recording") {
    pass("正常权限授予后仍能开始录音");
  } else fail("正常录制被回归破坏");
  await okPair.ctl.hooks.onRecordEnd();
  await wait(20);
  if (tracksStopped(gumOk.calls[0].stream)) pass("正常结束录音后释放音轨");
  else fail("正常结束未释放音轨");
  okPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumRej = createGumQueue();
  const rejPair = voiceWithGum(gumRej);
  const rejP = rejPair.ctl.hooks.onRecordStart();
  await wait(5);
  const denied = new Error("denied");
  denied.name = "NotAllowedError";
  gumRej.calls[0].reject(denied);
  await rejP;
  await wait(20);
  if (FakeMediaRecorder.instances.length === 0 && rejPair.view.getState().phase === "idle" && /权限被拒绝/.test(rejPair.view.getState().statusText || rejPair.view.getState().fileUploadHint || "")) {
    pass("权限拒绝时不启动录音，并给出可见错误");
  } else fail("权限拒绝路径不正确：" + JSON.stringify({ phase: rejPair.view.getState().phase, text: rejPair.view.getState().statusText }));
  rejPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumBoom = createGumQueue();
  const boomPair = voiceWithGum(gumBoom);
  const boomP = boomPair.ctl.hooks.onRecordStart();
  await wait(5);
  gumBoom.calls[0].reject(new Error("device exploded"));
  await boomP;
  await wait(20);
  if (FakeMediaRecorder.instances.length === 0 && boomPair.view.getState().phase === "idle") {
    pass("getUserMedia 异常时不启动录音并回收会话");
  } else fail("getUserMedia 异常路径不正确");
  boomPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumStaleRej = createGumQueue();
  const stalePair = voiceWithGum(gumStaleRej);
  stalePair.ctl.hooks.onRecordStart();
  await wait(5);
  stalePair.ctl.hooks.onRecordCancel();
  const staleErr = new Error("denied later");
  staleErr.name = "NotAllowedError";
  gumStaleRej.calls[0].reject(staleErr);
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && stalePair.view.getState().phase === "idle") {
    pass("已取消会话的迟到权限拒绝不重新拉起录音，也不覆盖取消状态");
  } else fail("迟到拒绝干扰了已取消的会话");
  stalePair.ctl.destroy();


  /* LAN voice must not call config/status */
  const sneak = (voiceWorld.calls || []).filter(function (c) {
    return /\/api\/v1\/(config|status|events)(\b|\/)/.test(c.url) || /\/api\/setup\//.test(c.url) || /\/api\/v1\/jobs\/claim\b/.test(c.url);
  });
  if (sneak.length === 0) pass("手机页未从局域网请求仅本机的 config/status/setup/events/platform/claim");
  else fail("手机页误请求仅本机接口：" + sneak.map(function (c) { return c.url; }).join(", "));

  voiceCtl.destroy();

  const mappedCaps = B.mapCapabilities([
    { id: "evdev_keyboards", label: "键盘设备读取", available: true, required: true, detail: "已读到 2 个键盘设备。" },
    { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
  ]);
  if (
    mappedCaps
    && mappedCaps[0]
    && mappedCaps[0].id === "evdev_keyboards"
    && mappedCaps[0].detail === "已读到 2 个键盘设备。"
    && mappedCaps[0].message === mappedCaps[0].detail
    && mappedCaps[1].id === "clipboard"
    && mappedCaps[1].detail.indexOf("无法写入剪贴板") >= 0
  ) {
    pass("mapCapabilities 保留 id 与 detail");
  } else fail("mapCapabilities 未保留 id/detail：" + JSON.stringify(mappedCaps));
  if (B.mapCapabilities(null) === null && Array.isArray(B.mapCapabilities([])) && B.mapCapabilities([]).length === 0) {
    pass("能力列表缺省或空数组时不编造检测项");
  } else fail("空能力列表映射错误");

  const startReqWorld = createWorld();
  startReqWorld.check = setupCheckBody({
    asr: Object.assign({}, setupCheckBody().asr, {
      status: "ready",
      weights_present: true,
      sidecar_running: false,
      message: "权重已就绪，识别服务未启动"
    })
  });
  startReqWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, startReqWorld.check); } },
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, { polish_enabled: false, custom_keywords: [], translate_lang_pair: "zh-en", translate_auto_enabled: true, hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }); } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      if (body.hotkey_voice && body.hotkey_voice === body.hotkey_screenshot) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "语音输入、截图翻译、重复上次输入 不能使用相同组合" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }, body));
    } },
    { path: "/api/v1/history", method: "GET", handler: function () { return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }); } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () { return jsonRes(200, { port: 8443, token: "secret-lan-token-do-not-log", token_masked: "sec****log", entries: [], message: "" }); } },
    { path: "/api/v1/audio-inputs", method: "GET", handler: function () { return jsonRes(200, { ok: true, default_device: null, devices: [] }); } }
  ];
  const startReqCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: startReqWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const startReqView = fakeView();
  startReqView.setState({
    hotkeyDrafts: { hotkey_voice: "ctrl+alt+q" },
    hotkeyCapturing: "hotkey_voice",
    hotkeyFocus: "hotkey_voice"
  });
  startReqCtl.bind(startReqView);
  await startReqCtl.start();
  if (startReqView.getState().hotkeyDrafts && startReqView.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q" && startReqView.getState().hotkeyCapturing === "hotkey_voice") {
    pass("loadAll 不重置快捷键草稿与录入态");
  } else fail("loadAll 丢掉了草稿或录入态：" + JSON.stringify({ drafts: startReqView.getState().hotkeyDrafts, cap: startReqView.getState().hotkeyCapturing }));
  if (startReqView.getState().asr && startReqView.getState().asr.status === "ready" && startReqView.getState().asr.sidecarRunning === false) {
    pass("mapAsr 保留 sidecarRunning=false，不把缺运行当成已就绪");
  } else fail("sidecarRunning 映射错误：" + JSON.stringify(startReqView.getState().asr));
  try {
    await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+z", hotkey_repeat: "ctrl+alt+r" });
    fail("快捷键互相重复时不应保存成功");
  } catch (err) {
    const st = startReqView.getState();
    if (
      st.hotkeyDrafts && st.hotkeyDrafts.hotkey_voice === "ctrl+alt+q"
      && /不能使用相同组合/.test((st.hotkeySaveError || "") + " " + (st.statusNotice || ""))
      && st.hotkeysSaved !== true
    ) {
      pass("快捷键互相重复时展示后端错误且不丢草稿");
    } else fail("保存冲突后草稿或错误不正确：" + JSON.stringify({ drafts: st.hotkeyDrafts, err: st.hotkeySaveError, notice: st.statusNotice, saved: st.hotkeysSaved, thrown: err && err.message }));
  }

  /* P1-5 回归：配置保存失败写入 statusNotice 后，再次保存成功立即恢复为无报错 */
  await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r" });
  if (startReqView.getState().statusNotice === null && startReqView.getState().hotkeySaveError === "") {
    pass("saveConfig 失败后再保存成功时立即清空顶部 statusNotice");
  } else fail("saveConfig 成功后未清空 statusNotice：" + JSON.stringify({ notice: startReqView.getState().statusNotice, err: startReqView.getState().hotkeySaveError }));

  startReqCtl.destroy();

  /* bootstrap.js pagehide 必须绑定在 window（HTML 规范中 pagehide 仅在 Window 触发且不冒泡到 Document） */
  const docEvents = {};
  const depWinEvents = {};
  const globalWinEvents = {};
  const bootDoc = {
    getElementById: function (id) { return id === "app" ? { id: "app" } : null; },
    addEventListener: function (ev, fn) { (docEvents[ev] = docEvents[ev] || []).push(fn); }
  };
  const depWin = {
    addEventListener: function (ev, fn) { (depWinEvents[ev] = depWinEvents[ev] || []).push(fn); }
  };
  const bootCtx = loadBusiness({ document: bootDoc });
  bootCtx.window.addEventListener = function (ev, fn) {
    (globalWinEvents[ev] = globalWinEvents[ev] || []).push(fn);
  };
  const mountedInits = [];
  bootCtx.window.V3.home = {
    mount: function (_el, init, hooks) {
      mountedInits.push(Object.assign({}, init || {}));
      return fakeView(init);
    }
  };
  const bootFetch = async function (url) {
    if (String(url).indexOf("/api/setup/check") >= 0) return jsonRes(200, { ok: true, asr: { status: "ready", sidecar_running: true }, ollama: { installed: true, running: true, model_status: "ready" }, capabilities: [] });
    if (String(url).indexOf("/api/v1/config") >= 0) return jsonRes(200, { polish_enabled: true, custom_keywords: [], translate_lang_pair: "zh-en", translate_auto_enabled: true, hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r" });
    if (String(url).indexOf("/api/v1/history") >= 0) return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    if (String(url).indexOf("/api/v1/lan-access") >= 0) return jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" });
    if (String(url).indexOf("/api/v1/audio-inputs") >= 0) return jsonRes(200, { ok: true, default_device: null, devices: [] });
    return jsonRes(404, { ok: false, error: "not_found", message: "missing" });
  };
  const bootLoc = { protocol: "https:", hostname: "127.0.0.1", origin: "https://127.0.0.1:17945", pathname: "/", href: "https://127.0.0.1:17945/", search: "", hash: "" };
  await bootCtx.window.V3Business.startPage("home", {
    openapi: snapshot,
    fetch: bootFetch,
    location: bootLoc,
    document: bootDoc,
    window: depWin
  });
  const bootCtl1 = bootCtx.window.__v3Business && bootCtx.window.__v3Business.controller;
  if (
    depWinEvents.pagehide && depWinEvents.pagehide.length === 1
    && !docEvents.pagehide
    && bootCtl1 && !bootCtl1._debug.isDestroyed()
  ) {
    depWinEvents.pagehide[0]();
    if (bootCtl1._debug.isDestroyed()) {
      pass("bootstrap.startPage 将 pagehide 监听绑定到 deps.window 而非 document，触发后销毁控制器");
    } else fail("deps.window pagehide 触发后未调用 controller.destroy()");
  } else {
    fail("bootstrap.startPage pagehide 绑定目标错误：" + JSON.stringify({ depWin: Object.keys(depWinEvents), doc: Object.keys(docEvents) }));
  }

  await bootCtx.window.V3Business.startPage("home", {
    openapi: snapshot,
    fetch: bootFetch,
    location: bootLoc,
    document: bootDoc
  });
  const bootCtl2 = bootCtx.window.__v3Business && bootCtx.window.__v3Business.controller;
  if (
    globalWinEvents.pagehide && globalWinEvents.pagehide.length === 1
    && !docEvents.pagehide
    && bootCtl2 && !bootCtl2._debug.isDestroyed()
  ) {
    globalWinEvents.pagehide[0]();
    if (bootCtl2._debug.isDestroyed()) {
      pass("bootstrap.startPage 缺省回退绑定到全局 window 的 pagehide，触发后销毁控制器");
    } else fail("全局 window pagehide 触发后未调用 controller.destroy()");
  } else {
    fail("bootstrap.startPage 缺省 pagehide 绑定目标错误：" + JSON.stringify({ globalWin: Object.keys(globalWinEvents), doc: Object.keys(docEvents) }));
  }

  /* bootstrap.startPage("home") 对 /setup 自动进入 setup 面板，并支持安全 URL 同步 */
  const setupPathLoc = {
    protocol: "https:",
    hostname: "127.0.0.1",
    origin: "https://127.0.0.1:17945",
    pathname: "/setup",
    search: "",
    hash: "",
    href: "https://127.0.0.1:17945/setup"
  };
  const urlSyncCalls = [];
  const setupPathHist = {
    state: { v: 1 },
    replaceState: function (state, title, url) {
      urlSyncCalls.push(String(url));
      const parts = String(url).split("#");
      const qParts = parts[0].split("?");
      setupPathLoc.pathname = qParts[0];
      setupPathLoc.search = qParts[1] ? "?" + qParts[1] : "";
      setupPathLoc.hash = parts[1] ? "#" + parts[1] : "";
      setupPathLoc.href = setupPathLoc.origin + String(url);
    }
  };
  await bootCtx.window.V3Business.startPage("home", {
    openapi: snapshot,
    fetch: bootFetch,
    location: setupPathLoc,
    history: setupPathHist,
    document: bootDoc,
    window: { addEventListener: function () {} }
  });
  const setupBootInit = mountedInits[mountedInits.length - 1];
  const setupBootCtl = bootCtx.window.__v3Business && bootCtx.window.__v3Business.controller;
  if (setupBootInit && setupBootInit.activePanel === "setup") {
    pass("bootstrap.startPage(home) 访问 /setup 自动设置 activePanel=setup");
  } else fail("bootstrap.startPage(home) 未对 /setup 设置 activePanel=setup：" + JSON.stringify(setupBootInit));

  setupBootCtl.hooks.onSelectPanel({ panel: "translate" });
  setupBootCtl.hooks.onSelectPanel({ panel: "setup" });
  setupBootCtl.hooks.onSelectPanel({ panel: "ocr" });
  if (urlSyncCalls.join(",") === "/,/setup,/") {
    pass("本机面板切换通过 replaceState 平滑同步 / 与 /setup 地址栏");
  } else fail("面板切换 URL 同步不符合预期：" + JSON.stringify(urlSyncCalls));
  setupBootCtl.destroy();

  /* /#setup 切到 setup 面板时必须剥离冗余 #setup，避免出现 /setup#setup */
  const hashSetupLoc = {
    protocol: "https:",
    hostname: "127.0.0.1",
    origin: "https://127.0.0.1:17945",
    pathname: "/",
    search: "",
    hash: "#setup",
    href: "https://127.0.0.1:17945/#setup"
  };
  const hashSetupUrls = [];
  const hashSetupHist = {
    state: null,
    replaceState: function (_s, _t, u) {
      hashSetupUrls.push(String(u));
      const parts = String(u).split("#");
      hashSetupLoc.pathname = parts[0];
      hashSetupLoc.hash = parts[1] ? "#" + parts[1] : "";
    }
  };
  const hashSetupCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: bootFetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    location: hashSetupLoc,
    history: hashSetupHist
  });
  hashSetupCtl.bind(fakeView(catalogCtx.V3.defaults.homeState()));
  hashSetupCtl.hooks.onSelectPanel({ panel: "setup" });
  hashSetupCtl.hooks.onSelectPanel({ panel: "translate" });
  if (hashSetupUrls.join(",") === "/setup,/") {
    pass("/#setup 切换面板自动清理冗余 #setup，不产生 /setup#setup");
  } else fail("/#setup URL 同步异常：" + JSON.stringify(hashSetupUrls));
  hashSetupCtl.destroy();

  /* loadAll 竞态防护：慢的旧请求晚于新请求返回时不得覆盖新状态 */
  let slowCheckResolve = null;
  let raceCallCount = 0;
  const raceFetch = function (url) {
    const u = String(url);
    if (u.indexOf("/api/setup/check") >= 0) {
      raceCallCount += 1;
      if (raceCallCount === 1) {
        return new Promise(function (resolve) {
          slowCheckResolve = function () {
            resolve(jsonRes(200, setupCheckBody({ asr: Object.assign({}, setupCheckBody().asr, { message: "旧慢响应" }) })));
          };
        });
      }
      return Promise.resolve(jsonRes(200, setupCheckBody({ asr: Object.assign({}, setupCheckBody().asr, { message: "最新响应" }) })));
    }
    if (u.indexOf("/api/v1/config") >= 0) {
      return Promise.resolve(jsonRes(200, {
        polish_enabled: true,
        custom_keywords: [],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: raceCallCount === 1 ? "ctrl+alt+1" : "ctrl+alt+2",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r"
      }));
    }
    if (u.indexOf("/api/v1/history") >= 0) return Promise.resolve(jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }));
    if (u.indexOf("/api/v1/lan-access") >= 0) return Promise.resolve(jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" }));
    if (u.indexOf("/api/v1/audio-inputs") >= 0) return Promise.resolve(jsonRes(200, { ok: true, default_device: null, devices: [] }));
    return Promise.resolve(jsonRes(404, { ok: false, error: "not_found", message: "missing" }));
  };
  const raceCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: raceFetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog
  });
  const raceView = fakeView(catalogCtx.V3.defaults.homeState());
  raceCtl.bind(raceView);
  const slowLoad = raceCtl.loadAll();
  const fastLoad = raceCtl.loadAll();
  await fastLoad;
  slowCheckResolve();
  await slowLoad;
  if (raceView.getState().asr.message === "最新响应" && raceView.getState().hotkey_voice === "ctrl+alt+2") {
    pass("loadAll 单调请求序号拦截旧慢响应，避免覆盖最新状态");
  } else fail("loadAll 竞态防护失效：" + JSON.stringify({ asr: raceView.getState().asr, voice: raceView.getState().hotkey_voice }));
  raceCtl.destroy();

  /* loadAll 容错隔离：lan-access 503 不阻断主连接、设置检查、历史与一键装齐，并清空陈旧局域网凭证 */
  const lanFailWorld = createWorld();
  lanFailWorld.failLan = false;
  lanFailWorld.failCheck = false;
  lanFailWorld.installPosts = 0;
  lanFailWorld.check = setupCheckBody();
  lanFailWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () {
      if (lanFailWorld.failCheck) return jsonRes(500, { ok: false, error: "internal", message: "设置检查内部异常" });
      return jsonRes(200, lanFailWorld.check);
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, {
        polish_enabled: true,
        custom_keywords: ["Kw"],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r",
        hotkey_annotate: "ctrl+alt+a",
        autostart_enabled: true
      });
    } },
    { path: "/api/v1/history", method: "GET", handler: function () {
      return jsonRes(200, {
        records: [{ id: "h-lan-fail", source: "desktop", raw_text: "orig", final_text: "final", polish: "on", created_at: "2026-11-22T00:00:00Z" }],
        total: 1,
        skipped_malformed: 0
      });
    } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () {
      if (lanFailWorld.failLan) {
        return jsonRes(503, { ok: false, error: "dependency_failed", message: "无法枚举网卡地址" });
      }
      return jsonRes(200, {
        port: 17945,
        token: "stale-secret-token",
        token_masked: "sta****ken",
        entries: [{
          ip: "192.0.2.9",
          url: "https://192.0.2.9:17945/voice?token=stale-secret-token",
          url_masked: "https://192.0.2.9:17945/voice?token=sta****ken",
          qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        }],
        message: ""
      });
    } },
    { path: "/api/setup/install-all", method: "POST", handler: function () {
      lanFailWorld.installPosts += 1;
      lanFailWorld.check = setupCheckBody({ install_all: installAllBody("running", ["running"], { current: "ollama_install" }) });
      return jsonRes(202, lanFailWorld.check.install_all);
    } },
    { path: "/api/v1/audio-inputs", method: "GET", handler: function () {
      return jsonRes(200, { ok: true, default_device: "USB Mic", devices: [{ name: "USB Mic", hostapi: "PulseAudio", is_default: true }] });
    } }
  ];
  const lanFailTimers = [];
  const lanFailCopied = [];
  const lanFailCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: lanFailWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: { writeText: function (text) { lanFailCopied.push(text); return Promise.resolve(); } },
    setTimeout: function (fn) { lanFailTimers.push(fn); return lanFailTimers.length; },
    clearTimeout: function (id) { lanFailTimers[id - 1] = null; }
  });
  const lanFailView = fakeView(catalogCtx.V3.defaults.homeState());
  lanFailCtl.bind(lanFailView);
  await lanFailCtl.loadAll();
  const seededLanOk = lanFailCtl._debug.getLanToken() === "stale-secret-token"
    && lanFailCtl._debug.getLanUrls().length === 1
    && lanFailView.getState().tokenPresent === true;

  lanFailWorld.failLan = true;
  await lanFailCtl.loadAll();
  const afterLanFail = lanFailView.getState();
  await lanFailCtl.hooks.onCopy({ kind: "token" });
  await lanFailCtl.hooks.onCopy({ kind: "lan_url", index: 0 });
  await lanFailCtl.hooks.onInstallAll();
  const afterLanFailInstall = lanFailView.getState();
  if (
    seededLanOk
    && afterLanFail.connection === "connected"
    && afterLanFail.connectionError === null
    && afterLanFail.asr && afterLanFail.asr.status === "missing"
    && afterLanFail.polish_enabled === true
    && Array.isArray(afterLanFail.custom_keywords) && afterLanFail.custom_keywords.join(",") === "Kw"
    && afterLanFail.history && afterLanFail.history.records.length === 1 && afterLanFail.history.records[0].id === "h-lan-fail"
    && lanFailCtl._debug.getLanToken() === null
    && lanFailCtl._debug.getLanUrls().length === 0
    && lanFailCopied.length === 0
    && Array.isArray(afterLanFail.lanEntries) && afterLanFail.lanEntries.length === 0
    && afterLanFail.tokenMasked === ""
    && afterLanFail.tokenPresent === false
    && /依赖失败/.test(afterLanFail.lanMessage || "")
    && /无法枚举网卡地址/.test(afterLanFail.lanMessage || "")
    && lanFailWorld.installPosts === 1
    && afterLanFailInstall.connection === "connected"
    && afterLanFailInstall.installAll && afterLanFailInstall.installAll.state === "running"
    && afterLanFailInstall.installAll.current === "ollama_install"
    && lanFailCtl._debug.polling() === true
  ) {
    pass("loadAll 隔离 lan-access 503：保持 connected 并装载设置/配置/历史、清空陈旧凭证、写 lanMessage 且一键装齐正常启动");
  } else {
    fail("loadAll lan-access 503 容错隔离失败：" + JSON.stringify({
      seededLanOk: seededLanOk,
      connection: afterLanFail.connection,
      lanEntries: afterLanFail.lanEntries,
      lanMessage: afterLanFail.lanMessage,
      tokenMasked: afterLanFail.tokenMasked,
      tokenPresent: afterLanFail.tokenPresent,
      privToken: lanFailCtl._debug.getLanToken(),
      privUrls: lanFailCtl._debug.getLanUrls(),
      copied: lanFailCopied,
      installPosts: lanFailWorld.installPosts,
      installAll: afterLanFailInstall.installAll,
      polling: lanFailCtl._debug.polling()
    }));
  }
  lanFailWorld.failCheck = true;
  await lanFailCtl.loadAll();
  const afterCoreFail = lanFailView.getState();
  if (afterCoreFail.connection === "error" && /设置检查内部异常/.test(afterCoreFail.connectionError || "")) {
    pass("loadAll 核心接口（setup/check）失败仍显式进入 connection=error");
  } else {
    fail("loadAll 核心接口失败未进入 error：" + JSON.stringify({ connection: afterCoreFail.connection, err: afterCoreFail.connectionError }));
  }
  lanFailCtl.destroy();

  /* 非 / 或 /setup 路径（例如测试路径）不改地址栏 */
  const otherLoc = { protocol: "https:", hostname: "127.0.0.1", origin: "https://127.0.0.1:17945", pathname: "/custom/path", search: "", hash: "" };
  const otherHistCalls = [];
  const otherCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: bootFetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    location: otherLoc,
    history: { state: null, replaceState: function (_s, _t, u) { otherHistCalls.push(String(u)); } }
  });
  otherCtl.bind(fakeView(catalogCtx.V3.defaults.homeState()));
  otherCtl.hooks.onSelectPanel({ panel: "setup" });
  otherCtl.hooks.onSelectPanel({ panel: "translate" });
  if (otherHistCalls.length === 0) {
    pass("非 / 与 /setup 路径切换面板不误调用 replaceState");
  } else fail("非目标路径误改地址栏：" + JSON.stringify(otherHistCalls));
  otherCtl.destroy();

  /* ---- ST4: map-setup 扩展 / 主题持久化与并发防闪回 / 麦克风 503 隔离与刷新保存 ---- */
  const pickedNew = B.pickLegalConfig({ ui_theme: "dark", audio_input_device: "USB Mic", unknown_x: 1 });
  if (
    pickedNew.ui_theme === "dark"
    && pickedNew.audio_input_device === "USB Mic"
    && !Object.prototype.hasOwnProperty.call(pickedNew, "unknown_x")
  ) {
    pass("pickLegalConfig 放行 ui_theme 与 audio_input_device 并过滤未知键");
  } else fail("pickLegalConfig 映射异常：" + JSON.stringify(pickedNew));

  const defApplyState = { theme: "dark", audio_input_device: "KeepMic" };
  B.applyConfig(defApplyState, { ui_theme: "sepia", audio_input_device: null });
  const okApplyState = { theme: "light", audio_input_device: null };
  B.applyConfig(okApplyState, { ui_theme: "dark", audio_input_device: "" });
  if (
    defApplyState.theme === "dark"
    && defApplyState.audio_input_device === "KeepMic"
    && okApplyState.theme === "dark"
    && okApplyState.audio_input_device === ""
  ) {
    pass("applyConfig 对 ui_theme 仅接受 light/dark、对 audio_input_device 仅非 null 时更新（支持空串）");
  } else fail("applyConfig 防御性映射异常：" + JSON.stringify({ defApplyState: defApplyState, okApplyState: okApplyState }));

  const mappedAudio = B.mapAudioInputs({
    ok: true,
    default_device: "USB Mic",
    devices: [
      { name: "USB Mic", hostapi: "Windows DirectSound", is_default: true },
      { name: "Line In", hostapi: "Windows DirectSound", is_default: false }
    ]
  });
  const mappedAudioEmpty = B.mapAudioInputs({ ok: true, default_device: null, devices: [] });
  if (
    mappedAudio
    && mappedAudio.defaultDevice === "USB Mic"
    && mappedAudio.devices.length === 2
    && mappedAudio.devices[0].name === "USB Mic"
    && mappedAudio.devices[0].hostapi === "Windows DirectSound"
    && mappedAudio.devices[0].isDefault === true
    && mappedAudio.devices[1].isDefault === false
    && mappedAudioEmpty && mappedAudioEmpty.defaultDevice === null && Array.isArray(mappedAudioEmpty.devices) && mappedAudioEmpty.devices.length === 0
    && B.mapAudioInputs(null) === null
  ) {
    pass("mapAudioInputs 正确映射设备列表、空列表与 null");
  } else fail("mapAudioInputs 映射异常：" + JSON.stringify({ mappedAudio: mappedAudio, mappedAudioEmpty: mappedAudioEmpty }));

  /* formatBytes B / KB / MB / GB 边界与 mapProgress 未知总量（GB）映射 */
  const GIB = 1024 * 1024 * 1024;
  const MIB = 1024 * 1024;
  const twoPoint86GiB = Math.round(2.86 * GIB);
  const fmtChecks = {
    zero: B.formatBytes(0),
    bMax: B.formatBytes(1023),
    kbMin: B.formatBytes(1024),
    mbMin: B.formatBytes(MIB),
    mbBeforeGib: B.formatBytes(GIB - MIB),
    gbMin: B.formatBytes(GIB),
    gb286: B.formatBytes(twoPoint86GiB),
    neg: B.formatBytes(-1),
    nan: B.formatBytes(NaN),
    inf: B.formatBytes(Infinity),
    nullVal: B.formatBytes(null)
  };
  if (
    fmtChecks.zero === "0 B"
    && fmtChecks.bMax === "1023 B"
    && fmtChecks.kbMin === "1 KB"
    && fmtChecks.mbMin === "1 MB"
    && fmtChecks.mbBeforeGib === "1023 MB"
    && fmtChecks.gbMin === "1.00 GB"
    && fmtChecks.gb286 === "2.86 GB"
    && fmtChecks.neg === null
    && fmtChecks.nan === null
    && fmtChecks.inf === null
    && fmtChecks.nullVal === null
  ) {
    pass("formatBytes 正确覆盖 B / KB / MB / GB 边界与非法输入（1 GiB=1.00 GB，2.86 GiB=2.86 GB）");
  } else fail("formatBytes 边界格式化异常：" + JSON.stringify(fmtChecks));

  const progUnknownTotal = B.mapProgress({
    completed_bytes: twoPoint86GiB,
    total_bytes: null,
    percent: null,
    stage: "正在下载 torch-2.9.1+cu128"
  });
  const progKnownTotal = B.mapProgress({
    completed_bytes: GIB,
    total_bytes: Math.round(3.5 * GIB),
    percent: 28.6,
    stage: "download"
  });
  if (
    progUnknownTotal
    && progUnknownTotal.receivedLabel === "2.86 GB"
    && progUnknownTotal.totalLabel === null
    && progUnknownTotal.percent === null
    && progUnknownTotal.stage === "正在下载 torch-2.9.1+cu128"
    && progKnownTotal
    && progKnownTotal.receivedLabel === "1.00 GB"
    && progKnownTotal.totalLabel === "3.50 GB"
    && progKnownTotal.percent === 28.6
    && B.mapProgress(null) === null
  ) {
    pass("mapProgress 在 total_bytes=null/percent=null 时真实映射为 GB 字节标签且不编造百分比");
  } else fail("mapProgress GB 映射异常：" + JSON.stringify({ progUnknownTotal: progUnknownTotal, progKnownTotal: progKnownTotal }));

  /* 主题持久化、连点乱序、错误回滚、并发其他配置防旧 theme 闪回、503 隔离与麦克风刷新/保存 */
  const audioThemeWorld = createWorld();
  audioThemeWorld.serverConfig = {
    polish_enabled: true,
    custom_keywords: ["Qwen"],
    translate_lang_pair: "zh-en",
    translate_auto_enabled: true,
    hotkey_voice: "ctrl+alt+z",
    hotkey_screenshot: "ctrl+alt+x",
    hotkey_ocr: "ctrl+alt+o",
    hotkey_repeat: "ctrl+alt+r",
    hotkey_annotate: "ctrl+alt+a",
    autostart_enabled: true,
    ui_theme: "dark",
    audio_input_device: "USB Mic",
    ollama_model: "qwen3.5:4b"
  };
  audioThemeWorld.failAudioInputs = true;
  audioThemeWorld.pendingPosts = [];
  audioThemeWorld.delayThemePosts = false;
  audioThemeWorld.failNextPost = null;
  audioThemeWorld.delayAudioRefresh = false;
  audioThemeWorld.pendingAudioGets = [];
  audioThemeWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, setupCheckBody()); } },
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, Object.assign({}, audioThemeWorld.serverConfig)); } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body || "{}");
      audioThemeWorld.lastPostBody = body;
      if (audioThemeWorld.delayThemePosts && Object.prototype.hasOwnProperty.call(body, "ui_theme")) {
        return new Promise(function (resolve) {
          audioThemeWorld.pendingPosts.push({
            body: body,
            resolveOk: function (overrideCfg) {
              Object.assign(audioThemeWorld.serverConfig, body);
              resolve(jsonRes(200, Object.assign({}, audioThemeWorld.serverConfig, overrideCfg || {})));
            },
            resolveErr: function (msg) {
              resolve(jsonRes(500, { ok: false, error: "config_io", message: msg || "写入配置失败" }));
            }
          });
        });
      }
      if (audioThemeWorld.failNextPost) {
        const msg = audioThemeWorld.failNextPost;
        audioThemeWorld.failNextPost = null;
        return jsonRes(500, { ok: false, error: "config_io", message: msg });
      }
      const nextCfg = Object.assign({}, audioThemeWorld.serverConfig, body);
      if (!audioThemeWorld.keepServerThemeOnNonThemePost) {
        Object.assign(audioThemeWorld.serverConfig, body);
      }
      return jsonRes(200, nextCfg);
    } },
    { path: "/api/v1/history", method: "GET", handler: function () { return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }); } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () { return jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" }); } },
    { path: "/api/v1/audio-inputs", method: "GET", handler: function () {
      if (audioThemeWorld.delayAudioRefresh) {
        return new Promise(function (resolve) {
          audioThemeWorld.pendingAudioGets.push({
            resolveOk: function (payload) {
              resolve(jsonRes(200, payload || {
                ok: true,
                default_device: "USB Mic",
                devices: [
                  { name: "USB Mic", hostapi: "PulseAudio", is_default: true },
                  { name: "Headset Mic", hostapi: "PulseAudio", is_default: false }
                ]
              }));
            },
            resolveErr: function (msg) {
              resolve(jsonRes(503, { ok: false, error: "dependency_failed", message: msg || "PortAudio 枚举失败" }));
            }
          });
        });
      }
      if (audioThemeWorld.failAudioInputs) {
        return jsonRes(503, { ok: false, error: "dependency_failed", message: "PortAudio 初始化失败" });
      }
      return jsonRes(200, {
        ok: true,
        default_device: "USB Mic",
        devices: [
          { name: "USB Mic", hostapi: "PulseAudio", is_default: true },
          { name: "Headset Mic", hostapi: "PulseAudio", is_default: false }
        ]
      });
    } }
  ];

  const atCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: audioThemeWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog
  });
  const atView = fakeView(catalogCtx.V3.defaults.homeState());
  atCtl.bind(atView);
  await atCtl.start();
  const afterAudio503 = atView.getState();
  if (
    afterAudio503.connection === "connected"
    && afterAudio503.theme === "dark"
    && afterAudio503.audio_input_device === "USB Mic"
    && afterAudio503.audioInputs === null
    && afterAudio503.audioInputsLoading === false
    && /依赖失败/.test(afterAudio503.audioInputError || "")
    && /PortAudio 初始化失败/.test(afterAudio503.audioInputError || "")
  ) {
    pass("loadAll 隔离 audio-inputs 503：保持 connected 与 ui_theme/audio_input_device、置 audioInputs=null 并呈现错误");
  } else fail("loadAll audio-inputs 503 隔离异常：" + JSON.stringify({
    conn: afterAudio503.connection,
    theme: afterAudio503.theme,
    dev: afterAudio503.audio_input_device,
    inputs: afterAudio503.audioInputs,
    loading: afterAudio503.audioInputsLoading,
    err: afterAudio503.audioInputError
  }));

  /* onRefreshAudioInputs 加载态 + 成功清错 + 503 重置 audioInputs=null */
  audioThemeWorld.failAudioInputs = false;
  audioThemeWorld.delayAudioRefresh = true;
  const refreshP = atCtl.hooks.onRefreshAudioInputs();
  const loadingFlagDuringRefresh = atView.getState().audioInputsLoading === true;
  audioThemeWorld.pendingAudioGets.shift().resolveOk();
  await refreshP;
  const afterRefreshOk = atView.getState();
  if (
    loadingFlagDuringRefresh
    && afterRefreshOk.audioInputsLoading === false
    && afterRefreshOk.audioInputError === ""
    && afterRefreshOk.audioInputs && afterRefreshOk.audioInputs.defaultDevice === "USB Mic"
    && afterRefreshOk.audioInputs.devices.length === 2
  ) {
    pass("onRefreshAudioInputs 置加载态、拉取并映射最新麦克风列表并清空错误");
  } else fail("onRefreshAudioInputs 成功态异常：" + JSON.stringify({ loadingFlagDuringRefresh: loadingFlagDuringRefresh, state: { loading: afterRefreshOk.audioInputsLoading, err: afterRefreshOk.audioInputError, inputs: afterRefreshOk.audioInputs } }));

  const refreshFailP = atCtl.hooks.onRefreshAudioInputs();
  audioThemeWorld.pendingAudioGets.shift().resolveErr("设备句柄忙");
  await refreshFailP;
  const afterRefreshFail = atView.getState();
  if (
    afterRefreshFail.audioInputs === null
    && afterRefreshFail.audioInputsLoading === false
    && /设备句柄忙/.test(afterRefreshFail.audioInputError || "")
  ) {
    pass("onRefreshAudioInputs 503 失败时置 audioInputs=null 并呈现错误");
  } else fail("onRefreshAudioInputs 失败态异常：" + JSON.stringify({ inputs: afterRefreshFail.audioInputs, err: afterRefreshFail.audioInputError }));
  audioThemeWorld.delayAudioRefresh = false;

  /* onChangeAudioInput 保存指定设备、切回空串（跟随系统默认）及失败提示 */
  await atCtl.hooks.onChangeAudioInput("Headset Mic");
  const afterMicSave = atView.getState();
  await atCtl.hooks.onChangeAudioInput("");
  const afterMicDefault = atView.getState();
  audioThemeWorld.failNextPost = "磁盘只读";
  let micSaveThrew = false;
  try {
    await atCtl.hooks.onChangeAudioInput("USB Mic");
  } catch (err) {
    micSaveThrew = true;
  }
  const afterMicFail = atView.getState();
  if (
    afterMicSave.audio_input_device === "Headset Mic" && afterMicSave.audioInputSaved === true && afterMicSave.audioInputError === ""
    && afterMicDefault.audio_input_device === "" && afterMicDefault.audioInputSaved === true
    && micSaveThrew && afterMicFail.audioInputSaved === false && /磁盘只读/.test(afterMicFail.audioInputError || "")
  ) {
    pass("onChangeAudioInput 持久化指定麦克风与空串默认、置 audioInputSaved，失败显式写 audioInputError");
  } else fail("onChangeAudioInput 行为异常：" + JSON.stringify({ afterMicSave: afterMicSave.audio_input_device, afterMicDefault: afterMicDefault.audio_input_device, micSaveThrew: micSaveThrew, err: afterMicFail.audioInputError }));

  /* onToggleTheme：默认归一化 light、落盘、连点乱序（旧成功与旧失败均拦截）、当前失败回滚、并发非主题写不闪回 */
  await atCtl.hooks.onToggleTheme({ theme: "invalid_value" });
  if (atView.getState().theme === "light" && audioThemeWorld.serverConfig.ui_theme === "light") {
    pass("onToggleTheme 对非法主题默认归一化 light 并经 /api/v1/config 持久化");
  } else fail("onToggleTheme 归一化/持久化异常：" + JSON.stringify({ theme: atView.getState().theme, server: audioThemeWorld.serverConfig.ui_theme }));

  /* 模拟 home.js 先行乐观翻转 view.theme=dark 后，本次持久化失败且仍 isCurrent → 回滚为 light */
  audioThemeWorld.failNextPost = "配置保存失败";
  atView.setState({ theme: "dark" });
  await atCtl.hooks.onToggleTheme({ theme: "dark" });
  if (atView.getState().theme === "light" && /配置保存失败/.test(atView.getState().statusNotice || "")) {
    pass("onToggleTheme 持久化失败且仍为最新请求时回滚主题至已落盘值并提示错误");
  } else fail("onToggleTheme 失败回滚异常：" + JSON.stringify({ theme: atView.getState().theme, notice: atView.getState().statusNotice }));

  /* 主题连点乱序：Click1(dark) 晚于 Click2(light) 返回成功 → 不得闪回 dark */
  audioThemeWorld.delayThemePosts = true;
  const rapid1 = atCtl.hooks.onToggleTheme({ theme: "dark" });
  const rapid2 = atCtl.hooks.onToggleTheme({ theme: "light" });
  const p1 = audioThemeWorld.pendingPosts.shift();
  const p2 = audioThemeWorld.pendingPosts.shift();
  p2.resolveOk({ ui_theme: "light" });
  await rapid2;
  p1.resolveOk({ ui_theme: "dark" });
  await rapid1;
  if (atView.getState().theme === "light") {
    pass("onToggleTheme 连点乱序时 themeReqId/isCurrent 丢弃旧慢成功响应，防止主题闪回");
  } else fail("onToggleTheme 连点乱序成功防护失效：" + JSON.stringify({ theme: atView.getState().theme }));

  /* 主题连点乱序：Click1(dark) 在 Click2(light) 之后失败 → 不得回滚也不得写旧错误 */
  atView.setState({ statusNotice: null });
  const rapidErr1 = atCtl.hooks.onToggleTheme({ theme: "dark" });
  const rapidErr2 = atCtl.hooks.onToggleTheme({ theme: "light" });
  const pe1 = audioThemeWorld.pendingPosts.shift();
  const pe2 = audioThemeWorld.pendingPosts.shift();
  pe1.resolveErr("过期的第一次点击错误");
  await rapidErr1;
  const afterStaleErrTheme = atView.getState().theme;
  const afterStaleErrNotice = atView.getState().statusNotice;
  pe2.resolveOk({ ui_theme: "light" });
  await rapidErr2;
  if (afterStaleErrTheme === "light" && afterStaleErrNotice === null && atView.getState().theme === "light") {
    pass("onToggleTheme 失败分支受 isCurrent 守卫，过期的主题写失败不回滚且不污染 statusNotice");
  } else fail("onToggleTheme 过期失败守卫失效：" + JSON.stringify({ theme: afterStaleErrTheme, notice: afterStaleErrNotice }));

  /* 在途主题写计数 > 0 时，并发其他 saveConfig 返回旧 ui_theme=light 不得把乐观 dark 闪回 */
  audioThemeWorld.serverConfig.ui_theme = "light";
  audioThemeWorld.keepServerThemeOnNonThemePost = true;
  const inflightDark = atCtl.hooks.onToggleTheme({ theme: "dark" });
  const darkEntry = audioThemeWorld.pendingPosts.shift();
  await atCtl.hooks.onTogglePolish(false);
  const themeAfterConcurrentPolish = atView.getState().theme;
  const polishAfterConcurrent = atView.getState().polish_enabled;
  darkEntry.resolveOk({ ui_theme: "dark" });
  await inflightDark;
  audioThemeWorld.delayThemePosts = false;
  audioThemeWorld.keepServerThemeOnNonThemePost = false;
  if (themeAfterConcurrentPolish === "dark" && polishAfterConcurrent === false && atView.getState().theme === "dark") {
    pass("在途主题写计数 > 0 时，并发其他 saveConfig 响应不回写旧 theme 防闪回");
  } else fail("在途主题写防闪回失效：" + JSON.stringify({ themeAfterConcurrentPolish: themeAfterConcurrentPolish, polishAfterConcurrent: polishAfterConcurrent, finalTheme: atView.getState().theme }));

  /* destroy 递增 audioInputsReqId 与 themeReqId：在途刷新与主题写返回不污染已销毁视图 */
  audioThemeWorld.delayAudioRefresh = true;
  const lateRefresh = atCtl.hooks.onRefreshAudioInputs();
  const lateAudioEntry = audioThemeWorld.pendingAudioGets.shift();
  atCtl.destroy();
  lateAudioEntry.resolveOk({ ok: true, default_device: "Ghost Mic", devices: [{ name: "Ghost Mic", hostapi: "X", is_default: true }] });
  await lateRefresh;
  if (!(atView.getState().audioInputs && atView.getState().audioInputs.defaultDevice === "Ghost Mic")) {
    pass("destroy() 递增 audioInputsReqId，丢弃在途迟到的麦克风刷新响应");
  } else fail("destroy() 后迟到 audioInputs 响应污染了视图");

  /* LAN 与未连接模式下 onToggleTheme 仅改内存、零 fetch */
  const lanThemeWorld = createWorld();
  const lanThemeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanThemeWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    catalog: catalog
  });
  const lanThemeView = fakeView(catalogCtx.V3.defaults.homeState());
  lanThemeCtl.bind(lanThemeView);
  await lanThemeCtl.hooks.onToggleTheme({ theme: "dark" });
  const discThemeWorld = createWorld();
  const discThemeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: discThemeWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog
  });
  const discThemeView = fakeView(catalogCtx.V3.defaults.homeState());
  discThemeCtl.bind(discThemeView);
  await discThemeCtl.hooks.onToggleTheme({ theme: "dark" });
  if (
    lanThemeView.getState().theme === "dark" && lanThemeWorld.calls.length === 0
    && discThemeView.getState().theme === "dark" && discThemeWorld.calls.length === 0
  ) {
    pass("LAN 与未连接模式下 onToggleTheme 仅更新内存主题，零 fetch 请求");
  } else fail("非本机/未连接 onToggleTheme 误发请求：" + JSON.stringify({ lanCalls: lanThemeWorld.calls.length, discCalls: discThemeWorld.calls.length }));
  lanThemeCtl.destroy();
  discThemeCtl.destroy();

  /* ---------- ST6: map-setup log_max_lines / clipboard.writeAsyncText / logs-actions 懒加载、竞态与非乐观保存 ---------- */
  const cfgNoCap = B.applyConfig({ log_max_lines: null }, { polish_enabled: true });
  const cfgWithCap = B.applyConfig({ log_max_lines: null }, { polish_enabled: true, log_max_lines: 5000 });
  if (
    B.LEGAL_CONFIG_KEYS.indexOf("log_max_lines") >= 0
    && B.pickLegalConfig({ log_max_lines: 500, bogus: 1 }).log_max_lines === 500
    && !("bogus" in B.pickLegalConfig({ log_max_lines: 500, bogus: 1 }))
    && cfgNoCap.log_max_lines === null
    && cfgWithCap.log_max_lines === 5000
  ) {
    pass("map-setup 合法化 log_max_lines 且缺省时不伪造 2000");
  } else fail("map-setup log_max_lines 映射异常：" + JSON.stringify({ cfgNoCap: cfgNoCap, cfgWithCap: cfgWithCap }));

  /* clipboard.writeAsyncText：1) ClipboardItem + write 路径保留同步手势与单次 fetch；2) 失败保留原始 ApiError 零未处理拒绝；3) 无 ClipboardItem 时回退 writeText 且零重复请求 */
  function FakeClipboardItem(items) { this.items = items; }
  const clipWrittenItems = [];
  let getTextCalls1 = 0;
  let wroteSync = false;
  const clipItemAdapter = B.createClipboard({
    ClipboardItem: FakeClipboardItem,
    Blob: Blob,
    clipboard: {
      write: function (items) {
        wroteSync = true;
        clipWrittenItems.push(items);
        return Promise.resolve(items[0].items["text/plain"]).then(function (blob) { return blob.text(); });
      },
      writeText: function () { throw new Error("should not call writeText when ClipboardItem write succeeds"); }
    }
  });
  let resolveAsyncText;
  const asyncClipP1 = clipItemAdapter.writeAsyncText(function () {
    getTextCalls1 += 1;
    return new Promise(function (res) { resolveAsyncText = res; });
  });
  const calledWriteSynchronously = wroteSync;
  resolveAsyncText("diagnostic-payload-ok");
  await asyncClipP1;
  const blobText1 = await (await clipWrittenItems[0][0].items["text/plain"]).text();
  if (calledWriteSynchronously && getTextCalls1 === 1 && blobText1 === "diagnostic-payload-ok") {
    pass("clipboard.writeAsyncText 同步调用 clipboard.write(ClipboardItem) 保留用户手势且单次调用 getText");
  } else fail("clipboard.writeAsyncText ClipboardItem 路径异常：" + JSON.stringify({ calledWriteSynchronously: calledWriteSynchronously, getTextCalls1: getTextCalls1, blobText1: blobText1 }));

  let unhandledCount = 0;
  const onUnhandled = function () { unhandledCount += 1; };
  process.on("unhandledRejection", onUnhandled);
  let getTextCallsErr = 0;
  const origApiErr = new B.ApiError({ error: "logs_io", message: "日志目录不可读", status: 500 });
  const clipItemErrAdapter = B.createClipboard({
    ClipboardItem: FakeClipboardItem,
    Blob: Blob,
    clipboard: {
      write: function (items) {
        return Promise.resolve(items[0].items["text/plain"]).then(function () {}, function () {
          throw new Error("DOMException: NotAllowedError");
        });
      },
      writeText: function () { return Promise.resolve(); }
    }
  });
  let caughtAsyncErr = null;
  try {
    await clipItemErrAdapter.writeAsyncText(function () {
      getTextCallsErr += 1;
      return Promise.reject(origApiErr);
    });
  } catch (err) {
    caughtAsyncErr = err;
  }
  await new Promise(function (res) { setTimeout(res, 10); });
  process.off("unhandledRejection", onUnhandled);
  if (caughtAsyncErr === origApiErr && getTextCallsErr === 1 && unhandledCount === 0) {
    pass("clipboard.writeAsyncText 在 getText 失败时保留原始 ApiError、零重复请求且零 unhandledRejection");
  } else fail("clipboard.writeAsyncText 错误保留异常：" + JSON.stringify({ same: caughtAsyncErr === origApiErr, err: caughtAsyncErr && caughtAsyncErr.message, calls: getTextCallsErr, unhandled: unhandledCount }));

  let getTextCallsFallback = 0;
  const fallbackTexts = [];
  const clipFallbackAdapter = B.createClipboard({
    ClipboardItem: function () { throw new TypeError("Promise value not supported"); },
    Blob: Blob,
    clipboard: {
      write: function () { throw new Error("should not call write"); },
      writeText: function (t) { fallbackTexts.push(t); return Promise.resolve(); }
    }
  });
  await clipFallbackAdapter.writeAsyncText(function () {
    getTextCallsFallback += 1;
    return Promise.resolve("fallback-summary-once");
  });
  let getTextCallsNoWrite = 0;
  const noWriteTexts = [];
  const clipTextOnlyAdapter = B.createClipboard({
    clipboard: {
      writeText: function (t) { noWriteTexts.push(t); return Promise.resolve(); }
    }
  });
  await clipTextOnlyAdapter.writeAsyncText(function () {
    getTextCallsNoWrite += 1;
    return Promise.resolve("text-only-summary");
  });
  if (
    getTextCallsFallback === 1 && fallbackTexts.join(",") === "fallback-summary-once"
    && getTextCallsNoWrite === 1 && noWriteTexts.join(",") === "text-only-summary"
  ) {
    pass("clipboard.writeAsyncText 不支持 Promise ClipboardItem 或无 write() 时复用同一 textPromise 回退 writeText（零重复请求）");
  } else fail("clipboard.writeAsyncText 回退路径异常：" + JSON.stringify({ getTextCallsFallback: getTextCallsFallback, fallbackTexts: fallbackTexts, getTextCallsNoWrite: getTextCallsNoWrite, noWriteTexts: noWriteTexts }));

  /* logs-actions + home-controller 完整业务单元测试（懒加载 / 乱序丢弃 / 非乐观上限保存+重读 / 清空与 409 / 复制成功与失败分离 / 非本机拦截 / destroy） */
  const logsWorld = createWorld();
  logsWorld.serverConfig = {
    ok: true,
    ui_theme: "light",
    audio_input_device: "",
    polish_enabled: true,
    custom_keywords: [],
    translate_lang_pair: "zh-en",
    translate_auto_enabled: true,
    log_max_lines: 2000,
    hotkey_voice: "ctrl+alt+z",
    hotkey_screenshot: "ctrl+alt+x",
    hotkey_ocr: "ctrl+alt+o",
    hotkey_repeat: "ctrl+alt+r",
    hotkey_annotate: "ctrl+alt+a",
    autostart_enabled: false
  };
  logsWorld.delayRead = false;
  logsWorld.pendingReads = [];
  logsWorld.delayCapPost = false;
  logsWorld.pendingCapPosts = [];
  logsWorld.delayThemePost = false;
  logsWorld.pendingThemePosts = [];
  logsWorld.failClear409 = false;
  logsWorld.failSummary = false;
  logsWorld.delayAbout = false;
  logsWorld.pendingAbouts = [];
  logsWorld.appLines = [
    { line_no: 1, level: "info", text: "boot ok token=***" },
    { line_no: 2, level: "warn", text: "warn line" }
  ];
  logsWorld.serverLines = [{ line_no: 1, level: "info", text: "server ok" }];
  logsWorld.asrLines = [{ line_no: 1, level: "info", text: "asr ok" }];
  logsWorld.routes.push(
    {
      path: "/api/setup/check",
      handler: function (_rec, res) {
        return res(200, {
          ok: true,
          session_type: "x11",
          asr: { model_name: "Qwen3-ASR-1.7B", status: "ready", weights_present: true, sidecar_running: true, message: "" },
          ollama: { model_name: "qwen3.5:4b", installed: true, running: true, version: "0.40.16", model_status: "ready", message: "" },
          capabilities: [],
          components: [],
          install_all: { state: "done", current: null, steps: [] }
        });
      }
    },
    {
      method: "GET",
      path: "/api/v1/config",
      handler: function (_rec, res) { return res(200, Object.assign({}, logsWorld.serverConfig)); }
    },
    {
      method: "POST",
      path: "/api/v1/config",
      handler: function (rec, res) {
        const body = JSON.parse(rec.body || "{}");
        if (logsWorld.delayThemePost && "ui_theme" in body) {
          return new Promise(function (resolve) {
            logsWorld.pendingThemePosts.push(function () {
              Object.assign(logsWorld.serverConfig, body);
              resolve(res(200, Object.assign({}, logsWorld.serverConfig)));
            });
          });
        }
        if (logsWorld.delayCapPost && "log_max_lines" in body) {
          return new Promise(function (resolve) {
            logsWorld.pendingCapPosts.push({
              cap: body.log_max_lines,
              resolveOk: function () {
                logsWorld.serverConfig.log_max_lines = body.log_max_lines;
                resolve(res(200, Object.assign({}, logsWorld.serverConfig)));
              }
            });
          });
        }
        Object.assign(logsWorld.serverConfig, body);
        return res(200, Object.assign({}, logsWorld.serverConfig));
      }
    },
    { path: "/api/v1/history", handler: function (_rec, res) { return res(200, { ok: true, records: [], total: 0, skipped_malformed: 0 }); } },
    { path: "/api/v1/lan-access", handler: function (_rec, res) { return res(200, { ok: true, port: 17945, token: "tok", token_masked: "t***k", entries: [], message: "" }); } },
    { path: "/api/v1/audio-inputs", handler: function (_rec, res) { return res(200, { ok: true, default_device: null, devices: [] }); } },
    {
      method: "GET",
      match: function (url) { return /\/api\/v1\/logs\/diagnostic-summary(\?|$)/.test(url); },
      handler: function (_rec, res) {
        if (logsWorld.failSummary) return res(500, { ok: false, error: "logs_io", message: "无法生成排障汇总" });
        return res(200, { ok: true, generated_at: "2026-11-23T10:00:00Z", log_max_lines: logsWorld.serverConfig.log_max_lines, truncated: false, included_files: ["app.log"], text: "=== Diagnostic Summary *** ===" });
      }
    },
    {
      method: "GET",
      match: function (url) { return /\/api\/v1\/logs(\?|$)/.test(url); },
      handler: function (_rec, res) {
        return res(200, {
          ok: true,
          log_max_lines: logsWorld.serverConfig.log_max_lines,
          allowed_max_lines: [500, 2000, 5000],
          sources: [
            { name: "app.log", group: "runtime", exists: true, line_count: logsWorld.appLines.length, bytes: 120, truncated: false, has_error: false, has_warn: logsWorld.appLines.length > 1, updated_at: "2026-11-23T10:00:00" },
            { name: "server.log", group: "runtime", exists: true, line_count: logsWorld.serverLines.length, bytes: 60, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-23T10:00:00" },
            { name: "asr.log", group: "runtime", exists: true, line_count: logsWorld.asrLines.length, bytes: 60, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-23T10:00:00" }
          ]
        });
      }
    },
    {
      method: "GET",
      match: function (url) { return /\/api\/v1\/logs\/[^/?]+(\?|$)/.test(url); },
      handler: function (rec, res) {
        const name = decodeURIComponent(rec.url.split("/api/v1/logs/")[1].split("?")[0]);
        const entries = name === "server.log" ? logsWorld.serverLines : (name === "asr.log" ? logsWorld.asrLines : logsWorld.appLines);
        const payload = {
          ok: true,
          name: name,
          group: "runtime",
          exists: true,
          max_lines: logsWorld.serverConfig.log_max_lines,
          line_count: entries.length,
          bytes: entries.length * 40,
          truncated: false,
          has_error: false,
          has_warn: false,
          updated_at: "2026-11-23T10:00:00",
          entries: entries.slice()
        };
        if (logsWorld.delayRead) {
          return new Promise(function (resolve) {
            logsWorld.pendingReads.push({ name: name, resolve: function () { resolve(res(200, payload)); } });
          });
        }
        return res(200, payload);
      }
    },
    {
      method: "DELETE",
      match: function (url) { return /\/api\/v1\/logs\/[^/?]+(\?|$)/.test(url); },
      handler: function (rec, res) {
        const name = decodeURIComponent(rec.url.split("/api/v1/logs/")[1].split("?")[0]);
        if (logsWorld.failClear409) {
          return res(409, { ok: false, error: "conflict", message: "安装任务运行中，暂不可清空" });
        }
        if (name === "app.log") logsWorld.appLines = [];
        if (name === "server.log") logsWorld.serverLines = [];
        return res(200, { ok: true, name: name, cleared: true, bytes_before: 80 });
      }
    },
    {
      method: "GET",
      path: "/api/v1/about",
      handler: function (_rec, res) {
        const payload = {
          ok: true,
          app_name: "Voice Studio",
          version: "3.4.6",
          summary: "本地语音与翻译工作台",
          os_name: "linux",
          platform_detail: "Ubuntu 24.04",
          session_type: "x11",
          python_version: "3.12.3",
          run_mode: "source",
          asr_variant: "cuda",
          asr_detail: "NVIDIA GPU",
          asr_model: "Qwen3-ASR-1.7B",
          ollama_model: "qwen3.5:4b",
          ollama_version: "0.40.16",
          data_root: "/var/lib/ovi",
          logs_dir: "/var/lib/ovi/logs",
          config_root: "/etc/ovi",
          license_title: "AGPL-3.0 + 商业授权",
          license_note: "个人免费",
          links: {
            repo_url: "https://github.com/jackche8/voice-input",
            pages_url: "https://jackche8.github.io/voice-input/",
            releases_url: "https://github.com/jackche8/voice-input/releases",
            issues_url: "https://github.com/jackche8/voice-input/issues"
          }
        };
        if (logsWorld.delayAbout) {
          return new Promise(function (resolve) {
            logsWorld.pendingAbouts.push(function (overrideVersion) {
              const p = Object.assign({}, payload);
              if (overrideVersion) p.version = overrideVersion;
              resolve(res(200, p));
            });
          });
        }
        return res(200, payload);
      }
    }
  );
  const logsClip = { texts: [], failNext: false };
  const logsCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: logsWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: B.createClipboard({
      clipboard: {
        writeText: function (t) {
          if (logsClip.failNext) {
            logsClip.failNext = false;
            return Promise.reject(new Error("剪贴板权限被拒绝"));
          }
          logsClip.texts.push(t);
          return Promise.resolve();
        }
      }
    })
  });
  const logsView = fakeView(catalogCtx.V3.defaults.homeState());
  logsCtl.bind(logsView);

  /* 1. loadAll() 严禁急切请求 /api/v1/logs 或 /api/v1/about */
  await logsCtl.start();
  const eagerLogCalls = logsWorld.calls.filter(function (c) { return c.url.indexOf("/logs") >= 0 || c.url.indexOf("/about") >= 0; });
  if (
    eagerLogCalls.length === 0
    && logsView.getState().log_max_lines === 2000
    && logsView.getState().logs.phase === "idle"
    && logsView.getState().about.phase === "idle"
  ) {
    pass("loadAll() 仅加载核心配置 log_max_lines=2000，严禁急切请求 logs/about（保持懒加载）");
  } else fail("loadAll() 误急切请求 logs/about：" + JSON.stringify(eagerLogCalls));

  /* 2. onSelectPanel("logs") 懒加载来源表与当前日志 */
  await logsCtl.hooks.onSelectPanel({ panel: "logs" });
  const logsAfterEnter = logsView.getState().logs;
  if (
    logsAfterEnter.phase === "ready"
    && logsAfterEnter.activeFile === "app.log"
    && logsAfterEnter.sources.length === 3
    && logsAfterEnter.entries.length === 2
  ) {
    pass("onSelectPanel('logs') 懒加载来源列表与 app.log 内容");
  } else fail("onSelectPanel('logs') 懒加载异常：" + JSON.stringify(logsAfterEnter));

  /* 3. 快速切源乱序返回丢弃旧响应（logReadReqId + activeFile） */
  logsWorld.delayRead = true;
  const readSlowServer = logsCtl.hooks.onSelectLogSource({ name: "server.log" });
  const readFastAsr = logsCtl.hooks.onSelectLogSource({ name: "asr.log" });
  const pSrv = logsWorld.pendingReads.shift();
  const pAsr = logsWorld.pendingReads.shift();
  pAsr.resolve();
  await readFastAsr;
  pSrv.resolve();
  await readSlowServer;
  logsWorld.delayRead = false;
  if (
    logsView.getState().logs.activeFile === "asr.log"
    && logsView.getState().logs.entries.length === 1
    && logsView.getState().logs.entries[0].text === "asr ok"
  ) {
    pass("快速切源乱序返回时 logReadReqId 丢弃滞后的 server.log 响应，保留最新 asr.log");
  } else fail("快速切源防竞态失效：" + JSON.stringify(logsView.getState().logs));

  /* 4. onChangeLogMaxLines 非乐观保存 + 乱序丢弃 + 回读后立刻重读当前日志 + 继承 inFlightThemeWrites 保护 */
  logsWorld.delayCapPost = true;
  const callsBeforeCap = logsWorld.calls.length;
  const capReq1 = logsCtl.hooks.onChangeLogMaxLines({ log_max_lines: 5000 });
  const duringCap = logsView.getState();
  const capReq2 = logsCtl.hooks.onChangeLogMaxLines({ log_max_lines: 500 });
  const c1 = logsWorld.pendingCapPosts.shift();
  const c2 = logsWorld.pendingCapPosts.shift();
  c2.resolveOk();
  await capReq2;
  c1.resolveOk();
  await capReq1;
  logsWorld.delayCapPost = false;
  const callsAfterCap = logsWorld.calls.slice(callsBeforeCap);
  const rereadAfterCap = callsAfterCap.some(function (c) { return c.method === "GET" && /\/api\/v1\/logs\/asr\.log/.test(c.url); });
  if (
    duringCap.logs.savingCap === true && duringCap.log_max_lines === 2000 && duringCap.logs.log_max_lines === 2000
    && logsView.getState().log_max_lines === 500 && logsView.getState().logs.log_max_lines === 500
    && logsView.getState().logs.capSaved === true && logsView.getState().logs.savingCap === false
    && logsView.getState().logs.max_lines === 500 && rereadAfterCap
  ) {
    pass("onChangeLogMaxLines 非乐观持久化、丢弃旧乱序请求，并在回读后立刻重读来源与当前日志");
  } else fail("onChangeLogMaxLines 行为异常：" + JSON.stringify({ duringCap: duringCap.logs.log_max_lines, after: logsView.getState().logs, rereadAfterCap: rereadAfterCap }));

  /* 验证继承 setupActions inFlightThemeWrites 保护：主题写未完成时并发改 log_max_lines 不闪回旧主题 */
  logsWorld.delayThemePost = true;
  const themeWriteInFlight = logsCtl.hooks.onToggleTheme({ theme: "dark" });
  await logsCtl.hooks.onChangeLogMaxLines({ log_max_lines: 2000 });
  const themeMidCap = logsView.getState().theme;
  logsWorld.pendingThemePosts.shift()();
  await themeWriteInFlight;
  logsWorld.delayThemePost = false;
  if (themeMidCap === "dark" && logsView.getState().theme === "dark") {
    pass("onChangeLogMaxLines 复用 setupActions.saveConfig，继承 inFlightThemeWrites 并发主题防闪回保护");
  } else fail("onChangeLogMaxLines 并发主题防闪回失效：" + JSON.stringify({ themeMidCap: themeMidCap, final: logsView.getState().theme }));

  /* 5. onClearCurrentLog：409 冲突保留原文写 copyError；成功后清空并刷新且保留已清空提示 */
  await logsCtl.hooks.onSelectLogSource({ name: "app.log" });
  logsWorld.failClear409 = true;
  await logsCtl.hooks.onClearCurrentLog({ name: "app.log" });
  const afterClear409 = logsView.getState().logs;
  logsWorld.failClear409 = false;
  await logsCtl.hooks.onClearCurrentLog({ name: "app.log" });
  const afterClearOk = logsView.getState().logs;
  if (
    afterClear409.clearing === false && afterClear409.copyOk === "" && /冲突|安装任务/.test(afterClear409.copyError || "") && afterClear409.entries.length === 2
    && afterClearOk.clearing === false && afterClearOk.phase === "empty" && afterClearOk.entries.length === 0 && /已清空 app\.log/.test(afterClearOk.copyOk || "") && afterClearOk.copyError === ""
  ) {
    pass("onClearCurrentLog 409 冲突写 copyError 保原文，成功后清空并刷新且保留已清空 copyOk");
  } else fail("onClearCurrentLog 行为异常：" + JSON.stringify({ afterClear409: afterClear409, afterClearOk: afterClearOk }));

  /* 6. copyCurrentLog / copyDiagnosticSummary / about / copyAboutInfo 的 copyOk 与 copyError 严格分离 */
  await logsCtl.hooks.onSelectLogSource({ name: "server.log" });
  await logsCtl.hooks.onCopyCurrentLog({ name: "server.log", text: "server ok" });
  const logCopyOkState = Object.assign({}, logsView.getState().logs);
  logsClip.failNext = true;
  await logsCtl.hooks.onCopyCurrentLog({ name: "server.log", text: "server ok" });
  const logCopyErrState = Object.assign({}, logsView.getState().logs);
  await logsCtl.hooks.onCopyDiagnosticSummary();
  const sumOkState = Object.assign({}, logsView.getState().logs);
  logsWorld.failSummary = true;
  await logsCtl.hooks.onCopyDiagnosticSummary();
  const sumErrState = Object.assign({}, logsView.getState().logs);
  logsWorld.failSummary = false;
  if (
    /已复制 server\.log/.test(logCopyOkState.copyOk) && logCopyOkState.copyError === ""
    && logCopyErrState.copyOk === "" && /剪贴板权限被拒绝/.test(logCopyErrState.copyError)
    && /已复制排障汇总/.test(sumOkState.copyOk) && sumOkState.copyError === "" && logsClip.texts[logsClip.texts.length - 1] === "=== Diagnostic Summary *** ==="
    && sumErrState.copyOk === "" && /日志读写失败|无法生成排障汇总/.test(sumErrState.copyError)
  ) {
    pass("onCopyCurrentLog 与 onCopyDiagnosticSummary 严格分离 copyOk 与 copyError（失败透传后端 ApiError）");
  } else fail("日志复制反馈分离异常：" + JSON.stringify({ logCopyOkState: logCopyOkState, logCopyErrState: logCopyErrState, sumOkState: sumOkState, sumErrState: sumErrState }));

  await logsCtl.hooks.onSelectPanel({ panel: "about" });
  const aboutLoaded = logsView.getState().about;
  await logsCtl.hooks.onCopyAboutInfo();
  const aboutCopyOk = Object.assign({}, logsView.getState().about);
  const aboutClipText = logsClip.texts[logsClip.texts.length - 1] || "";
  logsClip.failNext = true;
  await logsCtl.hooks.onCopyAboutInfo();
  const aboutCopyErr = Object.assign({}, logsView.getState().about);
  if (
    aboutLoaded.phase === "ready" && aboutLoaded.version === "3.4.6" && aboutLoaded.asr_variant === "cuda"
    && aboutCopyOk.copyOk === "已复制环境信息" && aboutCopyOk.copyError === ""
    && aboutClipText.indexOf("v3.4.6") >= 0 && aboutClipText.indexOf("/var/lib/ovi/logs") >= 0
    && aboutCopyErr.copyOk === "" && /剪贴板权限被拒绝/.test(aboutCopyErr.copyError)
  ) {
    pass("onSelectPanel('about') 懒加载关于信息，onCopyAboutInfo 同步复制并分离 copyOk/copyError");
  } else fail("关于面板业务动作异常：" + JSON.stringify({ aboutLoaded: aboutLoaded, aboutCopyOk: aboutCopyOk, aboutCopyErr: aboutCopyErr }));

  /* 7. aboutReqId 乱序防覆盖 + destroy() 拦截在途 about 响应；非本机（lanCtx）零 fetch 拦截 logs/about */
  logsWorld.delayAbout = true;
  const aboutSlow = logsCtl.hooks.onRefreshAbout();
  const aboutFast = logsCtl.hooks.onRefreshAbout();
  const ab1 = logsWorld.pendingAbouts.shift();
  const ab2 = logsWorld.pendingAbouts.shift();
  ab2("3.4.6");
  await aboutFast;
  ab1("0.0.0-stale");
  await aboutSlow;
  if (logsView.getState().about.version === "3.4.6") {
    pass("aboutReqId 丢弃滞后乱序的关于响应，保留最新版本号");
  } else fail("aboutReqId 防乱序覆盖失效：" + JSON.stringify(logsView.getState().about));

  const lateAboutP = logsCtl.hooks.onRefreshAbout();
  logsCtl.destroy();
  logsWorld.pendingAbouts.shift()("9.9.9-ghost");
  await lateAboutP;
  logsWorld.delayAbout = false;
  if (logsView.getState().about.phase === "loading") {
    pass("destroy() 递增 aboutReqId，丢弃销毁后迟到的关于响应");
  } else fail("destroy() 后迟到 about 响应污染了视图：" + JSON.stringify(logsView.getState().about));

  const lanLogsWorld = createWorld();
  const lanLogsCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanLogsWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    catalog: catalog
  });
  const lanLogsView = fakeView(catalogCtx.V3.defaults.homeState());
  lanLogsCtl.bind(lanLogsView);
  await lanLogsCtl.hooks.onSelectPanel({ panel: "logs" });
  await lanLogsCtl.hooks.onSelectPanel({ panel: "about" });
  await lanLogsCtl.hooks.onCopyDiagnosticSummary();
  if (
    lanLogsWorld.calls.length === 0
    && lanLogsView.getState().logs.phase === "error" && lanLogsView.getState().logs.error === "forbidden"
    && /仅允许在本机访问/.test(lanLogsView.getState().logs.copyError || "")
    && lanLogsView.getState().about.phase === "error" && lanLogsView.getState().about.error === "forbidden"
  ) {
    pass("非本机（LAN）访问 logs / about / diagnostic-summary 零 fetch 且在触碰剪贴板前显式呈报 forbidden");
  } else fail("非本机 logs/about 拦截失效：" + JSON.stringify({ calls: lanLogsWorld.calls.length, logs: lanLogsView.getState().logs, about: lanLogsView.getState().about }));
  lanLogsCtl.destroy();

  /* ---------- F2：home-controller 持久提示槽 + bootstrap 样式表自愈全分支 ---------- */
  /* 1. home-controller.setPersistentNotice：防 1s 轮询与 saveConfig 抹除、浅拷贝不改入参、显式非空报错仍正常显示、destroy 后忽略 */
  const persistCtx = loadBusiness();
  const PB = persistCtx.window.V3Business;
  const pCatalog = PB.createCatalog(snapshot);
  const pLocalCtx = PB.createContext({ protocol: "https:", hostname: "127.0.0.1", origin: "https://127.0.0.1:17945", href: "https://127.0.0.1:17945/", search: "" });
  let capturedHomeSetState = null;
  const origCreateSetupActions = PB.createSetupActions;
  PB.createSetupActions = function (opts) {
    capturedHomeSetState = opts && opts.setState;
    return origCreateSetupActions(opts);
  };
  let persistPollCount = 0;
  let persistFailNextConfig = false;
  const persistFetch = async function (url, init) {
    const u = String(url);
    const method = (init && init.method) || "GET";
    if (u.indexOf("/api/setup/check") >= 0) {
      persistPollCount += 1;
      const busyState = persistPollCount < 3 ? "running" : "done";
      return jsonRes(200, {
        ok: true,
        asr: { status: "ready", sidecar_running: true },
        ollama: { installed: true, running: true, model_status: "ready" },
        capabilities: [],
        components: [{ name: "asr", state: busyState === "running" ? "starting" : "running", reason: "", failures: 0 }],
        install_all: { state: busyState, current: null, steps: [] }
      });
    }
    if (u.indexOf("/api/v1/config") >= 0 && method === "POST") {
      if (persistFailNextConfig) {
        persistFailNextConfig = false;
        return jsonRes(400, { ok: false, error: "invalid_request", message: "显式配置保存报错" });
      }
      const parsed = JSON.parse((init && init.body) || "{}");
      return jsonRes(200, Object.assign({
        polish_enabled: true,
        custom_keywords: [],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r",
        hotkey_annotate: "ctrl+alt+a",
        autostart_enabled: false,
        ui_theme: "light",
        audio_input_device: "",
        log_max_lines: 2000
      }, parsed));
    }
    if (u.indexOf("/api/v1/config") >= 0) {
      return jsonRes(200, {
        polish_enabled: true,
        custom_keywords: [],
        translate_lang_pair: "zh-en",
        translate_auto_enabled: true,
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r",
        hotkey_annotate: "ctrl+alt+a",
        autostart_enabled: false,
        ui_theme: "light",
        audio_input_device: "",
        log_max_lines: 2000
      });
    }
    if (u.indexOf("/api/v1/history") >= 0) return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    if (u.indexOf("/api/v1/lan-access") >= 0) return jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" });
    if (u.indexOf("/api/v1/audio-inputs") >= 0) return jsonRes(200, { ok: true, default_device: null, devices: [] });
    return jsonRes(404, { ok: false, error: "not_found", message: "missing" });
  };
  const persistCtl = PB.createHomeController({
    client: PB.createHttp({ catalog: pCatalog, context: pLocalCtx, fetch: persistFetch, origin: "https://127.0.0.1:17945" }),
    context: pLocalCtx,
    catalog: pCatalog,
    pollInterval: 10
  });
  const persistView = fakeView(persistCtx.V3.defaults.homeState());
  persistCtl.bind(persistView);
  await persistCtl.start();
  const cssPersistMsg = "页面样式表加载失败（css/theme.css），请刷新或重启应用。";
  persistCtl.setPersistentNotice(cssPersistMsg);
  const pollSettled = await waitFor(function () { return persistPollCount >= 3 && !persistCtl._debug.polling(); }, 1500);
  const noticeAfterPoll = persistView.getState().statusNotice;
  await persistCtl.hooks.onTogglePolish(false);
  const noticeAfterSaveOk = persistView.getState().statusNotice;
  let frozenPatchErr = null;
  const frozenPatch = Object.freeze({ connection: "connected", statusNotice: null });
  try {
    capturedHomeSetState(frozenPatch);
  } catch (err) {
    frozenPatchErr = err;
  }
  const noticeAfterFrozen = persistView.getState().statusNotice;
  persistFailNextConfig = true;
  let explicitErrThrown = false;
  try {
    await persistCtl.hooks.onTogglePolish(true);
  } catch (err) {
    explicitErrThrown = true;
  }
  const noticeAfterExplicitErr = persistView.getState().statusNotice;
  await persistCtl.hooks.onTogglePolish(true);
  const noticeAfterRecoverSave = persistView.getState().statusNotice;
  persistCtl.setPersistentNotice(null);
  const noticeAfterClear = persistView.getState().statusNotice;
  persistCtl.destroy();
  persistCtl.setPersistentNotice("destroyed-must-be-ignored");
  const noticeAfterDestroy = persistView.getState().statusNotice;
  if (
    pollSettled
    && noticeAfterPoll === cssPersistMsg
    && noticeAfterSaveOk === cssPersistMsg
    && !frozenPatchErr && frozenPatch.statusNotice === null && noticeAfterFrozen === cssPersistMsg
    && explicitErrThrown && /显式配置保存报错/.test(noticeAfterExplicitErr || "")
    && noticeAfterRecoverSave === cssPersistMsg
    && noticeAfterClear === null
    && noticeAfterDestroy === null
  ) {
    pass("home-controller.setPersistentNotice 防 1s readinessPatch 与 saveConfig 抹除、浅拷贝不改入参、允许显式非空报错并在清空/destroy 后表现正确");
  } else {
    fail("home-controller.setPersistentNotice 行为异常：" + JSON.stringify({
      pollSettled: pollSettled,
      noticeAfterPoll: noticeAfterPoll,
      noticeAfterSaveOk: noticeAfterSaveOk,
      frozenPatchErr: frozenPatchErr && frozenPatchErr.message,
      noticeAfterFrozen: noticeAfterFrozen,
      noticeAfterExplicitErr: noticeAfterExplicitErr,
      noticeAfterRecoverSave: noticeAfterRecoverSave,
      noticeAfterClear: noticeAfterClear,
      noticeAfterDestroy: noticeAfterDestroy
    }));
  }

  /* 2. 轻量假 DOM 夹具，供 ensureStylesheetsLoaded 与 startPage 并行自愈 / pagehide / loadOpenApi 失败全分支验收 */
  function createFakeCssEnv(spec) {
    spec = spec || {};
    let bgVar = spec.bgVar != null ? spec.bgVar : "#f4f6fa";
    const createdLinks = [];
    const insertCalls = [];
    const consoleErrors = [];
    const winEvents = {};
    const timers = [];
    let nextTimerId = 1;

    function makeLink(href, sheet) {
      const listeners = {};
      const attrs = { rel: "stylesheet", href: href || "" };
      const node = {
        tagName: "LINK",
        rel: "stylesheet",
        href: href || "",
        media: "",
        sheet: sheet !== undefined ? sheet : null,
        parentNode: null,
        getAttribute: function (k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
        setAttribute: function (k, v) {
          attrs[k] = String(v);
          if (k === "href") node.href = String(v);
          if (k === "rel") node.rel = String(v);
        },
        addEventListener: function (ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
        removeEventListener: function (ev, fn) {
          if (!listeners[ev]) return;
          listeners[ev] = listeners[ev].filter(function (item) { return item !== fn; });
        },
        dispatch: function (ev) {
          const list = (listeners[ev] || []).slice();
          list.forEach(function (fn) { fn({ type: ev, target: node }); });
        },
        _listenerCount: function (ev) { return (listeners[ev] || []).length; }
      };
      return node;
    }

    const head = {
      children: [],
      insertBefore: function (newNode, refNode) {
        const idx = head.children.indexOf(refNode);
        if (idx >= 0) head.children.splice(idx, 0, newNode);
        else head.children.push(newNode);
        newNode.parentNode = head;
        const attempt = insertCalls.length + 1;
        insertCalls.push({ node: newNode, ref: refNode, index: idx, href: newNode.getAttribute("href") });
        if (typeof spec.onInsert === "function") {
          spec.onInsert(newNode, attempt, env);
        }
        return newNode;
      },
      removeChild: function (oldNode) {
        const idx = head.children.indexOf(oldNode);
        if (idx >= 0) head.children.splice(idx, 1);
        oldNode.parentNode = null;
        return oldNode;
      }
    };

    (spec.links || []).forEach(function (item) {
      const ln = makeLink(item.href, item.sheet);
      if (item.media) ln.media = item.media;
      ln.parentNode = head;
      head.children.push(ln);
    });

    const doc = {
      head: head,
      documentElement: { tagName: "HTML", dataset: { theme: "light" } },
      getElementById: function (id) { return id === "app" ? { id: "app" } : null; },
      querySelectorAll: function (sel) {
        if (String(sel).indexOf("stylesheet") >= 0) {
          return head.children.filter(function (el) { return el.tagName === "LINK" && el.rel === "stylesheet"; });
        }
        return [];
      },
      createElement: function (tag) {
        if (String(tag).toLowerCase() === "link") {
          const ln = makeLink("", null);
          createdLinks.push(ln);
          return ln;
        }
        return { tagName: String(tag).toUpperCase() };
      }
    };

    const win = {
      addEventListener: function (ev, fn) { (winEvents[ev] = winEvents[ev] || []).push(fn); },
      removeEventListener: function (ev, fn) {
        if (!winEvents[ev]) return;
        winEvents[ev] = winEvents[ev].filter(function (item) { return item !== fn; });
      },
      getComputedStyle: function (el) {
        if (el !== doc.documentElement) return { getPropertyValue: function () { return ""; } };
        return {
          getPropertyValue: function (prop) {
            return prop === "--c-bg" ? bgVar : "";
          }
        };
      }
    };

    const env = {
      head: head,
      doc: doc,
      win: win,
      winEvents: winEvents,
      createdLinks: createdLinks,
      insertCalls: insertCalls,
      consoleErrors: consoleErrors,
      console: { error: function () { consoleErrors.push(Array.prototype.slice.call(arguments).join(" ")); } },
      setBgVar: function (v) { bgVar = v; },
      setTimeout: function (fn, ms) {
        const id = nextTimerId++;
        timers.push({ id: id, fn: fn, ms: ms, cleared: false });
        return id;
      },
      clearTimeout: function (id) {
        timers.forEach(function (t) { if (t.id === id) t.cleared = true; });
      },
      flushTimers: function () {
        const active = timers.filter(function (t) { return !t.cleared; });
        active.forEach(function (t) { t.cleared = true; t.fn(); });
      }
    };
    return env;
  }

  /* 2a. 离线 file:// 零动作跳过 */
  const fileCssEnv = createFakeCssEnv({ links: [{ href: "css/theme.css", sheet: null }] });
  const fileCssRes = await B.ensureStylesheetsLoaded({
    document: fileCssEnv.doc,
    window: fileCssEnv.win,
    location: { protocol: "file:", href: "file:///tmp/index.html" },
    console: fileCssEnv.console
  });
  if (
    fileCssRes.ok === true && fileCssRes.skipped === true
    && fileCssRes.retried.length === 0 && fileCssRes.failed.length === 0
    && fileCssEnv.createdLinks.length === 0 && fileCssEnv.consoleErrors.length === 0
  ) {
    pass("ensureStylesheetsLoaded 在 file:// 离线模式下直接跳过（零 DOM 改动与网络请求）");
  } else fail("ensureStylesheetsLoaded file:// 跳过异常：" + JSON.stringify(fileCssRes));

  /* 2b. 全部样式表健康时零额外请求 */
  const okCssEnv = createFakeCssEnv({
    bgVar: "#f4f6fa",
    links: [
      { href: "css/theme.css", sheet: { cssRules: [{ cssText: ":root{--c-bg:#f4f6fa}" }] } },
      { href: "css/studio.css", sheet: { cssRules: [{ cssText: ".box-body{}" }] } }
    ]
  });
  const okCssRes = await B.ensureStylesheetsLoaded({
    document: okCssEnv.doc,
    window: okCssEnv.win,
    location: { protocol: "https:", origin: "https://127.0.0.1:17945" },
    console: okCssEnv.console
  });
  if (
    okCssRes.ok === true && okCssRes.skipped === false
    && okCssRes.retried.length === 0 && okCssRes.failed.length === 0
    && okCssEnv.createdLinks.length === 0 && okCssEnv.insertCalls.length === 0
  ) {
    pass("ensureStylesheetsLoaded 在样式表全部健康时零多余请求");
  } else fail("ensureStylesheetsLoaded 健康分支异常：" + JSON.stringify({ res: okCssRes, created: okCssEnv.createdLinks.length }));

  /* 2c. 规范替换 _css_retry（保留原 query 与 #hash，无重复 ?）、原位 insertBefore 保序、load 后缺 cssRules 继续第 2 次重试 */
  const healCssEnv = createFakeCssEnv({
    bgVar: "",
    links: [
      { href: "css/theme.css?v=3&_css_retry=9&lang=zh#top", sheet: null },
      { href: "css/studio.css", sheet: { cssRules: [{ cssText: ".studio{}" }] } }
    ],
    onInsert: function (fresh, attempt, env) {
      Promise.resolve().then(function () {
        if (attempt === 1) {
          /* 第 1 次触发 load 但 cssRules 为空：重验必须判定失败并继续第 2 次重试 */
          fresh.sheet = { cssRules: [] };
          fresh.dispatch("load");
        } else {
          fresh.sheet = { cssRules: [{ cssText: ":root{--c-bg:#f4f6fa}" }] };
          env.setBgVar("#f4f6fa");
          fresh.dispatch("load");
        }
      });
    }
  });
  const studioOrigLink = healCssEnv.head.children[1];
  const healCssRes = await B.ensureStylesheetsLoaded({
    document: healCssEnv.doc,
    window: healCssEnv.win,
    location: { protocol: "https:", origin: "https://127.0.0.1:17945" },
    console: healCssEnv.console
  });
  const retryHrefs = healCssEnv.insertCalls.map(function (c) { return c.href; });
  if (
    healCssRes.ok === true && healCssRes.skipped === false
    && healCssRes.retried.length === 1 && healCssRes.failed.length === 0
    && retryHrefs.length === 2
    && retryHrefs[0] === "css/theme.css?v=3&lang=zh&_css_retry=1#top"
    && retryHrefs[1] === "css/theme.css?v=3&lang=zh&_css_retry=2#top"
    && healCssEnv.head.children.length === 2
    && healCssEnv.head.children[0] === healCssEnv.createdLinks[1]
    && healCssEnv.head.children[1] === studioOrigLink
    && healCssEnv.consoleErrors.length === 0
  ) {
    pass("ensureStylesheetsLoaded 规范替换 _css_retry、原位 insertBefore 保序，且 load 后缺 cssRules 继续第 2 次重试直至恢复");
  } else {
    fail("ensureStylesheetsLoaded 原位重试或规则重验异常：" + JSON.stringify({ res: healCssRes, retryHrefs: retryHrefs, headLen: healCssEnv.head.children.length }));
  }

  /* 2d. theme.css 虽有 sheet.cssRules 但缺 --c-bg 时必须判定不健康并重试（且第 1 次 load 后仍缺 --c-bg 须继续第 2 次） */
  const bgMissingEnv = createFakeCssEnv({
    bgVar: "",
    links: [
      { href: "css/theme.css", sheet: { cssRules: [{ cssText: "body{margin:0}" }] } }
    ],
    onInsert: function (fresh, attempt, env) {
      Promise.resolve().then(function () {
        fresh.sheet = { cssRules: [{ cssText: ":root{--c-bg:#f4f6fa}" }] };
        if (attempt === 2) env.setBgVar("#f4f6fa");
        fresh.dispatch("load");
      });
    }
  });
  const bgMissingRes = await B.ensureStylesheetsLoaded({
    document: bgMissingEnv.doc,
    window: bgMissingEnv.win,
    location: { protocol: "https:", origin: "https://127.0.0.1:17945" },
    console: bgMissingEnv.console
  });
  if (
    bgMissingRes.ok === true && bgMissingRes.retried.join(",") === "css/theme.css"
    && bgMissingEnv.createdLinks.length === 2 && bgMissingEnv.consoleErrors.length === 0
  ) {
    pass("ensureStylesheetsLoaded 对 theme.css 校验 --c-bg 非空：即使 cssRules 非空但缺 --c-bg 也会触发重试并重验");
  } else {
    fail("ensureStylesheetsLoaded --c-bg 校验失效：" + JSON.stringify({ res: bgMissingRes, attempts: bgMissingEnv.createdLinks.length }));
  }

  /* 2e. 2 次重试全败（含超时与 error）记 console.error 且最多重试 2 次 */
  const failCssEnv = createFakeCssEnv({
    bgVar: "",
    links: [{ href: "css/theme.css", sheet: null }],
    onInsert: function (fresh, attempt, env) {
      if (attempt === 1) {
        Promise.resolve().then(function () { fresh.dispatch("error"); });
      } else {
        /* 第 2 次通过超时触发失败 */
        Promise.resolve().then(function () { env.flushTimers(); });
      }
    }
  });
  const failCssRes = await B.ensureStylesheetsLoaded({
    document: failCssEnv.doc,
    window: failCssEnv.win,
    location: { protocol: "https:", origin: "https://127.0.0.1:17945" },
    setTimeout: failCssEnv.setTimeout,
    clearTimeout: failCssEnv.clearTimeout,
    console: failCssEnv.console
  });
  if (
    failCssRes.ok === false && failCssRes.skipped === false
    && failCssRes.retried.join(",") === "css/theme.css"
    && failCssRes.failed.join(",") === "css/theme.css"
    && failCssEnv.createdLinks.length === 2
    && failCssEnv.consoleErrors.length === 1 && /css\/theme\.css/.test(failCssEnv.consoleErrors[0])
  ) {
    pass("ensureStylesheetsLoaded 最多重试 2 次，全败返回 failed 并记录 console.error");
  } else {
    fail("ensureStylesheetsLoaded 全败分支异常：" + JSON.stringify({ res: failCssRes, created: failCssEnv.createdLinks.length, errs: failCssEnv.consoleErrors }));
  }

  /* 3. startPage("home") 样式表全败：在 controller.start() 结算后写入持久提示槽，不被后续轮询/saveConfig 抹除 */
  const homeFailCssEnv = createFakeCssEnv({
    bgVar: "",
    links: [{ href: "css/theme.css", sheet: null }],
    onInsert: function (fresh) {
      Promise.resolve().then(function () { fresh.dispatch("error"); });
    }
  });
  const homeFailCtx = loadBusiness({ document: homeFailCssEnv.doc });
  let homeFailView = null;
  homeFailCtx.window.V3.home = {
    mount: function (_el, init) {
      homeFailView = fakeView(Object.assign(homeFailCtx.V3.defaults.homeState(), init || {}));
      return homeFailView;
    }
  };
  await homeFailCtx.window.V3Business.startPage("home", {
    openapi: snapshot,
    fetch: persistFetch,
    location: bootLoc,
    document: homeFailCssEnv.doc,
    window: homeFailCssEnv.win,
    console: homeFailCssEnv.console
  });
  const homeFailCtl = homeFailCtx.window.__v3Business.controller;
  const noticeAfterStart = homeFailView.getState().statusNotice;
  await homeFailCtl.hooks.onRefreshStatus();
  const noticeAfterRefresh = homeFailView.getState().statusNotice;
  await homeFailCtl.hooks.onTogglePolish(false);
  const noticeAfterCfg = homeFailView.getState().statusNotice;
  homeFailCtl.destroy();
  if (
    homeFailCssEnv.consoleErrors.length === 1
    && /样式表加载失败/.test(noticeAfterStart || "") && /css\/theme\.css/.test(noticeAfterStart || "")
    && noticeAfterRefresh === noticeAfterStart
    && noticeAfterCfg === noticeAfterStart
  ) {
    pass("startPage('home') 样式表全败记 console.error，在 controller.start() 后写入持久提示且不被刷新与 saveConfig 抹除");
  } else {
    fail("startPage('home') 样式表全败持久提示异常：" + JSON.stringify({
      errs: homeFailCssEnv.consoleErrors,
      start: noticeAfterStart,
      refresh: noticeAfterRefresh,
      cfg: noticeAfterCfg
    }));
  }

  /* 4. startPage("voice") 样式表全败：在 controller.start() 结算后写入 statusText + statusTone="err" */
  const voiceFailCssEnv = createFakeCssEnv({
    bgVar: "",
    links: [{ href: "css/theme.css", sheet: null }],
    onInsert: function (fresh) {
      Promise.resolve().then(function () { fresh.dispatch("error"); });
    }
  });
  const voiceFailCtx = loadBusiness({ document: voiceFailCssEnv.doc });
  let voiceFailView = null;
  voiceFailCtx.window.V3.mountVoice = function (_el, opts) {
    voiceFailView = fakeView(Object.assign(voiceFailCtx.V3.defaults.voiceState(), (opts && opts.initialState) || {}));
    return voiceFailView;
  };
  await voiceFailCtx.window.V3Business.startPage("voice", {
    openapi: snapshot,
    fetch: persistFetch,
    location: bootLoc,
    document: voiceFailCssEnv.doc,
    window: voiceFailCssEnv.win,
    console: voiceFailCssEnv.console
  });
  const voiceFailCtl = voiceFailCtx.window.__v3Business.controller;
  const voiceCssStatus = { text: voiceFailView.getState().statusText, tone: voiceFailView.getState().statusTone };
  voiceFailCtl.destroy();
  if (
    voiceFailCssEnv.consoleErrors.length === 1
    && /样式表加载失败/.test(voiceCssStatus.text || "")
    && voiceCssStatus.tone === "err"
  ) {
    pass("startPage('voice') 样式表全败记 console.error 并写入 statusText 与 statusTone='err'");
  } else {
    fail("startPage('voice') 样式表全败提示异常：" + JSON.stringify({ errs: voiceFailCssEnv.consoleErrors, status: voiceCssStatus }));
  }

  /* 5. pagehide 设 disposed=true 阻断 home 与 voice 销毁后的迟到样式表失败写入 */
  const pendingHomeLinks = [];
  const homeHideEnv = createFakeCssEnv({
    bgVar: "",
    links: [{ href: "css/theme.css", sheet: null }],
    onInsert: function (fresh) { pendingHomeLinks.push(fresh); }
  });
  const homeHideCtx = loadBusiness({ document: homeHideEnv.doc });
  let homeHideWrites = 0;
  let homeHideView = null;
  homeHideCtx.window.V3.home = {
    mount: function (_el, init) {
      const inner = fakeView(Object.assign(homeHideCtx.V3.defaults.homeState(), init || {}));
      homeHideView = {
        getState: inner.getState,
        setState: function (patch) { homeHideWrites += 1; return inner.setState(patch); }
      };
      return homeHideView;
    }
  };
  const homeHidePromise = homeHideCtx.window.V3Business.startPage("home", {
    openapi: snapshot,
    fetch: bootFetch,
    location: bootLoc,
    document: homeHideEnv.doc,
    window: homeHideEnv.win,
    console: homeHideEnv.console
  });
  await waitFor(function () { return !!(homeHideCtx.window.__v3Business && homeHideCtx.window.__v3Business.controller && homeHideView.getState().connection === "connected"); }, 1000);
  const homeHideCtl = homeHideCtx.window.__v3Business.controller;
  homeHideEnv.winEvents.pagehide.forEach(function (fn) { fn(); });
  const homeWritesAtHide = homeHideWrites;
  pendingHomeLinks.shift().dispatch("error");
  await waitFor(function () { return pendingHomeLinks.length === 1; }, 500);
  pendingHomeLinks.shift().dispatch("error");
  await homeHidePromise;

  const pendingVoiceLinks = [];
  const voiceHideEnv = createFakeCssEnv({
    bgVar: "",
    links: [{ href: "css/theme.css", sheet: null }],
    onInsert: function (fresh) { pendingVoiceLinks.push(fresh); }
  });
  const voiceHideCtx = loadBusiness({ document: voiceHideEnv.doc });
  let voiceHideWrites = 0;
  let voiceHideView = null;
  voiceHideCtx.window.V3.mountVoice = function (_el, opts) {
    const inner = fakeView(Object.assign(voiceHideCtx.V3.defaults.voiceState(), (opts && opts.initialState) || {}));
    voiceHideView = {
      getState: inner.getState,
      setState: function (patch) { voiceHideWrites += 1; return inner.setState(patch); }
    };
    return voiceHideView;
  };
  const voiceHidePromise = voiceHideCtx.window.V3Business.startPage("voice", {
    openapi: snapshot,
    fetch: bootFetch,
    location: bootLoc,
    document: voiceHideEnv.doc,
    window: voiceHideEnv.win,
    console: voiceHideEnv.console
  });
  await waitFor(function () { return !!(voiceHideCtx.window.__v3Business && voiceHideCtx.window.__v3Business.controller && voiceHideView.getState().connection === "connected"); }, 1000);
  voiceHideEnv.winEvents.pagehide.forEach(function (fn) { fn(); });
  const voiceWritesAtHide = voiceHideWrites;
  pendingVoiceLinks.shift().dispatch("error");
  await waitFor(function () { return pendingVoiceLinks.length === 1; }, 500);
  pendingVoiceLinks.shift().dispatch("error");
  await voiceHidePromise;

  if (
    homeHideCtl._debug.isDestroyed() && homeHideWrites === homeWritesAtHide
    && voiceHideWrites === voiceWritesAtHide
  ) {
    pass("pagehide 设 disposed=true 阻断 home 与 voice 销毁后的迟到样式表失败写入");
  } else {
    fail("pagehide 销毁后仍向视图写入：" + JSON.stringify({
      homeDestroyed: homeHideCtl._debug.isDestroyed(),
      homeDelta: homeHideWrites - homeWritesAtHide,
      voiceDelta: voiceHideWrites - voiceWritesAtHide
    }));
  }

  /* 6. loadOpenApi 失败仍并行完成样式表自愈 */
  const openApiFailEnv = createFakeCssEnv({
    bgVar: "",
    links: [
      { href: "css/theme.css", sheet: null },
      { href: "css/studio.css", sheet: { cssRules: [{ cssText: ".studio{}" }] } }
    ],
    onInsert: function (fresh, _attempt, env) {
      Promise.resolve().then(function () {
        fresh.sheet = { cssRules: [{ cssText: ":root{--c-bg:#f4f6fa}" }] };
        env.setBgVar("#f4f6fa");
        fresh.dispatch("load");
      });
    }
  });
  const openApiFailCtx = loadBusiness({ document: openApiFailEnv.doc });
  let openApiFailView = null;
  openApiFailCtx.window.V3.home = {
    mount: function (_el, init) {
      openApiFailView = fakeView(init);
      return openApiFailView;
    }
  };
  await openApiFailCtx.window.V3Business.startPage("home", {
    fetch: async function () { return jsonRes(500, { ok: false, error: "internal", message: "openapi down" }); },
    location: bootLoc,
    document: openApiFailEnv.doc,
    window: openApiFailEnv.win,
    console: openApiFailEnv.console
  });
  if (
    openApiFailView && openApiFailView.getState().connection === "error"
    && openApiFailEnv.head.children[0].getAttribute("href") === "css/theme.css?_css_retry=1"
    && openApiFailEnv.head.children[0].sheet && openApiFailEnv.head.children[0].sheet.cssRules.length === 1
  ) {
    pass("loadOpenApi 失败时 startPage 仍并行完成样式表原位自愈");
  } else {
    fail("loadOpenApi 失败未完成样式表自愈：" + JSON.stringify({
      conn: openApiFailView && openApiFailView.getState().connection,
      href: openApiFailEnv.head.children[0] && openApiFailEnv.head.children[0].getAttribute("href")
    }));
  }

  /* 3. OCR 预处理预设与预览业务接线单测（map-setup / 防抖合并 / previewReqId & 新图切图 ocrReqId 竞态 / 清图与 destroy 取消 / 非乐观保存锁与失败保草稿 / submitImage preprocess_params 与 ocr_meta） */
  const ocrMapState = B.applyConfig({}, {
    ocr_preprocess_presets: [
      { id: "p1", name: "深底反色", enabled: true, invert: true, contrast: 1.65, brightness: 1.05, sharpen: 0.8, binarize_mode: "off", threshold: 128 }
    ],
    ocr_preprocess_starter_templates: [
      { key: "dark_invert", label: "深底反色", suggested_name: "深底反色终端", params: { invert: true, contrast: 1.65, brightness: 1.05, sharpen: 0.8, binarize_mode: "off", threshold: 128 } }
    ]
  });
  const legalOcrPatch = B.pickLegalConfig({
    ocr_preprocess_presets: ocrMapState.ocr_preprocess_presets,
    ocr_preprocess_starter_templates: [{ key: "dark_invert" }]
  });
  if (
    Array.isArray(ocrMapState.ocr_preprocess_presets) && ocrMapState.ocr_preprocess_presets.length === 1
    && ocrMapState.ocr_preprocess_presets[0].id === "p1"
    && Array.isArray(ocrMapState.ocrStarterTemplates) && ocrMapState.ocrStarterTemplates.length === 1
    && ocrMapState.ocrStarterTemplates[0].key === "dark_invert"
    && Object.prototype.hasOwnProperty.call(legalOcrPatch, "ocr_preprocess_presets")
    && !Object.prototype.hasOwnProperty.call(legalOcrPatch, "ocr_preprocess_starter_templates")
  ) {
    pass("map-setup 映射 ocr_preprocess_presets 与只读 ocrStarterTemplates，且 pickLegalConfig 仅放行可写预设字段");
  } else {
    fail("map-setup OCR 预设字段映射或只读隔离异常：" + JSON.stringify({ mapped: ocrMapState, legal: legalOcrPatch }));
  }

  const ocrWorld = createWorld();
  const ocrTimers = [];
  let ocrNextTimer = 1;
  function ocrFakeSetTimeout(fn, ms) {
    const id = ocrNextTimer++;
    ocrTimers.push({ id: id, fn: fn, ms: ms, cleared: false });
    return id;
  }
  function ocrFakeClearTimeout(id) {
    ocrTimers.forEach(function (t) { if (t.id === id) t.cleared = true; });
  }
  function ocrFlushTimers() {
    const active = ocrTimers.filter(function (t) { return !t.cleared; });
    active.forEach(function (t) { t.cleared = true; t.fn(); });
  }

  const previewDeferreds = [];
  const ocrSubmitDeferreds = [];
  const configDeferreds = [];
  ocrWorld.routes.push({
    method: "POST",
    path: "/api/v1/ocr/preview",
    handler: function (rec) {
      let resolveFn;
      const p = new Promise(function (resolve) { resolveFn = resolve; });
      previewDeferreds.push({ rec: rec, resolve: resolveFn });
      return p;
    }
  });
  ocrWorld.routes.push({
    method: "POST",
    path: "/api/v1/ocr-translate",
    handler: function (rec) {
      let resolveFn;
      const p = new Promise(function (resolve) { resolveFn = resolve; });
      ocrSubmitDeferreds.push({ rec: rec, resolve: resolveFn });
      return p;
    }
  });
  ocrWorld.routes.push({
    method: "POST",
    path: "/api/v1/config",
    handler: function (rec) {
      let resolveFn;
      const p = new Promise(function (resolve) { resolveFn = resolve; });
      configDeferreds.push({ rec: rec, resolve: resolveFn });
      return p;
    }
  });

  const ocrHttp = B.createHttp({ catalog: catalog, context: localCtx, fetch: ocrWorld.fetch, origin: localCtx.origin });
  const ocrCtl = B.createHomeController({
    client: ocrHttp,
    context: localCtx,
    catalog: catalog,
    previewDebounceMs: 180,
    setTimeout: ocrFakeSetTimeout,
    clearTimeout: ocrFakeClearTimeout
  });
  const ocrView = fakeView(catalogCtx.window.V3.defaults.homeState());
  ocrCtl.bind(ocrView);

  /* 3a. 新图 0ms 立即发预览 + 180ms 滑块防抖合并 + 单调 previewReqId 丢弃迟到响应 */
  const imgA = new Blob(["img-a"], { type: "image/png" });
  const imgB = new Blob(["img-b"], { type: "image/png" });
  const pNew1 = ocrCtl.hooks.onRequestPreprocessPreview({
    file: imgA,
    params: { invert: false, contrast: 1.0, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
    immediate: true
  });
  if (previewDeferreds.length === 1 && ocrView.getState().ocr.previewPhase === "loading" && !ocrCtl._debug.previewDebouncePending()) {
    pass("新图选入 0ms 立即发起 POST /api/v1/ocr/preview（不等待防抖）");
  } else {
    fail("新图选入未 0ms 立即发预览：" + JSON.stringify({ count: previewDeferreds.length, phase: ocrView.getState().ocr.previewPhase }));
  }

  /* 连续 3 次同图滑块拖动（immediate:false）应在 180ms 内合并为最后 1 次 */
  const pSlide1 = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgA, params: { invert: false, contrast: 1.2, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 }, immediate: false });
  const pSlide2 = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgA, params: { invert: false, contrast: 1.6, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 }, immediate: false });
  const pSlide3 = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgA, params: { invert: true, contrast: 2.1, brightness: 1.0, sharpen: 0.5, binarize_mode: "otsu", threshold: 128 }, immediate: false });
  const lastPreviewTimer = ocrTimers[ocrTimers.length - 1];
  const callsBeforeFlush = previewDeferreds.length;
  ocrFlushTimers();
  const callsAfterFlush = previewDeferreds.length;
  const flushedParams = JSON.parse(previewDeferreds[1].rec.body.get("preprocess_params"));
  if (
    callsBeforeFlush === 1
    && callsAfterFlush === 2
    && lastPreviewTimer && lastPreviewTimer.ms === 180
    && flushedParams.contrast === 2.1
    && flushedParams.binarize_mode === "otsu"
  ) {
    pass("滑块连续拖动经 180ms 防抖合并为单次预览请求，且仅发送最终参数");
  } else {
    fail("滑块 180ms 防抖合并异常：" + JSON.stringify({ before: callsBeforeFlush, after: callsAfterFlush, ms: lastPreviewTimer && lastPreviewTimer.ms, params: flushedParams }));
  }

  /* 乱序回包：先回最后 1 次（新），再回第 1 次（旧），单调 previewReqId 必须丢弃旧回包 */
  const latestSlideIdx = previewDeferreds.length - 1;
  previewDeferreds[latestSlideIdx].resolve(jsonRes(200, {
    ok: true, media_type: "image/png", image_base64: "TkVX",
    image_width: 640, image_height: 360, preview_width: 640, preview_height: 360, otsu_threshold: 144
  }));
  for (let pi = 1; pi < latestSlideIdx; pi += 1) {
    previewDeferreds[pi].resolve(jsonRes(200, {
      ok: true, media_type: "image/png", image_base64: "TUlE",
      image_width: 10, image_height: 10, preview_width: 10, preview_height: 10, otsu_threshold: null
    }));
  }
  await pSlide3;
  await pSlide1;
  await pSlide2;
  previewDeferreds[0].resolve(jsonRes(200, {
    ok: true, media_type: "image/png", image_base64: "T0xE",
    image_width: 100, image_height: 100, preview_width: 100, preview_height: 100, otsu_threshold: 50
  }));
  await pNew1;
  if (
    ocrView.getState().ocr.previewPhase === "ready"
    && ocrView.getState().ocr.processedPreviewUrl === "data:image/png;base64,TkVX"
    && ocrView.getState().ocr.imageWidth === 640
    && ocrView.getState().ocr.otsuThreshold === 144
  ) {
    pass("单调 previewReqId 丢弃迟到预览响应，新预览图与 Otsu 阈值不被旧包覆盖");
  } else {
    fail("单调 previewReqId 未拦住迟到预览响应：" + JSON.stringify(ocrView.getState().ocr));
  }

  /* 3b. 切换新图同步递增 ocrReqId 丢弃旧图在途 OCR 响应 + submitImage preprocess_params & ocr_meta 映射 */
  const draftParamsObj = { invert: true, contrast: 1.8, brightness: 1.0, sharpen: 0.4, binarize_mode: "off", threshold: 128 };
  const pOldOcr = ocrCtl.hooks.onSubmitImage({ file: imgA, mode: "raw", panel: "ocr", preprocess_params: draftParamsObj });
  const ocrReqIdBeforeSwitch = ocrCtl._debug.getOcrReqId();
  const draftSentRaw = ocrSubmitDeferreds[0].rec.body.get("preprocess_params");
  /* 在旧图 OCR 在途期间切入新图 imgB（触发 0ms 预览并递增 ocrReqId） */
  const pNewFilePreview = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgB, params: draftParamsObj, immediate: true });
  const ocrReqIdAfterSwitch = ocrCtl._debug.getOcrReqId();
  previewDeferreds[previewDeferreds.length - 1].resolve(jsonRes(200, {
    ok: true, media_type: "image/png", image_base64: "SU1HQg==",
    image_width: 320, image_height: 180, preview_width: 320, preview_height: 180, otsu_threshold: null
  }));
  await pNewFilePreview;
  /* 迟到的旧图 OCR 响应到达：必须被丢弃，不得写入 ocr_text 或覆盖 phase */
  ocrSubmitDeferreds[0].resolve(jsonRes(200, {
    ok: true, mode: "raw", text: "OLD IMAGE TEXT", ocr_text: "OLD IMAGE TEXT",
    ocr_meta: { run_mode: "draft_params", matched_preset_id: null, matched_preset_name: "当前调参草稿", matched_rank: null, confidence: 90, elapsed_ms: 12, early_stopped: false, fallback_to_original: false, attempts: [] }
  }));
  await pOldOcr;
  if (
    draftSentRaw && JSON.parse(draftSentRaw).contrast === 1.8
    && ocrReqIdAfterSwitch > ocrReqIdBeforeSwitch
    && ocrView.getState().ocr.ocr_text !== "OLD IMAGE TEXT"
  ) {
    pass("切换新图同步递增 ocrReqId，丢弃上一张图片的迟到在途 OCR 响应");
  } else {
    fail("切新图未递增 ocrReqId 或未丢弃旧图 OCR 响应：" + JSON.stringify({
      before: ocrReqIdBeforeSwitch, after: ocrReqIdAfterSwitch, ocr: ocrView.getState().ocr
    }));
  }

  /* 正常草稿试识别与预设链路识别映射 ocr_meta 与 lastRunKind */
  const pDraftRun = ocrCtl.hooks.onSubmitImage({ file: imgB, mode: "raw", panel: "ocr", preprocess_params: draftParamsObj });
  ocrSubmitDeferreds[1].resolve(jsonRes(200, {
    ok: true, mode: "raw", text: "NEW DRAFT TEXT", ocr_text: "NEW DRAFT TEXT",
    ocr_meta: { run_mode: "draft_params", matched_preset_id: null, matched_preset_name: "当前调参草稿", matched_rank: null, confidence: 95, elapsed_ms: 19, early_stopped: false, fallback_to_original: false, attempts: [{ preset_id: null, label: "当前调参草稿", rank: null, confidence: 95, score: 95, valid_chars: 12, accepted: true }] }
  }));
  await pDraftRun;
  const draftResState = ocrView.getState().ocr;
  const pChainRun = ocrCtl.hooks.onSubmitImage({ file: imgB, mode: "translate", lang_pair: "zh-en", panel: "ocr" });
  const chainHasParams = ocrSubmitDeferreds[2].rec.body.has("preprocess_params");
  ocrSubmitDeferreds[2].resolve(jsonRes(200, {
    ok: true, mode: "translate", lang_pair: "zh-en", source_lang: "en", target_lang: "zh", direction_mode: "auto",
    ocr_text: "CHAIN TEXT", text: "链路译文",
    ocr_meta: { run_mode: "preset_chain", matched_preset_id: "p1", matched_preset_name: "深底反色", matched_rank: 1, confidence: 91, elapsed_ms: 25, early_stopped: true, fallback_to_original: false, attempts: [{ preset_id: "p1", label: "深底反色", rank: 1, confidence: 91, score: 91, valid_chars: 9, accepted: true }] }
  }));
  await pChainRun;
  const chainResState = ocrView.getState().ocr;
  if (
    draftResState.lastRunKind === "draft"
    && draftResState.ocr_meta && draftResState.ocr_meta.run_mode === "draft_params"
    && chainHasParams === false
    && chainResState.lastRunKind === "chain"
    && chainResState.ocr_meta && chainResState.ocr_meta.run_mode === "preset_chain"
    && chainResState.ocr_meta.matched_rank === 1
    && chainResState.text === "链路译文"
  ) {
    pass("submitImage 正确区分草稿 preprocess_params 与预设链路调用，并原样映射 ocr_meta 与 lastRunKind");
  } else {
    fail("submitImage preprocess_params 或 ocr_meta 映射异常：" + JSON.stringify({ draftResState, chainResState, chainHasParams }));
  }

  /* 3c. onClearOcr 取消未触发防抖定时器并丢弃在途预览与 OCR 响应 */
  const previewCallsBeforeClear = previewDeferreds.length;
  const pDebCancel = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgB, params: draftParamsObj, immediate: false });
  const ocrPendingBeforeClear = ocrCtl._debug.previewDebouncePending();
  const pInFlightOcrBeforeClear = ocrCtl.hooks.onSubmitImage({ file: imgB, mode: "raw", panel: "ocr" });
  ocrCtl.hooks.onClearOcr();
  const ocrPendingRightAfterClear = ocrCtl._debug.previewDebouncePending();
  ocrFlushTimers();
  for (let pi = previewCallsBeforeClear; pi < previewDeferreds.length; pi += 1) {
    previewDeferreds[pi].resolve(jsonRes(200, { ok: true, media_type: "image/png", image_base64: "TEFURQ==", image_width: 4, image_height: 4, preview_width: 4, preview_height: 4, otsu_threshold: null }));
  }
  await pDebCancel;
  ocrSubmitDeferreds[3].resolve(jsonRes(200, { ok: true, mode: "raw", text: "LATE AFTER CLEAR", ocr_text: "LATE AFTER CLEAR", ocr_meta: null }));
  await pInFlightOcrBeforeClear;
  if (
    ocrPendingBeforeClear === true
    && ocrPendingRightAfterClear === false
    && ocrCtl._debug.previewDebouncePending() === false
    && previewDeferreds.length === previewCallsBeforeClear
    && ocrView.getState().ocr.phase === "empty"
    && ocrView.getState().ocr.ocr_text === ""
    && ocrView.getState().ocr.processedPreviewUrl === ""
  ) {
    pass("onClearOcr 立即取消待触发防抖定时器、推进请求 ID 并丢弃迟到 OCR 响应");
  } else {
    fail("onClearOcr 取消竞态异常：" + JSON.stringify({
      ocrPendingBeforeClear, ocrPendingRightAfterClear, pendingAfter: ocrCtl._debug.previewDebouncePending(),
      previewBefore: previewCallsBeforeClear, previewAfter: previewDeferreds.length, ocr: ocrView.getState().ocr
    }));
  }

  /* 3d. 非乐观 saveOcrPresets：在途写锁、重入保护、400 失败保旧列表并回显后端中文错、成功仅按回读更新 */
  ocrView.setState({
    ocr_preprocess_presets: [
      { id: "orig-1", name: "原预设1", enabled: true, invert: true, contrast: 1.5, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 }
    ]
  });
  const pSaveFail = ocrCtl.hooks.onSaveOcrPresets({
    presets: [
      { id: "orig-1", name: "原预设1", enabled: true, invert: true, contrast: 1.5, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
      { id: "p-2", name: "预设2", enabled: true, invert: false, contrast: 2.0, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
      { id: "p-3", name: "预设3", enabled: true, invert: false, contrast: 2.0, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
      { id: "p-4", name: "超限第4条", enabled: true, invert: false, contrast: 2.0, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 }
    ],
    action: "create",
    targetId: "p-4"
  });
  /* 在途锁期间再次派发应被忽略且不乐观修改列表 */
  const pSaveReentrant = ocrCtl.hooks.onSaveOcrPresets({ presets: [], action: "delete", targetId: "orig-1" });
  const midSaveState = ocrView.getState();
  const midConfigCalls = configDeferreds.length;
  configDeferreds[0].resolve(jsonRes(400, { ok: false, error: "invalid_request", message: "最多只能同时启用 3 个 OCR 预处理预设" }));
  for (let ci = 1; ci < configDeferreds.length; ci += 1) {
    configDeferreds[ci].resolve(jsonRes(200, { ocr_preprocess_presets: [] }));
  }
  await pSaveFail;
  await pSaveReentrant;
  const afterFailState = ocrView.getState();
  if (
    midSaveState.ocr.savingPresets === true
    && midSaveState.ocr_preprocess_presets.length === 1
    && midConfigCalls === 1
    && afterFailState.ocr.savingPresets === false
    && afterFailState.ocr.presetsSaved === false
    && afterFailState.ocr.presetsSaveError.indexOf("最多只能同时启用 3 个") >= 0
    && afterFailState.ocr_preprocess_presets.length === 1
    && afterFailState.ocr_preprocess_presets[0].id === "orig-1"
  ) {
    pass("saveOcrPresets 严格非乐观写锁：在途阻断重入、400 失败保留原列表并回显后端中文错误");
  } else {
    fail("saveOcrPresets 非乐观写锁或失败回滚异常：" + JSON.stringify({ mid: midSaveState.ocr, after: afterFailState.ocr, presets: afterFailState.ocr_preprocess_presets, calls: midConfigCalls }));
  }

  const pSaveOk = ocrCtl.hooks.onSaveOcrPresets({
    presets: [
      { id: "orig-1", name: "原预设1", enabled: true, invert: true, contrast: 1.5, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
      { id: "p-2", name: "低对比灰字", enabled: true, invert: false, contrast: 2.3, brightness: 1.15, sharpen: 1.5, binarize_mode: "otsu", threshold: 128 }
    ],
    action: "create",
    targetId: "p-2"
  });
  configDeferreds[configDeferreds.length - 1].resolve(jsonRes(200, {
    ocr_preprocess_presets: [
      { id: "orig-1", name: "原预设1", enabled: true, invert: true, contrast: 1.5, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 },
      { id: "p-2", name: "低对比灰字（服务端权威）", enabled: true, invert: false, contrast: 2.3, brightness: 1.15, sharpen: 1.5, binarize_mode: "otsu", threshold: 128 }
    ]
  }));
  await pSaveOk;
  if (
    ocrView.getState().ocr.savingPresets === false
    && ocrView.getState().ocr.presetsSaved === true
    && ocrView.getState().ocr.presetsSaveError === ""
    && ocrView.getState().ocr_preprocess_presets.length === 2
    && ocrView.getState().ocr_preprocess_presets[1].name === "低对比灰字（服务端权威）"
  ) {
    pass("saveOcrPresets 成功后仅以服务端权威配置刷新 ocr_preprocess_presets 并置 presetsSaved=true");
  } else {
    fail("saveOcrPresets 成功回读刷新异常：" + JSON.stringify({ ocr: ocrView.getState().ocr, presets: ocrView.getState().ocr_preprocess_presets }));
  }

  /* 3e. destroy() 清理挂起预览防抖定时器，防止销毁后误发请求 */
  const pBeforeDestroy = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgB, params: draftParamsObj, immediate: true });
  previewDeferreds[previewDeferreds.length - 1].resolve(jsonRes(200, { ok: true, media_type: "image/png", image_base64: "T0s=", image_width: 4, image_height: 4, preview_width: 4, preview_height: 4, otsu_threshold: null }));
  await pBeforeDestroy;
  const callsBeforeDestroy = previewDeferreds.length;
  const pDebDestroy = ocrCtl.hooks.onRequestPreprocessPreview({ file: imgB, params: draftParamsObj, immediate: false });
  ocrCtl.destroy();
  const pendingRightAfterDestroy = ocrCtl._debug.previewDebouncePending();
  ocrFlushTimers();
  for (let pi = callsBeforeDestroy; pi < previewDeferreds.length; pi += 1) {
    previewDeferreds[pi].resolve(jsonRes(200, { ok: true, media_type: "image/png", image_base64: "TEFURQ==", image_width: 4, image_height: 4, preview_width: 4, preview_height: 4, otsu_threshold: null }));
  }
  await pDebDestroy;
  if (pendingRightAfterDestroy === false && ocrCtl._debug.previewDebouncePending() === false && previewDeferreds.length === callsBeforeDestroy) {
    pass("home-controller.destroy() 同步清理 ocrActions 防抖定时器，销毁后零误发请求");
  } else {
    fail("destroy() 未清理 ocrActions 预览防抖定时器：" + JSON.stringify({ pendingRightAfterDestroy, before: callsBeforeDestroy, after: previewDeferreds.length }));
  }
}

function runAsgi() {
  const python = path.join(repo, ".venv/bin/python");
  if (!fs.existsSync(python)) {
    fail("未找到项目 .venv/bin/python，隔离 ASGI 集成无法运行");
    return Promise.resolve();
  }
  const tmp = path.join(logDir, "business");
  fs.mkdirSync(tmp, { recursive: true });
  const metaPath = path.join(tmp, "asgi-meta.json");
  const sandboxRoot = path.join(tmp, "sandbox-" + process.pid);
  fs.mkdirSync(sandboxRoot, { recursive: true });
  if (fs.existsSync(metaPath)) fs.unlinkSync(metaPath);
  const child = spawn(python, [path.join(repo, "tests/ui/business/asgi_sandbox.py"), metaPath, sandboxRoot], {
    cwd: repo,
    stdio: ["ignore", "pipe", "pipe"]
  });
  let stderr = "";
  child.stderr.on("data", function (buf) { stderr += buf.toString(); });
  child.stdout.on("data", function (buf) { stderr += buf.toString(); });
  const started = Date.now();
  let listening = false;
  return new Promise(function (resolve) {
    let settled = false;
    function settle(okMsg, errMsg) {
      if (settled) return;
      settled = true;
      if (errMsg) fail(errMsg + (stderr ? " / " + stderr.slice(0, 400) : ""));
      else pass(okMsg);
      /* 成功与失败都经过这里：删掉本次创建的沙箱根目录（明确路径），不在 .pp/tmp 里逐次累积 */
      try {
        fs.rmSync(sandboxRoot, { recursive: true, force: true });
      } catch (err) {
        fail("清理 ASGI 沙箱目录失败：" + sandboxRoot + "：" + err.message);
      }
      resolve();
    }
    function processGone() {
      return child.exitCode != null || child.signalCode != null;
    }
    function finish(okMsg, errMsg) {
      if (settled) return;
      function afterStop() {
        if (okMsg && !processGone()) {
          settle(null, "已发出停止信号，但 PID " + child.pid + " 仍在运行");
          return;
        }
        settle(okMsg, errMsg);
      }
      if (processGone()) {
        afterStop();
        return;
      }
      try { if (child.pid) process.kill(child.pid, "SIGTERM"); } catch (e) {}
      const killer = setTimeout(function () {
        try { if (!processGone() && child.pid) process.kill(child.pid, "SIGKILL"); } catch (e) {}
      }, 1000);
      child.once("exit", function () {
        clearTimeout(killer);
        afterStop();
      });
    }
    function waitMeta() {
      if (settled || listening) return;
      if (fs.existsSync(metaPath)) {
        let meta = null;
        try { meta = JSON.parse(fs.readFileSync(metaPath, "utf8")); } catch (err) { meta = null; }
        if (meta && meta.ready === true && meta.port && meta.pid) {
          listening = true;
          return probe(meta);
        }
      }
      if (Date.now() - started > 8000) {
        return finish(null, "ASGI 沙箱未能在时限内进入监听（ready meta 未出现）");
      }
      setTimeout(waitMeta, 50);
    }
    function probe(meta) {
      const origin = "http://127.0.0.1:" + meta.port;
      const catalog = (function () {
        const ctx = loadBusiness();
        return ctx.window.V3Business.createCatalog(JSON.parse(fs.readFileSync(snapshotPath, "utf8")));
      })();
      const context = { kind: "local", allowFetch: true, origin: origin, canCallLocal: function () { return true; }, canCallLanToken: function () { return true; }, getToken: function () { return null; }, tokenPresent: false };
      const Bmod = loadBusiness();
      const client = Bmod.window.V3Business.createHttp({ catalog: catalog, context: context, fetch: fetch, origin: origin });
      Promise.resolve().then(async function () {
        const check = await client.call("setup_check_api_setup_check_get");
        if (!check || check.ok !== true || !check.asr || !check.ollama) throw new Error("setup_check 未返回真实 SetupCheckResponse");
        if (check.asr.status === "ready") throw new Error("沙箱默认不应把识别标为已就绪");
        if (!Array.isArray(check.capabilities)) throw new Error("setup_check 缺少 capabilities");
        const idleSteps = (check.install_all && check.install_all.steps || []).map(function (x) { return x.kind + ":" + x.state; }).join(",");
        if (!check.install_all || check.install_all.state !== "idle" || idleSteps !== "ollama_install:pending,wait_ollama:pending,ollama_pull:pending,asr_runtime_install:pending,asr_download:pending") {
          throw new Error("setup_check.install_all 未从空闲五步开始：" + JSON.stringify(check.install_all));
        }
        const snapRes = await fetch(origin + "/openapi.snapshot.json");
        if (!snapRes.ok) throw new Error("页面快照 /openapi.snapshot.json HTTP " + snapRes.status);
        const snapDoc = await snapRes.json();
        const expectedSnap = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));
        if (JSON.stringify(Object.keys(snapDoc.paths || {}).sort()) !== JSON.stringify(Object.keys(expectedSnap.paths || {}).sort())) {
          throw new Error("发布快照 paths 与 static/v3/openapi.json 不一致，条数 " + Object.keys(snapDoc.paths || {}).length);
        }
        const liveOpenapi = await fetch(origin + "/openapi.json");
        if (!liveOpenapi.ok) throw new Error("运行时 openapi.json HTTP " + liveOpenapi.status);
        const openapi = await liveOpenapi.json();
        if (Object.keys(openapi.paths).length < 24) throw new Error("运行时 openapi paths=" + Object.keys(openapi.paths).length);
        const pages = ["/", "/setup", "/voice"];
        const htmlByPage = {};
        for (const page of pages) {
          const res = await fetch(origin + page);
          if (!res.ok) throw new Error("新页面路由 " + page + " HTTP " + res.status);
          const html = await res.text();
          htmlByPage[page] = html;
          if (html.indexOf("js/business/bootstrap.js") < 0 && html.indexOf("/js/business/bootstrap.js") < 0) {
            throw new Error("页面 " + page + " 未加载业务脚本");
          }
        }
        if (htmlByPage["/"] !== htmlByPage["/setup"]) {
          throw new Error("GET / 与 GET /setup 应返回完全一致的 index.html");
        }
        for (const reqScript of ["js/confirm.js", "js/ocr-presets.js", "js/ocr-panel.js", "js/logs-panel.js", "js/about-panel.js", "js/business/logs-actions.js", "js/business/ocr-actions.js"]) {
          if (htmlByPage["/"].indexOf(reqScript) < 0) {
            throw new Error("GET / index.html 缺少脚本 " + reqScript);
          }
        }
        const jsRes = await fetch(origin + "/js/business/bootstrap.js");
        if (!jsRes.ok) throw new Error("/js 资源 HTTP " + jsRes.status);
        const logsActRes = await fetch(origin + "/js/business/logs-actions.js");
        if (!logsActRes.ok) throw new Error("/js/business/logs-actions.js HTTP " + logsActRes.status);
        const ocrActRes = await fetch(origin + "/js/business/ocr-actions.js");
        if (!ocrActRes.ok) throw new Error("/js/business/ocr-actions.js HTTP " + ocrActRes.status);
        const cssRes = await fetch(origin + "/css/theme.css");
        if (!cssRes.ok) throw new Error("/css 资源 HTTP " + cssRes.status);
        const cssCc = String(cssRes.headers.get("cache-control") || "").toLowerCase();
        const cssEtag = cssRes.headers.get("etag") || "";
        if (cssCc.indexOf("no-cache") < 0 || !cssEtag) {
          throw new Error("/css/theme.css 缺少 Cache-Control: no-cache 或 ETag：" + JSON.stringify({ cc: cssCc, etag: cssEtag }));
        }
        const css304Res = await fetch(origin + "/css/theme.css", { headers: { "If-None-Match": cssEtag } });
        if (css304Res.status !== 304) {
          throw new Error("/css/theme.css 携带 If-None-Match 应返回 304，实际 " + css304Res.status);
        }
        const cssRetryRes = await fetch(origin + "/css/theme.css?_css_retry=1");
        const cssRetryCc = String(cssRetryRes.headers.get("cache-control") || "").toLowerCase();
        const cssRetryText = await cssRetryRes.text();
        if (cssRetryRes.status !== 200 || cssRetryCc.indexOf("no-cache") < 0 || cssRetryText.indexOf("--c-bg") < 0) {
          throw new Error("/css/theme.css?_css_retry=1 响应异常：" + JSON.stringify({ status: cssRetryRes.status, cc: cssRetryCc, hasBg: cssRetryText.indexOf("--c-bg") >= 0 }));
        }
        /* 设置页业务层（home-controller + setup-actions + logs-actions）经真实路由驱动真实一键装齐、日志脱敏与关于信息 */
        const since = "2026-11-20T00:00:00+00:00";
        const reported = await client.call("report_components_api_v1_components_post", { body: { components: [
          { name: "ollama", state: "running", since: since },
          { name: "asr", state: "stopped", reason: "沙箱：识别服务已停止", failures: 5, since: since },
          { name: "server", state: "running", since: since }
        ] } });
        if (!reported || reported.ok !== true) throw new Error("组件上报未被接受");
        const asgiClipTexts = [];
        const asgiClip = Bmod.window.V3Business.createClipboard({
          clipboard: {
            writeText: function (t) { asgiClipTexts.push(String(t)); return Promise.resolve(); }
          }
        });
        const setupCtl = Bmod.window.V3Business.createHomeController({ client: client, context: context, catalog: catalog, clipboard: asgiClip, pollInterval: 20 });
        const setupView = fakeView(Bmod.window.V3.defaults.homeState());
        setupCtl.bind(setupView);
        await setupCtl.start();
        if (setupView.getState().log_max_lines !== 2000 || setupView.getState().logs.phase !== "idle" || setupView.getState().about.phase !== "idle") {
          throw new Error("隔离 ASGI start() 后 log_max_lines 或 logs/about 懒加载初始态异常：" + JSON.stringify({
            cap: setupView.getState().log_max_lines,
            logsPhase: setupView.getState().logs.phase,
            aboutPhase: setupView.getState().about.phase
          }));
        }
        const comps = setupView.getState().components || [];
        if (setupView.getState().connection !== "connected" || comps.map(function (c) { return c.name + ":" + c.state; }).join(",") !== "ollama:running,asr:stopped,server:running") {
          throw new Error("设置页未读到上报的组件状态：" + JSON.stringify(comps));
        }
        await setupCtl.hooks.onInstallAll();
        const installed = await waitFor(function () {
          const ia = setupView.getState().installAll;
          return ia && ia.state === "done" && setupCtl._debug.polling() === false;
        }, 8000);
        const firstRun = ((setupView.getState().installAll || {}).steps || []).map(function (x) { return x.state; }).join(",");
        if (!installed || firstRun !== "done,done,done,done,done") throw new Error("一键装齐未经真实顺序任务完成：" + JSON.stringify(setupView.getState().installAll));
        await setupCtl.hooks.onInstallAll();
        const rerun = await waitFor(function () {
          const ia = setupView.getState().installAll;
          return ia.state === "done" && ia.steps[0].state === "skipped" && setupCtl._debug.polling() === false;
        }, 8000);
        const secondRun = setupView.getState().installAll.steps.map(function (x) { return x.state; }).join(",");
        if (!rerun || secondRun !== "skipped,done,skipped,skipped,skipped") throw new Error("再次装齐应按事实跳过已具备的步：" + secondRun);
        await setupCtl.hooks.onRetryComponent("asr");
        if (!/已请求重试/.test(setupView.getState().statusNotice || "")) throw new Error("组件重试未被服务端接受：" + setupView.getState().statusNotice);
        await setupCtl.hooks.onRetryComponent("nope");
        if (!/未找到/.test(setupView.getState().statusNotice || "")) throw new Error("未知组件重试未呈现 404：" + setupView.getState().statusNotice);
        await setupCtl.hooks.onToggleAutostart(false);
        if (setupView.getState().autostart_enabled !== false) throw new Error("开机自启未经 config 回读为 false");
        if (setupView.getState().hotkey_annotate !== "ctrl+alt+a") {
          throw new Error("隔离 ASGI 默认 hotkey_annotate 未映射为 ctrl+alt+a：" + setupView.getState().hotkey_annotate);
        }
        await setupCtl.hooks.onSaveHotkeys({
          hotkey_voice: "ctrl+alt+z",
          hotkey_screenshot: "ctrl+alt+x",
          hotkey_ocr: "ctrl+alt+o",
          hotkey_repeat: "ctrl+alt+r",
          hotkey_annotate: "ctrl+alt+b"
        });
        if (setupView.getState().hotkey_annotate !== "ctrl+alt+b") {
          throw new Error("隔离 ASGI 保存 hotkey_annotate 未回读：" + setupView.getState().hotkey_annotate);
        }
        let dupRejected = false;
        try {
          await setupCtl.hooks.onSaveHotkeys({
            hotkey_voice: "ctrl+alt+z",
            hotkey_screenshot: "ctrl+alt+x",
            hotkey_ocr: "ctrl+alt+o",
            hotkey_repeat: "ctrl+alt+r",
            hotkey_annotate: "ctrl+alt+z"
          });
        } catch (err) {
          dupRejected = true;
        }
        if (!dupRejected || !setupView.getState().hotkeySaveError) {
          throw new Error("隔离 ASGI 第五键冲突未拒保存或未写 hotkeySaveError：" + JSON.stringify({ dupRejected: dupRejected, err: setupView.getState().hotkeySaveError }));
        }
        /* 真 HTTP 往返验证新翻译契约：config / translate(auto & manual swap) / ocr-translate(translate & raw) */
        if (setupView.getState().translate_lang_pair !== "zh-en" || setupView.getState().translate_auto_enabled !== true) {
          throw new Error("隔离 ASGI 默认翻译配置未映射：" + JSON.stringify({ pair: setupView.getState().translate_lang_pair, auto: setupView.getState().translate_auto_enabled }));
        }
        await setupCtl.hooks.onToggleAutoTranslate(false);
        if (setupView.getState().translate_auto_enabled !== false || setupView.getState().translateSettingsSaved !== true) {
          throw new Error("隔离 ASGI 保存 translate_auto_enabled=false 未回读");
        }
        await setupCtl.hooks.onToggleAutoTranslate(true);
        await setupCtl.hooks.onChangeLangPair("zh-en");
        if (setupView.getState().translate_lang_pair !== "zh-en" || setupView.getState().translate_auto_enabled !== true) {
          throw new Error("隔离 ASGI 保存 translate_lang_pair 未回读");
        }
        const legacyCfgRes = await fetch(origin + "/api/v1/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ translate_target_lang: "en" })
        });
        if (legacyCfgRes.status !== 400) {
          throw new Error("隔离 ASGI 传入旧键 translate_target_lang 应返回 400，实际 " + legacyCfgRes.status);
        }
        await setupCtl.hooks.onTranslateText({ text: "本地运行 Ollama 翻译测试", lang_pair: "zh-en", trigger: "manual" });
        const asgiAutoTr = setupView.getState().translate;
        if (asgiAutoTr.phase !== "result" || asgiAutoTr.source_lang !== "zh" || asgiAutoTr.target_lang !== "en" || asgiAutoTr.direction_mode !== "auto" || asgiAutoTr.text !== "[zh->en]本地运行 Ollama 翻译测试") {
          throw new Error("隔离 ASGI 自动翻译契约异常：" + JSON.stringify(asgiAutoTr));
        }
        await setupCtl.hooks.onSwapDirection({ text: "本地运行 Ollama 翻译测试", lang_pair: "zh-en" });
        const asgiManualTr = setupView.getState().translate;
        if (asgiManualTr.phase !== "result" || asgiManualTr.source_lang !== "en" || asgiManualTr.target_lang !== "zh" || asgiManualTr.direction_mode !== "manual" || !asgiManualTr.manualOverride || asgiManualTr.text !== "[en->zh]本地运行 Ollama 翻译测试") {
          throw new Error("隔离 ASGI 互换锁定翻译契约异常：" + JSON.stringify(asgiManualTr));
        }
        await setupCtl.hooks.onSwapDirection({ text: "本地运行 Ollama 翻译测试", lang_pair: "zh-en" });
        const asgiRestoredTr = setupView.getState().translate;
        if (asgiRestoredTr.phase !== "result" || asgiRestoredTr.direction_mode !== "auto" || asgiRestoredTr.manualOverride !== null || asgiRestoredTr.source_lang !== "zh" || asgiRestoredTr.target_lang !== "en") {
          throw new Error("隔离 ASGI 再次点击 ⇄ 未恢复自动方向：" + JSON.stringify(asgiRestoredTr));
        }
        const asgiImg = new Blob(["Hello ASGI OCR"], { type: "text/plain" });
        await setupCtl.hooks.onSubmitImage({ file: asgiImg, mode: "translate", lang_pair: "zh-en", panel: "translate" });
        const asgiImgTr = setupView.getState().translate;
        if (asgiImgTr.phase !== "result" || asgiImgTr.ocr_text !== "Hello ASGI OCR" || asgiImgTr.text !== "[en->zh]Hello ASGI OCR" || asgiImgTr.source_lang !== "en" || asgiImgTr.target_lang !== "zh" || asgiImgTr.direction_mode !== "auto") {
          throw new Error("隔离 ASGI 图片翻译契约异常：" + JSON.stringify(asgiImgTr));
        }
        await setupCtl.hooks.onSubmitImage({ file: asgiImg, mode: "raw", panel: "ocr" });
        const asgiImgRaw = setupView.getState().ocr;
        if (asgiImgRaw.phase !== "result" || asgiImgRaw.mode !== "raw" || asgiImgRaw.ocr_text !== "Hello ASGI OCR" || asgiImgRaw.text !== "" || asgiImgRaw.source_lang !== null || !asgiImgRaw.ocr_meta || asgiImgRaw.ocr_meta.run_mode !== "original") {
          throw new Error("隔离 ASGI 纯 OCR 契约异常：" + JSON.stringify(asgiImgRaw));
        }
        /* 真 HTTP 往返验证 OCR 预处理预设与预览闭环：starterTemplates / POST /api/v1/ocr/preview / 保存 3 预设 / 第 4 条 400 拦截 / 草稿试识别与预设链路 */
        const asgiStarters = setupView.getState().ocrStarterTemplates;
        if (
          !Array.isArray(setupView.getState().ocr_preprocess_presets)
          || setupView.getState().ocr_preprocess_presets.length !== 0
          || !Array.isArray(asgiStarters)
          || asgiStarters.map(function (t) { return t.key; }).join(",") !== "dark_invert,low_contrast,strong_binary"
        ) {
          throw new Error("隔离 ASGI 初始 OCR 预设或起步模板未映射：" + JSON.stringify({
            presets: setupView.getState().ocr_preprocess_presets,
            starters: asgiStarters
          }));
        }
        const demoCtx = { window: {} };
        vm.createContext(demoCtx);
        vm.runInContext(fs.readFileSync(path.join(__dirname, "fixtures/demo-state.js"), "utf8"), demoCtx, { filename: "demo-state.js" });
        const pngB64 = demoCtx.window.V3Fixtures.SAMPLE_PNG_DATA_URL.split(",")[1];
        const realPngBlob = new Blob([Buffer.from(pngB64, "base64")], { type: "image/png" });
        await setupCtl.hooks.onRequestPreprocessPreview({
          file: realPngBlob,
          params: { invert: true, contrast: 1.8, brightness: 1.0, sharpen: 0.5, binarize_mode: "otsu", threshold: 128 },
          immediate: true
        });
        const asgiPreview = setupView.getState().ocr;
        if (
          asgiPreview.previewPhase !== "ready"
          || String(asgiPreview.processedPreviewUrl || "").indexOf("data:image/png;base64,") !== 0
          || asgiPreview.imageWidth !== 4
          || asgiPreview.imageHeight !== 4
          || !(asgiPreview.previewWidth > 0 && asgiPreview.previewHeight > 0)
          || typeof asgiPreview.otsuThreshold !== "number"
        ) {
          throw new Error("隔离 ASGI POST /api/v1/ocr/preview 响应映射异常：" + JSON.stringify({
            phase: asgiPreview.previewPhase,
            urlPrefix: String(asgiPreview.processedPreviewUrl || "").slice(0, 32),
            w: asgiPreview.imageWidth,
            h: asgiPreview.imageHeight,
            pw: asgiPreview.previewWidth,
            ph: asgiPreview.previewHeight,
            otsu: asgiPreview.otsuThreshold,
            err: asgiPreview.previewError
          }));
        }
        const threePresets = [
          { id: "asgi-p1", name: "深底反色终端", enabled: true, invert: true, contrast: 1.65, brightness: 1.05, sharpen: 0.8, binarize_mode: "off", threshold: 128 },
          { id: "asgi-p2", name: "低对比灰字增强", enabled: true, invert: false, contrast: 2.3, brightness: 1.15, sharpen: 1.6, binarize_mode: "off", threshold: 128 },
          { id: "asgi-p3", name: "强黑白字幕二值化", enabled: true, invert: false, contrast: 1.8, brightness: 1.0, sharpen: 1.0, binarize_mode: "otsu", threshold: 128 }
        ];
        await setupCtl.hooks.onSaveOcrPresets({ presets: threePresets, action: "create", targetId: "asgi-p3" });
        const cfgAfter3Presets = await client.call("get_config_api_v1_config_get");
        if (
          setupView.getState().ocr.savingPresets !== false
          || setupView.getState().ocr.presetsSaved !== true
          || setupView.getState().ocr.presetsSaveError !== ""
          || setupView.getState().ocr_preprocess_presets.length !== 3
          || !Array.isArray(cfgAfter3Presets.ocr_preprocess_presets)
          || cfgAfter3Presets.ocr_preprocess_presets.length !== 3
        ) {
          throw new Error("隔离 ASGI 保存 3 条启用预设未持久化回读：" + JSON.stringify({
            ocr: setupView.getState().ocr,
            statePresets: setupView.getState().ocr_preprocess_presets,
            cfgPresets: cfgAfter3Presets.ocr_preprocess_presets
          }));
        }
        /* 第 4 条启用预设必须被真实后端 400 拒绝，且前端非乐观保留原 3 条并呈现具体中文报错 */
        await setupCtl.hooks.onSaveOcrPresets({
          presets: threePresets.concat([
            { id: "asgi-p4", name: "违规第四条", enabled: true, invert: false, contrast: 1.2, brightness: 1.0, sharpen: 0.0, binarize_mode: "off", threshold: 128 }
          ]),
          action: "create",
          targetId: "asgi-p4"
        });
        if (
          setupView.getState().ocr.savingPresets !== false
          || setupView.getState().ocr.presetsSaved !== false
          || String(setupView.getState().ocr.presetsSaveError || "").indexOf("3") < 0
          || setupView.getState().ocr_preprocess_presets.length !== 3
        ) {
          throw new Error("隔离 ASGI 第 4 条启用预设 400 拦截或非乐观保留异常：" + JSON.stringify({
            ocr: setupView.getState().ocr,
            presetsLen: setupView.getState().ocr_preprocess_presets.length
          }));
        }
        /* 草稿试识别（preprocess_params）与已启用预设链路识别（无 preprocess_params） */
        await setupCtl.hooks.onSubmitImage({
          file: asgiImg,
          mode: "raw",
          panel: "ocr",
          preprocess_params: { invert: true, contrast: 1.9, brightness: 1.0, sharpen: 0.5, binarize_mode: "off", threshold: 128 }
        });
        const asgiDraftOcr = setupView.getState().ocr;
        if (
          asgiDraftOcr.phase !== "result"
          || asgiDraftOcr.lastRunKind !== "draft"
          || !asgiDraftOcr.ocr_meta
          || asgiDraftOcr.ocr_meta.run_mode !== "draft_params"
        ) {
          throw new Error("隔离 ASGI 草稿试识别 ocr_meta 异常：" + JSON.stringify(asgiDraftOcr));
        }
        await setupCtl.hooks.onSubmitImage({ file: asgiImg, mode: "translate", lang_pair: "zh-en", panel: "ocr" });
        const asgiChainOcr = setupView.getState().ocr;
        if (
          asgiChainOcr.phase !== "result"
          || asgiChainOcr.lastRunKind !== "chain"
          || asgiChainOcr.text !== "[en->zh]Hello ASGI OCR"
          || !asgiChainOcr.ocr_meta
          || asgiChainOcr.ocr_meta.run_mode !== "preset_chain"
          || asgiChainOcr.ocr_meta.matched_rank !== 1
          || !Array.isArray(asgiChainOcr.ocr_meta.attempts)
          || asgiChainOcr.ocr_meta.attempts[0].label !== "深底反色终端"
        ) {
          throw new Error("隔离 ASGI 启用预设链路 ocr_meta 异常：" + JSON.stringify(asgiChainOcr));
        }
        await setupCtl.hooks.onSaveOcrPresets({ presets: [], action: "delete", targetId: "asgi-p1" });
        if (setupView.getState().ocr_preprocess_presets.length !== 0) {
          throw new Error("隔离 ASGI 清空预设未回读为空列表");
        }
        /* 真 HTTP 往返验证主题持久化与麦克风选择契约：/api/v1/audio-inputs + ui_theme / audio_input_device */
        const initAsgiState = setupView.getState();
        if (
          initAsgiState.theme !== "light"
          || initAsgiState.audio_input_device !== ""
          || !initAsgiState.audioInputs
          || initAsgiState.audioInputs.defaultDevice !== "Sandbox USB Mic"
          || initAsgiState.audioInputs.devices.length !== 2
          || initAsgiState.audioInputs.devices[0].isDefault !== true
          || initAsgiState.audioInputError !== ""
        ) {
          throw new Error("隔离 ASGI 初始主题或音频输入列表未对齐：" + JSON.stringify({
            theme: initAsgiState.theme,
            dev: initAsgiState.audio_input_device,
            inputs: initAsgiState.audioInputs,
            err: initAsgiState.audioInputError
          }));
        }
        await setupCtl.hooks.onToggleTheme({ theme: "dark" });
        const cfgAfterDark = await client.call("get_config_api_v1_config_get");
        if (setupView.getState().theme !== "dark" || cfgAfterDark.ui_theme !== "dark") {
          throw new Error("隔离 ASGI 切换暗色未持久化：" + JSON.stringify({ state: setupView.getState().theme, cfg: cfgAfterDark.ui_theme }));
        }
        await setupCtl.hooks.onToggleTheme({ theme: "light" });
        const cfgAfterLight = await client.call("get_config_api_v1_config_get");
        if (setupView.getState().theme !== "light" || cfgAfterLight.ui_theme !== "light") {
          throw new Error("隔离 ASGI 切回亮色未持久化：" + JSON.stringify({ state: setupView.getState().theme, cfg: cfgAfterLight.ui_theme }));
        }
        const badThemeRes = await fetch(origin + "/api/v1/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ui_theme: "sepia" })
        });
        if (badThemeRes.status !== 400) {
          throw new Error("隔离 ASGI 非法 ui_theme 应返回 400，实际 " + badThemeRes.status);
        }
        await setupCtl.hooks.onRefreshAudioInputs();
        if (!setupView.getState().audioInputs || setupView.getState().audioInputs.devices.length !== 2 || setupView.getState().audioInputError !== "") {
          throw new Error("隔离 ASGI 刷新音频输入列表异常：" + JSON.stringify(setupView.getState().audioInputs));
        }
        await setupCtl.hooks.onChangeAudioInput("Sandbox Headset Mic");
        const cfgAfterMic = await client.call("get_config_api_v1_config_get");
        if (setupView.getState().audio_input_device !== "Sandbox Headset Mic" || setupView.getState().audioInputSaved !== true || cfgAfterMic.audio_input_device !== "Sandbox Headset Mic") {
          throw new Error("隔离 ASGI 保存指定麦克风未回读：" + JSON.stringify({ state: setupView.getState().audio_input_device, saved: setupView.getState().audioInputSaved, cfg: cfgAfterMic.audio_input_device }));
        }
        await setupCtl.hooks.onChangeAudioInput("");
        const cfgAfterMicDefault = await client.call("get_config_api_v1_config_get");
        if (setupView.getState().audio_input_device !== "" || cfgAfterMicDefault.audio_input_device !== "") {
          throw new Error("隔离 ASGI 切回跟随系统默认（空串）未持久化：" + JSON.stringify({ state: setupView.getState().audio_input_device, cfg: cfgAfterMicDefault.audio_input_device }));
        }
        /* 真 HTTP 往返验证运行日志与关于页面：logs 9 源 / app.log 真脱敏 / log_max_lines=500 / DELETE server.log / diagnostic-summary / about */
        await setupCtl.hooks.onSelectPanel({ panel: "logs" });
        const asgiLogs = setupView.getState().logs;
        const asgiSourceNames = (asgiLogs.sources || []).map(function (s) { return s.name; });
        const asgiAppJoined = (asgiLogs.entries || []).map(function (e) { return e.text; }).join("\n");
        if (
          asgiLogs.phase !== "ready"
          || asgiLogs.activeFile !== "app.log"
          || asgiSourceNames.length !== 9
          || asgiSourceNames.indexOf("setup-asr_download.log") < 0
          || asgiLogs.entries.length !== 3
          || asgiAppJoined.indexOf("ui-business-sandbox-token") >= 0
          || asgiAppJoined.indexOf("alice:secret") >= 0
          || asgiAppJoined.indexOf("***") < 0
        ) {
          throw new Error("隔离 ASGI 运行日志读取或脱敏校验失败：" + JSON.stringify({
            phase: asgiLogs.phase,
            sources: asgiSourceNames,
            entries: asgiLogs.entries
          }));
        }
        await setupCtl.hooks.onChangeLogMaxLines({ log_max_lines: 500 });
        const cfgAfterCap500 = await client.call("get_config_api_v1_config_get");
        if (
          setupView.getState().log_max_lines !== 500
          || setupView.getState().logs.log_max_lines !== 500
          || setupView.getState().logs.max_lines !== 500
          || setupView.getState().logs.capSaved !== true
          || cfgAfterCap500.log_max_lines !== 500
        ) {
          throw new Error("隔离 ASGI 保存 log_max_lines=500 或立刻重读未对齐：" + JSON.stringify({
            stateCap: setupView.getState().log_max_lines,
            logs: setupView.getState().logs,
            cfg: cfgAfterCap500.log_max_lines
          }));
        }
        await setupCtl.hooks.onSelectLogSource({ name: "server.log" });
        if (setupView.getState().logs.activeFile !== "server.log" || setupView.getState().logs.entries.length !== 1) {
          throw new Error("隔离 ASGI 切换读取 server.log 异常：" + JSON.stringify(setupView.getState().logs));
        }
        await setupCtl.hooks.onClearCurrentLog({ name: "server.log" });
        if (
          setupView.getState().logs.phase !== "empty"
          || setupView.getState().logs.entries.length !== 0
          || !/已清空 server\.log/.test(setupView.getState().logs.copyOk || "")
        ) {
          throw new Error("隔离 ASGI 清空 server.log 未生效或未保留反馈：" + JSON.stringify(setupView.getState().logs));
        }
        const badCapRes = await fetch(origin + "/api/v1/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ log_max_lines: 1234 })
        });
        if (badCapRes.status !== 400) {
          throw new Error("隔离 ASGI 非法 log_max_lines 应返回 400，实际 " + badCapRes.status);
        }
        await setupCtl.hooks.onClearCurrentLog({ name: "unknown-not-allowed.log" });
        if (setupView.getState().logs.copyOk !== "" || !/未找到/.test(setupView.getState().logs.copyError || "")) {
          throw new Error("隔离 ASGI 清空未知日志未呈报 404 copyError：" + JSON.stringify(setupView.getState().logs));
        }
        await setupCtl.hooks.onCopyDiagnosticSummary();
        const asgiSummaryText = asgiClipTexts[asgiClipTexts.length - 1] || "";
        if (
          !/已复制排障汇总/.test(setupView.getState().logs.copyOk || "")
          || setupView.getState().logs.copyError !== ""
          || !asgiSummaryText
          || asgiSummaryText.indexOf("ui-business-sandbox-token") >= 0
          || asgiSummaryText.indexOf("alice:secret") >= 0
          || asgiSummaryText.indexOf("***") < 0
        ) {
          throw new Error("隔离 ASGI 复制排障汇总或脱敏失败：" + JSON.stringify({
            copyOk: setupView.getState().logs.copyOk,
            copyError: setupView.getState().logs.copyError,
            sample: asgiSummaryText.slice(0, 200)
          }));
        }
        await setupCtl.hooks.onSelectPanel({ panel: "about" });
        const directAbout = await client.call("get_about_api_v1_about_get");
        const asgiAbout = setupView.getState().about;
        if (
          asgiAbout.phase !== "ready"
          || !/^\d+\.\d+\.\d+/.test(asgiAbout.version || "")
          || asgiAbout.version !== directAbout.version
          || !asgiAbout.links
          || !asgiAbout.links.repo_url
          || !asgiAbout.links.pages_url
          || !asgiAbout.links.releases_url
          || !asgiAbout.links.issues_url
        ) {
          throw new Error("隔离 ASGI 关于页面信息与服务端契约不一致：" + JSON.stringify({ asgiAbout: asgiAbout, direct: directAbout }));
        }
        await setupCtl.hooks.onCopyAboutInfo();
        const asgiAboutClip = asgiClipTexts[asgiClipTexts.length - 1] || "";
        if (
          setupView.getState().about.copyOk !== "已复制环境信息"
          || setupView.getState().about.copyError !== ""
          || asgiAboutClip.indexOf(asgiAbout.version) < 0
        ) {
          throw new Error("隔离 ASGI 复制环境信息失败：" + JSON.stringify({ about: setupView.getState().about, clip: asgiAboutClip }));
        }
        setupCtl.destroy();
        const lan = await client.call("lan_access_api_v1_lan_access_get");
        if (!lan || !lan.token_masked || !Array.isArray(lan.entries) || !lan.entries[0] || !lan.entries[0].url_masked) {
          throw new Error("lan-access 响应缺少遮罩字段");
        }
        if (!lan.entries[0].url_masked || lan.entries[0].url_masked === lan.entries[0].url) {
          throw new Error("lan-access 未提供区别于原址的遮罩网址");
        }
        const posted = await client.call("post_config_api_v1_config_post", { body: { custom_keywords: ["Alpha"] } });
        if (!posted || !Array.isArray(posted.custom_keywords) || posted.custom_keywords[0] !== "Alpha") throw new Error("config POST 未回读合法字段");
        function makeTinyWavBytes() {
          const sampleRate = 16000;
          const samples = 320;
          const dataBytes = samples * 2;
          const buf = new ArrayBuffer(44 + dataBytes);
          const view = new DataView(buf);
          function putStr(off, s) { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); }
          putStr(0, "RIFF");
          view.setUint32(4, 36 + dataBytes, true);
          putStr(8, "WAVE");
          putStr(12, "fmt ");
          view.setUint32(16, 16, true);
          view.setUint16(20, 1, true);
          view.setUint16(22, 1, true);
          view.setUint32(24, sampleRate, true);
          view.setUint32(28, sampleRate * 2, true);
          view.setUint16(32, 2, true);
          view.setUint16(34, 16, true);
          putStr(36, "data");
          view.setUint32(40, dataBytes, true);
          for (let i = 0; i < samples; i++) {
            view.setInt16(44 + i * 2, Math.round(Math.sin(i * 0.25) * 6000), true);
          }
          return new Uint8Array(buf);
        }
        const fd = new FormData();
        fd.append("job_id", "11111111-1111-4111-8111-111111111111");
        fd.append("audio", new Blob([makeTinyWavBytes()], { type: "audio/wav" }), "recording.wav");
        const accepted = await client.call("from_audio_api_v1_utterances_from_audio_post", { body: fd });
        if (!accepted || accepted.state !== "queued" || accepted.text !== "sandbox transcript") throw new Error("from-audio 未按沙箱转写入队");
        const hist = await client.call("history_api_v1_history_get", { query: { limit: 5, offset: 0 } });
        if (!hist || !Array.isArray(hist.records)) throw new Error("history 响应不符合契约");
        const plat = await client.call("platform_event_api_v1_events_platform_post", { body: { event: "busy" } });
        if (!plat || plat.ok !== true) throw new Error("platform 事件未被本机接受");
        const claimed = await client.call("claim_api_v1_jobs_claim_post", { body: { timeout_s: 0 } });
        if (claimed != null && claimed.job_id == null) throw new Error("无任务时应返回 null");
        if (claimed != null && claimed.job_id) {
          /* 沙箱若已有任务也只确认形状，不得把空队列编造成功 */
        } else if (claimed != null) throw new Error("claim 空响应不是 null");
        finish("隔离 ASGI 集成：新路由/快照/check/设置页一键装齐与组件重试/开机自启/lan-access/config/platform/claim/from-audio/history 对齐，已停止 PID " + meta.pid, null);
      }).catch(function (err) {
        finish(null, "ASGI 集成失败：" + err.message);
      });
    }
    child.on("exit", function (code, signal) {
      if (settled || listening) return;
      finish(null, "ASGI 进程在进入监听前退出，码 " + code + (signal ? " 信号 " + signal : ""));
    });
    waitMeta();
  });
}

runUnit()
  .then(runAsgi)
  .then(function () {
    const report = { passes: passes, failures: failures };
    fs.writeFileSync(path.join(logDir, "check_business.json"), JSON.stringify(report, null, 2));
    console.log("业务接线检查通过 " + passes.length + "，失败 " + failures.length);
    failures.forEach(function (m) { console.log("FAIL " + m); });
    passes.forEach(function (m) { console.log("PASS " + m); });
    if (failures.length) process.exit(1);
  })
  .catch(function (err) {
    console.error(err);
    fail("测试运行器异常：" + err.message);
    fs.writeFileSync(path.join(logDir, "check_business.json"), JSON.stringify({ passes: passes, failures: failures }, null, 2));
    process.exit(1);
  });

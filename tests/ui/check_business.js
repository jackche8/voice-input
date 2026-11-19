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
  "setup_task_start_api_setup_tasks_post",
  "setup_task_get_api_setup_tasks__task_id__get",
  "lan_access_api_v1_lan_access_get",
  "status_api_v1_status_get",
  "get_config_api_v1_config_get",
  "post_config_api_v1_config_post",
  "history_api_v1_history_get",
  "translate_api_v1_translate_post",
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
  if (/localStorage|sessionStorage/.test(text)) fail(rel + " 使用了 Web Storage");
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
  context.window.V3 = {};
  context.window.V3Business = {};
  vm.createContext(context);
  ["dom.js", "labels.js", "icons.js", "defaults.js"].forEach(function (name) {
    vm.runInContext(fs.readFileSync(path.join(v3js, name), "utf8"), context, { filename: name });
  });
  const order = [
    "catalog.js", "context.js", "job-id.js", "http.js", "sse.js", "clipboard.js",
    "audio.js", "platform.js", "map-setup.js", "setup-actions.js", "home-controller.js",
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
        message: "尚未下载识别模型",
        action: "asr_download",
        task_id: null
      },
      ollama: {
        model_name: "qwen3.5:4b",
        installed: false,
        version: null,
        running: false,
        model_status: "missing",
        size_label: null,
        progress: null,
        message: "尚未安装 Ollama",
        action: "ollama_install",
        task_id: null
      },
      capabilities: [
        { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
      ],
      unmet_gates: ["v3 尚未切流"]
    };
    return Object.assign(base, overrides || {});
  }

  /* setup controller: real schema mapping, config POST, tasks, LAN */
  const setupWorld = createWorld();
  setupWorld.check = setupCheckBody();
  setupWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () {
      return jsonRes(200, setupWorld.check);
    } },
    { path: "/api/v1/status", method: "GET", handler: function () {
      return jsonRes(200, { ok: true, service: "ollama-voice-input", unmet_gates: ["v3 尚未切流"], injected: { transcribe: true, polish: true, translate: true } });
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, { polish_enabled: false, custom_keywords: ["Alpha"], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" });
    } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      setupWorld.lastConfig = body;
      if (Object.prototype.hasOwnProperty.call(body, "baidu_appkey") || Object.prototype.hasOwnProperty.call(body, "ollama_model")) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "未知或只读字段" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }, body));
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
    { path: "/api/setup/tasks", method: "POST", handler: function (rec) {
      const urlPath = rec.url.split("?")[0];
      if (/\/api\/setup\/tasks\/[^/]+$/.test(urlPath) && rec.method === "POST") return jsonRes(404, { ok: false, error: "not_found", message: "no post task id" });
      if (urlPath.indexOf("/api/setup/tasks/") >= 0 && urlPath !== "/api/setup/tasks" && !urlPath.endsWith("/api/setup/tasks")) {
        return null;
      }
      const body = JSON.parse(rec.body || "{}");
      setupWorld.started = (setupWorld.started || []).concat([body.kind]);
      if (setupWorld.failStart) {
        return jsonRes(500, { ok: false, error: "dependency_failed", message: setupWorld.failStart });
      }
      setupWorld.task = {
        task_id: "task-asr-1",
        kind: body.kind,
        state: "running",
        progress: setupWorld.startProgress || { completed_bytes: 10, total_bytes: 100, percent: 10, stage: "download" },
        message: "downloading",
        error: null,
        started_at: "2026-11-10T00:00:00+00:00",
        finished_at: null
      };
      setupWorld.polls = 0;
      return jsonRes(202, setupWorld.task);
    } },
    { match: function (url, rec) {
      return rec.method === "GET" && /\/api\/setup\/tasks\/[^/]+/.test(String(url).split("?")[0]);
    }, handler: function (rec) {
      setupWorld.polls = (setupWorld.polls || 0) + 1;
      if (typeof setupWorld.onPoll === "function") return setupWorld.onPoll(rec, setupWorld.polls);
      const cur = Object.assign({}, setupWorld.task || {});
      if (setupWorld.polls === 1) {
        cur.state = "running";
        cur.progress = { completed_bytes: 40, total_bytes: 100, percent: 40, stage: "download" };
      } else {
        cur.state = "succeeded";
        cur.progress = { completed_bytes: 100, total_bytes: 100, percent: 100, stage: "done" };
        cur.finished_at = "2026-11-10T00:00:01+00:00";
        cur.message = "downloaded";
        setupWorld.check = setupCheckBody({
          unmet_gates: ["v3 尚未切流"],
          asr: Object.assign({}, setupCheckBody().asr, {
            status: "missing",
            weights_present: true,
            message: "权重已在，等待启动",
            action: "asr_start",
            task_id: null
          })
        });
      }
      setupWorld.task = cur;
      return jsonRes(200, cur);
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
  if (st.connection === "connected" && st.asr && st.asr.status === "missing" && st.asr.action === "asr_download" && st.ollama.modelStatus === "missing" && !st.asr.progress && st.history.records.length === 1 && st.polish_enabled === false) {
    pass("统一工作台设置已连接，按 asr.status/model_status 映射，不用 injected 猜就绪，也无假进度");
  } else fail("设置就绪映射错误：" + JSON.stringify({ connection: st.connection, asr: st.asr, ollama: st.ollama }));
  if (st.statusNotice && (st.statusNotice.indexOf("未达上线") >= 0 || st.statusNotice.indexOf("尚未切流") >= 0)) pass("设置展示 unmet_gates 说明");
  else fail("设置未展示上线条件：" + st.statusNotice);
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
    hotkey_repeat: "ctrl+alt+r"
  });
  const savedKeys = setupWorld.lastConfig;
  const afterKeys = setupView.getState();
  if (
    st.hotkey_ocr === "ctrl+alt+o" &&
    savedKeys &&
    savedKeys.hotkey_ocr === "ctrl+alt+p" &&
    afterKeys.hotkey_ocr === "ctrl+alt+p" &&
    afterKeys.hotkeysSaved === true &&
    afterKeys.hotkeyCaptureWarning === ""
  ) {
    pass("home-controller + setup-actions 读取并保存第四热键 hotkey_ocr，保存成功时清空 hotkeyCaptureWarning");
  } else fail("home-controller hotkey_ocr 保存异常：" + JSON.stringify({ initOcr: st.hotkey_ocr, savedKeys: savedKeys, afterOcr: afterKeys.hotkey_ocr, warn: afterKeys.hotkeyCaptureWarning }));

  const downloadP = setupCtl.hooks.onDownloadAsr();
  await downloadP;
  const after = setupView.getState();
  if (setupWorld.started && setupWorld.started[0] === "asr_download" && setupWorld.polls >= 2 && after.asr.action === "asr_start" && after.asr.status !== "ready" && after.asr.progress == null) {
    pass("下载任务经 202 与轮询完成后再查 check，不以任务成功冒充模型就绪");
  } else fail("任务生命周期映射错误：" + JSON.stringify({ started: setupWorld.started, polls: setupWorld.polls, asr: after.asr }));

  /* injected 不得覆盖真实 missing */
  const injWorld = createWorld();
  injWorld.routes = setupWorld.routes.map(function (r) { return r; });
  const injCheck = setupCheckBody();
  injWorld.routes = injWorld.routes.filter(function (r) { return r.path !== "/api/setup/check"; });
  injWorld.routes.unshift({ path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, injCheck); } });
  const injCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: injWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const injView = fakeView();
  injCtl.bind(injView);
  await injCtl.start();
  if (injView.getState().asr.status === "missing" && injView.getState().ollama.modelStatus === "missing") {
    pass("status.injected=true 时仍按 check.asr.status 显示未就绪");
  } else fail("injected 干扰了就绪判断：" + JSON.stringify(injView.getState().asr));
  injCtl.destroy();

  /* 无 percent 时不编造百分比 */
  const nopWorld = createWorld();
  nopWorld.check = setupCheckBody();
  nopWorld.failStart = null;
  nopWorld.startProgress = { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" };
  nopWorld.routes = setupWorld.routes;
  const origOnStart = setupWorld.startProgress;
  setupWorld.startProgress = { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" };
  setupWorld.check = setupCheckBody();
  setupWorld.onPoll = function (rec, n) {
    if (n === 1) {
      return jsonRes(200, {
        task_id: "task-asr-1",
        kind: "asr_download",
        state: "running",
        progress: { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" },
        message: "fetching",
        error: null,
        started_at: "2026-11-10T00:00:00+00:00",
        finished_at: null
      });
    }
    return jsonRes(200, {
      task_id: "task-asr-1",
      kind: "asr_download",
      state: "failed",
      progress: null,
      message: "download failed",
      error: "dependency_failed",
      started_at: "2026-11-10T00:00:00+00:00",
      finished_at: "2026-11-10T00:00:02+00:00"
    });
  };
  const nopCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const nopView = fakeView();
  nopCtl.bind(nopView);
  await nopCtl.start();
  const midP = nopCtl.hooks.onDownloadAsr();
  await wait(5);
  const mid = nopView.getState();
  if (mid.asr && mid.asr.progress && mid.asr.progress.percent == null && mid.asr.progress.receivedLabel && !mid.asr.progress.totalLabel && mid.asr.status === "downloading") {
    pass("服务端未给 percent 时不展示假百分比，只显示已给字节");
  } else fail("假百分比或进度映射错误：" + JSON.stringify(mid.asr && mid.asr.progress));
  await midP;
  if (nopView.getState().asr.status === "failed" && /failed|失败/.test(nopView.getState().asr.message || nopView.getState().statusNotice || "")) {
    pass("任务失败如实呈现，不造成功");
  } else fail("失败任务未正确呈现：" + JSON.stringify(nopView.getState().asr));
  nopCtl.destroy();
  setupWorld.onPoll = null;
  setupWorld.startProgress = origOnStart;
  setupWorld.check = setupCheckBody();

  /* destroy 停止轮询且不重复启动 */
  const desWorldPolls = { n: 0, afterDestroy: 0 };
  let setupDes;
  setupWorld.onPoll = function (rec, n) {
    desWorldPolls.n += 1;
    if (setupDes && setupDes._debug.isDestroyed()) desWorldPolls.afterDestroy += 1;
    return jsonRes(200, {
      task_id: "task-asr-1",
      kind: "asr_download",
      state: "running",
      progress: { completed_bytes: 1, total_bytes: 100, percent: 1, stage: "download" },
      message: "still running",
      error: null,
      started_at: "2026-11-10T00:00:00+00:00",
      finished_at: null
    });
  };
  setupDes = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const desView = fakeView();
  setupDes.bind(desView);
  await setupDes.start();
  setupDes.hooks.onDownloadAsr();
  await wait(25);
  const pollsBefore = desWorldPolls.n;
  setupDes.destroy();
  await wait(40);
  if (setupDes._debug.isDestroyed() && setupDes._debug.polling() === null && desWorldPolls.afterDestroy === 0 && desWorldPolls.n >= 1 && pollsBefore === desWorldPolls.n) {
    pass("destroy 后停止任务轮询，不再重新启动");
  } else fail("destroy 未停轮询：" + JSON.stringify(desWorldPolls) + " before=" + pollsBefore);
  setupWorld.onPoll = null;

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
        unmet_gates: [],
        asr: Object.assign({}, setupCheckBody().asr, { status: "ready", weights_present: true, sidecar_running: true, action: null, message: "" }),
        ollama: Object.assign({}, setupCheckBody().ollama, { installed: true, running: true, model_status: "ready", action: null, message: "" })
      }));
    } },
    { path: "/api/v1/status", method: "GET", handler: function () {
      return jsonRes(200, { ok: true, service: "ollama-voice-input", unmet_gates: [] });
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, {
        polish_enabled: true,
        custom_keywords: ["Qwen"],
        translate_target_lang: "zh",
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r"
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
      if (homeWorld.translateDelay) {
        return new Promise(function (resolve) {
          homeWorld.resolveDelayedTranslate = function () {
            resolve(jsonRes(200, { ok: true, text: "迟到译文", target_lang: body.target_lang }));
          };
        });
      }
      if (homeWorld.translateFail) {
        return jsonRes(502, { ok: false, error: "translate_failed", message: "本地模型无响应", details: { ocr_text: "不应被当成译文" } });
      }
      return jsonRes(200, { ok: true, text: "你好世界", target_lang: body.target_lang });
    } },
    { path: "/api/v1/ocr-translate", method: "POST", handler: function (rec) {
      const fd = rec.body;
      if (!(fd instanceof FormData) || !fd.get("target_lang") || !fd.get("image")) {
        return jsonRes(422, { ok: false, error: "invalid_request", message: "缺少字段" });
      }
      const lang = fd.get("target_lang");
      homeWorld.lastOcrLang = lang;
      if (homeWorld.ocrFail) {
        return jsonRes(502, { ok: false, error: "translate_failed", message: "翻译服务异常", details: { ocr_text: "DO_NOT_FAKE_TRANSLATION" } });
      }
      if (lang === "raw") {
        return jsonRes(200, { ok: true, text: "Pure OCR line", ocr_text: "Pure OCR line", target_lang: "raw" });
      }
      return jsonRes(200, { ok: true, text: "版本说明 3.2", ocr_text: "Release notes 3.2", target_lang: lang });
    } }
  ];

  const homeCopied = [];
  let fakeClock = 1000;
  const homeCtl = B.createHomeController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: homeWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: { writeText: function (t) { homeCopied.push(t); return Promise.resolve(); } },
    now: function () { fakeClock += 45; return fakeClock; }
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
    hs.translate_target_lang === "zh" &&
    hs.history.records.length === 1 &&
    hs.lanEntries &&
    hs.lanEntries[0].urlMasked.indexOf("hom****eak") >= 0 &&
    !hs.lanEntries[0].url &&
    JSON.stringify(hs).indexOf("home-secret-lan-token-do-not-leak") < 0
  ) {
    pass("home-controller start 并行加载状态/四热键/历史/脱敏局域网入口，明文令牌不进 setState");
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

  await homeCtl.hooks.onTranslateText({ text: "Hello world", target_lang: "zh" });
  const hTr = homeView.getState().translate;
  if (hTr.phase === "result" && hTr.text === "你好世界" && hTr.ocr_text === "" && hTr.target_lang === "zh" && typeof hTr.elapsedMs === "number" && hTr.elapsedMs >= 45) {
    pass("home-controller 文本翻译走 OpenAPI translate 并记录耗时");
  } else fail("home-controller 文本翻译异常：" + JSON.stringify(hTr));

  const callsBeforeRaw = homeWorld.calls.length;
  await homeCtl.hooks.onTranslateText({ text: "Raw pass through", target_lang: "raw" });
  const hRaw = homeView.getState().translate;
  if (homeWorld.calls.length === callsBeforeRaw && hRaw.phase === "result" && hRaw.text === "Raw pass through" && hRaw.target_lang === "raw") {
    pass("home-controller target_lang=raw 直接回填，不发网络请求");
  } else fail("home-controller raw 模式异常：" + JSON.stringify(hRaw));

  homeWorld.translateFail = true;
  await homeCtl.hooks.onTranslateText({ text: "Fail me", target_lang: "zh" });
  const hTrErr = homeView.getState().translate;
  if (hTrErr.phase === "error" && hTrErr.error === "translate_failed" && hTrErr.text === "" && /无响应/.test(hTrErr.message || "")) {
    pass("home-controller 翻译失败如实呈现错误，不伪造译文");
  } else fail("home-controller 翻译失败状态错误：" + JSON.stringify(hTrErr));
  homeWorld.translateFail = false;

  const imgBlob = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "zh", mode: "translate" });
  const hImgTr = homeView.getState().translate;
  if (hImgTr.phase === "result" && hImgTr.text === "版本说明 3.2" && hImgTr.ocr_text === "Release notes 3.2") {
    pass("home-controller mode=translate 图片提交更新 state.translate（含 text 与 ocr_text）");
  } else fail("home-controller 图片翻译异常：" + JSON.stringify(hImgTr));

  await homeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "raw", mode: "ocr" });
  const hImgOcr = homeView.getState().ocr;
  if (hImgOcr.phase === "result" && hImgOcr.ocr_text === "Pure OCR line" && hImgOcr.text === "") {
    pass("home-controller mode=ocr 纯提取更新 state.ocr（仅 ocr_text，不重复译文）");
  } else fail("home-controller 图片纯 OCR 异常：" + JSON.stringify(hImgOcr));

  await homeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "zh", mode: "ocr" });
  const hImgOcrTr = homeView.getState().ocr;
  if (hImgOcrTr.phase === "result" && hImgOcrTr.ocr_text === "Release notes 3.2" && hImgOcrTr.text === "版本说明 3.2") {
    pass("home-controller OCR 面板提取并翻译同时写入 ocr_text 与 text");
  } else fail("home-controller OCR 翻译异常：" + JSON.stringify(hImgOcrTr));

  homeWorld.ocrFail = true;
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "zh", mode: "translate" });
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
  await homeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "zh", mode: "translate" });
  if (homeView.getState().theme === "light" && homeView.getState().activePanel === "ocr" && homeView.getState().ocr.text === "版本说明 3.2") {
    pass("home-controller 支持主题/面板切换，且在 OCR 面板选译文模式时更新 state.ocr");
  } else fail("home-controller 主题或 OCR 面板路由异常");

  /* destroy 防异步回调污染 */
  homeWorld.translateDelay = true;
  const inflight = homeCtl.hooks.onTranslateText({ text: "Late", target_lang: "zh" });
  homeCtl.destroy();
  homeWorld.resolveDelayedTranslate();
  await inflight;
  if (homeCtl._debug.isDestroyed() && homeView.getState().translate.text !== "迟到译文") {
    pass("home-controller destroy 后迟到响应不污染已销毁视图");
  } else fail("home-controller destroy 防护失效：" + JSON.stringify(homeView.getState().translate));
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
  await lanHomeCtl.hooks.onTranslateText({ text: "hello", target_lang: "zh" });
  await lanHomeCtl.hooks.onSubmitImage({ file: imgBlob, target_lang: "zh", mode: "translate" });
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
    { id: "desktop_daemon", label: "桌面守护进程", available: true, required: true, detail: "守护进程正在运行，快捷键已加载。" },
    { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
  ]);
  if (
    mappedCaps
    && mappedCaps[0]
    && mappedCaps[0].id === "desktop_daemon"
    && mappedCaps[0].detail === "守护进程正在运行，快捷键已加载。"
    && mappedCaps[0].message === mappedCaps[0].detail
    && mappedCaps[1].id === "clipboard"
    && mappedCaps[1].detail.indexOf("无法写入剪贴板") >= 0
  ) {
    pass("mapCapabilities 保留 id 与 detail");
  } else fail("mapCapabilities 未保留 id/detail：" + JSON.stringify(mappedCaps));
  if (B.mapCapabilities(null) === null && Array.isArray(B.mapCapabilities([])) && B.mapCapabilities([]).length === 0) {
    pass("能力列表缺省或空数组时不编造 desktop_daemon");
  } else fail("空能力列表映射错误");

  const startFailModels = B.applyTaskToModels(
    { status: "ready", sidecarRunning: false, message: "权重已在", action: "asr_start" },
    { modelStatus: "ready", running: true },
    { kind: "asr_start", state: "failed", message: "启动识别服务失败：未授权", task_id: "t-start-1", error: "unauthorized" }
  );
  if (
    startFailModels.asr.status === "ready"
    && startFailModels.asr.sidecarRunning === false
    && startFailModels.asr.message.indexOf("未授权") >= 0
    && startFailModels.asr.action === "asr_start"
    && startFailModels.asr.taskId === "t-start-1"
  ) {
    pass("asr_start 失败不把 weights status 写成 failed，只叠加任务说明");
  } else fail("applyTaskToModels asr_start 失败路径错误：" + JSON.stringify(startFailModels.asr));

  const dlFail = B.applyTaskToModels(
    { status: "downloading" },
    {},
    { kind: "asr_download", state: "failed", message: "下载中断", task_id: "t-dl" }
  );
  if (dlFail.asr.status === "failed" && dlFail.asr.message === "下载中断") pass("asr_download 失败仍写入 weights status=failed");
  else fail("asr_download 失败路径被误改：" + JSON.stringify(dlFail.asr));

  const startReqWorld = createWorld();
  startReqWorld.check = setupCheckBody({
    asr: Object.assign({}, setupCheckBody().asr, {
      status: "ready",
      weights_present: true,
      sidecar_running: false,
      message: "权重已就绪，识别服务未启动",
      action: "asr_start",
      task_id: null
    })
  });
  startReqWorld.failStart = "启动识别服务失败：端口被占用";
  startReqWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, startReqWorld.check); } },
    { path: "/api/v1/status", method: "GET", handler: function () { return jsonRes(200, { ok: true, service: "ollama-voice-input", unmet_gates: [], injected: {} }); } },
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, { polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }); } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      if (body.hotkey_voice && body.hotkey_voice === body.hotkey_screenshot) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "语音输入、截图翻译、重复上次输入 不能使用相同组合" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }, body));
    } },
    { path: "/api/v1/history", method: "GET", handler: function () { return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }); } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () { return jsonRes(200, { port: 8443, token: "secret-lan-token-do-not-log", token_masked: "sec****log", entries: [], message: "" }); } },
    { path: "/api/setup/tasks", method: "POST", handler: function (rec) {
      const urlPath = rec.url.split("?")[0];
      if (urlPath.indexOf("/api/setup/tasks/") >= 0 && urlPath !== "/api/setup/tasks") return null;
      const body = JSON.parse(rec.body || "{}");
      startReqWorld.started = (startReqWorld.started || []).concat([body.kind]);
      if (startReqWorld.failStart) {
        return jsonRes(500, { ok: false, error: "dependency_failed", message: startReqWorld.failStart });
      }
      startReqWorld.task = {
        task_id: "task-start-1",
        kind: body.kind,
        state: "running",
        progress: null,
        message: "starting",
        error: null,
        started_at: "2026-11-11T00:00:00+00:00",
        finished_at: null
      };
      startReqWorld.polls = 0;
      return jsonRes(202, startReqWorld.task);
    } },
    { match: function (url, rec) {
      return rec.method === "GET" && /\/api\/setup\/tasks\/[^/]+/.test(String(url).split("?")[0]);
    }, handler: function () {
      startReqWorld.polls = (startReqWorld.polls || 0) + 1;
      if (typeof startReqWorld.onPoll === "function") return startReqWorld.onPoll(startReqWorld.polls);
      const cur = Object.assign({}, startReqWorld.task || {});
      cur.state = "failed";
      cur.message = "sidecar 未能监听";
      cur.error = "dependency_failed";
      cur.finished_at = "2026-11-11T00:00:01+00:00";
      startReqWorld.task = cur;
      return jsonRes(200, cur);
    } }
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
  await startReqCtl.hooks.onStartAsr();
  const afterReqFail = startReqView.getState();
  if (afterReqFail.asr.status === "ready" && /端口被占用/.test((afterReqFail.asr.message || "") + " " + (afterReqFail.statusNotice || "")) && afterReqFail.asr.action === "asr_start") {
    pass("启动请求失败不把 status 写成 failed，并展示后端说明");
  } else fail("启动请求失败路径错误：" + JSON.stringify({ asr: afterReqFail.asr, notice: afterReqFail.statusNotice }));

  startReqWorld.failStart = null;
  await startReqCtl.hooks.onStartAsr();
  const afterOverlay = startReqView.getState();
  if (afterOverlay.asr.status === "ready" && /未能监听/.test((afterOverlay.asr.message || "") + " " + (afterOverlay.statusNotice || "")) && afterOverlay.asr.action === "asr_start") {
    pass("asr_start 任务失败叠加路径不改 weights status，只保留任务说明");
  } else fail("overlayTaskFailure asr_start 路径错误：" + JSON.stringify({ asr: afterOverlay.asr, notice: afterOverlay.statusNotice, polls: startReqWorld.polls }));

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

  /* P1-5 回归：配置保存失败写入 statusNotice 后，再次保存成功立即恢复为无报错（或按 unmetGates 还原） */
  startReqView.setState({ unmetGates: [] });
  await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r" });
  if (startReqView.getState().statusNotice === null && startReqView.getState().hotkeySaveError === "") {
    pass("saveConfig 失败后再保存成功时（无 unmetGates）立即清空顶部 statusNotice");
  } else fail("saveConfig 成功后未清空 statusNotice：" + JSON.stringify({ notice: startReqView.getState().statusNotice, err: startReqView.getState().hotkeySaveError }));

  startReqView.setState({ unmetGates: ["v3 尚未切流"] });
  try {
    await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+z", hotkey_repeat: "ctrl+alt+r" });
  } catch (err) {}
  await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r" });
  if (startReqView.getState().statusNotice === "未达上线条件：v3 尚未切流") {
    pass("saveConfig 失败后再保存成功时（存在 unmetGates）还原门闩提示，不残留旧报错");
  } else fail("saveConfig 成功后未还原 unmetGates statusNotice：" + JSON.stringify(startReqView.getState().statusNotice));

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
    if (String(url).indexOf("/api/setup/check") >= 0) return jsonRes(200, { ok: true, asr: { status: "ready", sidecar_running: true }, ollama: { installed: true, running: true, model_status: "ready" }, capabilities: [], unmet_gates: [] });
    if (String(url).indexOf("/api/v1/status") >= 0) return jsonRes(200, { ok: true, unmet_gates: [] });
    if (String(url).indexOf("/api/v1/config") >= 0) return jsonRes(200, { polish_enabled: true, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_ocr: "ctrl+alt+o", hotkey_repeat: "ctrl+alt+r" });
    if (String(url).indexOf("/api/v1/history") >= 0) return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    if (String(url).indexOf("/api/v1/lan-access") >= 0) return jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" });
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
    if (u.indexOf("/api/v1/status") >= 0) return Promise.resolve(jsonRes(200, { ok: true, unmet_gates: [] }));
    if (u.indexOf("/api/v1/config") >= 0) {
      return Promise.resolve(jsonRes(200, {
        polish_enabled: true,
        custom_keywords: [],
        translate_target_lang: "zh",
        hotkey_voice: raceCallCount === 1 ? "ctrl+alt+1" : "ctrl+alt+2",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_ocr: "ctrl+alt+o",
        hotkey_repeat: "ctrl+alt+r"
      }));
    }
    if (u.indexOf("/api/v1/history") >= 0) return Promise.resolve(jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }));
    if (u.indexOf("/api/v1/lan-access") >= 0) return Promise.resolve(jsonRes(200, { port: 17945, token: "", token_masked: "", entries: [], message: "" }));
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
      if (meta.port === 17945 || meta.port === 11434) return finish(null, "沙箱误用保留端口 " + meta.port);
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
        if (!Array.isArray(check.unmet_gates) || !check.unmet_gates.length) throw new Error("setup_check 应保留 unmet_gates，ok 只表示检查成功");
        if (!Array.isArray(check.capabilities)) throw new Error("setup_check 缺少 capabilities");
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
        const jsRes = await fetch(origin + "/js/business/bootstrap.js");
        if (!jsRes.ok) throw new Error("/js 资源 HTTP " + jsRes.status);
        const cssRes = await fetch(origin + "/css/theme.css");
        if (!cssRes.ok) throw new Error("/css 资源 HTTP " + cssRes.status);
        const started = await client.call("setup_task_start_api_setup_tasks_post", { body: { kind: "asr_download" } });
        if (!started || started.state !== "running" || !started.task_id) throw new Error("启动任务未返回 running");
        const same = await client.call("setup_task_start_api_setup_tasks_post", { body: { kind: "asr_download" } });
        if (!same || same.task_id !== started.task_id) throw new Error("同 kind 运行中任务应幂等返回");
        const mid = await client.call("setup_task_get_api_setup_tasks__task_id__get", { pathParams: { task_id: started.task_id } });
        if (!mid || (mid.state !== "running" && mid.state !== "succeeded")) throw new Error("任务查询状态异常");
        if (mid.progress && mid.progress.percent != null && (mid.progress.percent < 0 || mid.progress.percent > 100)) {
          throw new Error("任务进度超出范围");
        }
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
        finish("隔离 ASGI 集成：新路由/快照/check/tasks/lan-access/config/platform/claim/from-audio/history 对齐，已停止 PID " + meta.pid, null);
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

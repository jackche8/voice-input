/* 两页显式业务启动（home / voice）。已有 __V3_HOOKS__ 夹具且未要求启动业务时保持离线。 */
(function (root) {
  "use strict";

  var SCRIPT_PAGE = (typeof document !== "undefined" && document.currentScript && document.currentScript.getAttribute("data-page")) || "";
  var DEFAULT_CSS_MAX_RETRIES = 2;
  var DEFAULT_CSS_RETRY_TIMEOUT_MS = 4000;

  function getLinkHref(link) {
    if (!link) return "";
    if (typeof link.getAttribute === "function") {
      var attr = link.getAttribute("href");
      if (attr) return String(attr);
    }
    return link.href ? String(link.href) : "";
  }

  function isThemeStylesheet(href) {
    var clean = String(href || "").split("#")[0].split("?")[0];
    return /(?:^|\/)theme\.css$/i.test(clean);
  }

  function buildRetryHref(rawHref, attempt) {
    var s = String(rawHref || "");
    var hashIdx = s.indexOf("#");
    var hash = hashIdx >= 0 ? s.slice(hashIdx) : "";
    var beforeHash = hashIdx >= 0 ? s.slice(0, hashIdx) : s;
    var qIdx = beforeHash.indexOf("?");
    var base = qIdx >= 0 ? beforeHash.slice(0, qIdx) : beforeHash;
    var query = qIdx >= 0 ? beforeHash.slice(qIdx + 1) : "";
    var pairs = query ? query.split("&").filter(function (part) {
      if (!part) return false;
      var eq = part.indexOf("=");
      var key = eq >= 0 ? part.slice(0, eq) : part;
      try { key = decodeURIComponent(key); } catch (e) {}
      return key !== "_css_retry";
    }) : [];
    pairs.push("_css_retry=" + encodeURIComponent(String(attempt)));
    return base + "?" + pairs.join("&") + hash;
  }

  function isStylesheetHealthy(link, doc, win) {
    if (!link || !link.sheet) return false;
    try {
      var rules = link.sheet.cssRules;
      if (rules != null && rules.length === 0) return false;
    } catch (err) {
      /* 跨域样式表读取 cssRules 会抛 SecurityError，此时保留 link.sheet 存在判定。 */
      if (!err || err.name !== "SecurityError") return false;
    }
    if (isThemeStylesheet(getLinkHref(link))) {
      var docEl = doc && doc.documentElement;
      if (docEl) {
        var cs = null;
        try {
          if (win && typeof win.getComputedStyle === "function") {
            cs = win.getComputedStyle(docEl);
          } else if (typeof getComputedStyle === "function") {
            cs = getComputedStyle(docEl);
          }
        } catch (err2) {
          return false;
        }
        if (cs && typeof cs.getPropertyValue === "function") {
          var bgVal = String(cs.getPropertyValue("--c-bg") || "").trim();
          if (!bgVal) return false;
        }
      }
    }
    return true;
  }

  function loadSingleStylesheetAttempt(doc, win, currentLink, nextHref, retryTimeoutMs, setTimeoutFn, clearTimeoutFn) {
    return new Promise(function (resolve) {
      var parent = currentLink && currentLink.parentNode;
      if (!parent || typeof parent.insertBefore !== "function" || typeof doc.createElement !== "function") {
        resolve({ ok: false, link: currentLink });
        return;
      }
      var fresh = doc.createElement("link");
      if (typeof fresh.setAttribute === "function") {
        fresh.setAttribute("rel", "stylesheet");
        fresh.setAttribute("href", nextHref);
      }
      if (!fresh.rel) fresh.rel = "stylesheet";
      if (!fresh.href) fresh.href = nextHref;
      if (currentLink.media) fresh.media = currentLink.media;

      var done = false;
      var timer = null;
      function cleanup() {
        if (timer != null && clearTimeoutFn) {
          clearTimeoutFn(timer);
          timer = null;
        }
        if (typeof fresh.removeEventListener === "function") {
          fresh.removeEventListener("load", onLoad);
          fresh.removeEventListener("error", onError);
        } else {
          if (fresh.onload === onLoad) fresh.onload = null;
          if (fresh.onerror === onError) fresh.onerror = null;
        }
      }
      function finish(ok) {
        if (done) return;
        done = true;
        cleanup();
        resolve({ ok: ok, link: fresh });
      }
      function onLoad() {
        finish(isStylesheetHealthy(fresh, doc, win));
      }
      function onError() {
        finish(false);
      }
      if (typeof fresh.addEventListener === "function") {
        fresh.addEventListener("load", onLoad);
        fresh.addEventListener("error", onError);
      } else {
        fresh.onload = onLoad;
        fresh.onerror = onError;
      }
      if (setTimeoutFn) {
        timer = setTimeoutFn(function () {
          timer = null;
          finish(isStylesheetHealthy(fresh, doc, win));
        }, retryTimeoutMs);
      }
      parent.insertBefore(fresh, currentLink);
      if (typeof parent.removeChild === "function" && currentLink.parentNode === parent) {
        parent.removeChild(currentLink);
      } else if (typeof currentLink.remove === "function") {
        currentLink.remove();
      }
    });
  }

  function retryStylesheet(link, originalHref, doc, win, maxRetries, retryTimeoutMs, setTimeoutFn, clearTimeoutFn) {
    if (maxRetries <= 0) {
      return Promise.resolve({ ok: false, retried: false, href: originalHref });
    }
    var current = link;
    var attempt = 0;
    function next() {
      attempt += 1;
      var nextHref = buildRetryHref(originalHref, attempt);
      return loadSingleStylesheetAttempt(doc, win, current, nextHref, retryTimeoutMs, setTimeoutFn, clearTimeoutFn).then(function (res) {
        current = res.link;
        if (res.ok) return { ok: true, retried: true, href: originalHref };
        if (attempt < maxRetries) return next();
        return { ok: false, retried: true, href: originalHref };
      });
    }
    return next();
  }

  function ensureStylesheetsLoaded(options) {
    var opts = options || {};
    var doc = opts.document || (typeof document !== "undefined" ? document : null);
    var win = opts.window || (typeof window !== "undefined" ? window : null);
    var loc = opts.location || (win && win.location) || (typeof location !== "undefined" ? location : null);
    var protocol = loc && loc.protocol ? String(loc.protocol).toLowerCase() : "";
    if ((protocol !== "http:" && protocol !== "https:") || !doc || typeof doc.querySelectorAll !== "function") {
      return Promise.resolve({ ok: true, skipped: true, retried: [], failed: [] });
    }
    var maxRetries = typeof opts.maxRetries === "number" && isFinite(opts.maxRetries) && opts.maxRetries >= 0
      ? Math.floor(opts.maxRetries)
      : DEFAULT_CSS_MAX_RETRIES;
    var retryTimeoutMs = typeof opts.retryTimeoutMs === "number" && isFinite(opts.retryTimeoutMs) && opts.retryTimeoutMs > 0
      ? opts.retryTimeoutMs
      : DEFAULT_CSS_RETRY_TIMEOUT_MS;
    var setTimeoutFn = opts.setTimeout || (typeof setTimeout === "function" ? setTimeout : null);
    var clearTimeoutFn = opts.clearTimeout || (typeof clearTimeout === "function" ? clearTimeout : null);
    var consoleObj = opts.console || (typeof console !== "undefined" ? console : null);

    var links = Array.prototype.slice.call(doc.querySelectorAll('link[rel="stylesheet"]') || []);
    var tasks = [];
    for (var i = 0; i < links.length; i += 1) {
      var link = links[i];
      var href = getLinkHref(link);
      if (!href) continue;
      if (isStylesheetHealthy(link, doc, win)) continue;
      tasks.push(retryStylesheet(link, href, doc, win, maxRetries, retryTimeoutMs, setTimeoutFn, clearTimeoutFn));
    }
    if (!tasks.length) {
      return Promise.resolve({ ok: true, skipped: false, retried: [], failed: [] });
    }
    return Promise.all(tasks).then(function (results) {
      var retried = [];
      var failed = [];
      for (var j = 0; j < results.length; j += 1) {
        if (results[j].retried) retried.push(results[j].href);
        if (!results[j].ok) failed.push(results[j].href);
      }
      if (failed.length && consoleObj && typeof consoleObj.error === "function") {
        consoleObj.error("[v3] 样式表加载失败（已重试 " + maxRetries + " 次）：" + failed.join(", "));
      }
      return {
        ok: failed.length === 0,
        skipped: false,
        retried: retried,
        failed: failed
      };
    });
  }

  function formatStylesheetError(failed) {
    var list = Array.isArray(failed) && failed.length ? failed.join("、") : "关键样式表";
    return "页面样式表加载失败（" + list + "），请刷新或重启应用。";
  }

  function applyStylesheetFailure(page, controller, view, cssResult, isDisposed) {
    if (isDisposed() || !cssResult || cssResult.ok) return;
    var msg = formatStylesheetError(cssResult.failed);
    if (page === "home") {
      if (controller && typeof controller.setPersistentNotice === "function") {
        controller.setPersistentNotice(msg);
      }
    } else if (page === "voice") {
      if (view && typeof view.setState === "function") {
        view.setState({ statusText: msg, statusTone: "err" });
      }
    }
  }

  function shouldStartBusiness() {
    if (window.__V3_DISABLE_BUSINESS__ === true) return false;
    if (window.__V3_START_BUSINESS__ === false) return false;
    if (window.__V3_START_BUSINESS__ === true) return true;
    if (window.__V3_HOOKS__) return false;
    return true;
  }

  function detectInitialHomePanel(loc) {
    if (!loc) return null;
    var p = String(loc.pathname || "");
    var h = String(loc.hash || "");
    if (p === "/setup" || p === "/setup/" || h === "#setup") return "setup";
    return null;
  }

  function mountView(page, hooks, initialState, docRef) {
    var d = docRef || (typeof document !== "undefined" ? document : null);
    var app = d && typeof d.getElementById === "function" ? d.getElementById("app") : null;
    var V3ns = window.V3;
    if (!app || !V3ns) throw new Error("页面尚未准备好挂载");
    var opts = { hooks: hooks || {}, initialState: initialState || {} };
    if (page === "home") {
      if (V3ns.home && typeof V3ns.home.mount === "function") {
        window.__v3Home = V3ns.home.mount(app, initialState || {}, hooks || {});
      } else if (typeof V3ns.mountHome === "function") {
        window.__v3Home = V3ns.mountHome(app, opts);
      } else {
        throw new Error("主页视图模块未加载");
      }
      return window.__v3Home;
    }
    if (page === "voice") {
      window.__v3Voice = V3ns.mountVoice(app, opts);
      return window.__v3Voice;
    }
    throw new Error("未知页面：" + page);
  }

  function loadOpenApi(deps, context, fetchImpl) {
    if (deps.openapi) return Promise.resolve(deps.openapi);
    if (!context.allowFetch) {
      return Promise.reject(new Error("当前页面不能加载接口说明。"));
    }
    function getJson(url) {
      return Promise.resolve(fetchImpl(url, {
        headers: { Accept: "application/json" },
        referrerPolicy: root.FETCH_REFERRER_POLICY || "no-referrer"
      })).then(function (res) {
        if (!res || !res.ok) throw new Error("加载失败 " + (res && res.status));
        return res.json();
      });
    }
    var origin = (deps.origin != null ? deps.origin : context.origin) || "";
    var snapshotUrl = deps.snapshotUrl || "/openapi.snapshot.json";
    if (context.kind === "local") {
      return getJson(origin + "/openapi.json").then(function (doc) { return doc; }, function () {
        return getJson(snapshotUrl);
      });
    }
    /* 局域网不得请求仅本机的 /openapi.json，改用随页面发布的冻结快照。 */
    if (deps.openapiSnapshot) return Promise.resolve(deps.openapiSnapshot);
    return getJson(snapshotUrl);
  }

  function createClient(deps, context, catalog) {
    return root.createHttp({
      catalog: catalog,
      context: context,
      fetch: deps.fetch,
      origin: deps.origin != null ? deps.origin : context.origin
    });
  }

  function startPage(page, deps) {
    deps = deps || window.__V3_BUSINESS_DEPS__ || {};
    var doc = deps.document || (typeof document !== "undefined" ? document : null);
    var win = deps.window || (typeof window !== "undefined" ? window : null);
    var loc = deps.location || (typeof location !== "undefined" ? location : null);
    var hist = deps.history || (typeof history !== "undefined" ? history : null);
    var disposed = false;
    var activeController = null;
    if (win && typeof win.addEventListener === "function") {
      win.addEventListener("pagehide", function () {
        disposed = true;
        if (activeController && typeof activeController.destroy === "function") {
          activeController.destroy();
        }
      });
    }
    var cssPromise = ensureStylesheetsLoaded({
      document: doc,
      window: win,
      location: loc,
      maxRetries: deps.cssMaxRetries,
      retryTimeoutMs: deps.cssRetryTimeoutMs,
      setTimeout: deps.setTimeout,
      clearTimeout: deps.clearTimeout,
      console: deps.console
    });
    /* 任何 fetch 之前先从地址栏取走令牌。失败则令牌作废，不得带着 URL 里的密钥继续请求。 */
    if (!deps.context && root.adoptTokenFromLocation) {
      root.adoptTokenFromLocation({
        location: loc,
        history: hist
      });
    }
    var contextOpts = {
      location: loc || {},
      allowHttp: !!deps.allowHttp,
      origin: deps.origin
    };
    if (Object.prototype.hasOwnProperty.call(deps, "token")) contextOpts.token = deps.token;
    var context = deps.context || root.createContext(contextOpts);
    var fetchImpl = deps.fetch || (typeof fetch === "function" ? fetch.bind(window) : null);
    var initialPanel = page === "home" ? detectInitialHomePanel(loc) : null;
    return loadOpenApi(deps, context, fetchImpl).then(function (docApi) {
      if (disposed) {
        return cssPromise.then(function () {});
      }
      var catalog = root.createCatalog(docApi);
      var client = createClient(deps, context, catalog);
      var controller;
      var extraState = { connection: "loading" };
      if (page === "home") {
        if (initialPanel) extraState.activePanel = initialPanel;
        controller = root.createHomeController({
          client: client,
          context: context,
          catalog: catalog,
          clipboard: root.createClipboard(deps),
          now: deps.now,
          pageOrigin: context.protocol === "https:" || context.kind === "local" ? context.origin : "",
          pollInterval: deps.pollInterval,
          setTimeout: deps.setTimeout,
          clearTimeout: deps.clearTimeout,
          location: loc,
          history: hist
        });
      } else if (page === "voice") {
        controller = root.createVoiceController({
          client: client,
          context: context,
          jobIds: root.createJobIds(),
          MediaRecorder: deps.MediaRecorder,
          mediaDevices: deps.mediaDevices,
          document: deps.document,
          window: deps.window
        });
      } else {
        throw new Error("未知页面：" + page);
      }
      var view = mountView(page, controller.hooks, extraState, doc);
      controller.bind(view);
      activeController = controller;
      window.__v3Business = {
        page: page,
        context: context,
        catalog: catalog,
        client: client,
        controller: controller,
        view: view
      };
      var startDone = Promise.resolve().then(function () {
        return controller.start();
      });
      return startDone.then(function (startVal) {
        return cssPromise.then(function (cssResult) {
          applyStylesheetFailure(page, controller, view, cssResult, function () { return disposed; });
          return startVal;
        });
      }, function (startErr) {
        return cssPromise.then(function (cssResult) {
          applyStylesheetFailure(page, controller, view, cssResult, function () { return disposed; });
          throw startErr;
        });
      });
    }, function (err) {
      if (disposed) {
        return cssPromise.then(function () {});
      }
      var message = (err && err.message) || "无法加载接口说明";
      var hooks = {};
      var failState = {
        connection: "error",
        connectionError: message,
        message: message,
        statusNotice: message
      };
      if (initialPanel) failState.activePanel = initialPanel;
      var view = mountView(page, hooks, failState, doc);
      window.__v3Business = { page: page, error: message, view: view };
      return cssPromise.then(function (cssResult) {
        if (disposed || !cssResult || cssResult.ok) return;
        var cssMsg = formatStylesheetError(cssResult.failed);
        if (view && typeof view.setState === "function") {
          if (page === "voice") {
            view.setState({ statusText: cssMsg, statusTone: "err" });
          } else {
            view.setState({ statusNotice: message + "；" + cssMsg });
          }
        }
      });
    });
  }

  function boot(page) {
    page = page || SCRIPT_PAGE;
    if (!shouldStartBusiness()) {
      var initState = Object.assign({}, (window.__V3_HOOKS__ && window.__V3_HOOKS__.initialState) || {});
      if (page === "home" && !initState.activePanel && !initState.panel) {
        var detected = detectInitialHomePanel(typeof location !== "undefined" ? location : null);
        if (detected) initState.activePanel = detected;
      }
      mountView(page, window.__V3_HOOKS__ || {}, initState);
      return Promise.resolve();
    }
    return startPage(page, window.__V3_BUSINESS_DEPS__ || {});
  }

  root.boot = boot;
  root.startPage = startPage;
  root.loadOpenApi = loadOpenApi;
  root.shouldStartBusiness = shouldStartBusiness;
  root.ensureStylesheetsLoaded = ensureStylesheetsLoaded;

  if (SCRIPT_PAGE && typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { boot(SCRIPT_PAGE); });
    } else {
      boot(SCRIPT_PAGE);
    }
  }
})(window.V3Business = window.V3Business || {});

/* 两页显式业务启动（home / voice）。已有 __V3_HOOKS__ 夹具且未要求启动业务时保持离线。 */
(function (root) {
  "use strict";

  var SCRIPT_PAGE = (typeof document !== "undefined" && document.currentScript && document.currentScript.getAttribute("data-page")) || "";

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

  function mountView(page, hooks, initialState) {
    var app = document.getElementById("app");
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
    var loc = deps.location || (typeof location !== "undefined" ? location : null);
    var hist = deps.history || (typeof history !== "undefined" ? history : null);
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
    return loadOpenApi(deps, context, fetchImpl).then(function (doc) {
      var catalog = root.createCatalog(doc);
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
      var view = mountView(page, controller.hooks, extraState);
      controller.bind(view);
      if (controller.destroy) {
        var hideTarget = deps.window || (typeof window !== "undefined" ? window : null);
        if (hideTarget && hideTarget.addEventListener) {
          hideTarget.addEventListener("pagehide", function () { controller.destroy(); });
        }
      }
      window.__v3Business = {
        page: page,
        context: context,
        catalog: catalog,
        client: client,
        controller: controller,
        view: view
      };
      return controller.start();
    }, function (err) {
      var message = (err && err.message) || "无法加载接口说明";
      var hooks = {};
      var failState = {
        connection: "error",
        connectionError: message,
        message: message,
        statusNotice: message
      };
      if (initialPanel) failState.activePanel = initialPanel;
      var view = mountView(page, hooks, failState);
      window.__v3Business = { page: page, error: message, view: view };
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

  if (SCRIPT_PAGE && typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { boot(SCRIPT_PAGE); });
    } else {
      boot(SCRIPT_PAGE);
    }
  }
})(window.V3Business = window.V3Business || {});

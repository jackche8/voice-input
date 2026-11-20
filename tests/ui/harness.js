/* 浏览器运行时断言。使用测试夹具，不碰真实配置、历史或剪贴板。 */
(function () {
  "use strict";
  const reportEl = document.getElementById("report");
  const failures = [];
  const passes = [];
  function fail(msg) { failures.push(msg); }
  function pass(msg) { passes.push(msg); }
  function assert(cond, msg) { if (cond) pass(msg); else fail(msg); }

  try {
    const copies = [];
    const confirms = [];
    const records = [];
    const translations = [];
    const themes = [];
    const panels = [];
    const polishToggles = [];
    const keywordSaves = [];
    const targetLangChanges = [];
    const setupActions = [];
    const hooks = {
      onCopy: function (payload) { copies.push(payload); },
      onConfirmResendChoice: function (payload) { confirms.push(payload); },
      onSendOriginal: function (payload) { confirms.push({ sendOriginal: true, payload: payload }); },
      onRecordStart: function () { records.push("start"); },
      onRecordEnd: function () { records.push("end"); },
      onRefreshStatus: function () { records.push("refresh"); },
      onTranslateText: function (payload) { translations.push(payload); },
      onToggleTheme: function (payload) { themes.push(payload); },
      onSelectPanel: function (payload) { panels.push(payload); },
      onTogglePolish: function (enabled) { polishToggles.push(enabled); },
      onSaveKeywords: function (val) { keywordSaves.push(val); },
      onChangeTargetLang: function (val) { targetLangChanges.push(val); },
      onInstallAll: function () { setupActions.push("install-all"); },
      onRetryComponent: function (name) { setupActions.push("retry:" + name); },
      onToggleAutostart: function (enabled) { setupActions.push("autostart:" + enabled); }
    };

    /* ---------- 1. 方案 C 统一工作台 (#home-root：5 面板 + 内置 6 标签设置) ---------- */
    const homeRoot = document.getElementById("home-root");
    const home = V3.home.mount(homeRoot, {}, hooks);
    const homeInitText = homeRoot.innerText;
    assert(homeInitText.indexOf("Voice Studio") !== -1, "主页展示 Voice Studio 品牌栏");
    assert(homeInitText.indexOf("未连接") !== -1, "主页默认显示未连接");
    assert(homeInitText.indexOf("Qwen3-ASR-1.7B") !== -1 && homeInitText.indexOf("qwen3.5:4b") !== -1, "主页侧栏展示两个固定模型");
    assert(!!homeRoot.querySelector('kbd[data-hotkey-badge="hotkey_ocr"]'), "主页侧栏展示第四热键 hotkey_ocr");
    assert(!homeRoot.querySelector('a[href="/setup"]'), "主页侧栏无 /setup 跳页外链");
    assert(homeRoot.querySelectorAll('button[data-panel]').length === 5, "主页侧栏提供 5 项页内切换面板");
    assert(!!homeRoot.querySelector('button[data-panel="setup"]'), "主页侧栏提供页内设置按钮");

    /* 切到内置设置面板：校验未连接默认态、6 子标签与原地切换主题 */
    homeRoot.querySelector('button[data-panel="setup"]').click();
    assert(home.getPanel() === "setup", "点击侧栏设置按钮原地切入 setup 面板");
    assert(panels.some(function (p) { return p && p.panel === "setup"; }), "切换 setup 面板触发 onSelectPanel");
    const setupTabs = Array.prototype.slice.call(homeRoot.querySelectorAll('.setup-tabs button[data-tab]'));
    assert(
      setupTabs.map(function (b) { return b.dataset.tab; }).join(",") === "models,voice,shot,keys,api,sys",
      "内置设置面板呈现 6 个子标签且不重复 lan/hist"
    );
    const setupOffText = homeRoot.innerText;
    assert(setupOffText.indexOf("未连接本地服务") !== -1, "设置面板默认显示未连接");
    assert(setupOffText.indexOf("全部就绪") === -1, "设置面板默认不显示全部就绪");
    assert(home.getState().asr.status === "unknown", "默认识别状态不是 ready");

    homeRoot.querySelector('.setup-tabs button[data-tab="api"]').click();
    const apiOffText = homeRoot.querySelector('[data-panel-view="setup"]').innerText;
    assert(apiOffText.indexOf("尚未读取") !== -1, "离线接口子标签声明尚未读取路径");
    assert(apiOffText.indexOf("/api/") === -1, "离线接口子标签没有冒充 /api/ 路径");

    homeRoot.querySelector('.setup-tabs button[data-tab="voice"]').click();
    const voiceSwOff = homeRoot.querySelector('[data-panel-view="setup"] button[role="switch"]');
    assert(!!voiceSwOff && voiceSwOff.getAttribute("aria-checked") === "mixed" && voiceSwOff.disabled, "未连接时润色开关为 mixed 且禁用");

    homeRoot.querySelector('.setup-tabs button[data-tab="shot"]').click();
    assert(homeRoot.querySelector('[data-panel-view="setup"]').innerText.indexOf("尚未读取") !== -1, "截图翻译未读取时显示尚未读取");

    homeRoot.querySelector('.setup-tabs button[data-tab="sys"]').click();
    assert(homeRoot.querySelector('[data-panel-view="setup"]').innerText.indexOf("尚未读取平台能力") !== -1, "系统检测未读取时声明尚未读取");

    const themeBeforeSetup = document.documentElement.dataset.theme;
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    assert(
      document.documentElement.dataset.theme !== themeBeforeSetup && home.getPanel() === "setup" && themes.length === 1,
      "在 setup 面板下切换主题同步改 data-theme 且停留在当前面板"
    );
    /* 切回默认主题并回到 translate 面板 */
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    homeRoot.querySelector('button[data-panel="translate"]').click();

    home.setState(V3Fixtures.homeConnected);
    const homeConnText = homeRoot.innerText;
    assert(homeConnText.indexOf("已就绪") !== -1, "主页在双模型就绪时显示已就绪");
    assert(homeRoot.querySelector('kbd[data-hotkey-badge="hotkey_ocr"]').textContent === "Ctrl+Alt+O", "主页侧栏正确格式化 hotkey_ocr");
    assert(homeRoot.innerHTML.indexOf("secret-should-be-stripped-by-ui") === -1, "主页视图层剥离明文 LAN URL，不写入 DOM");

    const srcInput = homeRoot.querySelector('textarea[data-role="translate-input"]');
    srcInput.focus();
    home.setState({ asr: { message: "后台状态轮询" }, hotkeyFocus: "hotkey_voice" });
    assert(document.activeElement === srcInput, "非 setup 面板时状态刷新绝不把焦点抢到热键框");

    srcInput.value = "Hello studio";
    srcInput.dispatchEvent(new Event("input", { bubbles: true }));
    homeRoot.querySelector('button[data-role="translate-submit"]').click();
    assert(
      translations.length === 1 && translations[0].text === "Hello studio" && translations[0].target_lang === "zh",
      "主页提交翻译仅通过 onTranslateText 回调传给业务层"
    );

    home.setState(V3Fixtures.homeTranslateError);
    const trErrOut = homeRoot.querySelector('[data-role="translate-output"]').innerText;
    assert(trErrOut.indexOf("本地模型无响应") !== -1, "主页翻译失败展示后端错误原文");
    assert(trErrOut.indexOf("Release notes 3.2") === -1, "主页翻译失败不拿 ocr_text 冒充译文");
    assert(homeRoot.querySelector('button[data-role="copy-translation"]').disabled === true, "翻译失败时禁用复制译文按钮");

    home.setState(V3Fixtures.homeTranslateResult);
    const trResOut = homeRoot.querySelector('[data-role="translate-output"]').innerText;
    assert(trResOut.indexOf("版本说明 3.2") !== -1, "主页翻译成功渲染译文");
    assert(trResOut.indexOf("Release notes 3.2") !== -1, "主页翻译成功在次级块展示 OCR 原文");
    homeRoot.querySelector('button[data-role="copy-translation"]').click();
    assert(
      copies.length >= 1 && copies[copies.length - 1].kind === "translation" && copies[copies.length - 1].text === "版本说明 3.2",
      "主页复制译文只走 onCopy 回调"
    );

    homeRoot.querySelector('button[data-panel="ocr"]').click();
    assert(panels.some(function (p) { return p && p.panel === "ocr"; }), "切换 OCR 面板触发 onSelectPanel");
    home.setState(V3Fixtures.homeOcrResult);
    const ocrOut = homeRoot.querySelector('[data-role="ocr-output"]').innerText;
    assert(ocrOut.indexOf("Hello OCR world") !== -1 && ocrOut.indexOf("你好 OCR 世界") !== -1, "主页 OCR 面板展示原文与可选译文");
    homeRoot.querySelector('button[data-role="copy-ocr"]').click();
    assert(
      copies[copies.length - 1].kind === "ocr" && copies[copies.length - 1].text === "Hello OCR world",
      "主页复制 OCR 原文走 onCopy 回调"
    );

    homeRoot.querySelector('button[data-panel="history"]').click();
    const homeHistText = homeRoot.querySelector('[data-role="history-list"]').innerText;
    assert(homeHistText.indexOf("已润色") !== -1 && homeHistText.indexOf("润色失败") !== -1, "主页历史面板展示润色状态");
    assert(homeHistText.indexOf("本机") !== -1 && homeHistText.indexOf("手机") !== -1, "主页历史面板展示来源");
    assert(!/已输入(?!到)/.test(homeHistText), "主页历史面板不谎称已输入");

    homeRoot.querySelector('button[data-panel="lan"]').click();
    const lanDom = homeRoot.querySelector('[data-role="lan-list"]');
    assert(lanDom.innerText.indexOf("sec••••ui") !== -1, "主页扫码面板显示脱敏网址");
    assert(lanDom.innerHTML.indexOf("secret-should-be-stripped-by-ui") === -1, "主页扫码面板绝不暴露明文令牌");
    const qrBox = lanDom.querySelector(".qr");
    const qrBg = qrBox ? window.getComputedStyle(qrBox).backgroundColor : "";
    assert(
      document.documentElement.dataset.theme === "dark" && qrBg === "rgb(255, 255, 255)",
      "暗色主题下二维码容器 .qr 背景保持纯白高对比静区（rgb(255, 255, 255)）"
    );
    const lanCopyBtn = lanDom.querySelector("button.btn");
    assert(!!lanCopyBtn && !lanCopyBtn.disabled, "脱敏网址存在时可点击复制网址");
    lanCopyBtn.click();
    assert(
      copies[copies.length - 1].kind === "lan_url" && copies[copies.length - 1].index === 0 && !copies[copies.length - 1].text,
      "主页复制局域网网址仅传 index 给业务层，不经 DOM 传递明文"
    );

    /* WCAG 2.1 运行时计算样式对比度校验辅助函数 */
    function parseRgb(str) {
      const m = String(str || "").match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
    }
    function lum(rgb) {
      const c = rgb.map(function (v) {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    }
    function wcagRatio(el) {
      const cs = window.getComputedStyle(el);
      const fg = parseRgb(cs.color);
      const bg = parseRgb(cs.backgroundColor);
      if (!fg || !bg) return 0;
      const l1 = lum(fg);
      const l2 = lum(bg);
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    }

    /* ---------- 2. 内置设置面板状态与 6 子标签验收 ---------- */
    homeRoot.querySelector('button[data-panel="setup"]').click();
    home.setState(V3Fixtures.setupFailedDownload);
    home.setSetupTab("models");
    const setupPane = homeRoot.querySelector('[data-panel-view="setup"]');
    const failText = setupPane.innerText;
    assert(failText.indexOf("下载中断") !== -1, "下载失败显示后端原因");
    assert(failText.indexOf("已就绪") === -1 || (failText.indexOf("语音识别") !== -1 && failText.indexOf("失败") !== -1), "失败不得显示成成功进度");
    const stepRows = Array.prototype.slice.call(setupPane.querySelectorAll("[data-step]"));
    assert(
      stepRows.map(function (r) { return r.dataset.step; }).join(",") === "ollama_install,wait_ollama,ollama_pull,asr_runtime_install,asr_download"
        && stepRows[0].innerText.indexOf("安装 Ollama 程序") !== -1 && stepRows[3].innerText.indexOf("安装语音识别运行环境") !== -1
        && stepRows[4].innerText.indexOf("失败") !== -1 && stepRows[4].innerText.indexOf("下载中断") !== -1,
      "一键装齐按固定顺序显示五步，失败步显示原因"
    );
    const installBtn = setupPane.querySelector('button[data-role="install-all"]');
    assert(!!installBtn && installBtn.textContent === "从此步重试" && !installBtn.disabled, "失败时同一按钮显示「从此步重试」");
    const visibleButtons = Array.prototype.slice.call(setupPane.querySelectorAll("button.btn:not([hidden])")).map(function (b) { return b.textContent; });
    assert(visibleButtons.join(",") === "从此步重试,重试", "模型页只有一键装齐与已停止服务的重试两个按钮，无逐项下载/安装/启动按钮：" + visibleButtons.join(","));
    installBtn.click();
    assert(setupActions.join(",") === "install-all", "点「从此步重试」走 onInstallAll 回调");
    const ollamaRow = setupPane.querySelector('[data-component="ollama"]');
    const asrRow = setupPane.querySelector('[data-component="asr"]');
    assert(
      ollamaRow.innerText.indexOf("已停止") !== -1 && ollamaRow.innerText.indexOf("端口 11434 被占用") !== -1
        && asrRow.innerText.indexOf("等待重启") !== -1 && !asrRow.querySelector("button:not([hidden])"),
      "组件显示状态与原因；只有已停止的组件出现重试"
    );
    ollamaRow.querySelector("button").click();
    assert(setupActions[1] === "retry:ollama", "组件「重试」走 onRetryComponent(name)");
    const autostart = setupPane.querySelector('button[role="switch"][aria-label="开机自启"]');
    assert(!!autostart && autostart.getAttribute("aria-checked") === "true", "开机自启开关显示配置值");
    autostart.click();
    assert(setupActions[2] === "autostart:false", "开机自启开关走 onToggleAutostart 回调");
    const primaryBtnDark = setupPane.querySelector("button.btn.primary");
    assert(!!primaryBtnDark && wcagRatio(primaryBtnDark) >= 4.5, "暗色主题主按钮 .btn.primary 运行时 WCAG 对比度 ≥ 4.5:1");

    home.setState(V3Fixtures.setupDownloading);
    const dlText = setupPane.innerText;
    assert(dlText.indexOf("42%") !== -1 && dlText.indexOf("下载中") !== -1, "仅在注入进度时显示百分比");
    assert(
      setupPane.querySelector('button[data-role="install-all"]') === installBtn && installBtn.disabled && installBtn.textContent === "安装中…",
      "装齐进行中重读状态不重建按钮（点击不被吞），按钮禁用并显示安装中"
    );

    home.setState(Object.assign({}, V3Fixtures.setupConnected, {
      apiPaths: [
        { method: "GET", path: "/api/setup/check", operationId: "setup_check_api_setup_check_get", access: "local" },
        { method: "POST", path: "/api/v1/translate", operationId: "translate_api_v1_translate_post", access: "local" }
      ]
    }));

    home.setSetupTab("voice");
    const kwArea0 = setupPane.querySelector("textarea.field");
    kwArea0.focus();
    kwArea0.value = "Qwen, Studio";
    kwArea0.dispatchEvent(new Event("input", { bubbles: true }));
    assert(home.getState().keywordsDraft === "Qwen, Studio", "关键词输入实时写入 keywordsDraft");

    /* 轮询刷新、切换主题、切换润色开关均不销毁关键词 textarea，不丢未保存草稿与焦点 */
    home.setState({ custom_keywords: ["BackendOld"], asr: { message: "后台轮询刷新" } });
    assert(
      setupPane.querySelector("textarea.field") === kwArea0 && kwArea0.value === "Qwen, Studio" && document.activeElement === kwArea0,
      "轮询刷新保留关键词未保存草稿、DOM 节点与焦点"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    home.setState({ theme: document.documentElement.dataset.theme });
    assert(
      setupPane.querySelector("textarea.field") === kwArea0 && kwArea0.value === "Qwen, Studio",
      "切换主题保留关键词未保存草稿与 DOM 节点"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    home.setState({ theme: "dark" });

    setupPane.querySelector('button[role="switch"]').click();
    home.setState({ polish_enabled: false });
    assert(polishToggles.length === 1 && polishToggles[0] === false, "语音与润色子标签切换开关走 onTogglePolish 回调");
    assert(
      setupPane.querySelector("textarea.field") === kwArea0 && kwArea0.value === "Qwen, Studio",
      "切换润色开关后仍保留未保存关键词草稿"
    );
    setupPane.querySelector("button.btn.primary").click();
    assert(keywordSaves.length === 1 && keywordSaves[0] === "Qwen, Studio", "保存关键词走 onSaveKeywords 回调");
    home.setState({ custom_keywords: ["Qwen", "Studio"], keywordsSaved: true, keywordsDraft: null });
    assert(
      home.getState().keywordsDraft === null && kwArea0.value === "Qwen、Studio" && setupPane.innerText.indexOf("已保存") !== -1,
      "关键词保存成功后清空草稿并同步回读值"
    );

    home.setSetupTab("shot");
    const shotSelect = setupPane.querySelector("select.field");
    shotSelect.value = "en";
    shotSelect.dispatchEvent(new Event("change", { bubbles: true }));
    assert(targetLangChanges.length === 1 && targetLangChanges[0] === "en", "截图翻译目标语言切换走 onChangeTargetLang 回调");

    home.setSetupTab("api");
    const apiOnText = setupPane.innerText;
    assert(apiOnText.indexOf("/api/setup/check") !== -1 && apiOnText.indexOf("a8f3••••7c21") !== -1, "开放接口子标签展示派生 apiPaths 与脱敏令牌");
    setupPane.querySelector("button.btn").click();
    assert(copies[copies.length - 1].kind === "token", "开放接口复制令牌仅传 kind=token 给业务层");

    home.setSetupTab("sys");
    const sysText = setupPane.innerText;
    assert(sysText.indexOf("portal 截图（Wayland）") !== -1 && sysText.indexOf("sudo apt install xclip") !== -1, "系统检测子标签展示平台能力与安装命令");

    homeRoot.querySelector('button[data-role="setup-refresh"]').click();
    assert(records.indexOf("refresh") !== -1, "设置面板右上角刷新状态按钮触发 onRefreshStatus");

    home.destroy();

    /* ---------- 3. 手机录音页 (#voice-root) ---------- */
    const voice = V3.mountVoice(document.getElementById("voice-root"), { hooks: hooks });
    const v0 = document.getElementById("voice-root").innerText;
    assert(v0.indexOf("未连接") !== -1, "手机页默认未连接");
    const mic0 = document.getElementById("voice-root").querySelector(".mic");
    assert(mic0 && mic0.disabled, "未连接时录音按钮禁用");

    voice.setState({ connection: "connected", tokenValid: true, phase: "idle", micEnabled: true, useFileUpload: false });
    const mic1 = document.getElementById("voice-root").querySelector(".mic");
    assert(mic1 === mic0 && !mic1.disabled, "连接且支持录音时麦克风按钮可按且保持固定 DOM 节点");
    assert(wcagRatio(mic1) >= 4.5, "暗色主题下 /voice 麦克风按钮 .mic WCAG 对比度 ≥ 4.5:1");

    voice.setState({ phase: "recording" });
    assert(wcagRatio(mic1) >= 4.5, "暗色主题下 /voice 录音中按钮 .mic.rec WCAG 对比度 ≥ 4.5:1");

    voice.setState({ phase: "idle", useFileUpload: true });
    const uploadLbl = document.getElementById("voice-root").querySelector(".upload");
    assert(!!uploadLbl && wcagRatio(uploadLbl) >= 4.5, "暗色主题下 /voice 上传按钮 .upload WCAG 对比度 ≥ 4.5:1");
    voice.setState({ useFileUpload: false });

    voice.setState(V3Fixtures.voicePolishFail);
    const v1 = document.getElementById("voice-root").innerText;
    assert(v1.indexOf("识别原文（未输入）") !== -1, "润色失败展示原文");
    assert(v1.indexOf("发送原文") !== -1, "润色失败提供发送原文");
    document.getElementById("voice-root").querySelector(".btn.primary").click();
    assert(confirms.some(function (c) { return c.sendOriginal && c.payload.text.indexOf("把这个函数拆成两个") !== -1; }), "发送原文只走回调");
    voice.destroy();

    /* ---------- 4. 统一工作台内置设置：四热键录入与 sidecar 就绪验收 ---------- */
    let rejectSave = false;
    const savedHotkeys = [];
    const keyHome = V3.home.mount(homeRoot, {
      connection: "connected",
      activePanel: "setup",
      setupTab: "keys",
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r"
    }, {
      onSaveHotkeys: function (keys) {
        savedHotkeys.push(keys);
        if (rejectSave) {
          const message = "请求无效：快捷键不能使用相同组合";
          keyHome.setState({
            connection: "connected",
            statusNotice: message,
            hotkeySaveError: message
          });
          return Promise.reject({ error: "invalid_request", message: message });
        }
        keyHome.setState({
          hotkeysSaved: true,
          hotkeyDrafts: null,
          hotkeyCapturing: null,
          hotkeyCaptureError: "",
          hotkeyCaptureWarning: "",
          hotkeySaveError: ""
        });
        return Promise.resolve();
      }
    });
    const keyRoot = homeRoot;
    const keySetupPane = keyRoot.querySelector('[data-panel-view="setup"]');
    function keyField(name) {
      return keySetupPane.querySelector('input[data-hotkey="' + name + '"]');
    }
    function fireKey(el, init) {
      const ev = new KeyboardEvent("keydown", Object.assign({
        bubbles: true,
        cancelable: true,
        ctrlKey: false,
        altKey: false,
        shiftKey: false,
        metaKey: false
      }, init));
      return { prevented: !el.dispatchEvent(ev) };
    }

    assert(!!V3.hotkey && typeof V3.hotkey.fromKeyboardEvent === "function", "harness 已加载 hotkey-capture");
    const voice0 = keyField("hotkey_voice");
    assert(!!voice0 && voice0.value === "ctrl+alt+z", "已保存的合法组合仍显示");
    assert(!!keyField("hotkey_ocr") && keyField("hotkey_ocr").value === "ctrl+alt+o", "设置面板展示第四热键 hotkey_ocr");
    voice0.focus();
    assert(keyHome.getState().hotkeyCapturing === "hotkey_voice", "聚焦进入录入态");
    assert(document.activeElement === keyField("hotkey_voice"), "进入录入后焦点仍在该输入框");
    assert(keyField("hotkey_voice").getAttribute("data-capturing") === "true", "录入态标记 data-capturing=true");
    const rec = fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyQ", ctrlKey: true, altKey: true });
    assert(rec.prevented, "录入完整组合时 preventDefault");
    assert(keyHome.getState().hotkeyDrafts && keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q", "草稿写入 viewstate");
    assert(keyField("hotkey_voice").value === "ctrl+alt+q", "输入框显示草稿");
    assert(document.activeElement === keyField("hotkey_voice"), "录入后焦点不丢");

    const beforePending = keyHome.getState().hotkeyDrafts.hotkey_voice;
    const pend = fireKey(keyField("hotkey_voice"), { key: "Control", code: "ControlLeft", ctrlKey: true });
    assert(!pend.prevented, "仅修饰键不 preventDefault");
    assert(keyHome.getState().hotkeyCapturing === "hotkey_voice", "仅修饰键保持录入态");
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === beforePending, "仅修饰键不改草稿");

    const esc = fireKey(keyField("hotkey_voice"), { key: "Escape", code: "Escape" });
    assert(esc.prevented, "Esc 取消时 preventDefault");
    assert(keyHome.getState().hotkeyCapturing == null, "Esc 结束录入");
    assert(keyField("hotkey_voice").value === "ctrl+alt+z", "Esc 恢复已保存值");

    keyField("hotkey_ocr").focus();
    fireKey(keyField("hotkey_ocr"), { key: "O", code: "KeyO", shiftKey: true });
    assert(
      keyHome.getState().hotkeyDrafts && keyHome.getState().hotkeyDrafts.hotkey_ocr === "shift+o" && /Shift/.test(keySetupPane.innerText),
      "hotkey_ocr 录入单独 Shift+Key 时给出警告横幅"
    );
    fireKey(keyField("hotkey_ocr"), { key: "p", code: "KeyP", ctrlKey: true, altKey: true });
    assert(keyHome.getState().hotkeyDrafts.hotkey_ocr === "ctrl+alt+p" && !keyHome.getState().hotkeyCaptureWarning, "重新录入安全组合后清空警告");

    keyField("hotkey_voice").focus();
    fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyQ", ctrlKey: true, altKey: true });
    assert((keyHome.getState().hotkeyDrafts || {}).hotkey_voice === "ctrl+alt+q", "Esc 后仍聚焦时可重新录入");
    assert(keyHome.getState().hotkeyCapturing === "hotkey_voice", "重新录入后回到录入态");
    const capBeforePoll = keyHome.getState().hotkeyCapturing;
    keyHome.setState({ asr: { message: "轮询刷新", status: "ready", sidecarRunning: false } });
    assert((keyHome.getState().hotkeyDrafts || {}).hotkey_voice === "ctrl+alt+q", "轮询重渲染不丢草稿");
    assert(keyHome.getState().hotkeyCapturing === capBeforePoll, "轮询不取消录入态");
    assert(document.activeElement === keyField("hotkey_voice"), "轮询不丢焦点");
    assert(keyField("hotkey_voice").value === "ctrl+alt+q", "轮询不改用户输入");

    const tabEv = fireKey(keyField("hotkey_voice"), { key: "Tab", code: "Tab" });
    assert(!tabEv.prevented, "单独 Tab 不 preventDefault，留给导航");
    assert(keyHome.getState().hotkeyCapturing == null, "单独 Tab 结束录入");
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q", "Tab 离开仍保留草稿");

    keyField("hotkey_voice").focus();
    const ctab = fireKey(keyField("hotkey_voice"), { key: "Tab", code: "Tab", ctrlKey: true });
    assert(ctab.prevented, "Ctrl+Tab 被录入并 preventDefault");
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+tab", "Ctrl+Tab 记为 ctrl+tab");

    keyField("hotkey_screenshot").focus();
    const stab = fireKey(keyField("hotkey_screenshot"), { key: "Tab", code: "Tab", shiftKey: true });
    assert(!stab.prevented, "Shift+Tab 无 ctrl/alt/super 时用于反向导航");
    assert(keyHome.getState().hotkeyCapturing == null, "Shift+Tab 结束录入");

    const savesBeforeBlur = savedHotkeys.length;
    keyField("hotkey_repeat").focus();
    fireKey(keyField("hotkey_repeat"), { key: "r", code: "KeyR", ctrlKey: true, shiftKey: true });
    assert(keyHome.getState().hotkeyDrafts.hotkey_repeat === "ctrl+shift+r", "重复键草稿");
    keyField("hotkey_repeat").blur();
    assert(keyHome.getState().hotkeyCapturing == null, "blur 结束录入");
    assert(keyHome.getState().hotkeyDrafts.hotkey_repeat === "ctrl+shift+r", "blur 保留草稿且不自动保存");
    assert(savedHotkeys.length === savesBeforeBlur, "blur 不调用保存");

    keyField("hotkey_voice").focus();
    const unsup = fireKey(keyField("hotkey_voice"), { key: "ArrowLeft", code: "ArrowLeft", ctrlKey: true });
    assert(!unsup.prevented, "不支持的键不 preventDefault、不伪造成功");
    assert(/不支持/.test(keyHome.getState().hotkeyCaptureError || keySetupPane.innerText), "不支持按键显示说明");
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+tab", "不支持按键不改已有草稿");

    fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyA", ctrlKey: true, altKey: true });
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+a", "非 US 布局按物理 code 记录");
    assert(/键帽/.test(keyHome.getState().hotkeyCaptureHint || keySetupPane.innerText), "物理键位与键帽不同时给出提示");

    rejectSave = true;
    keySetupPane.querySelector("button.primary").click();
    assert(/不能使用相同组合|请求无效/.test(keyRoot.innerText + " " + (keyHome.getState().hotkeySaveError || "") + " " + (keyHome.getState().statusNotice || "")), "互相重复显示后端错误");
    assert(keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+a", "保存失败不丢草稿");
    assert(keyHome.getState().hotkeysSaved !== true, "失败不显示已保存");
    assert(document.activeElement && document.activeElement.getAttribute("data-hotkey"), "保存失败后快捷键区焦点仍在输入框");

    assert(keySetupPane.innerText.indexOf("以后端返回的提示为准") === -1, "已删除空承诺提示");
    assert(keySetupPane.innerText.indexOf("已生效") === -1, "保存文案不含已生效");
    assert(keySetupPane.innerText.indexOf("修改快捷键后需重启应用生效") !== -1, "快捷键页如实提示修改后需重启应用生效");
    assert(keySetupPane.innerText.indexOf("systemctl") === -1 && keySetupPane.innerText.indexOf("restart.sh") === -1, "前端不自行推荐重启命令");

    rejectSave = false;
    const savedCount = savedHotkeys.length;
    keySetupPane.querySelector("button.primary").click();
    assert(savedHotkeys.length === savedCount + 1 && savedHotkeys[savedHotkeys.length - 1].hotkey_ocr === "ctrl+alt+p", "保存提交包含 hotkey_ocr 的四项草稿");
    assert(keySetupPane.innerText.indexOf("已保存") !== -1, "保存成功只显示已保存");
    assert(keySetupPane.innerText.indexOf("已生效") === -1, "已保存旁不出现已生效");

    keyHome.setState({
      hotkeyDrafts: null,
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
      hotkeysSaved: false
    });
    assert(
      keyField("hotkey_voice").value === "ctrl+alt+z"
        && keyField("hotkey_screenshot").value === "ctrl+alt+x"
        && keyField("hotkey_ocr").value === "ctrl+alt+o"
        && keyField("hotkey_repeat").value === "ctrl+alt+r",
      "已保存的四项合法组合在无草稿时仍可显示和配置"
    );

    keyField("hotkey_voice").focus();
    fireKey(keyField("hotkey_voice"), { key: "v", code: "KeyV", ctrlKey: true, altKey: true });
    keyField("hotkey_ocr").focus();
    fireKey(keyField("hotkey_ocr"), { key: "k", code: "KeyK", ctrlKey: true, altKey: true });
    assert(
      keyHome.getState().hotkeyDrafts
        && keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+v"
        && keyHome.getState().hotkeyDrafts.hotkey_ocr === "ctrl+alt+k",
      "同时编辑两条热键草稿时两者均保留"
    );
    const escFirst = fireKey(keyField("hotkey_ocr"), { key: "Escape", code: "Escape" });
    assert(escFirst.prevented, "双草稿下对当前条按 Esc 时 preventDefault");
    assert(keyField("hotkey_ocr").value === "ctrl+alt+o", "双草稿下按 Esc 仅回滚当前条回已保存值");
    assert(
      keyField("hotkey_voice").value === "ctrl+alt+v"
        && keyHome.getState().hotkeyDrafts
        && keyHome.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+v"
        && keyHome.getState().hotkeyDrafts.hotkey_ocr == null,
      "双草稿下按 Esc 保留另一条热键草稿"
    );
    keyField("hotkey_voice").focus();
    const escSecond = fireKey(keyField("hotkey_voice"), { key: "Escape", code: "Escape" });
    assert(escSecond.prevented, "对剩余草稿按 Esc 时 preventDefault");
    assert(keyField("hotkey_voice").value === "ctrl+alt+z", "对剩余草稿按 Esc 回滚为已保存值");
    assert(keyHome.getState().hotkeyDrafts === null, "全部热键草稿均按 Esc 后 hotkeyDrafts 清空为 null");

    /* 真实鼠标点击时序回归：mousedown → blur → mouseup → click 严禁卸载目标节点或吞掉首次点击 */
    function simulateRealClick(fromInput, targetEl) {
      targetEl.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      fromInput.blur();
      const stillConnected = targetEl.isConnected === true;
      if (stillConnected && targetEl.tagName === "INPUT") targetEl.focus();
      if (stillConnected) {
        targetEl.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
        targetEl.click();
      }
      return stillConnected;
    }

    const voiceElPersist = keyField("hotkey_voice");
    const ocrElPersist = keyField("hotkey_ocr");
    const saveBtnPersist = keySetupPane.querySelector("button.primary");
    const modelsTabPersist = keySetupPane.querySelector('.setup-tabs button[data-tab="models"]');

    /* 1) 热键录入中直接一次点击「保存快捷键」 */
    voiceElPersist.focus();
    fireKey(voiceElPersist, { key: "m", code: "KeyM", ctrlKey: true, altKey: true });
    const savesBeforeReal = savedHotkeys.length;
    const saveConnected = simulateRealClick(voiceElPersist, saveBtnPersist);
    assert(
      saveConnected && keySetupPane.querySelector("button.primary") === saveBtnPersist && savedHotkeys.length === savesBeforeReal + 1,
      "热键录入中 mousedown→blur→mouseup→click 一次点击「保存快捷键」立即触发且按钮未被卸载"
    );

    /* 2) 热键录入中（即使刚经历过保存冲突报错）直接一次点击另一个热键框 */
    rejectSave = true;
    voiceElPersist.focus();
    fireKey(voiceElPersist, { key: "n", code: "KeyN", ctrlKey: true, altKey: true });
    simulateRealClick(voiceElPersist, saveBtnPersist);
    assert(document.activeElement === voiceElPersist, "保存冲突后焦点回归原热键框");
    rejectSave = false;
    const ocrConnected = simulateRealClick(voiceElPersist, ocrElPersist);
    assert(
      ocrConnected && keyField("hotkey_ocr") === ocrElPersist && document.activeElement === ocrElPersist && keyHome.getState().hotkeyCapturing === "hotkey_ocr",
      "保存冲突后直接一次点击另一个热键框立即切焦进入录入态，不被旧错误抢焦"
    );

    /* 3) 热键录入中直接一次点击子标签切换 */
    const tabConnected = simulateRealClick(ocrElPersist, modelsTabPersist);
    assert(
      tabConnected && keySetupPane.querySelector('.setup-tabs button[data-tab="models"]') === modelsTabPersist && keyHome.getSetupTab() === "models",
      "热键录入中 mousedown→blur→mouseup→click 一次点击子标签立即切换且标签按钮未被卸载"
    );

    keyHome.setSetupTab("models");
    keyHome.setState({
      connection: "connected",
      asr: { status: "ready", sidecarRunning: false, sizeLabel: "3.5 GB", message: "识别服务尚未启动" },
      ollama: { modelStatus: "ready", running: true, version: "0.12.6" }
    });
    const asrTextOff = keySetupPane.querySelectorAll(".row")[0].innerText;
    assert(keyRoot.innerText.indexOf("全部就绪") === -1, "权重齐未启动 sidecar 不显示全部就绪");
    assert(asrTextOff.indexOf("已就绪") === -1, "权重齐未启动不显示已就绪");
    assert(asrTextOff.indexOf("未启动") !== -1, "权重齐未启动显示未启动角标");
    assert(asrTextOff.indexOf("识别服务尚未启动") !== -1, "未启动时展示后端原文，不吞消息");
    assert(keyRoot.innerText.indexOf("有事项需要处理") !== -1, "未启动时 header 为需处理而非全部就绪");

    keyHome.setState({ asr: { status: "ready", sidecarRunning: undefined, message: "底层未给出运行状态" } });
    const asrTextMissing = keySetupPane.querySelectorAll(".row")[0].innerText;
    assert(asrTextMissing.indexOf("已就绪") === -1, "缺 sidecarRunning 字段时不是已就绪");
    assert(asrTextMissing.indexOf("底层未给出运行状态") !== -1, "缺字段时仍展示后端说明");

    keyHome.setState({
      asr: { status: "ready", sidecarRunning: true, sizeLabel: "3.5 GB", message: "" },
      ollama: { modelStatus: "ready", running: true, version: "0.12.6" }
    });
    const asrTextOn = keySetupPane.querySelectorAll(".row")[0].innerText;
    assert(asrTextOn.indexOf("已就绪") !== -1, "status ready 且 sidecarRunning===true 才显示已就绪");
    assert(keyRoot.innerText.indexOf("全部就绪") !== -1, "两项都就绪时 header 全部就绪");

    keyHome.setState({ asr: { status: "unknown", sidecarRunning: null, message: "" } });
    assert(keySetupPane.querySelectorAll(".row")[0].innerText.indexOf("已就绪") === -1, "底层未知不是 ready");
    keyHome.destroy();

    window.__V3_HARNESS__ = { passes: passes, failures: failures, copies: copies, confirms: confirms };
  } catch (err) {
    fail("harness 异常：" + err.stack);
  }

  reportEl.textContent = JSON.stringify({ passes: passes, failures: failures }, null, 2);
  document.title = failures.length ? "HARNESS_FAIL " + failures.length : "HARNESS_PASS " + passes.length;
})();

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
    const translateInputs = [];
    const swapCalls = [];
    const clearCalls = [];
    const imageSubmits = [];
    const themes = [];
    const panels = [];
    const polishToggles = [];
    const keywordSaves = [];
    const langPairChanges = [];
    const autoTranslateToggles = [];
    const audioInputChanges = [];
    let audioInputRefreshes = 0;
    const setupActions = [];
    const hooks = {
      onCopy: function (payload) { copies.push(payload); },
      onConfirmResendChoice: function (payload) { confirms.push(payload); },
      onSendOriginal: function (payload) { confirms.push({ sendOriginal: true, payload: payload }); },
      onRecordStart: function () { records.push("start"); },
      onRecordEnd: function () { records.push("end"); },
      onRefreshStatus: function () { records.push("refresh"); },
      onTranslateInput: function (payload) { translateInputs.push(payload); },
      onTranslateText: function (payload) { translations.push(payload); },
      onSwapDirection: function (payload) { swapCalls.push(payload); },
      onClearTranslate: function () { clearCalls.push("clear"); },
      onSubmitImage: function (payload) { imageSubmits.push(payload); },
      onToggleTheme: function (payload) { themes.push(payload); },
      onSelectPanel: function (payload) { panels.push(payload); },
      onTogglePolish: function (enabled) { polishToggles.push(enabled); },
      onSaveKeywords: function (val) { keywordSaves.push(val); },
      onChangeLangPair: function (val) { langPairChanges.push(val); },
      onToggleAutoTranslate: function (enabled) { autoTranslateToggles.push(enabled); },
      onChangeAudioInput: function (val) { audioInputChanges.push(val); },
      onRefreshAudioInputs: function () { audioInputRefreshes += 1; },
      onInstallAll: function () { setupActions.push("install-all"); },
      onRetryComponent: function (name) { setupActions.push("retry:" + name); },
      onToggleAutostart: function (enabled) { setupActions.push("autostart:" + enabled); }
    };

    /* ---------- 1. 方案 C 统一工作台 (#home-root：5 面板 + 内置 6 标签设置) ---------- */
    const homeRoot = document.getElementById("home-root");
    const home = V3.home.mount(homeRoot, {}, hooks);
    const homeInitText = homeRoot.innerText;
    const initThemeBadge = homeRoot.querySelector('button[data-action="toggle-theme"] .badge');
    assert(
      document.documentElement.dataset.theme === "light" && home.getState().theme === "light",
      "主页首次挂载默认亮色主题（data-theme=light）"
    );
    assert(
      !!initThemeBadge && initThemeBadge.textContent === "浅色",
      "主页首次挂载主题徽章默认显示浅色"
    );
    home.setState({ theme: "unknown" });
    assert(
      document.documentElement.dataset.theme === "light" && home.getState().theme === "light" && initThemeBadge.textContent === "浅色",
      "未知 theme 值归一化回默认亮色（light / 浅色）"
    );
    assert(homeInitText.indexOf("Voice Studio") !== -1, "主页展示 Voice Studio 品牌栏");
    assert(homeInitText.indexOf("未连接") !== -1, "主页默认显示未连接");
    assert(homeInitText.indexOf("Qwen3-ASR-1.7B") !== -1 && homeInitText.indexOf("qwen3.5:4b") !== -1, "主页侧栏展示两个固定模型");
    assert(!!homeRoot.querySelector('kbd[data-hotkey-badge="hotkey_ocr"]'), "主页侧栏展示第四热键 hotkey_ocr");
    assert(!!homeRoot.querySelector('kbd[data-hotkey-badge="hotkey_annotate"]'), "主页侧栏展示第五热键 hotkey_annotate");
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
    const voiceOffPane = homeRoot.querySelector('[data-panel-view="setup"]');
    const voiceSwOff = voiceOffPane.querySelector('button[role="switch"]');
    const audioOffSelect = voiceOffPane.querySelector('select[data-role="setup-audio-input"]');
    const audioOffRefresh = voiceOffPane.querySelector('button[data-role="setup-audio-input-refresh"]');
    assert(!!voiceSwOff && voiceSwOff.getAttribute("aria-checked") === "mixed" && voiceSwOff.disabled, "未连接时润色开关为 mixed 且禁用");
    assert(
      !!audioOffSelect && audioOffSelect.disabled && audioOffSelect.options.length === 1 && audioOffSelect.options[0].textContent === "尚未读取",
      "未连接时麦克风下拉显示尚未读取且禁用"
    );
    assert(
      !!audioOffRefresh && audioOffRefresh.disabled && !audioOffRefresh.classList.contains("primary"),
      "未连接时刷新设备按钮禁用且非 primary 样式"
    );

    homeRoot.querySelector('.setup-tabs button[data-tab="shot"]').click();
    const shotOffPane = homeRoot.querySelector('[data-panel-view="setup"]');
    const shotOffSelect = shotOffPane.querySelector('select[data-role="setup-lang-pair"]');
    const shotOffAutoSw = shotOffPane.querySelector('button[data-role="setup-auto-translate"]');
    assert(shotOffPane.innerText.indexOf("尚未读取") !== -1 && !!shotOffSelect && shotOffSelect.disabled, "翻译设置未读取时语言对显示尚未读取且禁用");
    assert(!!shotOffAutoSw && shotOffAutoSw.getAttribute("aria-checked") === "mixed" && shotOffAutoSw.disabled, "未连接时自动翻译开关为 mixed 且禁用");

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
    assert(homeRoot.querySelector('kbd[data-hotkey-badge="hotkey_annotate"]').textContent === "Ctrl+Alt+A", "主页侧栏正确格式化 hotkey_annotate");
    assert(homeRoot.innerHTML.indexOf("secret-should-be-stripped-by-ui") === -1, "主页视图层剥离明文 LAN URL，不写入 DOM");

    const srcInput = homeRoot.querySelector('textarea[data-role="translate-input"]');
    const dirBadge = homeRoot.querySelector('[data-role="translate-direction-badge"]');
    const swapBtn = homeRoot.querySelector('button[data-role="translate-swap"]');
    const pairSelect = homeRoot.querySelector('select[data-role="translate-lang-pair"]');
    const autoHint = homeRoot.querySelector('[data-role="translate-auto-hint"]');

    assert(dirBadge.textContent === "自动判定" && swapBtn.disabled === true && swapBtn.getAttribute("aria-pressed") === "false", "翻译空态展示自动判定且禁用互换按钮");
    assert(pairSelect.options.length === 1 && pairSelect.value === "zh-en" && pairSelect.options[0].textContent === "中 ⇄ 英", "工作台语言对下拉仅含中⇄英且选中 zh-en");
    pairSelect.dispatchEvent(new Event("change", { bubbles: true }));
    assert(langPairChanges.length === 1 && langPairChanges[0] === "zh-en", "工作台切换语言对走 onChangeLangPair 回调");
    assert(autoHint.textContent.indexOf("1s 自动翻译") !== -1, "自动翻译开启时左盒头展示停顿 1s 提示");

    /* 校验 studio.css 仅对翻译面板 .studio-box 设视口级 max-height（不再硬顶 560px 留白），且不裁切 OCR 预览 */
    const trBoxes = homeRoot.querySelectorAll('[data-panel-view="translate"] .studio-box');
    const trBoxEl = trBoxes[0];
    const trLastBoxEl = trBoxes[trBoxes.length - 1];
    const trSplitEl = homeRoot.querySelector('[data-panel-view="translate"] .studio-split');
    const trBoxMaxH = window.getComputedStyle(trBoxEl).maxHeight;
    const expectedMaxH = Math.max(0, window.innerHeight - 114);
    assert(
      trBoxMaxH !== "none" && Math.abs(parseFloat(trBoxMaxH) - expectedMaxH) <= 4,
      "翻译面板 .studio-box 随视口高度自适应 max-height（无 560px 硬上限）：" + trBoxMaxH + " vs " + expectedMaxH
    );
    const trGapBottom = Math.abs(trSplitEl.getBoundingClientRect().bottom - trLastBoxEl.getBoundingClientRect().bottom);
    assert(trGapBottom <= 2, "翻译面板 .studio-box 填满父容器高度无底部大块留白：gap=" + trGapBottom.toFixed(1));

    srcInput.focus();
    home.setState({ asr: { message: "后台状态轮询" }, hotkeyFocus: "hotkey_voice" });
    assert(document.activeElement === srcInput, "非 setup 面板时状态刷新绝不把焦点抢到热键框");

    srcInput.value = "Hello studio";
    srcInput.dispatchEvent(new Event("input", { bubbles: true }));
    assert(
      translateInputs.length === 1 && translateInputs[0].text === "Hello studio" && translations.length === 0,
      "文本输入仅派发 onTranslateInput，纯 UI 不自行发起翻译请求"
    );
    assert(swapBtn.disabled === false, "输入非空后启用互换按钮");

    home.setState({ translate: { debouncePending: true } });
    assert(autoHint.classList.contains("pending") && autoHint.innerText.indexOf("等待停顿") !== -1, "防抖等待态在左盒头显示等待停顿提示");

    homeRoot.querySelector('button[data-role="translate-submit"]').click();
    assert(
      translations.length === 1 && translations[0].text === "Hello studio" && translations[0].lang_pair === "zh-en" && translations[0].trigger === "manual",
      "主页提交翻译仅通过 onTranslateText({text, lang_pair, trigger:'manual'}) 传给业务层"
    );

    home.setState(V3Fixtures.homeTranslateError);
    const trErrOut = homeRoot.querySelector('[data-role="translate-output"]').innerText;
    assert(trErrOut.indexOf("本地模型无响应") !== -1, "主页翻译失败展示后端错误原文");
    assert(trErrOut.indexOf("Release notes 3.2") === -1, "主页翻译失败不拿 ocr_text 冒充译文");
    assert(homeRoot.querySelector('button[data-role="copy-translation"]').disabled === true, "翻译失败时禁用复制译文按钮");

    home.setState(Object.assign({}, V3Fixtures.homeTranslateResult, {
      translate: Object.assign({}, V3Fixtures.homeTranslateResult.translate, {
        source_lang: "zh",
        target_lang: "en",
        chunks: 3
      })
    }));
    const trResOut = homeRoot.querySelector('[data-role="translate-output"]').innerText;
    const trMetaText = homeRoot.querySelector('[data-role="translate-meta"]').textContent;
    assert(trResOut.indexOf("版本说明 3.2") !== -1, "主页翻译成功渲染译文");
    assert(trResOut.indexOf("Release notes 3.2") !== -1, "主页翻译成功在次级块展示 OCR 原文");
    assert(dirBadge.classList.contains("accent") && dirBadge.textContent === "自动：中 → 英", "自动译文态展示蓝色方向徽章 自动：中 → 英");
    assert(trMetaText.indexOf("共 3 段") !== -1, "多段长文本在底部元信息展示分段数");
    homeRoot.querySelector('button[data-role="copy-translation"]').click();
    assert(
      copies.length >= 1 && copies[copies.length - 1].kind === "translation" && copies[copies.length - 1].text === "版本说明 3.2",
      "主页复制译文只走 onCopy 回调"
    );

    swapBtn.click();
    assert(
      swapCalls.length === 1 && swapCalls[0].text === "Hello studio" && swapCalls[0].lang_pair === "zh-en",
      "点击互换按钮仅派发 onSwapDirection({text, lang_pair})"
    );

    /* 手动锁定态在 result / loading / error 下均保留琥珀锁徽章可见，loading 时禁用互换按钮 */
    home.setState({
      translate: {
        phase: "result",
        source_lang: "en",
        target_lang: "zh",
        direction_mode: "manual",
        manualOverride: { source_lang: "en", target_lang: "zh" },
        text: "手动锁译文"
      }
    });
    assert(
      dirBadge.classList.contains("warn") && dirBadge.innerText.indexOf("手动：英 → 中") !== -1 && !!dirBadge.querySelector('use[href="#i-lock"]') && swapBtn.getAttribute("aria-pressed") === "true" && swapBtn.innerText.indexOf("恢复自动") !== -1,
      "手动锁定态展示琥珀锁徽章 手动：英 → 中 且按钮显示恢复自动"
    );
    home.setState({ translate: { phase: "loading" } });
    assert(
      dirBadge.classList.contains("warn") && dirBadge.innerText.indexOf("手动：英 → 中") !== -1 && swapBtn.disabled === true,
      "手动锁定的 loading 态保留琥珀锁徽章可见且禁用互换按钮"
    );
    home.setState({ translate: { phase: "error", error: "translate_failed", message: "超时" } });
    assert(
      dirBadge.classList.contains("warn") && dirBadge.innerText.indexOf("手动：英 → 中") !== -1 && swapBtn.disabled === false,
      "手动锁定的 error 态保留琥珀锁徽章可见且允许再次点击解除锁定"
    );

    /* 工作台清空按钮重置输入框、方向徽章与锁态，并派发 onClearTranslate */
    homeRoot.querySelector('button[data-role="translate-clear"]').click();
    assert(
      srcInput.value === "" && dirBadge.textContent === "自动判定" && swapBtn.disabled === true && clearCalls.length === 1,
      "点击清空按钮清空输入、恢复自动判定并派发 onClearTranslate"
    );

    /* 工作台粘贴截图派发 onSubmitImage({file, mode:'translate', lang_pair:'zh-en', panel:'translate'}) */
    const trDropBox = homeRoot.querySelector('[data-panel-view="translate"] .studio-box');
    const mockImg = new File(["png"], "shot.png", { type: "image/png" });
    const dropEv = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(dropEv, "dataTransfer", { value: { files: [mockImg] } });
    trDropBox.dispatchEvent(dropEv);
    assert(
      imageSubmits.length === 1 && imageSubmits[0].file === mockImg && imageSubmits[0].mode === "translate" && imageSubmits[0].lang_pair === "zh-en" && imageSubmits[0].panel === "translate",
      "工作台拖入图片派发 onSubmitImage({file, mode:'translate', lang_pair:'zh-en', panel:'translate'})"
    );

    homeRoot.querySelector('button[data-panel="ocr"]').click();
    assert(panels.some(function (p) { return p && p.panel === "ocr"; }), "切换 OCR 面板触发 onSelectPanel");
    const ocrBoxMaxH = window.getComputedStyle(homeRoot.querySelector('[data-panel-view="ocr"] .studio-box')).maxHeight;
    assert(ocrBoxMaxH === "none", "OCR 面板 .studio-box 不受 max-height 裁切：" + ocrBoxMaxH);
    const ocrSelect = homeRoot.querySelector('select[data-role="ocr-mode"]');
    const ocrOptionVals = Array.prototype.slice.call(ocrSelect.options).map(function (o) { return o.value; });
    assert(ocrOptionVals.join(",") === "raw,zh-en", "OCR 面板下拉收敛为 raw 与 zh-en 二选一：" + ocrOptionVals.join(","));

    const ocrDrop = homeRoot.querySelector('[data-panel-view="ocr"] .ocr-dropzone');
    const ocrRawDrop = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(ocrRawDrop, "dataTransfer", { value: { files: [mockImg] } });
    ocrDrop.dispatchEvent(ocrRawDrop);
    assert(
      imageSubmits.length === 2 && imageSubmits[1].mode === "raw" && imageSubmits[1].panel === "ocr" && !("lang_pair" in imageSubmits[1]),
      "OCR 面板 raw 模式提交图片派发 mode:'raw', panel:'ocr'"
    );

    ocrSelect.value = "zh-en";
    ocrSelect.dispatchEvent(new Event("change", { bubbles: true }));
    const ocrTrDrop = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(ocrTrDrop, "dataTransfer", { value: { files: [mockImg] } });
    ocrDrop.dispatchEvent(ocrTrDrop);
    assert(
      imageSubmits.length === 3 && imageSubmits[2].mode === "translate" && imageSubmits[2].lang_pair === "zh-en" && imageSubmits[2].panel === "ocr",
      "OCR 面板翻译模式提交图片派发 mode:'translate', lang_pair:'zh-en', panel:'ocr'"
    );

    home.setState(V3Fixtures.homeOcrResult);
    const ocrOut = homeRoot.querySelector('[data-role="ocr-output"]').innerText;
    assert(ocrOut.indexOf("Hello OCR world") !== -1 && ocrOut.indexOf("你好 OCR 世界") !== -1, "主页 OCR 面板展示原文与可选译文");
    assert(ocrOut.indexOf("译文（英 → 中）") !== -1, "OCR 面板译文块标题使用 directionLabel 渲染方向");
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
    const qrBgLight = qrBox ? window.getComputedStyle(qrBox).backgroundColor : "";
    assert(
      document.documentElement.dataset.theme === "light" && qrBgLight === "rgb(255, 255, 255)",
      "亮色主题下二维码容器 .qr 背景保持纯白高对比静区（rgb(255, 255, 255)）"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    const qrBgDark = qrBox ? window.getComputedStyle(qrBox).backgroundColor : "";
    assert(
      document.documentElement.dataset.theme === "dark" && qrBgDark === "rgb(255, 255, 255)",
      "暗色主题下二维码容器 .qr 背景保持纯白高对比静区（rgb(255, 255, 255)）"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    assert(document.documentElement.dataset.theme === "light", "二维码双主题对比校验后切回亮色主题");
    const lanCopyBtn = lanDom.querySelector("button.btn");
    assert(!!lanCopyBtn && !lanCopyBtn.disabled, "脱敏网址存在时可点击复制网址");
    lanCopyBtn.click();
    assert(
      copies[copies.length - 1].kind === "lan_url" && copies[copies.length - 1].index === 0 && !copies[copies.length - 1].text,
      "主页复制局域网网址仅传 index 给业务层，不经 DOM 传递明文"
    );

    /* WCAG 2.1 运行时计算样式对比度校验辅助函数 */
    function parseColor(str) {
      const m = String(str || "").match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)$/);
      if (!m) return null;
      const alpha = m[4] === undefined ? 1 : Number(m[4]);
      return { rgb: [Number(m[1]), Number(m[2]), Number(m[3])], alpha: alpha };
    }
    function lum(rgb) {
      const c = rgb.map(function (v) {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    }
    function pairRatio(fgStr, bgStr) {
      const fg = parseColor(fgStr);
      const bg = parseColor(bgStr);
      if (!fg || !bg || fg.alpha < 1 || bg.alpha < 1) return 0;
      const l1 = lum(fg.rgb);
      const l2 = lum(bg.rgb);
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    }
    function wcagRatio(el) {
      const cs = window.getComputedStyle(el);
      return pairRatio(cs.color, cs.backgroundColor);
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
    const primaryBtn = setupPane.querySelector("button.btn.primary");
    assert(
      document.documentElement.dataset.theme === "light" && !!primaryBtn && wcagRatio(primaryBtn) >= 4.5,
      "亮色主题主按钮 .btn.primary 运行时 WCAG 对比度 ≥ 4.5:1"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    assert(
      document.documentElement.dataset.theme === "dark" && wcagRatio(primaryBtn) >= 4.5,
      "暗色主题主按钮 .btn.primary 运行时 WCAG 对比度 ≥ 4.5:1"
    );
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    assert(document.documentElement.dataset.theme === "light", "主按钮双主题 WCAG 校验后切回亮色主题");

    home.setState(V3Fixtures.setupDownloading);
    const dlText = setupPane.innerText;
    assert(dlText.indexOf("42%") !== -1 && dlText.indexOf("下载中") !== -1, "仅在注入进度时显示百分比");
    assert(
      setupPane.querySelector('button[data-role="install-all"]') === installBtn && installBtn.disabled && installBtn.textContent === "安装中…",
      "装齐进行中重读状态不重建按钮（点击不被吞），按钮禁用并显示安装中"
    );

    /* 一键装齐运行中隐藏并禁用 asr 重试按钮（防换 venv 期间误点撞锁），ollama / server 不受影响；非 running 态恢复 */
    const asrRetryBtn = asrRow.querySelector("button");
    const ollamaRetryBtn = ollamaRow.querySelector("button");
    home.setState({
      connection: "connected",
      installAll: V3Fixtures.setupDownloading.installAll,
      components: [
        { name: "ollama", state: "stopped", reason: "端口 11434 被占用", failures: 1 },
        { name: "asr", state: "stopped", reason: "正在升级识别运行环境", failures: 0 },
        { name: "server", state: "stopped", reason: "服务已停止", failures: 1 }
      ]
    });
    const serverRow = setupPane.querySelector('[data-component="server"]');
    const serverRetryBtn = serverRow.querySelector("button");
    assert(
      asrRow.querySelector("button") === asrRetryBtn && asrRetryBtn.hidden === true && asrRetryBtn.disabled === true,
      "一键装齐 running 且 asr 为 stopped 时隐藏并禁用 asr 重试按钮"
    );
    const actionsBeforeBlockedAsr = setupActions.length;
    asrRetryBtn.click();
    assert(
      setupActions.length === actionsBeforeBlockedAsr,
      "一键装齐 running 时点击被禁用的 asr 重试按钮不触发 onRetryComponent"
    );
    assert(
      ollamaRow.querySelector("button") === ollamaRetryBtn && ollamaRetryBtn.hidden === false && ollamaRetryBtn.disabled === false,
      "一键装齐 running 时同样 stopped 的 ollama 重试按钮仍可见可用"
    );
    assert(
      !!serverRetryBtn && serverRetryBtn.hidden === false && serverRetryBtn.disabled === false,
      "一键装齐 running 时同样 stopped 的 server 重试按钮仍可见可用"
    );
    const actionsBeforeOllamaRunning = setupActions.length;
    ollamaRetryBtn.click();
    assert(
      setupActions.length === actionsBeforeOllamaRunning + 1 && setupActions[setupActions.length - 1] === "retry:ollama",
      "一键装齐 running 时点击 ollama 重试按钮正常触发 onRetryComponent('ollama')"
    );

    ["failed", "done", "idle"].forEach(function (nonRunningState) {
      home.setState({
        installAll: {
          state: nonRunningState,
          current: null,
          steps: V3Fixtures.setupDownloading.installAll.steps
        }
      });
      assert(
        asrRow.querySelector("button") === asrRetryBtn && asrRetryBtn.hidden === false && asrRetryBtn.disabled === false,
        "installAll.state=" + nonRunningState + " 且 asr 为 stopped 时恢复展示可用 asr 重试按钮"
      );
    });
    const actionsBeforeAsrRecover = setupActions.length;
    asrRetryBtn.click();
    assert(
      setupActions.length === actionsBeforeAsrRecover + 1 && setupActions[setupActions.length - 1] === "retry:asr",
      "非 running 态恢复展示后点击 asr 重试按钮正常触发 onRetryComponent('asr')"
    );

    /* ASR 模型卡片副标题如实呈现 deviceLabel 升级提示与旧驱动提示文案 */
    const upgradeDeviceLabel = "CPU（检测到 NVIDIA GPU，点「一键装齐」可升级 GPU 加速）";
    home.setState({
      connection: "connected",
      asr: {
        status: "ready",
        sidecarRunning: true,
        sizeLabel: "3.5 GB",
        deviceLabel: upgradeDeviceLabel,
        message: ""
      }
    });
    const asrModelSubUpgrade = setupPane.querySelectorAll(".row")[0].querySelector(".row-sub");
    assert(
      !!asrModelSubUpgrade && asrModelSubUpgrade.textContent.indexOf(upgradeDeviceLabel) !== -1,
      "ASR 卡片副标题正常呈现 deviceLabel 升级提示文案"
    );
    const oldDriverDeviceLabel = "CPU（检测到 NVIDIA 驱动版本过低 < 570，请升级驱动后再启用 GPU 加速）";
    home.setState({
      asr: {
        status: "ready",
        sidecarRunning: true,
        sizeLabel: "3.5 GB",
        deviceLabel: oldDriverDeviceLabel,
        message: ""
      }
    });
    const asrModelSubOldDriver = setupPane.querySelectorAll(".row")[0].querySelector(".row-sub");
    assert(
      !!asrModelSubOldDriver && asrModelSubOldDriver.textContent.indexOf(oldDriverDeviceLabel) !== -1,
      "ASR 卡片副标题正常呈现 deviceLabel 旧驱动提示文案"
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
    home.setState({ theme: document.documentElement.dataset.theme });

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

    /* 语音与润色顶部「输入设备」卡片全态验收：持久 DOM / 选项签名防重绘 / 切换与刷新回调 / 加载态 / 503 错误 / 空列表 / 未连接设备 */
    const voiceSections = setupPane.querySelectorAll(".setup-section");
    const audioSelect = setupPane.querySelector('select[data-role="setup-audio-input"]');
    const audioRefreshBtn = setupPane.querySelector('button[data-role="setup-audio-input-refresh"]');
    const audioSavedEl = setupPane.querySelector('span[data-role="setup-audio-input-saved"]');
    const audioErrEl = setupPane.querySelector('[data-role="setup-audio-input-error"]');
    assert(
      voiceSections.length >= 2 && voiceSections[0].contains(audioSelect) && voiceSections[0].innerText.indexOf("输入设备") !== -1,
      "「输入设备」卡片位于语音与润色子标签顶部"
    );
    assert(
      voiceSections[0].innerText.indexOf("切换后下次录音立即生效，无需重启") !== -1 && voiceSections[0].innerText.indexOf("已生效") === -1,
      "输入设备提示文案使用「切换后下次录音立即生效，无需重启」且不含禁止子串"
    );
    assert(
      !!audioSelect && !audioSelect.disabled && audioSelect.options.length === 3
        && audioSelect.options[0].value === "" && audioSelect.options[0].textContent === "跟随系统默认（Built-in Microphone）"
        && audioSelect.options[1].value === "Built-in Microphone" && audioSelect.options[1].textContent === "Built-in Microphone（系统默认）"
        && audioSelect.options[2].value === "USB Condenser Mic" && audioSelect.value === "",
      "已连接且读取设备后展示跟随系统默认及设备列表，默认选中跟随系统"
    );
    assert(
      !!audioRefreshBtn && !audioRefreshBtn.disabled && audioRefreshBtn.classList.contains("btn") && !audioRefreshBtn.classList.contains("primary"),
      "刷新设备按钮启用且使用普通 .btn 样式不抢占 .btn.primary"
    );
    assert(!!audioSavedEl && audioSavedEl.hidden && !!audioErrEl && audioErrEl.hidden, "输入设备初始不显示已保存或错误提示");

    /* 轮询刷新时选项签名不变，严禁清空重建 <option> 防收起原生下拉 */
    const audioOpt0BeforePoll = audioSelect.options[0];
    home.setState({ asr: { message: "后台状态轮询-麦克风列表不变" } });
    assert(
      setupPane.querySelector('select[data-role="setup-audio-input"]') === audioSelect
        && audioSelect.options[0] === audioOpt0BeforePoll
        && setupPane.querySelector('button[data-role="setup-audio-input-refresh"]') === audioRefreshBtn,
      "轮询状态刷新时输入设备下拉与 <option> 节点不重建（防收起原生下拉）"
    );

    audioSelect.value = "USB Condenser Mic";
    audioSelect.dispatchEvent(new Event("change", { bubbles: true }));
    assert(
      audioInputChanges.length === 1 && audioInputChanges[0] === "USB Condenser Mic",
      "选择指定麦克风经 onChangeAudioInput(deviceName) 通知业务层"
    );
    home.setState({ audio_input_device: "USB Condenser Mic", audioInputSaved: true, audioInputError: "" });
    assert(
      audioSelect.value === "USB Condenser Mic" && !audioSavedEl.hidden && audioSavedEl.textContent === "已保存",
      "回读保存配置后选中目标麦克风并显示已保存徽标"
    );
    audioSelect.value = "";
    audioSelect.dispatchEvent(new Event("change", { bubbles: true }));
    assert(
      audioInputChanges.length === 2 && audioInputChanges[1] === "",
      "切回「跟随系统默认」传空串给 onChangeAudioInput"
    );

    audioRefreshBtn.click();
    assert(audioInputRefreshes === 1, "点击刷新设备按钮触发 onRefreshAudioInputs");
    home.setState({ audioInputsLoading: true, audioInputSaved: false });
    assert(
      audioRefreshBtn.disabled && audioRefreshBtn.innerText.indexOf("刷新中") !== -1 && audioSelect.disabled && audioSavedEl.hidden,
      "刷新设备加载态禁用下拉与刷新按钮并显示刷新中"
    );
    audioRefreshBtn.click();
    assert(audioInputRefreshes === 1, "刷新进行中重复点击不重复触发 onRefreshAudioInputs");

    /* 枚举失败（audioInputs=null + audioInputError）：如实呈现错误，严禁把已存设备误标为「当前未连接」 */
    home.setState({
      audioInputs: null,
      audioInputsLoading: false,
      audio_input_device: "USB Condenser Mic",
      audioInputError: "依赖失败：PortAudio 初始化异常"
    });
    assert(
      !audioErrEl.hidden && audioErrEl.textContent.indexOf("PortAudio 初始化异常") !== -1,
      "麦克风列表加载失败时显示后端错误原因"
    );
    assert(
      audioSelect.value === "USB Condenser Mic"
        && Array.prototype.slice.call(audioSelect.options).every(function (o) { return o.textContent.indexOf("当前未连接") === -1; }),
      "audioInputs 为 null 时不误判已保存设备为当前未连接"
    );
    assert(!audioRefreshBtn.disabled, "加载失败后刷新设备按钮恢复可点以便重试");

    /* 枚举成功但 0 麦克风（devices=[]） */
    home.setState({
      audioInputs: { defaultDevice: null, devices: [] },
      audio_input_device: "",
      audioInputError: ""
    });
    assert(
      audioErrEl.hidden && audioSelect.options.length === 1 && audioSelect.options[0].value === ""
        && audioSelect.options[0].textContent.indexOf("未检测到") !== -1,
      "枚举成功但 0 麦克风时提示未检测到可用麦克风且无错误行"
    );

    /* 仅当枚举成功（audioInputs.devices 为数组）且已存设备非空不在列时显示「<名>（当前未连接）」 */
    home.setState({
      audioInputs: V3Fixtures.setupConnected.audioInputs,
      audio_input_device: "Blue Yeti Nano",
      audioInputError: ""
    });
    const unpluggedOpt = Array.prototype.slice.call(audioSelect.options).find(function (o) { return o.value === "Blue Yeti Nano"; });
    assert(
      !!unpluggedOpt && unpluggedOpt.textContent === "Blue Yeti Nano（当前未连接）" && audioSelect.value === "Blue Yeti Nano",
      "枚举成功且已选麦克风不在列表时显示「<名>（当前未连接）」并保持选中"
    );

    /* 恢复正常已选默认设备态供后续双态 select 校验 */
    home.setState({
      audio_input_device: "",
      audioInputs: V3Fixtures.setupConnected.audioInputs,
      audioInputsLoading: false,
      audioInputError: "",
      audioInputSaved: false
    });

    home.setSetupTab("shot");
    const shotSelect = setupPane.querySelector('select[data-role="setup-lang-pair"]');
    const shotAutoSw = setupPane.querySelector('button[data-role="setup-auto-translate"]');
    assert(!!shotSelect && shotSelect.value === "zh-en" && !shotSelect.disabled, "设置页翻译语言对默认选中 zh-en 且可交互");
    const langPairBeforeShot = langPairChanges.length;
    shotSelect.dispatchEvent(new Event("change", { bubbles: true }));
    assert(langPairChanges.length === langPairBeforeShot + 1 && langPairChanges[langPairChanges.length - 1] === "zh-en", "翻译语言对切换走 onChangeLangPair 回调");
    assert(!!shotAutoSw && shotAutoSw.getAttribute("aria-checked") === "true" && !shotAutoSw.disabled, "设置页自动翻译开关反映配置 true");
    shotAutoSw.click();
    assert(
      autoTranslateToggles.length === 1 && autoTranslateToggles[0] === false && shotAutoSw.getAttribute("aria-checked") === "true",
      "自动翻译开关走 onToggleAutoTranslate(false) 且不做乐观状态翻转"
    );
    home.setState({ translate_auto_enabled: false, translateSettingsSaved: true });
    assert(
      setupPane.querySelector('button[data-role="setup-auto-translate"]').getAttribute("aria-checked") === "false" && setupPane.innerText.indexOf("已保存") !== -1,
      "回读配置后自动翻译开关更新为 false 并展示 translateSettingsSaved 已保存提示"
    );

    /* 亮/暗双主题下校验全站 4 处 <select> 与 <option> 的 colorScheme 及主题对比度 */
    function readSelectMetrics(el) {
      if (!el || !el.options || !el.options.length) return null;
      const cs = window.getComputedStyle(el);
      const ocs = window.getComputedStyle(el.options[0]);
      const bgParsed = parseColor(cs.backgroundColor);
      const optBgParsed = parseColor(ocs.backgroundColor);
      return {
        colorScheme: cs.colorScheme,
        bg: cs.backgroundColor,
        fg: cs.color,
        bgOpaque: !!bgParsed && bgParsed.alpha === 1,
        bgLum: bgParsed ? lum(bgParsed.rgb) : -1,
        ratio: pairRatio(cs.color, cs.backgroundColor),
        optBg: ocs.backgroundColor,
        optFg: ocs.color,
        optBgOpaque: !!optBgParsed && optBgParsed.alpha === 1,
        optBgLum: optBgParsed ? lum(optBgParsed.rgb) : -1,
        optRatio: pairRatio(ocs.color, ocs.backgroundColor)
      };
    }
    function snapshotAllSelects() {
      homeRoot.querySelector('button[data-panel="translate"]').click();
      const tr = readSelectMetrics(homeRoot.querySelector('select[data-role="translate-lang-pair"]'));
      homeRoot.querySelector('button[data-panel="ocr"]').click();
      const ocr = readSelectMetrics(homeRoot.querySelector('select[data-role="ocr-mode"]'));
      homeRoot.querySelector('button[data-panel="setup"]').click();
      home.setSetupTab("voice");
      const audio = readSelectMetrics(setupPane.querySelector('select[data-role="setup-audio-input"]'));
      home.setSetupTab("shot");
      const shot = readSelectMetrics(setupPane.querySelector('select[data-role="setup-lang-pair"]'));
      return [
        { name: "工作台翻译语言对", m: tr },
        { name: "工作台 OCR 模式", m: ocr },
        { name: "设置页输入设备", m: audio },
        { name: "设置页翻译语言对", m: shot }
      ];
    }
    assert(document.documentElement.dataset.theme === "light", "双态 select 校验前处于默认亮色主题");
    const lightSelects = snapshotAllSelects();
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    home.setState({ theme: document.documentElement.dataset.theme });
    assert(document.documentElement.dataset.theme === "dark", "双态 select 校验已切换至暗色主题");
    const darkSelects = snapshotAllSelects();
    for (let si = 0; si < darkSelects.length; si += 1) {
      const label = darkSelects[si].name;
      const dm = darkSelects[si].m;
      const lm = lightSelects[si].m;
      assert(
        !!dm && dm.colorScheme === "dark" && dm.bgOpaque && dm.bgLum < 0.1 && dm.ratio >= 4.5,
        "暗色主题下" + label + " <select> colorScheme=dark 且深底高对比可读"
      );
      assert(
        !!dm && dm.optBgOpaque && dm.optBgLum < 0.1 && dm.optRatio >= 4.5,
        "暗色主题下" + label + " <option> 深底不透明且对比度 ≥ 4.5:1"
      );
      assert(
        !!lm && lm.colorScheme === "light" && lm.bgOpaque && lm.bgLum > 0.8 && lm.ratio >= 4.5,
        "亮色主题下" + label + " <select> colorScheme=light 且浅底高对比可读"
      );
      assert(
        !!lm && lm.optBgOpaque && lm.optBgLum > 0.8 && lm.optRatio >= 4.5,
        "亮色主题下" + label + " <option> 浅底不透明且对比度 ≥ 4.5:1"
      );
      assert(
        !!dm && !!lm && dm.bg !== lm.bg && dm.fg !== lm.fg && dm.optBg !== lm.optBg && dm.optFg !== lm.optFg,
        label + " <select>/<option> 背景色与文字色随暗/亮主题同步切换"
      );
    }
    homeRoot.querySelector('button[data-action="toggle-theme"]').click();
    home.setState({ theme: document.documentElement.dataset.theme });
    assert(document.documentElement.dataset.theme === "light", "双态 select 校验后恢复默认亮色主题");

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
    assert(document.documentElement.dataset.theme === "light" && wcagRatio(mic1) >= 4.5, "亮色主题下 /voice 麦克风按钮 .mic WCAG 对比度 ≥ 4.5:1");

    voice.setState({ phase: "recording" });
    assert(wcagRatio(mic1) >= 4.5, "亮色主题下 /voice 录音中按钮 .mic.rec WCAG 对比度 ≥ 4.5:1");

    voice.setState({ phase: "idle", useFileUpload: true });
    const uploadLbl = document.getElementById("voice-root").querySelector(".upload");
    assert(!!uploadLbl && wcagRatio(uploadLbl) >= 4.5, "亮色主题下 /voice 上传按钮 .upload WCAG 对比度 ≥ 4.5:1");

    /* 显式切至暗色保留暗色 /voice WCAG 覆盖，再切回亮色 */
    document.documentElement.dataset.theme = "dark";
    voice.setState({ phase: "idle", useFileUpload: false });
    assert(wcagRatio(mic1) >= 4.5, "暗色主题下 /voice 麦克风按钮 .mic WCAG 对比度 ≥ 4.5:1");
    voice.setState({ phase: "recording" });
    assert(wcagRatio(mic1) >= 4.5, "暗色主题下 /voice 录音中按钮 .mic.rec WCAG 对比度 ≥ 4.5:1");
    voice.setState({ phase: "idle", useFileUpload: true });
    assert(wcagRatio(uploadLbl) >= 4.5, "暗色主题下 /voice 上传按钮 .upload WCAG 对比度 ≥ 4.5:1");
    document.documentElement.dataset.theme = "light";
    voice.setState({ useFileUpload: false });

    voice.setState(V3Fixtures.voicePolishFail);
    const v1 = document.getElementById("voice-root").innerText;
    assert(v1.indexOf("识别原文（未输入）") !== -1, "润色失败展示原文");
    assert(v1.indexOf("发送原文") !== -1, "润色失败提供发送原文");
    document.getElementById("voice-root").querySelector(".btn.primary").click();
    assert(confirms.some(function (c) { return c.sendOriginal && c.payload.text.indexOf("把这个函数拆成两个") !== -1; }), "发送原文只走回调");
    voice.destroy();

    /* ---------- 4. 统一工作台内置设置：五热键录入与 sidecar 就绪验收 ---------- */
    let rejectSave = false;
    const savedHotkeys = [];
    const keyHome = V3.home.mount(homeRoot, {
      connection: "connected",
      activePanel: "setup",
      setupTab: "keys",
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
      hotkey_annotate: "ctrl+alt+a"
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
    assert(!!keyField("hotkey_annotate") && keyField("hotkey_annotate").value === "ctrl+alt+a", "设置面板展示第五热键 hotkey_annotate");
    assert(keySetupPane.innerText.indexOf("五个全局快捷键") !== -1, "设置面板标题展示五个全局快捷键");
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

    keyField("hotkey_annotate").focus();
    fireKey(keyField("hotkey_annotate"), { key: "b", code: "KeyB", ctrlKey: true, altKey: true });
    assert(keyHome.getState().hotkeyDrafts.hotkey_annotate === "ctrl+alt+b", "hotkey_annotate 录入草稿写入 viewstate");
    assert(keyField("hotkey_annotate").value === "ctrl+alt+b", "hotkey_annotate 输入框显示草稿");

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
    assert(
      savedHotkeys.length === savedCount + 1
        && savedHotkeys[savedHotkeys.length - 1].hotkey_ocr === "ctrl+alt+p"
        && savedHotkeys[savedHotkeys.length - 1].hotkey_annotate === "ctrl+alt+b",
      "保存提交包含 hotkey_ocr 与 hotkey_annotate 的五项草稿"
    );
    assert(keySetupPane.innerText.indexOf("已保存") !== -1, "保存成功只显示已保存");
    assert(keySetupPane.innerText.indexOf("已生效") === -1, "已保存旁不出现已生效");

    keyHome.setState({
      hotkeyDrafts: null,
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
      hotkey_annotate: "ctrl+alt+a",
      hotkeysSaved: false
    });
    assert(
      keyField("hotkey_voice").value === "ctrl+alt+z"
        && keyField("hotkey_screenshot").value === "ctrl+alt+x"
        && keyField("hotkey_ocr").value === "ctrl+alt+o"
        && keyField("hotkey_repeat").value === "ctrl+alt+r"
        && keyField("hotkey_annotate").value === "ctrl+alt+a",
      "已保存的五项合法组合在无草稿时仍可显示和配置"
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

    /* ---------- 5. 设置页与工作台长报错防溢出几何与样式验收 ---------- */
    const longUnspacedErr = "https://r2.example.invalid/blobs/sha256-" + "a1b2c3d4e5".repeat(22);
    assert(longUnspacedErr.length === 260 && !/\s/.test(longUnspacedErr), "长错测试样本为 260 字无空格字符串");

    const overflowHome = V3.home.mount(homeRoot, {
      connection: "connected",
      activePanel: "setup",
      setupTab: "models",
      statusNotice: longUnspacedErr,
      asr: { status: "ready", sidecarRunning: true, sizeLabel: "3.5 GB", message: "" },
      ollama: {
        installed: true,
        running: true,
        modelStatus: "failed",
        message: longUnspacedErr
      },
      installAll: {
        state: "failed",
        current: "ollama_pull",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "", progress: null },
          { kind: "wait_ollama", state: "done", message: "", progress: null },
          { kind: "ollama_pull", state: "failed", message: longUnspacedErr, progress: null },
          { kind: "asr_runtime_install", state: "pending", message: "", progress: null },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      },
      components: [
        { name: "ollama", state: "stopped", reason: longUnspacedErr, failures: 5 },
        { name: "asr", state: "running", reason: "", failures: 0 },
        { name: "server", state: "running", reason: "", failures: 0 }
      ],
      capabilities: [
        { name: "xclip（剪贴板）", available: false, message: longUnspacedErr, installCommand: longUnspacedErr }
      ],
      audio_input_device: "USB-Audio-Conference-Microphone-" + "LongDeviceModelName".repeat(10),
      audioInputs: {
        defaultDevice: "USB-Audio-Conference-Microphone-" + "LongDeviceModelName".repeat(10),
        devices: [
          {
            name: "USB-Audio-Conference-Microphone-" + "LongDeviceModelName".repeat(10),
            hostapi: "Windows DirectSound",
            isDefault: true
          }
        ]
      },
      audioInputError: longUnspacedErr,
      lanEntries: [],
      lanMessage: longUnspacedErr
    }, hooks);

    const ovSetupPane = homeRoot.querySelector('[data-panel-view="setup"] .setup-pane');
    const ovStepRow = ovSetupPane.querySelector('[data-step="ollama_pull"]');
    const ovRowMain = ovStepRow.querySelector(".row-main");
    const ovRowSub = ovStepRow.querySelector(".row-sub.err");
    const ovBadge = ovStepRow.querySelector(".badge");
    const ovStepRect = ovStepRow.getBoundingClientRect();
    const ovRowMainRect = ovRowMain.getBoundingClientRect();
    const ovBadgeRect = ovBadge.getBoundingClientRect();
    const ovBadgeCs = window.getComputedStyle(ovBadge);
    const ovRowSubCs = window.getComputedStyle(ovRowSub);

    assert(
      ovRowSub.scrollWidth <= ovRowSub.clientWidth + 1 && ovStepRow.scrollWidth <= ovStepRow.clientWidth + 1,
      "设置页一键装齐失败步 260 字无空格长错不横向溢出（rowSub " + ovRowSub.scrollWidth + "/" + ovRowSub.clientWidth + "）"
    );
    assert(
      ovBadgeRect.width > 0 && ovBadgeRect.right <= ovStepRect.right + 1,
      "设置页长错不遮挡或顶出右侧失败徽章（badge.right=" + ovBadgeRect.right.toFixed(1) + " vs row.right=" + ovStepRect.right.toFixed(1) + "）"
    );
    assert(
      ovRowMainRect.width > 20,
      "设置页失败步左侧 .row-main 宽度未被压塌（width=" + ovRowMainRect.width.toFixed(1) + "）"
    );
    assert(ovBadgeCs.flexShrink === "0", "右侧 .badge 计算样式 flexShrink 为 0");
    assert(ovRowSubCs.overflowWrap === "anywhere", "报错副文 .row-sub 计算样式 overflowWrap 为 anywhere");

    const ovBanner = homeRoot.querySelector(".banner:not([hidden])");
    assert(
      !!ovBanner && ovBanner.scrollWidth <= ovBanner.clientWidth + 1 && window.getComputedStyle(ovBanner).overflowWrap === "anywhere",
      "顶部 .banner 承载 260 字无空格长错正常折行无横向溢出"
    );
    assert(
      ovSetupPane.scrollWidth <= ovSetupPane.clientWidth + 1 && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
      "设置页模型子标签与页面整体无横向滚动溢出"
    );

    /* 设置页「系统检测」子标签：右侧普通 span 承载 260 字无空格长错与安装命令不压塌左侧 .row-main */
    overflowHome.setSetupTab("sys");
    const ovSysRows = ovSetupPane.querySelectorAll(".rows .row");
    const ovSysCapRow = ovSysRows[0];
    const ovSysCapMain = ovSysCapRow.querySelector(".row-main");
    const ovSysCapSpan = ovSysCapRow.querySelector("span:not(.badge):not(.dot):not(.saved)");
    const ovSysCapRowRect = ovSysCapRow.getBoundingClientRect();
    const ovSysCapMainRect = ovSysCapMain.getBoundingClientRect();
    const ovSysCapSpanRect = ovSysCapSpan.getBoundingClientRect();
    const ovSysCapSpanCs = window.getComputedStyle(ovSysCapSpan);
    assert(
      ovSysCapRow.scrollWidth <= ovSysCapRow.clientWidth + 1 && ovSysCapSpan.scrollWidth <= ovSysCapSpan.clientWidth + 1,
      "系统检测能力行 260 字无空格长错正常折行不横向溢出"
    );
    assert(
      ovSysCapMainRect.width > 20 && ovSysCapSpanRect.right <= ovSysCapRowRect.right + 1 && ovSysCapSpanRect.width <= ovSysCapRowRect.width * 0.66 + 1,
      "系统检测右侧长错受 max-width:65% 约束且左侧 .row-main 未被压塌（main=" + ovSysCapMainRect.width.toFixed(1) + " span=" + ovSysCapSpanRect.width.toFixed(1) + "/" + ovSysCapRowRect.width.toFixed(1) + "）"
    );
    assert(
      ovSysCapSpanCs.overflowWrap === "anywhere" && ovSysCapSpanCs.textAlign === "right",
      "系统检测右侧长错计算样式 overflowWrap=anywhere 且 textAlign=right"
    );
    const ovSysCmdRow = ovSysRows[1];
    const ovSysCmdMainRect = ovSysCmdRow.querySelector(".row-main").getBoundingClientRect();
    assert(
      ovSysCmdRow.scrollWidth <= ovSysCmdRow.clientWidth + 1 && ovSysCmdMainRect.width > 20,
      "系统检测安装命令长串不横向溢出且左侧标签不被压塌"
    );

    /* 设置页「语音与润色」子标签：超长麦克风名称与长错误防横向溢出 */
    overflowHome.setSetupTab("voice");
    const ovAudioSelect = ovSetupPane.querySelector('select[data-role="setup-audio-input"]');
    const ovAudioRow = ovAudioSelect.closest(".row");
    const ovAudioErr = ovSetupPane.querySelector('[data-role="setup-audio-input-error"]');
    const ovAudioSelectCs = window.getComputedStyle(ovAudioSelect);
    const ovAudioSelectRect = ovAudioSelect.getBoundingClientRect();
    const ovAudioRowRect = ovAudioRow.getBoundingClientRect();
    assert(
      ovAudioSelectCs.minWidth === "0px" && ovAudioSelectCs.maxWidth === "100%",
      "麦克风下拉计算样式具备 minWidth=0px 与 maxWidth=100%"
    );
    assert(
      ovAudioSelectRect.right <= ovAudioRowRect.right + 1
        && ovAudioRow.scrollWidth <= ovAudioRow.clientWidth + 1
        && ovSetupPane.scrollWidth <= ovSetupPane.clientWidth + 1
        && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
      "超长无空格麦克风设备名不撑破设置页输入设备行与页面视口（select.right=" + ovAudioSelectRect.right.toFixed(1) + " vs row.right=" + ovAudioRowRect.right.toFixed(1) + "）"
    );
    assert(
      !!ovAudioErr && !ovAudioErr.hidden && ovAudioErr.scrollWidth <= ovAudioErr.clientWidth + 1,
      "输入设备 260 字无空格长错正常折行无横向溢出"
    );

    /* 工作台翻译区 .box-error 与扫码区 .empty 260 字无空格长错防溢出 */
    overflowHome.setState({
      activePanel: "translate",
      statusNotice: "",
      translate: { phase: "error", error: "translate_failed", message: longUnspacedErr }
    });
    const ovBoxError = homeRoot.querySelector('[data-panel-view="translate"] .box-error');
    const ovBoxErrorSpan = ovBoxError.querySelector("span");
    const ovBoxBody = homeRoot.querySelector('[data-role="translate-output"]');
    assert(
      ovBoxError.scrollWidth <= ovBoxError.clientWidth + 1
        && ovBoxErrorSpan.scrollWidth <= ovBoxErrorSpan.clientWidth + 1
        && ovBoxBody.scrollWidth <= ovBoxBody.clientWidth + 1
        && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
      "工作台翻译失败 .box-error 260 字无空格长错不横向溢出"
    );
    assert(
      window.getComputedStyle(ovBoxError).overflowWrap === "anywhere" && window.getComputedStyle(ovBoxErrorSpan).minWidth === "0px",
      "工作台 .box-error 与子 span 计算样式具备 overflowWrap=anywhere 与 minWidth=0px"
    );

    overflowHome.setPanel("lan");
    const ovLanEmpty = homeRoot.querySelector('[data-panel-view="lan"] .empty');
    assert(
      !!ovLanEmpty
        && ovLanEmpty.scrollWidth <= ovLanEmpty.clientWidth + 1
        && window.getComputedStyle(ovLanEmpty).overflowWrap === "anywhere"
        && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
      "工作台空态/异常 .empty 260 字无空格长错不横向溢出"
    );

    overflowHome.destroy();

    /* ---------- 6. 单源流动进度条、设置页持久 DOM 与文案边界验收 ---------- */
    const progHome = V3.home.mount(homeRoot, {
      connection: "connected",
      activePanel: "setup",
      setupTab: "models",
      asr: {
        status: "downloading",
        progress: { receivedLabel: "1.47 GB", totalLabel: "3.50 GB", percent: 42 }
      },
      ollama: {
        installed: true,
        running: true,
        modelStatus: "downloading",
        progress: { receivedLabel: "640 MB", totalLabel: null, percent: null }
      },
      installAll: {
        state: "running",
        current: "asr_runtime_install",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "已具备，跳过", progress: null },
          { kind: "wait_ollama", state: "done", message: "Ollama 已运行", progress: null },
          { kind: "ollama_pull", state: "done", message: "", progress: null },
          {
            kind: "asr_runtime_install",
            state: "running",
            message: "正在下载 torch-2.9.1+cu128…",
            progress: { receivedLabel: "1.25 GB", totalLabel: null, percent: null }
          },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      }
    }, hooks);

    const progSetupPane = homeRoot.querySelector('[data-panel-view="setup"] .setup-pane');
    const progModelRows = progSetupPane.querySelectorAll(".setup-section")[0].querySelectorAll(".row");
    const progAsrSub = progModelRows[0].querySelector(".row-sub");
    const progAsrBar = progModelRows[0].querySelector(".progress");
    const progAsrFill = progAsrBar.querySelector("i");
    const progOllSub = progModelRows[1].querySelector(".row-sub");
    const progOllBar = progModelRows[1].querySelector(".progress");
    const progOllFill = progOllBar.querySelector("i");
    const progStepRow = progSetupPane.querySelector('[data-step="asr_runtime_install"]');
    const progStepSub = progStepRow.querySelector(".row-sub");
    const progStepBar = progStepRow.querySelector(".progress");
    const progStepFill = progStepBar.querySelector("i");
    const progStepsContainer = progStepRow.parentNode;

    const childObserver = new MutationObserver(function () {});
    childObserver.observe(progStepsContainer, { childList: true, subtree: true });
    childObserver.observe(progModelRows[0], { childList: true, subtree: true });
    childObserver.observe(progModelRows[1], { childList: true, subtree: true });
    function countElementChildMutations(observer) {
      let count = 0;
      observer.takeRecords().forEach(function (rec) {
        Array.prototype.forEach.call(rec.addedNodes, function (n) { if (n.nodeType === 1) count += 1; });
        Array.prototype.forEach.call(rec.removedNodes, function (n) { if (n.nodeType === 1) count += 1; });
      });
      return count;
    }

    assert(
      progAsrSub.textContent === "下载中 · 1.47 GB / 3.50 GB · 42%"
        && !progAsrBar.hidden
        && !progAsrBar.classList.contains("indeterminate")
        && progAsrFill.style.width === "42%"
        && progAsrBar.getAttribute("aria-valuemin") === "0"
        && progAsrBar.getAttribute("aria-valuemax") === "100"
        && progAsrBar.getAttribute("aria-valuenow") === "42"
        && !progAsrBar.hasAttribute("aria-valuetext"),
      "确定态进度条渲染 received / total · percent、内联宽度与 aria-valuemin/max/now"
    );
    assert(
      progOllSub.textContent === "下载中 · 已写入 640 MB"
        && progOllSub.textContent.indexOf(" / ?") === -1
        && !progOllBar.hidden
        && progOllBar.classList.contains("indeterminate")
        && progOllFill.style.width === ""
        && window.getComputedStyle(progOllFill).animationName === "v3-indeterminate"
        && !progOllBar.hasAttribute("aria-valuemin")
        && !progOllBar.hasAttribute("aria-valuemax")
        && !progOllBar.hasAttribute("aria-valuenow")
        && progOllBar.getAttribute("aria-valuetext") === "进度未知",
      "未知总量仅已写入态显示「已写入 640 MB」、无「 / ?」且启用单源 v3-indeterminate 流动动画"
    );
    assert(
      progStepSub.textContent === "正在下载 torch-2.9.1+cu128… · 已写入 1.25 GB"
        && progStepSub.textContent.indexOf(" / ?") === -1
        && progStepSub.textContent.indexOf("……") === -1
        && !progStepBar.hidden
        && progStepBar.classList.contains("indeterminate")
        && progStepFill.style.width === ""
        && window.getComputedStyle(progStepFill).animationName === "v3-indeterminate"
        && progStepBar.getAttribute("aria-valuetext") === "进度未知",
      "步骤未知总量运行态显示「已写入 1.25 GB」、以 · 连接且启用单源流动动画"
    );

    /* 连续轮询 tick 2：无已写入有总量（0 B / total）、title 以 … 结尾不重复拼 ……、全部节点原地更新零 childList 变更 */
    progHome.setState({
      asr: {
        status: "downloading",
        progress: { receivedLabel: "2.10 GB", totalLabel: "3.50 GB", percent: 60 }
      },
      ollama: {
        installed: true,
        running: true,
        modelStatus: "downloading",
        progress: { receivedLabel: null, totalLabel: "3.40 GB", percent: null }
      },
      installAll: {
        state: "running",
        current: "asr_runtime_install",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "已具备，跳过", progress: null },
          { kind: "wait_ollama", state: "done", message: "Ollama 已运行", progress: null },
          { kind: "ollama_pull", state: "done", message: "", progress: null },
          {
            kind: "asr_runtime_install",
            state: "running",
            message: "正在解析依赖…",
            progress: { receivedLabel: null, totalLabel: null, percent: null }
          },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      }
    });
    const tick2ElMutations = countElementChildMutations(childObserver);
    assert(
      tick2ElMutations === 0
        && progSetupPane.querySelector('[data-step="asr_runtime_install"]') === progStepRow
        && progStepRow.querySelector(".row-sub") === progStepSub
        && progStepRow.querySelector(".progress") === progStepBar
        && progStepBar.querySelector("i") === progStepFill
        && progModelRows[0].querySelector(".row-sub") === progAsrSub
        && progModelRows[0].querySelector(".progress") === progAsrBar
        && progAsrBar.querySelector("i") === progAsrFill
        && progModelRows[1].querySelector(".row-sub") === progOllSub
        && progModelRows[1].querySelector(".progress") === progOllBar
        && progOllBar.querySelector("i") === progOllFill,
      "连续轮询 tick 更新进度时步骤行、进度条、asrSub/ollamaSub 节点身份不变且零元素节点拆挂（不重置流动动画）"
    );
    assert(
      progStepSub.textContent === "正在解析依赖…" && progStepSub.textContent.indexOf("……") === -1,
      "title 已以「…」结尾且无字节/百分比时不重复拼接「……」"
    );
    assert(
      progOllSub.textContent === "下载中 · 0 B / 3.40 GB" && progOllSub.textContent.indexOf("?") === -1,
      "无 received 有 total 时渲染「0 B / 3.40 GB」且不出现「?」"
    );

    /* tick 3：无末尾 … 的 title 正常补单一 …；确定态 ↔ 不确定态双向切换彻底清理 width 与 aria-* */
    progHome.setState({
      asr: {
        status: "downloading",
        progress: { receivedLabel: "2.20 GB", totalLabel: null, percent: null }
      },
      installAll: {
        state: "running",
        current: "asr_runtime_install",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "已具备，跳过", progress: null },
          { kind: "wait_ollama", state: "done", message: "Ollama 已运行", progress: null },
          { kind: "ollama_pull", state: "done", message: "", progress: null },
          {
            kind: "asr_runtime_install",
            state: "running",
            message: "安装 Python 3.12",
            progress: { receivedLabel: null, totalLabel: null, percent: null }
          },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      }
    });
    assert(
      progStepSub.textContent === "安装 Python 3.12…" && progStepSub.textContent.indexOf("……") === -1,
      "无末尾「…」的 title 在无字节/百分比时补单一「…」"
    );
    assert(
      countElementChildMutations(childObserver) === 0
        && progAsrBar.classList.contains("indeterminate")
        && progAsrFill.style.width === ""
        && !progAsrBar.hasAttribute("aria-valuemin")
        && !progAsrBar.hasAttribute("aria-valuemax")
        && !progAsrBar.hasAttribute("aria-valuenow")
        && progAsrBar.getAttribute("aria-valuetext") === "进度未知"
        && progAsrSub.textContent === "下载中 · 已写入 2.20 GB",
      "确定态 → 不确定态切换彻底清空 barFill.style.width 与 aria-valuemin/max/now 并切回 .indeterminate"
    );

    /* tick 4：不确定态 → 确定态切换清理 aria-valuetext 与 .indeterminate */
    progHome.setState({
      installAll: {
        state: "running",
        current: "asr_runtime_install",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "已具备，跳过", progress: null },
          { kind: "wait_ollama", state: "done", message: "Ollama 已运行", progress: null },
          { kind: "ollama_pull", state: "done", message: "", progress: null },
          {
            kind: "asr_runtime_install",
            state: "running",
            message: "安装识别依赖（cuda）",
            progress: { receivedLabel: "2.00 GB", totalLabel: "2.86 GB", percent: 70 }
          },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      }
    });
    assert(
      countElementChildMutations(childObserver) === 0
        && !progStepBar.classList.contains("indeterminate")
        && progStepFill.style.width === "70%"
        && !progStepBar.hasAttribute("aria-valuetext")
        && progStepBar.getAttribute("aria-valuemin") === "0"
        && progStepBar.getAttribute("aria-valuemax") === "100"
        && progStepBar.getAttribute("aria-valuenow") === "70"
        && progStepSub.textContent === "安装识别依赖（cuda） · 2.00 GB / 2.86 GB · 70%",
      "不确定态 → 确定态切换清理 .indeterminate 与 aria-valuetext 并设置精确 width 与 aria-valuenow"
    );

    /* tick 5：运行态 → 非运行态（ready / failed / done）彻底隐藏并清空进度条属性与样式，节点仍不重建 */
    progHome.setState({
      asr: { status: "ready", sidecarRunning: true, sizeLabel: "3.5 GB", deviceLabel: "GPU 加速", progress: null },
      ollama: { installed: true, running: true, modelStatus: "failed", message: "拉取超时", progress: null },
      installAll: {
        state: "failed",
        current: "asr_runtime_install",
        steps: [
          { kind: "ollama_install", state: "skipped", message: "已具备，跳过", progress: null },
          { kind: "wait_ollama", state: "done", message: "Ollama 已运行", progress: null },
          { kind: "ollama_pull", state: "done", message: "", progress: null },
          { kind: "asr_runtime_install", state: "failed", message: "安装停滞超过 5 分钟", progress: null },
          { kind: "asr_download", state: "pending", message: "", progress: null }
        ]
      }
    });
    assert(
      countElementChildMutations(childObserver) === 0
        && progAsrBar.hidden === true
        && !progAsrBar.classList.contains("indeterminate")
        && progAsrFill.style.width === ""
        && !progAsrBar.hasAttribute("aria-label")
        && !progAsrBar.hasAttribute("aria-valuemin")
        && !progAsrBar.hasAttribute("aria-valuemax")
        && !progAsrBar.hasAttribute("aria-valuenow")
        && !progAsrBar.hasAttribute("aria-valuetext"),
      "asrSub 不确定态 → 就绪态彻底隐藏进度条并清空 width / .indeterminate / 全部 aria-*"
    );
    assert(
      progStepSub.classList.contains("err")
        && progStepSub.textContent === "安装停滞超过 5 分钟"
        && progStepBar.hidden === true
        && !progStepBar.classList.contains("indeterminate")
        && progStepFill.style.width === ""
        && !progStepBar.hasAttribute("aria-label")
        && !progStepBar.hasAttribute("aria-valuemin")
        && !progStepBar.hasAttribute("aria-valuemax")
        && !progStepBar.hasAttribute("aria-valuenow")
        && !progStepBar.hasAttribute("aria-valuetext")
        && progOllSub.classList.contains("err")
        && progOllSub.textContent === "拉取超时"
        && progOllBar.hidden === true,
      "步骤确定态 → 失败态与 ollama 失败态保留 .row-sub.err 并彻底隐藏清理进度条"
    );

    /* tick 6：翻译与 OCR 加载态均复用单源 .box-loading .progress.indeterminate 流动动画 */
    progHome.setState({
      activePanel: "translate",
      translate: { phase: "loading", sourceText: "Hello" },
      ocr: { phase: "loading" }
    });
    const trLoadBar = homeRoot.querySelector('[data-panel-view="translate"] .box-loading .progress.indeterminate');
    const trLoadFill = trLoadBar ? trLoadBar.querySelector("i") : null;
    assert(
      !!trLoadBar && !!trLoadFill
        && window.getComputedStyle(trLoadBar).width === "180px"
        && window.getComputedStyle(trLoadFill).animationName === "v3-indeterminate",
      "工作台翻译 loading 态使用单源 .box-loading .progress.indeterminate（宽 180px + v3-indeterminate 动画）"
    );
    progHome.setPanel("ocr");
    const ocrLoadBar = homeRoot.querySelector('[data-panel-view="ocr"] .box-loading .progress.indeterminate');
    const ocrLoadFill = ocrLoadBar ? ocrLoadBar.querySelector("i") : null;
    assert(
      !!ocrLoadBar && !!ocrLoadFill
        && window.getComputedStyle(ocrLoadBar).width === "180px"
        && window.getComputedStyle(ocrLoadFill).animationName === "v3-indeterminate",
      "工作台 OCR loading 态使用单源 .box-loading .progress.indeterminate（宽 180px + v3-indeterminate 动画）"
    );

    childObserver.disconnect();
    progHome.destroy();

    window.__V3_HARNESS__ = { passes: passes, failures: failures, copies: copies, confirms: confirms };
  } catch (err) {
    fail("harness 异常：" + err.stack);
  }

  reportEl.textContent = JSON.stringify({ passes: passes, failures: failures }, null, 2);
  document.title = failures.length ? "HARNESS_FAIL " + failures.length : "HARNESS_PASS " + passes.length;
})();

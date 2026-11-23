(function () {
  "use strict";

  var RELEASE = {
    version: "3.4.3",
    repo: "https://github.com/jackche8/voice-input",
    assets: {
      linux: "ollama-voice-input_{version}_amd64.deb",
      windows: "Ollama-Voice-Input-{version}-windows-x64-setup.exe",
      macos: "Ollama-Voice-Input-{version}-macos-arm64.dmg",
      sha256: "SHA256SUMS.txt"
    }
  };

  var STORAGE_LANG = "ovi-site-lang";
  var STORAGE_THEME = "ovi-site-theme";

  function assetName(key) {
    var tpl = RELEASE.assets[key] || "";
    return tpl.replace(/\{version\}/g, RELEASE.version);
  }

  function downloadUrl(key) {
    return RELEASE.repo + "/releases/download/v" + RELEASE.version + "/" + assetName(key);
  }

  function releasesUrl() {
    return RELEASE.repo + "/releases";
  }

  function interpolate(text) {
    return String(text)
      .replace(/\{version\}/g, RELEASE.version)
      .replace(/\{linux_file\}/g, assetName("linux"))
      .replace(/\{windows_file\}/g, assetName("windows"))
      .replace(/\{macos_file\}/g, assetName("macos"))
      .replace(/\{sha256_file\}/g, assetName("sha256"));
  }

  function getStorage() {
    try {
      return window.localStorage || null;
    } catch (_err) {
      return null;
    }
  }

  function readStore(key) {
    try {
      var st = getStorage();
      return st ? st.getItem(key) : null;
    } catch (_err) {
      return null;
    }
  }

  function writeStore(key, value) {
    try {
      var st = getStorage();
      if (st) st.setItem(key, value);
    } catch (_err) {
      /* ignore restricted storage environments */
    }
  }

  function matchQuery(query) {
    try {
      return typeof window.matchMedia === "function" && window.matchMedia(query).matches;
    } catch (_err) {
      return false;
    }
  }

  function readQueryParam(name) {
    try {
      if (typeof URLSearchParams !== "function") return null;
      return new URLSearchParams(window.location.search).get(name);
    } catch (_err) {
      return null;
    }
  }

  var I18N = {
    zh: {
      page_title: "Ollama Voice Input — 本地离线语音输入、截屏原位翻译与画笔标注",
      nav_features: "核心能力",
      nav_downloads: "免费下载",
      nav_license: "开源与许可",
      hero_eyebrow: "100% 本地离线 · 零云端 API · 零订阅费",
      hero_title: "完全离线的跨平台语音输入与截屏工作台",
      hero_lead: "集全局流式语音输入、截屏原位翻译、画笔标注、纯 OCR 识别与中英双向翻译于一体。所有识别与纠错均在本机运行，隐私不离设备。",
      hero_cta_dl: "免费下载 v{version}",
      hero_cta_gh: "查看 GitHub 源码",
      fact_os: "Linux · Windows · macOS",
      fact_models: "Qwen3-ASR 1.7B + qwen3.5:4b",
      fact_setup: "设置页一键装齐运行环境",
      demo_region_label: "Voice Studio 实时语音输入动态演示",
      demo_pause_label: "暂停语音演示",
      demo_play_label: "播放语音演示",
      demo_replay_label: "重播语音演示",
      mock_win_title: "Voice Studio · 本地翻译与语音工作台",
      mock_nav_tr: "翻译框 · 主页",
      mock_nav_ocr: "图片 OCR 识别",
      mock_nav_hist: "语音与翻译历史",
      mock_nav_lan: "手机扫码语音",
      mock_nav_setup: "模型与热键设置",
      mock_dir_label: "中",
      mock_dir_target: "英",
      mock_auto_tag: "自动判向 · 1s 防抖",
      mock_pair_tag: "zh → en",
      mock_src_lbl: "原文",
      mock_dst_lbl: "译文",
      feat_h2: "五大本地桌面能力与默认快捷键",
      feat_sub: "全部快捷键可在主窗口「模型与热键设置」自定义；本地看护进程自动管理服务与崩溃重启。",
      cap1_tag: "按住说话 · 松开直出",
      cap1_title: "全局流式语音输入",
      cap1_desc: "在任意焦点应用中按住快捷键说话，Qwen3-ASR 1.7B 实时流式识别并在屏幕底部一体字幕 HUD 呈现稳定字与流式尾字；松开后交由本地 qwen3.5:4b 只纠错不扩写，自动输入回原窗口。",
      cap1_m1: "底部一体字幕 HUD",
      cap1_m2: "自定义热词与连按排队",
      cap2_tag: "全屏红十字框选",
      cap2_title: "截屏原位翻译",
      cap2_desc: "全屏红色十字定位框选任意屏幕区域，按行识别原文位置与格式，经本地模型整段翻译后，以采样底色半透明译文贴片精准排回原位；框下迷你菜单一键复制译文或原文，Esc 随时退出。",
      cap2_m1: "原行位精准贴回",
      cap2_m2: "简体 / 繁体 / 英文 OCR",
      cap2_m3: "复制译文 / 复制原文",
      cap3_tag: "Enter 复制并关闭",
      cap3_title: "截屏画笔标注",
      cap3_desc: "框选冻结画面后直接进行矩形、箭头、自由画笔、文字与马赛克 5 种标注，提供 6 色色板与 3 档粗细；309px 精简纯矢量图标主条与等高二级样式条零抖动切换，按 Enter 自动复制 PNG 到剪贴板并关闭。",
      cap3_bar_note: "309px 纯矢量图标条",
      cap4_tag: "秒提字 · 一键重发",
      cap4_title: "纯 OCR 原位识别 & 重复输入",
      cap4_desc: "Ctrl+Alt+O 复用红十字框选与原位浮窗，跳过 LLM 秒级提取屏幕文字并支持一键复制；Ctrl+Alt+R 将上一次语音识别结果立即重发到当前光标所在窗口。",
      cap5_kbd: "主窗口 / 扫码",
      cap5_tag: "HTTPS :17945",
      cap5_title: "双向自动翻译工作台 & 局域网手机语音",
      cap5_desc: "主窗口提供中⇄英自动判向、1 秒输入防抖、⇄ 手动锁定方向与长文本分段翻译；支持局域网手机扫码打开本地 HTTPS 语音页录入文字；首次运行可在设置页一键装齐 Ollama、qwen3.5:4b 与 Qwen3-ASR 运行环境（自动匹配 CUDA / Apple MPS / CPU）。",
      cap5_m1: "中⇄英自动判向 · 1s 防抖",
      cap5_m2: "长文本分段翻译",
      cap5_m3: "局域网 HTTPS 扫码语音",
      shot_h2: "实机界面预览",
      shot_sub: "工作台截图随当前浅色/深色主题自动切换，标注图展示 309px 纯矢量双条与实时画笔/马赛克效果。",
      shot_tab_wb: "双向翻译工作台",
      shot_tab_ann: "309px 截屏标注",
      shot_cap_wb: "Voice Studio 主窗口：中⇄英自动判向、1 秒防抖、本地运行状态与 5 组默认快捷键总览。",
      shot_cap_ann: "截屏标注浮窗（Ctrl+Alt+A）：矩形/箭头/画笔/马赛克标注、309px 纯矢量主条与 34px 样式条。",
      shot_alt_wb_light: "Voice Studio 本地翻译与 OCR 工作台浅色界面",
      shot_alt_wb_dark: "Voice Studio 本地翻译与 OCR 工作台深色界面",
      shot_alt_ann: "截屏画笔标注与 309px 纯矢量图标双条界面",
      dl_h2: "免费下载 v{version} 安装包",
      dl_sub: "安装包由 GitHub Actions 自动构建并发布于 GitHub Releases，安装后在设置页一键装齐全部本地模型。",
      dl_all_releases: "查看 Releases 全部版本",
      dl_linux_note: "推荐运行环境（Ubuntu 24.04 + Python 3.12），独立 venv 安装，不污染系统 Python。命令：sudo apt install ./{linux_file}",
      dl_btn_linux: "下载 .deb 安装包",
      dl_win_note: "Windows x64 Inno Setup 安装器（未签名，尚未经 Windows 真机验收）。如遇 SmartScreen 提示，点击「更多信息 → 仍要运行」。",
      dl_btn_win: "下载 .exe 安装器",
      dl_mac_note: "Apple Silicon (arm64) 镜像（未公证，尚未经 macOS 真机验收）。拖入 Applications 后需解除隔离属性并授予麦克风、辅助功能与屏幕录制权限。",
      dl_btn_mac: "下载 .dmg 镜像",
      btn_copy_cmd: "复制命令",
      btn_copy_sha: "复制校验命令",
      btn_copied: "已复制",
      verify_lbl: "完整性校验：",
      verify_link: "下载 {sha256_file}",
      unsigned_summary: "未签名安装包首次打开指引（Windows SmartScreen / macOS Gatekeeper）",
      unsigned_win: "开源构建暂未购买商业代码签名证书，且尚未经 Windows 真机验收。若弹出「Windows 已保护你的电脑」，点击「更多信息」→「仍要运行」即可。",
      unsigned_mac: 'macOS 镜像未公证且尚未经 macOS 真机验收。将应用拖入 Applications 后，在终端执行 <code>xattr -cr "/Applications/Ollama Voice Input.app"</code>（或在系统设置 → 隐私与安全性点击「仍要打开」），随后授予麦克风、辅助功能与屏幕录制权限。',
      lic_h2: "开源与商业双许可",
      lic_sub: "清晰边界：个人、研究、企业内部自用及开源分发完全免费；仅闭源二次封装或闭源商用需获取商业授权。",
      lic_free_h3: "开源免费许可",
      lic_free_1: "个人下载、安装与日常使用完全免费",
      lic_free_2: "学术研究、教学评估与个人二次开发免费",
      lic_free_3: "企业 / 组织在内部办公环境部署自用免费",
      lic_free_4: "遵循 AGPL-3.0-only 全部条款的开源修改与分发免费",
      lic_comm_h3: "商业授权",
      lic_comm_badge: "需商业许可",
      lic_comm_1: "闭源二次封装、换标贴牌分发（OEM / White-label）",
      lic_comm_2: "将本软件核心代码或模块集成至闭源商业产品中分发",
      lic_comm_3: "基于本软件对外提供闭源商业 SaaS 或托管服务售卖",
      lic_comm_doc: "查看 COMMERCIAL.md",
      lic_comm_contact: "通过 GitHub Issues 联系",
      foot_copy: "Ollama Voice Input v{version} · AGPL-3.0-only & Commercial Dual License",
      theme_to_dark: "切换深色模式",
      theme_to_light: "切换浅色模式"
    },
    en: {
      page_title: "Ollama Voice Input — Offline Voice Typing, In-Place Translation & Screenshot Annotation",
      nav_features: "Capabilities",
      nav_downloads: "Downloads",
      nav_license: "Licensing",
      hero_eyebrow: "100% Local & Offline · Zero Cloud APIs · Zero Subscription",
      hero_title: "Offline Cross-Platform Voice Input & Screen Workbench",
      hero_lead: "Global streaming voice typing, in-place screenshot translation, brush annotation, pure OCR, and bidirectional translation — all running locally so your voice and screen never leave your machine.",
      hero_cta_dl: "Download v{version} Free",
      hero_cta_gh: "GitHub Repository",
      fact_os: "Linux · Windows · macOS",
      fact_models: "Qwen3-ASR 1.7B + qwen3.5:4b",
      fact_setup: "One-click local runtime setup",
      demo_region_label: "Voice Studio live streaming voice input demo",
      demo_pause_label: "Pause voice demo",
      demo_play_label: "Play voice demo",
      demo_replay_label: "Replay voice demo",
      mock_win_title: "Voice Studio · Local Translate & Voice Workbench",
      mock_nav_tr: "Translate · Home",
      mock_nav_ocr: "Image OCR",
      mock_nav_hist: "History",
      mock_nav_lan: "LAN Mobile Voice",
      mock_nav_setup: "Models & Hotkeys",
      mock_dir_label: "ZH",
      mock_dir_target: "EN",
      mock_auto_tag: "Auto-detect · 1s debounce",
      mock_pair_tag: "en → zh",
      mock_src_lbl: "Source",
      mock_dst_lbl: "Translation",
      feat_h2: "Five Local Desktop Capabilities & Default Hotkeys",
      feat_sub: "Every shortcut is customizable in Models & Hotkeys. Built-in process supervision manages Ollama, ASR, and local HTTPS restarts automatically.",
      cap1_tag: "Hold to speak · Release to type",
      cap1_title: "Global Streaming Voice Input",
      cap1_desc: "Hold the global hotkey in any focused app to dictate. Qwen3-ASR 1.7B streams live captions to the docked bottom HUD, then local qwen3.5:4b corrects typos without expanding meaning and types straight into your cursor.",
      cap1_m1: "Docked bottom subtitle HUD",
      cap1_m2: "Custom hotwords & rapid-fire queue",
      cap2_tag: "Red crosshair capture",
      cap2_title: "In-Place Screenshot Translation",
      cap2_desc: "Drag a full-screen red crosshair over any region. OCR preserves line layout and overlays translucent background-matched translation patches right back in place, with a mini copy bar underneath.",
      cap2_m1: "Exact line-position overlay",
      cap2_m2: "Simplified / Traditional / English OCR",
      cap2_m3: "Copy translation / Copy source",
      cap3_tag: "Enter to copy & close",
      cap3_title: "Screenshot Brush Annotation",
      cap3_desc: "Annotate frozen screenshots with 5 tools (Rectangle, Arrow, Freehand Brush, Text, Mosaic), 6 colors, and 3 stroke/block sizes. A compact 309px pure-vector icon bar with zero-jitter secondary controls copies PNG on Enter.",
      cap3_bar_note: "309px pure-vector toolbar",
      cap4_tag: "Instant OCR · One-key resend",
      cap4_title: "Pure In-Place OCR & Repeat Input",
      cap4_desc: "Ctrl+Alt+O reuses the crosshair selector and skips the LLM for instant text extraction in place; Ctrl+Alt+R immediately re-types the last recognized text into the active window.",
      cap5_kbd: "Workbench / QR",
      cap5_tag: "HTTPS :17945",
      cap5_title: "Bidirectional Translate Workbench & LAN Mobile Voice",
      cap5_desc: "Built-in ZH⇄EN auto-direction detection with 1s debounce, manual ⇄ direction lock, and chunked long-text translation. Scan a QR code on your LAN phone for HTTPS voice input, and install all local runtimes in one click (CUDA / Apple MPS / CPU).",
      cap5_m1: "ZH⇄EN auto-detect · 1s debounce",
      cap5_m2: "Chunked long-text translation",
      cap5_m3: "LAN HTTPS phone voice QR",
      shot_h2: "Real Application Previews",
      shot_sub: "The workbench preview follows your active light or dark theme automatically; the annotation view shows the 309px pure-vector dual toolbar in action.",
      shot_tab_wb: "Translate Workbench",
      shot_tab_ann: "309px Annotate Bar",
      shot_cap_wb: "Voice Studio main window: ZH⇄EN auto-direction, 1s debounce, live runtime status, and 5 default hotkeys.",
      shot_cap_ann: "Screenshot annotation overlay (Ctrl+Alt+A): rectangle, arrow, brush, and mosaic with the 309px vector bar and 34px style bar.",
      shot_alt_wb_light: "Voice Studio local translate and OCR workbench in light theme",
      shot_alt_wb_dark: "Voice Studio local translate and OCR workbench in dark theme",
      shot_alt_ann: "Screenshot brush annotation overlay with 309px pure-vector icon toolbar",
      dl_h2: "Download v{version} Installers Free",
      dl_sub: "Official packages built via GitHub Actions and published on GitHub Releases. Run one-click setup inside Settings after install.",
      dl_all_releases: "View All Releases",
      dl_linux_note: "Primary verified target (Ubuntu 24.04 + Python 3.12). Uses an isolated venv without modifying system Python. Install: sudo apt install ./{linux_file}",
      dl_btn_linux: "Download .deb Package",
      dl_win_note: "Windows x64 Inno Setup installer (unsigned; not yet verified on real Windows hardware). On SmartScreen, choose More info → Run anyway.",
      dl_btn_win: "Download .exe Installer",
      dl_mac_note: "Apple Silicon (arm64) disk image (unnotarized; not yet verified on real macOS hardware). Drag to Applications, clear quarantine, and grant Microphone, Accessibility & Screen Recording permissions.",
      dl_btn_mac: "Download .dmg Image",
      btn_copy_cmd: "Copy command",
      btn_copy_sha: "Copy verify cmd",
      btn_copied: "Copied",
      verify_lbl: "Checksum verification:",
      verify_link: "Download {sha256_file}",
      unsigned_summary: "First-open guidance for unsigned installers (Windows SmartScreen / macOS Gatekeeper)",
      unsigned_win: 'Open-source releases are not commercially code-signed and have not yet been verified on real Windows hardware. On SmartScreen ("Windows protected your PC"), click More info → Run anyway.',
      unsigned_mac: 'macOS images are unnotarized and have not yet been verified on real macOS hardware. After dragging to Applications, run <code>xattr -cr "/Applications/Ollama Voice Input.app"</code> (or System Settings → Privacy & Security → Open Anyway), then grant Microphone, Accessibility, and Screen Recording permissions.',
      lic_h2: "Open Source & Commercial Dual License",
      lic_sub: "Clear terms: free for individuals, academic research, internal company use, and AGPL-3.0 open-source redistribution; commercial license applies only to closed-source redistribution or proprietary SaaS.",
      lic_free_h3: "Open Source — Free",
      lic_free_1: "Free for personal download, installation, and everyday use",
      lic_free_2: "Free for academic research, teaching, evaluation, and personal forks",
      lic_free_3: "Free for internal deployment inside companies and organizations",
      lic_free_4: "Free for open-source modification & redistribution under AGPL-3.0-only",
      lic_comm_h3: "Commercial License",
      lic_comm_badge: "License Required",
      lic_comm_1: "Closed-source repackaging, white-labeling, or OEM redistribution",
      lic_comm_2: "Embedding or integrating core code into closed-source commercial products",
      lic_comm_3: "Operating closed-source commercial SaaS or hosted services without AGPL-3.0 disclosure",
      lic_comm_doc: "Read COMMERCIAL.md",
      lic_comm_contact: "Contact via GitHub Issues",
      foot_copy: "Ollama Voice Input v{version} · AGPL-3.0-only & Commercial Dual License",
      theme_to_dark: "Switch to dark theme",
      theme_to_light: "Switch to light theme"
    }
  };

  /* 首屏实时语音输入 4 阶段脚本（纯离散 setTimeout 推进，中/英独立） */
  var DEMO_SCRIPTS = {
    zh: {
      finalSrc: "按住快捷键说话，本地实时语音输入演示，断网也能稳定直出。",
      finalDst: "Hold the hotkey to speak — local real-time voice input demo, reliable even completely offline.",
      waitingSrc: "正在监听 Ctrl+Alt+Z 语音流，松开后自动输入光标处…",
      waitingDst: "译文将在输入完成后 1 秒自动呈现…",
      polishingSrc: "本地 qwen3.5:4b 仅修正同音错字，不改写原意…",
      polishingDst: "准备同步翻译…",
      steps: [
        {
          phase: "recording",
          delay: 650,
          status: "录音中 · ASR",
          captionType: "stream",
          stable: "按住快捷键说话，",
          tail: "本地实时语音属于",
          dstMeta: "listening"
        },
        {
          phase: "recording",
          delay: 650,
          status: "录音中 · ASR",
          captionType: "stream",
          stable: "按住快捷键说话，本地实时语音属于",
          tail: "演示，断网",
          dstMeta: "listening"
        },
        {
          phase: "recording",
          delay: 650,
          status: "录音中 · ASR",
          captionType: "stream",
          stable: "按住快捷键说话，本地实时语音属于演示，",
          tail: "断网也能稳定直出",
          dstMeta: "listening"
        },
        {
          phase: "polishing",
          delay: 950,
          status: "4B 纠错中",
          captionType: "polish",
          before: "本地实时",
          fixed: "语音输入",
          after: "演示",
          note: "属于→输入 · 只纠错不扩写",
          dstMeta: "polishing"
        },
        {
          phase: "typed",
          delay: 900,
          status: "已输入",
          captionType: "plain",
          text: "已修正并输入当前光标窗口 · 零云端请求",
          dstMeta: "142 ms"
        },
        {
          phase: "dwell",
          delay: 2500,
          status: "已输入",
          captionType: "plain",
          text: "已修正并输入当前光标窗口 · 零云端请求",
          dstMeta: "142 ms"
        }
      ]
    },
    en: {
      finalSrc: "Hold the hotkey to speak — local real-time voice input demo, reliable even completely offline.",
      finalDst: "按住快捷键说话，本地实时语音输入演示，断网也能稳定直出。",
      waitingSrc: "Listening on Ctrl+Alt+Z — release to type at cursor…",
      waitingDst: "Translation updates automatically 1s after input…",
      polishingSrc: "Local qwen3.5:4b fixing typos only, preserving wording…",
      polishingDst: "Preparing translation…",
      steps: [
        {
          phase: "recording",
          delay: 650,
          status: "REC · ASR",
          captionType: "stream",
          stable: "Hold the hotkey to speak — ",
          tail: "voice in put",
          dstMeta: "listening"
        },
        {
          phase: "recording",
          delay: 650,
          status: "REC · ASR",
          captionType: "stream",
          stable: "Hold the hotkey to speak — local voice in put ",
          tail: "demo, reliable",
          dstMeta: "listening"
        },
        {
          phase: "recording",
          delay: 650,
          status: "REC · ASR",
          captionType: "stream",
          stable: "Hold the hotkey to speak — local voice in put demo, ",
          tail: "completely offline",
          dstMeta: "listening"
        },
        {
          phase: "polishing",
          delay: 950,
          status: "4B Polish",
          captionType: "polish",
          before: "Local ",
          fixed: "voice input",
          after: " demo",
          note: "in put→input · typo-only",
          dstMeta: "polishing"
        },
        {
          phase: "typed",
          delay: 900,
          status: "Typed",
          captionType: "plain",
          text: "Typed into focused window · 0 KB cloud",
          dstMeta: "142 ms"
        },
        {
          phase: "dwell",
          delay: 2500,
          status: "Typed",
          captionType: "plain",
          text: "Typed into focused window · 0 KB cloud",
          dstMeta: "142 ms"
        }
      ]
    }
  };

  var HTML_KEYS = { unsigned_mac: true };

  var currentLang = "zh";
  var currentTheme = "light";
  var activeShot = "workbench";

  var demoState = {
    stepIndex: 0,
    paused: false,
    timerId: null
  };

  var elDemo = document.getElementById("voice-demo");
  var elDemoToggle = document.getElementById("demo-toggle");
  var elDemoToggleIcon = document.getElementById("demo-toggle-icon");
  var elDemoReplay = document.getElementById("demo-replay");
  var elDemoSrcPane = document.getElementById("demo-src-pane");
  var elDemoSrcText = document.getElementById("demo-src-text");
  var elDemoDstPane = document.getElementById("demo-dst-pane");
  var elDemoDstText = document.getElementById("demo-dst-text");
  var elDemoDstMeta = document.getElementById("demo-dst-meta");
  var elDemoHudStatus = document.getElementById("demo-hud-status");
  var elDemoHudCaption = document.getElementById("demo-hud-caption");

  function isReducedMotion() {
    return matchQuery("(prefers-reduced-motion: reduce)");
  }

  function applyReleaseBindings() {
    document.querySelectorAll("[data-release-version]").forEach(function (el) {
      el.textContent = "v" + RELEASE.version;
    });
    document.querySelectorAll("[data-release-file]").forEach(function (el) {
      var k = el.getAttribute("data-release-file");
      if (k && RELEASE.assets[k]) el.textContent = assetName(k);
    });
    document.querySelectorAll("[data-release-href]").forEach(function (el) {
      var k = el.getAttribute("data-release-href");
      if (k === "releases") {
        el.setAttribute("href", releasesUrl());
      } else if (k && RELEASE.assets[k]) {
        el.setAttribute("href", downloadUrl(k));
      }
    });
    document.querySelectorAll("[data-release-copy]").forEach(function (el) {
      var k = el.getAttribute("data-release-copy");
      if (k === "linux") {
        el.setAttribute("data-copy", "sudo apt install ./" + assetName("linux"));
      } else if (k === "sha256") {
        el.setAttribute("data-copy", "sha256sum -c " + assetName("sha256"));
      }
    });
    document.querySelectorAll("[data-release-verify-cmd]").forEach(function (el) {
      el.textContent = "sha256sum -c " + assetName("sha256");
    });
  }

  function updateThemeBtn() {
    var btn = document.getElementById("theme-toggle");
    var icon = document.getElementById("theme-icon");
    if (!btn || !icon) return;
    var dict = I18N[currentLang];
    var isDark = currentTheme === "dark";
    var label = isDark ? dict.theme_to_light : dict.theme_to_dark;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
    icon.innerHTML = isDark
      ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
      : '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';
  }

  function updateScreenshotView() {
    var img = document.getElementById("shot-img");
    var cap = document.getElementById("shot-caption");
    var meta = document.getElementById("shot-meta");
    var dict = I18N[currentLang];
    if (img) {
      if (activeShot === "annotate") {
        img.setAttribute("src", "./assets/annotate-icons.png");
        img.setAttribute("width", "1200");
        img.setAttribute("height", "750");
        img.setAttribute("alt", dict.shot_alt_ann);
      } else {
        var file = currentTheme === "dark" ? "workbench-dark.png" : "workbench-light.png";
        img.setAttribute("src", "./assets/" + file);
        img.setAttribute("width", "1280");
        img.setAttribute("height", "800");
        img.setAttribute("alt", currentTheme === "dark" ? dict.shot_alt_wb_dark : dict.shot_alt_wb_light);
      }
    }
    if (cap) {
      cap.textContent = activeShot === "annotate" ? dict.shot_cap_ann : dict.shot_cap_wb;
    }
    if (meta) {
      var assetLabel = activeShot === "annotate"
        ? "annotate-icons.png"
        : (currentTheme === "dark" ? "workbench-dark.png" : "workbench-light.png");
      meta.textContent = "16:10 · " + assetLabel;
    }
    document.querySelectorAll("[data-shot-tab]").forEach(function (tab) {
      var selected = tab.getAttribute("data-shot-tab") === activeShot;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("tabindex", selected ? "0" : "-1");
    });
  }

  function clearDemoTimer() {
    if (demoState.timerId !== null) {
      window.clearTimeout(demoState.timerId);
      demoState.timerId = null;
    }
  }

  function updateDemoControls() {
    var dict = I18N[currentLang];
    if (elDemo) {
      elDemo.setAttribute("aria-label", dict.demo_region_label);
      elDemo.setAttribute("data-demo-paused", demoState.paused ? "true" : "false");
    }
    if (elDemoToggle) {
      var lbl = demoState.paused ? dict.demo_play_label : dict.demo_pause_label;
      elDemoToggle.setAttribute("aria-pressed", demoState.paused ? "true" : "false");
      elDemoToggle.setAttribute("aria-label", lbl);
      elDemoToggle.setAttribute("title", lbl);
    }
    if (elDemoToggleIcon) {
      elDemoToggleIcon.innerHTML = demoState.paused
        ? '<polygon points="4.5,3 13,8 4.5,13"/>'
        : '<rect x="4" y="3" width="3" height="10" rx="0.8"/><rect x="9" y="3" width="3" height="10" rx="0.8"/>';
    }
    if (elDemoReplay) {
      elDemoReplay.setAttribute("aria-label", dict.demo_replay_label);
      elDemoReplay.setAttribute("title", dict.demo_replay_label);
    }
  }

  function renderDemoCaption(step) {
    if (!elDemoHudCaption) return;
    elDemoHudCaption.textContent = "";
    if (step.captionType === "stream") {
      elDemoHudCaption.appendChild(document.createTextNode(step.stable));
      var tail = document.createElement("span");
      tail.className = "hud-tail";
      tail.textContent = step.tail;
      elDemoHudCaption.appendChild(tail);
    } else if (step.captionType === "polish") {
      elDemoHudCaption.appendChild(document.createTextNode(step.before));
      var fix = document.createElement("mark");
      fix.className = "hud-fix";
      fix.textContent = step.fixed;
      elDemoHudCaption.appendChild(fix);
      elDemoHudCaption.appendChild(document.createTextNode(step.after));
      var note = document.createElement("span");
      note.className = "hud-note";
      note.textContent = step.note;
      elDemoHudCaption.appendChild(note);
    } else {
      elDemoHudCaption.textContent = step.text;
    }
  }

  function renderDemoStep(index) {
    var script = DEMO_SCRIPTS[currentLang];
    var step = script.steps[index] || script.steps[0];
    demoState.stepIndex = index;

    if (elDemo) {
      elDemo.setAttribute("data-demo-phase", step.phase);
    }
    if (elDemoHudStatus) {
      elDemoHudStatus.textContent = step.status;
    }
    renderDemoCaption(step);

    if (elDemoDstMeta) {
      elDemoDstMeta.textContent = step.dstMeta;
    }

    var isFinal = step.phase === "typed" || step.phase === "dwell";
    if (elDemoSrcPane && elDemoSrcText) {
      elDemoSrcPane.classList.toggle("is-placeholder", !isFinal);
      elDemoSrcText.textContent = isFinal
        ? script.finalSrc
        : (step.phase === "polishing" ? script.polishingSrc : script.waitingSrc);
    }
    if (elDemoDstPane && elDemoDstText) {
      elDemoDstPane.classList.toggle("is-placeholder", !isFinal);
      elDemoDstText.textContent = isFinal
        ? script.finalDst
        : (step.phase === "polishing" ? script.polishingDst : script.waitingDst);
    }
  }

  function scheduleNextDemoStep() {
    clearDemoTimer();
    if (demoState.paused) return;
    var script = DEMO_SCRIPTS[currentLang];
    var step = script.steps[demoState.stepIndex] || script.steps[0];
    demoState.timerId = window.setTimeout(function () {
      var nextIndex = (demoState.stepIndex + 1) % script.steps.length;
      renderDemoStep(nextIndex);
      scheduleNextDemoStep();
    }, step.delay);
  }

  function startDemo(fromStart) {
    clearDemoTimer();
    var script = DEMO_SCRIPTS[currentLang];
    var typedIndex = 4;
    if (isReducedMotion() && !fromStart) {
      demoState.paused = true;
      renderDemoStep(typedIndex);
      updateDemoControls();
      return;
    }
    demoState.paused = false;
    renderDemoStep(0);
    updateDemoControls();
    scheduleNextDemoStep();
  }

  function applyTheme(theme, persist) {
    currentTheme = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", currentTheme);
    if (persist) writeStore(STORAGE_THEME, currentTheme);
    updateThemeBtn();
    updateScreenshotView();
  }

  function applyLang(lang, persist, restartDemo) {
    currentLang = lang === "en" ? "en" : "zh";
    var dict = I18N[currentLang];
    document.documentElement.lang = currentLang === "en" ? "en" : "zh-CN";
    document.title = interpolate(dict.page_title);
    if (persist) writeStore(STORAGE_LANG, currentLang);

    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var k = el.getAttribute("data-i18n");
      if (!(k in dict)) return;
      var val = interpolate(dict[k]);
      if (HTML_KEYS[k]) el.innerHTML = val;
      else el.textContent = val;
    });

    document.querySelectorAll(".lang-seg button").forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.getAttribute("data-lang") === currentLang ? "true" : "false");
    });

    updateThemeBtn();
    updateScreenshotView();

    if (restartDemo) {
      if (isReducedMotion() && demoState.paused) {
        renderDemoStep(4);
        updateDemoControls();
      } else {
        startDemo(true);
      }
    } else {
      updateDemoControls();
    }
  }

  /* 交互绑定 */
  document.querySelectorAll(".lang-seg button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var nextLang = btn.getAttribute("data-lang");
      if (nextLang !== currentLang) {
        applyLang(nextLang, true, true);
      }
    });
  });

  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      applyTheme(currentTheme === "dark" ? "light" : "dark", true);
    });
  }

  var shotTabs = Array.prototype.slice.call(document.querySelectorAll("[data-shot-tab]"));
  shotTabs.forEach(function (tab, idx) {
    tab.addEventListener("click", function () {
      activeShot = tab.getAttribute("data-shot-tab") === "annotate" ? "annotate" : "workbench";
      updateScreenshotView();
    });
    tab.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        var next = shotTabs[(idx + (ev.key === "ArrowRight" ? 1 : -1) + shotTabs.length) % shotTabs.length];
        activeShot = next.getAttribute("data-shot-tab") === "annotate" ? "annotate" : "workbench";
        updateScreenshotView();
        next.focus();
      }
    });
  });

  if (elDemoToggle) {
    elDemoToggle.addEventListener("click", function () {
      if (demoState.paused) {
        demoState.paused = false;
        updateDemoControls();
        scheduleNextDemoStep();
      } else {
        demoState.paused = true;
        clearDemoTimer();
        updateDemoControls();
      }
    });
  }

  if (elDemoReplay) {
    elDemoReplay.addEventListener("click", function () {
      startDemo(true);
    });
  }

  document.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var key = btn.getAttribute("data-i18n");
      var done = function () {
        btn.textContent = I18N[currentLang].btn_copied;
        window.setTimeout(function () {
          if (key && I18N[currentLang][key]) {
            btn.textContent = interpolate(I18N[currentLang][key]);
          }
        }, 1400);
      };
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        done();
      }
    });
  });

  /* 初始化优先级：URL 参数 > localStorage > 系统偏好 / 默认值 */
  applyReleaseBindings();

  var paramLang = readQueryParam("lang");
  var storedLang = readStore(STORAGE_LANG);
  var initLang = (paramLang === "en" || paramLang === "zh")
    ? paramLang
    : ((storedLang === "en" || storedLang === "zh") ? storedLang : "zh");

  var paramTheme = readQueryParam("theme");
  var storedTheme = readStore(STORAGE_THEME);
  var initTheme = (paramTheme === "dark" || paramTheme === "light")
    ? paramTheme
    : ((storedTheme === "dark" || storedTheme === "light")
      ? storedTheme
      : (matchQuery("(prefers-color-scheme: dark)") ? "dark" : "light"));

  applyTheme(initTheme, false);
  applyLang(initLang, false, false);
  startDemo(false);
})();

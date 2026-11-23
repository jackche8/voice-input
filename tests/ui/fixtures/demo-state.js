/* 仅供测试 / 预览使用的示例数据，不得当作生产页默认状态。 */
(function (global) {
  "use strict";

  const RAW = "今天下午三点我们在会议室讨论一下新版本的发布计化，嗯，还有千问的接入方案";
  const POLISHED = "今天下午三点，我们在会议室讨论一下新版本的发布计划，还有 Qwen 的接入方案。";

  const SAMPLE_HISTORY = {
    records: [
      {
        timestamp: "11-09 10:42",
        raw_text: RAW,
        final_text: POLISHED,
        language: "zh",
        source: "desktop",
        polish: "on",
        job_id: "job-1"
      },
      {
        timestamp: "11-09 10:37",
        raw_text: "把这个函数拆成两个，一个负责读取一个负责校验",
        final_text: "把这个函数拆成两个，一个负责读取一个负责校验",
        language: "zh",
        source: "web",
        polish: "failed",
        job_id: "job-2"
      }
    ],
    total: 2,
    skipped_malformed: 0
  };

  const STEP_KINDS = ["ollama_install", "wait_ollama", "ollama_pull", "asr_runtime_install", "asr_download"];

  /* 视图状态里的一键装齐（已经业务层映射）：states 按固定五步顺序给出，extra 按 kind 覆盖 message / progress。 */
  function installAll(state, current, states, extra) {
    return {
      state: state,
      current: current,
      steps: STEP_KINDS.map(function (kind, i) {
        return Object.assign({ kind: kind, state: states[i] || "pending", message: "", progress: null }, (extra || {})[kind] || {});
      })
    };
  }

  global.V3Fixtures = {
    RAW: RAW,
    POLISHED: POLISHED,
    setupConnected: {
      connection: "connected",
      asr: {
        name: "Qwen3-ASR-1.7B",
        status: "ready",
        sidecarRunning: true,
        sizeLabel: "3.5 GB",
        deviceLabel: "GPU 加速"
      },
      ollama: {
        modelName: "qwen3.5:4b",
        installed: true,
        running: true,
        version: "0.12.6",
        modelStatus: "ready",
        sizeLabel: "3.4 GB"
      },
      installAll: installAll("done", null, ["skipped", "done", "skipped", "skipped", "skipped"]),
      components: [
        { name: "ollama", state: "running", reason: "", failures: 0 },
        { name: "asr", state: "running", reason: "", failures: 0 },
        { name: "server", state: "running", reason: "", failures: 0 }
      ],
      autostart_enabled: true,
      audio_input_device: "",
      audioInputs: {
        defaultDevice: "Built-in Microphone",
        devices: [
          { name: "Built-in Microphone", hostapi: "PulseAudio", isDefault: true },
          { name: "USB Condenser Mic", hostapi: "PulseAudio", isDefault: false }
        ]
      },
      audioInputsLoading: false,
      audioInputError: "",
      audioInputSaved: false,
      polish_enabled: true,
      custom_keywords: ["Qwen", "Ollama"],
      translate_lang_pair: "zh-en",
      translate_auto_enabled: true,
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
      hotkey_annotate: "ctrl+alt+a",
      lanEntries: [{
        ip: "192.0.2.8",
        urlMasked: "https://example.invalid/voice#token=a8f3••••7c21",
        qrSvg: null
      }],
      tokenMasked: "a8f3••••7c21",
      tokenPresent: true,
      serviceUrl: "https://example.invalid",
      localUrl: "https://127.0.0.1:8765",
      history: SAMPLE_HISTORY,
      capabilities: [
        { name: "portal 截图（Wayland）", available: true },
        { name: "xclip（剪贴板）", available: false, installCommand: "sudo apt install xclip" }
      ]
    },
    setupError: {
      connection: "error",
      connectionError: {
        ok: false,
        error: "dependency_failed",
        message: "connect ECONNREFUSED 127.0.0.1:8765"
      }
    },
    setupDownloading: {
      connection: "connected",
      asr: {
        status: "downloading",
        progress: { receivedLabel: "1.47 GB", totalLabel: "3.50 GB", percent: 42 }
      },
      ollama: { installed: true, running: true, modelStatus: "ready" },
      installAll: installAll("running", "asr_download", ["skipped", "done", "skipped", "done", "running"], {
        asr_download: { message: "下载识别模型", progress: { receivedLabel: "1.47 GB", totalLabel: "3.50 GB", percent: 42 } }
      })
    },
    setupFailedDownload: {
      connection: "connected",
      asr: {
        status: "failed",
        message: "下载中断：连接被重置（测试夹具，不是成功）"
      },
      ollama: {
        installed: true,
        running: false,
        modelStatus: "missing",
        message: "Ollama 未运行"
      },
      installAll: installAll("failed", "asr_download", ["skipped", "done", "skipped", "done", "failed"], {
        asr_download: { message: "下载中断：连接被重置（测试夹具，不是成功）" }
      }),
      components: [
        { name: "ollama", state: "stopped", reason: "连续失败 5 次：端口 11434 被占用（测试夹具）", failures: 5 },
        { name: "asr", state: "backing_off", reason: "加载超时", failures: 2 },
        { name: "server", state: "running", reason: "", failures: 0 }
      ],
      autostart_enabled: true
    },
    homeConnected: {
      connection: "connected",
      theme: "light",
      activePanel: "translate",
      asr: {
        name: "Qwen3-ASR-1.7B",
        status: "ready",
        sidecarRunning: true,
        message: ""
      },
      ollama: {
        modelName: "qwen3.5:4b",
        installed: true,
        running: true,
        modelStatus: "ready",
        message: ""
      },
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
      hotkey_annotate: "ctrl+alt+a",
      translate_lang_pair: "zh-en",
      translate_auto_enabled: true,
      history: SAMPLE_HISTORY,
      lanEntries: [{
        ip: "192.0.2.8",
        url: "https://example.invalid/voice#token=secret-should-be-stripped-by-ui",
        urlMasked: "https://example.invalid/voice#token=sec••••ui",
        qrSvg: "<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12'></svg>"
      }],
      tokenMasked: "sec••••ui",
      tokenPresent: true
    },
    homeTranslateResult: {
      connection: "connected",
      translate: {
        phase: "result",
        sourceText: "Release notes 3.2",
        lang_pair: "zh-en",
        source_lang: "en",
        target_lang: "zh",
        direction_mode: "auto",
        chunks: 1,
        debouncePending: false,
        manualOverride: null,
        text: "版本说明 3.2",
        ocr_text: "Release notes 3.2",
        elapsedMs: 128,
        error: null,
        message: ""
      }
    },
    homeTranslateError: {
      connection: "connected",
      translate: {
        phase: "error",
        lang_pair: "zh-en",
        source_lang: null,
        target_lang: null,
        direction_mode: null,
        chunks: null,
        debouncePending: false,
        manualOverride: null,
        text: "",
        ocr_text: "Release notes 3.2",
        error: "translate_failed",
        message: "本地模型无响应（Ollama 未运行）。"
      }
    },
    homeOcrResult: {
      connection: "connected",
      ocr: {
        phase: "result",
        mode: "translate",
        lang_pair: "zh-en",
        source_lang: "en",
        target_lang: "zh",
        direction_mode: "auto",
        fileName: "capture.png",
        ocr_text: "Hello OCR world",
        text: "你好 OCR 世界",
        error: null,
        message: ""
      }
    },
    logsConnected: {
      connection: "connected",
      log_max_lines: 2000,
      logs: {
        phase: "ready",
        log_max_lines: 2000,
        allowed_max_lines: [500, 2000, 5000],
        activeFile: "app.log",
        exists: true,
        max_lines: 2000,
        line_count: 10,
        bytes: 18840,
        truncated: false,
        updated_at: "2026-11-23T19:58:42",
        savingCap: false,
        capSaved: false,
        capError: "",
        clearing: false,
        copyOk: "",
        copyError: "",
        error: null,
        message: "",
        sources: [
          { name: "app.log", group: "runtime", exists: true, line_count: 10, bytes: 18840, truncated: false, has_error: true, has_warn: true, updated_at: "2026-11-23T19:58:42" },
          { name: "server.log", group: "runtime", exists: true, line_count: 5, bytes: 9420, truncated: false, has_error: false, has_warn: true, updated_at: "2026-11-23T19:58:40" },
          { name: "window.log", group: "runtime", exists: true, line_count: 3, bytes: 4120, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-23T19:50:11" },
          { name: "asr.log", group: "runtime", exists: true, line_count: 4, bytes: 32510, truncated: false, has_error: false, has_warn: true, updated_at: "2026-11-23T19:57:18" },
          { name: "ollama.log", group: "runtime", exists: true, line_count: 3, bytes: 15280, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-23T19:52:12" },
          { name: "setup-asr_runtime_install.log", group: "setup", exists: true, line_count: 3, bytes: 24910, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-22T14:22:18" },
          { name: "setup-ollama_install.log", group: "setup", exists: false, line_count: 0, bytes: 0, truncated: false, has_error: false, has_warn: false, updated_at: null },
          { name: "setup-ollama_pull.log", group: "setup", exists: true, line_count: 3, bytes: 6340, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-22T14:18:10" },
          { name: "setup-asr_download.log", group: "setup", exists: true, line_count: 2, bytes: 8190, truncated: false, has_error: false, has_warn: false, updated_at: "2026-11-22T14:25:44" }
        ],
        entries: [
          { line_no: 1, level: "info", text: "2026-11-23 19:41:02 [INFO] Ollama Voice Input v3.4.6 已启动 (linux/x11)，热键后端 x11_grab" },
          { line_no: 2, level: "info", text: "2026-11-23 19:41:02 [INFO] paths.data_root=/var/lib/example/ollama-voice-input paths.logs=/var/lib/example/ollama-voice-input/logs" },
          { line_no: 3, level: "info", text: "2026-11-23 19:41:03 [INFO] supervisor: 启动本地服务 server (127.0.0.1:17945), web_input_token=***" },
          { line_no: 4, level: "info", text: "2026-11-23 19:41:04 [INFO] proxy: 同步系统代理配置 HTTPS_PROXY=http://***:***@127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost" },
          { line_no: 5, level: "info", text: "2026-11-23 19:41:05 [INFO] supervisor: ollama 已就绪 (127.0.0.1:17947, version=0.40.16, model=qwen3.5:4b)" },
          { line_no: 6, level: "info", text: "2026-11-23 19:41:08 [INFO] supervisor: asr sidecar 已就绪 (Qwen3-ASR-1.7B, variant=cuda, device=cuda:0)" },
          { line_no: 7, level: "warn", text: "2026-11-23 19:45:19 [WARN] hotkey: 目标焦点窗口返回短暂拒绝输入，保留 <span class=\"raw\">a & b</span> 原样输出 token=***" },
          { line_no: 8, level: "info", text: "2026-11-23 19:52:11 [INFO] translate: direction=zh->en chunks=2 elapsed_ms=1142 trace=https://127.0.0.1:17945/t?token=***&upstream_proxy=http://***:***@127.0.0.1:7890&seg=long_diagnostic_segment_boundary_check_ok" },
          { line_no: 9, level: "error", text: "2026-11-23 19:55:03 [ERROR] ocr: Tesseract 识别返回空文本区域 (rect=142,88,24,18)，已如实上报 ocr_failed" },
          { line_no: 10, level: "info", text: "2026-11-23 19:58:42 [INFO] log_retention: 单文件保留上限 log_max_lines=2000，已完成尾部截断校验" }
        ]
      }
    },
    aboutConnected: {
      connection: "connected",
      about: {
        phase: "ready",
        app_name: "Voice Studio · Ollama Voice Input",
        version: "3.4.6",
        summary: "100% 本地离线的跨平台语音输入、截屏原位翻译与画笔标注工作台",
        os_name: "linux",
        platform_detail: "Ubuntu 24.04.1 LTS (x86_64)",
        session_type: "X11",
        python_version: "3.12.3",
        run_mode: "source",
        asr_variant: "cuda",
        asr_detail: "CUDA 加速 · NVIDIA GeForce RTX 4070",
        asr_model: "Qwen3-ASR-1.7B",
        ollama_model: "qwen3.5:4b",
        ollama_version: "0.40.16",
        data_root: "/var/lib/example/ollama-voice-input",
        logs_dir: "/var/lib/example/ollama-voice-input/logs",
        config_root: "/etc/example/ollama-voice-input",
        license_title: "AGPL-3.0 + 商业授权",
        license_note: "个人及企业内部非二次封装商用均免费；闭源二次封装或商业再分发需获取商业授权。",
        links: {
          repo_url: "https://github.com/jackche8/voice-input",
          pages_url: "https://jackche8.github.io/voice-input/",
          releases_url: "https://github.com/jackche8/voice-input/releases",
          issues_url: "https://github.com/jackche8/voice-input/issues"
        },
        copyOk: "",
        copyError: "",
        error: null,
        message: ""
      }
    },
    voicePolishFail: {
      connection: "connected",
      tokenValid: true,
      micEnabled: true,
      phase: "polish_failed",
      error: "polish_failed",
      message: "本地模型无响应。",
      raw_text: "把这个函数拆成两个，一个负责读取一个负责校验",
      text: "把这个函数拆成两个，一个负责读取一个负责校验",
      polish: "failed",
      history: { records: [], total: 0, skipped_malformed: 0 }
    }
  };
})(typeof window !== "undefined" ? window : global);

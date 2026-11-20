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
      aboutText: "本地运行（测试夹具）",
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
      polish_enabled: true,
      custom_keywords: ["Qwen", "Ollama"],
      translate_target_lang: "zh",
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_ocr: "ctrl+alt+o",
      hotkey_repeat: "ctrl+alt+r",
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
      theme: "dark",
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
      translate_target_lang: "zh",
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
        target_lang: "zh",
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
        target_lang: "zh",
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
        target_lang: "zh",
        fileName: "capture.png",
        ocr_text: "Hello OCR world",
        text: "你好 OCR 世界",
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

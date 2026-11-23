/* 关于页面纯 UI（模板 C：聚焦头卡 + 紧凑双列键值表 + 5 态 ASR 变体徽章 + 4 外链）。
 * 严守纯展示边界：不发起网络请求、不读写剪贴板或存储、不编造默认版本或路径，所有信息来自业务层注入的 state.about。
 */
(function (V3) {
  "use strict";

  var LINK_SPECS = [
    { key: "repo_url", label: "GitHub 仓库" },
    { key: "pages_url", label: "产品主页 (GitHub Pages)" },
    { key: "releases_url", label: "Releases" },
    { key: "issues_url", label: "Issues" }
  ];

  function call(hooks, name, payload) {
    var fn = hooks && hooks[name];
    if (typeof fn !== "function") return undefined;
    var res = fn(payload);
    if (res && typeof res.then === "function") {
      res.then(undefined, function () {});
    }
    return res;
  }

  function formatError(errCode, message, fallback) {
    if (errCode && Object.prototype.hasOwnProperty.call(V3.labels.ERROR_CODE, errCode)) {
      return V3.labels.formatError(errCode, message);
    }
    if (errCode && message && String(errCode) !== String(message)) {
      return String(errCode) + "：" + String(message);
    }
    if (message) return String(message);
    if (errCode) return String(errCode);
    return fallback || "无法读取关于信息";
  }

  /* 冻结 5 态 ASR 变体徽章唯一规则：
   * cuda / mps / cpu → ok + 原值；null → warn + 未安装；corrupt → danger + 清单损坏；未知原样透传。
   */
  function asrVariantBadge(variant) {
    if (variant === "cuda" || variant === "mps" || variant === "cpu") {
      return { tone: "ok", text: variant, mono: true };
    }
    if (variant === "corrupt") {
      return { tone: "danger", text: "清单损坏", mono: false };
    }
    if (variant == null || variant === "") {
      return { tone: "warn", text: "未安装", mono: false };
    }
    return { tone: "", text: String(variant), mono: true };
  }

  function formatVersionBadge(ver) {
    var s = String(ver || "").trim();
    if (!s) return "";
    return /^v/i.test(s) ? s : ("v" + s);
  }

  function formatPlatform(about) {
    var parts = [];
    var osLabel = V3.labels.labelOf(V3.labels.OS_NAME || {}, about.os_name, "");
    var detail = about.platform_detail ? String(about.platform_detail).trim() : "";
    var session = about.session_type ? String(about.session_type).trim() : "";
    if (osLabel && detail) {
      if (detail.toLowerCase().indexOf(osLabel.toLowerCase()) === 0) {
        parts.push(detail);
      } else {
        parts.push(osLabel, detail);
      }
    } else if (osLabel) {
      parts.push(osLabel);
    } else if (detail) {
      parts.push(detail);
    }
    if (session) parts.push(session);
    return parts.join(" · ") || "—";
  }

  function formatPython(about) {
    var ver = about.python_version ? String(about.python_version).trim() : "";
    var head = ver ? (/^python\b/i.test(ver) ? ver : ("Python " + ver)) : "";
    var mode = V3.labels.labelOf(V3.labels.RUN_MODE || {}, about.run_mode, "");
    if (head && mode) return head + "（" + mode + "）";
    return head || mode || "—";
  }

  function formatModels(about) {
    var parts = [];
    if (about.asr_model) parts.push(String(about.asr_model));
    if (about.ollama_model) {
      var ol = String(about.ollama_model);
      if (about.ollama_version) ol += "（Ollama " + String(about.ollama_version) + "）";
      parts.push(ol);
    } else if (about.ollama_version) {
      parts.push("Ollama " + String(about.ollama_version));
    }
    return parts.join(" · ") || "—";
  }

  function isSafeExternalUrl(url) {
    return typeof url === "string" && /^https?:\/\//i.test(url.trim());
  }

  function kvRow(label, valueNode) {
    return V3.dom.el("div", { className: "kv-table-row" }, [
      V3.dom.el("div", { className: "kv-k", text: label }),
      valueNode
    ]);
  }

  function createAboutPanel(container, options) {
    var opts = options || {};
    var hooks = opts.hooks || {};

    var wrapEl = V3.dom.el("div", {
      className: "about-wrap",
      dataset: { role: "about-body" }
    });
    V3.dom.empty(container);
    container.appendChild(wrapEl);

    var lastAboutRef = null;
    var lastConnected = null;

    function render(state) {
      state = state || {};
      var connected = state.connection === "connected";
      var about = state.about || {};
      if (about === lastAboutRef && connected === lastConnected) return;
      lastAboutRef = about;
      lastConnected = connected;

      V3.dom.empty(wrapEl);
      var phase = about.phase || "idle";

      if (phase === "loading") {
        wrapEl.appendChild(V3.dom.el("div", { className: "studio-box" }, [
          V3.dom.el("div", { className: "box-loading", role: "status", "aria-live": "polite" }, [
            V3.dom.el("span", { text: "正在读取版本与运行环境信息…" }),
            V3.dom.el("div", { className: "progress indeterminate", "aria-hidden": "true" }, [V3.dom.el("i")])
          ])
        ]));
        return;
      }

      if (phase === "error") {
        wrapEl.appendChild(V3.dom.el("div", { className: "studio-box" }, [
          V3.dom.el("div", { className: "box-error", role: "alert" }, [
            V3.dom.el("div", { className: "box-error-main" }, [
              V3.dom.icon("alert"),
              V3.dom.el("span", { text: formatError(about.error, about.message, "无法读取关于信息") })
            ]),
            V3.dom.el("button", {
              type: "button",
              className: "btn btn-sm",
              dataset: { role: "about-retry" },
              text: "重试",
              onClick: function () { call(hooks, "onRefreshAbout"); }
            })
          ])
        ]));
        return;
      }

      if (phase === "empty" || (!about.app_name && !about.version)) {
        wrapEl.appendChild(V3.dom.el("div", { className: "studio-box" }, [
          V3.dom.el("div", { className: "box-empty" }, [
            V3.dom.el("div", {
              text: connected ? "尚未读取版本与运行环境信息。" : "尚未连接本地服务，无法读取版本与运行环境信息。"
            }),
            V3.dom.el("button", {
              type: "button",
              className: "btn btn-sm",
              dataset: { role: "about-empty-refresh" },
              onClick: function () { call(hooks, "onRefreshAbout"); }
            }, [V3.dom.icon("refresh", "ic ic-sm"), document.createTextNode("重新读取")])
          ])
        ]));
        return;
      }

      /* 1. 聚焦头卡 */
      var versionText = formatVersionBadge(about.version);
      var feedbackEl = null;
      if (about.copyError) {
        feedbackEl = V3.dom.el("span", {
          className: "copy-err",
          role: "alert",
          dataset: { role: "about-copy-feedback" },
          text: String(about.copyError)
        });
      } else if (about.copyOk) {
        feedbackEl = V3.dom.el("span", {
          className: "saved",
          role: "status",
          dataset: { role: "about-copy-feedback" },
          text: String(about.copyOk)
        });
      }

      var heroEl = V3.dom.el("div", { className: "about-hero", dataset: { role: "about-hero" } }, [
        V3.dom.el("div", { className: "about-hero-left" }, [
          V3.dom.el("div", { className: "about-mark", "aria-hidden": "true" }, [
            V3.dom.icon("mic", "ic ic-lg")
          ]),
          V3.dom.el("div", { className: "about-hero-text" }, [
            V3.dom.el("div", { className: "about-hero-title-row" }, [
              V3.dom.el("span", { className: "about-hero-title", text: about.app_name || "" }),
              versionText
                ? V3.dom.el("span", {
                    className: "badge accent mono",
                    dataset: { role: "about-version" },
                    text: versionText
                  })
                : null
            ]),
            about.summary
              ? V3.dom.el("div", { className: "about-hero-sub", text: about.summary })
              : null
          ])
        ]),
        V3.dom.el("div", { className: "about-hero-actions" }, [
          feedbackEl,
          V3.dom.el("button", {
            type: "button",
            className: "btn primary",
            dataset: { role: "about-copy-info" },
            onClick: function () { call(hooks, "onCopyAboutInfo"); }
          }, [V3.dom.icon("copy"), document.createTextNode("复制环境信息")])
        ])
      ]);

      /* 2. 紧凑键值表（9 行） */
      var variantSpec = asrVariantBadge(about.asr_variant);
      var variantBadgeCls = "badge" + (variantSpec.tone ? " " + variantSpec.tone : "") + (variantSpec.mono ? " mono" : "");
      var asrChildren = [
        V3.dom.el("span", {
          className: variantBadgeCls,
          dataset: { role: "about-asr-variant" },
          text: variantSpec.text
        })
      ];
      if (about.asr_detail) {
        asrChildren.push(V3.dom.el("span", { text: String(about.asr_detail) }));
      }

      var licenseChildren = [];
      if (about.license_title) {
        licenseChildren.push(V3.dom.el("div", { className: "kv-v" }, [
          V3.dom.el("span", { className: "badge accent", text: String(about.license_title) })
        ]));
      }
      if (about.license_note) {
        licenseChildren.push(V3.dom.el("div", { className: "muted kv-note", text: String(about.license_note) }));
      }
      if (!licenseChildren.length) {
        licenseChildren.push(V3.dom.el("div", { className: "kv-v", text: "—" }));
      }

      var linksMap = (about.links && typeof about.links === "object") ? about.links : {};
      var linkNodes = [];
      LINK_SPECS.forEach(function (spec) {
        var href = linksMap[spec.key];
        if (!isSafeExternalUrl(href)) return;
        linkNodes.push(V3.dom.el("a", {
          className: "linkbtn",
          href: href.trim(),
          target: "_blank",
          rel: "noopener",
          dataset: { aboutLink: spec.key }
        }, [
          document.createTextNode(spec.label),
          V3.dom.icon("external", "ic ic-sm")
        ]));
      });
      var linksCell = linkNodes.length
        ? V3.dom.el("div", { className: "kv-v", dataset: { role: "about-links" } }, linkNodes)
        : V3.dom.el("div", { className: "kv-v", dataset: { role: "about-links" }, text: "—" });

      var rowsEl = V3.dom.el("div", { className: "rows", dataset: { role: "about-kv" } }, [
        kvRow("操作系统与平台", V3.dom.el("div", { className: "kv-v", text: formatPlatform(about) })),
        kvRow("Python 运行环境", V3.dom.el("div", { className: "kv-v mono", text: formatPython(about) })),
        kvRow("ASR 运行变体", V3.dom.el("div", { className: "kv-v" }, asrChildren)),
        kvRow("本地模型栈", V3.dom.el("div", { className: "kv-v mono", text: formatModels(about) })),
        kvRow("本地数据目录", V3.dom.el("div", { className: "kv-v mono kv-path", text: about.data_root || "—" })),
        kvRow("本地日志目录", V3.dom.el("div", { className: "kv-v mono kv-path", text: about.logs_dir || "—" })),
        kvRow("本地配置目录", V3.dom.el("div", { className: "kv-v mono kv-path", text: about.config_root || "—" })),
        kvRow("开源与商业双许可", V3.dom.el("div", { className: "kv-v-stack" }, licenseChildren)),
        kvRow("项目与文档链接", linksCell)
      ]);

      wrapEl.appendChild(heroEl);
      wrapEl.appendChild(rowsEl);
    }

    function destroy() {
      lastAboutRef = null;
      lastConnected = null;
    }

    return {
      render: render,
      destroy: destroy
    };
  }

  V3.aboutPanel = {
    create: createAboutPanel,
    asrVariantBadge: asrVariantBadge
  };
})(window.V3 = window.V3 || {});

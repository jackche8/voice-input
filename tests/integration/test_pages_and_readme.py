"""GitHub Pages 双语产品主页、单源实机截图、Pages 工作流与双语 README 契约及真浏览器验收测试。"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from _paths import PROGRAM_ROOT
from _version import APP_VERSION
from tests.integration.test_packaging_scripts import _load_yaml

ROOT = PROGRAM_ROOT
SITE_DIR = ROOT / "docs" / "site"
ASSETS_DIR = SITE_DIR / "assets"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
LINUX_CONTROL = ROOT / "packaging" / "linux" / "control.in"
README_ZH = ROOT / "README.md"
README_EN = ROOT / "README.en.md"

REPO_URL = "https://github.com/jackche8/voice-input"
PAGES_URL = "https://jackche8.github.io/voice-input/"
LEGACY_OWNER = "legacy-owner-removed"

EXPECTED_SITE_NONEMPTY = (
    SITE_DIR / "index.html",
    SITE_DIR / "styles.css",
    SITE_DIR / "site.js",
    ASSETS_DIR / "workbench-light.png",
    ASSETS_DIR / "workbench-dark.png",
    ASSETS_DIR / "annotate-icons.png",
)


def _find_chrome() -> str:
    """动态查找可用 Chrome/Chromium；缺失时显式 fail，严禁静默 skip。"""
    env_bin = os.environ.get("CHROME_BIN", "").strip()
    if env_bin:
        candidate = Path(env_bin)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        resolved = shutil.which(env_bin)
        if resolved:
            return resolved
        pytest.fail(f"环境变量 CHROME_BIN={env_bin!r} 不可执行或未在 PATH 中找到")
    for name in ("google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    pytest.fail("未找到可用 Chrome/Chromium（已查 CHROME_BIN / google-chrome / chromium / chromium-browser）")


def _make_short_chrome_root() -> Path:
    """复用短路径策略（优先 $XDG_RUNTIME_DIR，回退 .pp/tmp，绝不写 /tmp）并校验 socket 字节长 < 104。"""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and Path(xdg).is_dir():
        base = Path(xdg) / f"ovi-ch-{os.getpid()}"
    else:
        base = ROOT / ".pp" / "tmp" / f"c-{os.getpid()}"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _prepare_chrome_subprofile(base: Path, name: str) -> Path:
    sub = base / name
    if sub.exists():
        shutil.rmtree(sub)
    sub.mkdir(parents=True, exist_ok=True)
    probe = sub / "com.google.Chrome.XXXXXX" / "SingletonSocket"
    probe_len = len(str(probe).encode("utf-8"))
    assert probe_len < 104, f"Chrome SingletonSocket 路径超长（{probe_len} >= 104B）：{probe}"
    return sub


def _run_chrome_dump_dom(
    chrome: str,
    profile_dir: Path,
    url: str,
    *,
    virtual_time_budget_ms: int,
    extra_args: tuple[str, ...] = (),
) -> str:
    env = os.environ.copy()
    env["TMPDIR"] = str(profile_dir)
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
        f"--user-data-dir={profile_dir}",
        f"--virtual-time-budget={virtual_time_budget_ms}",
        *extra_args,
        "--dump-dom",
        url,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"Chrome --dump-dom 失败 rc={proc.returncode} url={url}\nstderr={proc.stderr[:1000]}"
    return proc.stdout


def _repo_non_ignored_files() -> list[Path]:
    """返回所有已追踪与未忽略的仓库文件（排除 .gitignore 条目及 .pp/ 工作目录）。"""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(ROOT),
        capture_output=True,
        check=True,
    )
    out: list[Path] = []
    for raw in proc.stdout.split(b"\x00"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if rel == ".pp" or rel.startswith(".pp/"):
            continue
        p = ROOT / rel
        if p.is_file():
            out.append(p)
    return out


def _release_asset_names_from_workflow() -> list[str]:
    """从 .github/workflows/release.yml 解析权威产物文件名集合。"""
    src = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    expected = [
        f"ollama-voice-input_{APP_VERSION}_amd64.deb",
        f"Ollama-Voice-Input-{APP_VERSION}-windows-x64-setup.exe",
        f"Ollama-Voice-Input-{APP_VERSION}-macos-arm64.dmg",
        "SHA256SUMS.txt",
    ]
    templates = (
        "ollama-voice-input_${APP_VERSION}_amd64.deb",
        "Ollama-Voice-Input-${APP_VERSION}-windows-x64-setup.exe",
        "Ollama-Voice-Input-${APP_VERSION}-macos-arm64.dmg",
        "SHA256SUMS.txt",
    )
    for tpl in templates:
        assert tpl in src, f"release.yml 缺少产物模板 {tpl!r}"
    return expected


def _parse_release_object(site_js: str) -> dict[str, Any]:
    match = re.search(r"var\s+RELEASE\s*=\s*\{(?P<body>[\s\S]*?)\n\s*\};", site_js)
    assert match is not None, "docs/site/site.js 缺少单源 RELEASE 对象定义"
    body = match.group("body")
    ver_m = re.search(r'version\s*:\s*"([^"]+)"', body)
    repo_m = re.search(r'repo\s*:\s*"([^"]+)"', body)
    assert ver_m and repo_m, "RELEASE 对象缺少 version 或 repo 字段"
    assets: dict[str, str] = {}
    for key in ("linux", "windows", "macos", "sha256"):
        am = re.search(rf'{key}\s*:\s*"([^"]+)"', body)
        assert am is not None, f"RELEASE.assets 缺少 {key}"
        assets[key] = am.group(1)
    return {"version": ver_m.group(1), "repo": repo_m.group(1), "assets": assets}


# ---------------------------------------------------------------------------
# 1. 单源实机截图尺寸、体积、不透明、去重与隐私校验
# ---------------------------------------------------------------------------
def test_site_assets_dimensions_budget_and_privacy() -> None:
    expected_specs = {
        "workbench-light.png": (1280, 800),
        "workbench-dark.png": (1280, 800),
        "annotate-icons.png": (1200, 750),
    }
    total_bytes = 0
    luma: dict[str, float] = {}
    asset_hashes: dict[str, str] = {}

    for name, expected_size in expected_specs.items():
        path = ASSETS_DIR / name
        assert path.is_file(), f"缺少实机截图：{path}"
        raw = path.read_bytes()
        size = len(raw)
        total_bytes += size
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是合法 PNG 头"
        assert size <= 300 * 1024, f"{name} 超出单张 300KB 预算：{size}B"
        for forbidden in (b"/home/", LEGACY_OWNER.encode("ascii"), b"192.168.", b"token="):
            assert forbidden not in raw, f"{name} 原始字节含隐私或旧归属信息 {forbidden!r}"

        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            assert im.size == expected_size, f"{name} 尺寸 {im.size} != {expected_size}"
            if im.mode == "RGB":
                pass
            elif "A" in im.getbands():
                min_alpha, _max_alpha = im.getchannel("A").getextrema()
                assert min_alpha == 255, f"{name} 存在半透明像素（min_alpha={min_alpha}）"
            else:
                pytest.fail(f"{name} 图像色彩模式不受支持：{im.mode}")
            for k, v in im.info.items():
                val_s = str(v)
                assert "/home/" not in val_s and LEGACY_OWNER not in val_s, f"{name} 元数据 {k} 含隐私：{val_s}"
            gray = im.convert("L").tobytes()
            luma[name] = sum(gray) / len(gray)

        asset_hashes[name] = hashlib.sha256(raw).hexdigest()

    assert total_bytes <= 700 * 1024, f"3 张实机截图总计超出 700KB 预算：{total_bytes}B"
    assert asset_hashes["workbench-light.png"] != asset_hashes["workbench-dark.png"], "浅/深工作台截图不得为同图"
    assert luma["workbench-light.png"] > luma["workbench-dark.png"] + 80.0, (
        f"浅色图均亮度应显著高于暗色图：light={luma['workbench-light.png']:.1f}, dark={luma['workbench-dark.png']:.1f}"
    )

    # 全仓非忽略/已追踪文件严禁存在这 3 张 PNG 的重复副本
    target_hash_set = set(asset_hashes.values())
    allowed_paths = {(ASSETS_DIR / name).resolve() for name in expected_specs}
    for repo_file in _repo_non_ignored_files():
        if repo_file.suffix.lower() != ".png":
            continue
        if repo_file.resolve() in allowed_paths:
            continue
        digest = hashlib.sha256(repo_file.read_bytes()).hexdigest()
        assert digest not in target_hash_set, f"发现单源实机图的重复副本：{repo_file}"


# ---------------------------------------------------------------------------
# 2. 站点离线零构建、零外链 CDN、零越界路径与单源 RELEASE 版本一致
# ---------------------------------------------------------------------------
def test_site_static_offline_and_single_source_version() -> None:
    nojekyll = SITE_DIR / ".nojekyll"
    assert nojekyll.is_file() and nojekyll.stat().st_size == 0, "docs/site/.nojekyll 必须存在且为 0 字节"
    for path in EXPECTED_SITE_NONEMPTY:
        assert path.is_file() and path.stat().st_size > 0, f"缺少或为空的站点文件：{path}"

    html = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    css = (SITE_DIR / "styles.css").read_text(encoding="utf-8")
    js = (SITE_DIR / "site.js").read_text(encoding="utf-8")
    joined = "\n".join((html, css, js))

    # 离线与纯静态禁忌
    forbidden_patterns = (
        (r'type\s*=\s*["\']module["\']', 'type="module"'),
        (r"\bfetch\s*\(", "fetch("),
        (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
        (r"navigator\.mediaDevices", "navigator.mediaDevices"),
        (r"\brequestAnimationFrame\b", "requestAnimationFrame"),
        (re.escape(LEGACY_OWNER), "legacy repo owner"),
        (r"(?:href|src)\s*=\s*[\"']\.\./", "parent-escape ../ attribute"),
        (r"(?:href|src)\s*=\s*[\"']/assets/", "root-absolute /assets/ path"),
        (r"url\(\s*['\"]?\s*(?:https?:|//|/|\.\./)", "external/root/parent CSS url()"),
    )
    for pattern, label in forbidden_patterns:
        assert not re.search(pattern, joined, flags=re.IGNORECASE), f"docs/site/ 违规包含 {label}"

    # 外部资源加载禁用（仅 <a> 允许外链；<script>/<link rel="stylesheet">/<img> 严禁外链或根路径）
    for tag_re, desc in (
        (r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']', "<script src>"),
        (r'<link[^>]+rel\s*=\s*["\']stylesheet["\'][^>]+href\s*=\s*["\']([^"\']+)["\']', "<link stylesheet>"),
        (r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', "<img src>"),
    ):
        for m in re.finditer(tag_re, html, flags=re.IGNORECASE):
            val = m.group(1).strip()
            assert not val.startswith(("http://", "https://", "//", "/", "../")), f"{desc} 不得使用外链或越界路径：{val!r}"

    # favicon data:image/svg+xml 必须把所有 # 转义为 %23（按外层引号配对，允许内部单引号属性）
    fav_m = re.search(
        r'<link[^>]+rel\s*=\s*["\']icon["\'][^>]+href\s*=\s*(?P<q>["\'])(?P<val>data:image/svg\+xml.*?)(?P=q)',
        html,
    )
    assert fav_m is not None, "index.html 缺少 data:image/svg+xml favicon"
    fav_val = fav_m.group("val")
    assert "#" not in fav_val, "favicon data:image/svg+xml 中存在未转义的 #"
    assert "%23" in fav_val, "favicon data:image/svg+xml 应包含 %23 转义色值"

    # 单源 RELEASE 与 _version.APP_VERSION + release.yml 产物名严格同步
    rel = _parse_release_object(js)
    assert rel["version"] == APP_VERSION, f"site.js RELEASE.version={rel['version']!r} != _version.APP_VERSION={APP_VERSION!r}"
    assert rel["repo"] == REPO_URL, f"site.js RELEASE.repo={rel['repo']!r} != {REPO_URL!r}"

    derived_assets = [rel["assets"][k].replace("{version}", rel["version"]) for k in ("linux", "windows", "macos", "sha256")]
    workflow_assets = _release_asset_names_from_workflow()
    assert derived_assets == workflow_assets, f"site.js 派生包名 {derived_assets} != release.yml {workflow_assets}"

    for asset in workflow_assets:
        expected_url = f"{REPO_URL}/releases/download/v{APP_VERSION}/{asset}"
        assert expected_url in html, f"index.html 静态兜底缺少直链 {expected_url}"
    assert f'href="{REPO_URL}/releases"' in html, "index.html 缺少 /releases 全部版本入口"


# ---------------------------------------------------------------------------
# 3. 真 Chrome 双路径（裸 file:// + /voice-input/ 子路径同源 runner + reduced-motion）
# ---------------------------------------------------------------------------
def test_site_real_browser_file_and_subpath_interactions() -> None:
    chrome = _find_chrome()
    chrome_root = _make_short_chrome_root()

    try:
        # ---- 路径 ①：裸 file://（绝不传 --allow-file-access-from-files）----
        file_url = (SITE_DIR / "index.html").resolve().as_uri()
        p_file_short = _prepare_chrome_subprofile(chrome_root, "f1")
        dom_short = _run_chrome_dump_dom(chrome, p_file_short, file_url, virtual_time_budget_ms=200)
        assert 'data-demo-phase="recording"' in dom_short, "裸 file:// 短预算应处于 recording 阶段"

        p_file_long = _prepare_chrome_subprofile(chrome_root, "f2")
        dom_long = _run_chrome_dump_dom(chrome, p_file_long, file_url, virtual_time_budget_ms=3200)
        m_phase = re.search(r'id="voice-demo"[^>]*data-demo-phase="([^"]+)"', dom_long)
        assert m_phase is not None, "裸 file:// 长预算未找到 #voice-demo data-demo-phase"
        assert m_phase.group(1) in ("polishing", "typed", "dwell"), (
            f"裸 file:// 长预算下语音演示未推进：{m_phase.group(1)!r}"
        )

        p_file_en = _prepare_chrome_subprofile(chrome_root, "f3")
        dom_en = _run_chrome_dump_dom(chrome, p_file_en, f"{file_url}?lang=en&theme=dark", virtual_time_budget_ms=400)
        assert re.search(r'<html[^>]*lang="en"[^>]*data-theme="dark"', dom_en) or re.search(
            r'<html[^>]*data-theme="dark"[^>]*lang="en"', dom_en
        ), "裸 file:// ?lang=en&theme=dark 未正确应用双语与深色主题"
        assert 'src="./assets/workbench-dark.png"' in dom_en, "裸 file:// 深色模式未自动切换 workbench-dark.png"

        # ---- 路径 ② & ③：本地 /voice-input/ 子路径同源服务 ----
        requested_paths: list[str] = []
        not_found_paths: list[str] = []
        site_html_raw = (SITE_DIR / "index.html").read_text(encoding="utf-8")

        probe_html = """<!doctype html>
<html><head><meta charset="utf-8"><link rel="icon" href="data:,"><title>PROBE_RUNNING</title></head>
<body style="margin:0">
<iframe id="app" src="/voice-input/index.html" style="width:1280px;height:900px;border:0;display:block"></iframe>
<pre id="probe-result"></pre>
<script>
(function () {
  var frame = document.getElementById("app");
  var outEl = document.getElementById("probe-result");
  var errors = [];
  var seenPhases = [];
  var phaseHeights = {};
  var phaseDone = false;

  function finish(ok, payload) {
    outEl.textContent = JSON.stringify({ ok: ok, errors: errors, data: payload });
    document.title = ok ? "PROBE_PASS" : "PROBE_FAIL";
  }

  function waitImgLoaded(img, cb) {
    if (img.complete && img.naturalWidth > 0) {
      window.setTimeout(cb, 30);
      return;
    }
    var settled = false;
    function done() {
      if (settled) return;
      settled = true;
      cb();
    }
    img.addEventListener("load", done, { once: true });
    img.addEventListener("error", done, { once: true });
    window.setTimeout(done, 1200);
  }

  frame.addEventListener("load", function () {
    var win = frame.contentWindow;
    var doc = frame.contentDocument;
    var origErr = win.console && win.console.error;
    if (win.console) {
      win.console.error = function () {
        errors.push("console.error:" + Array.prototype.join.call(arguments, " "));
        if (origErr) origErr.apply(win.console, arguments);
      };
    }
    win.addEventListener("error", function (ev) {
      errors.push("window.error:" + (ev.message || "unknown"));
    });
    win.addEventListener("unhandledrejection", function (ev) {
      errors.push("unhandledrejection:" + String(ev.reason || "unknown"));
    });

    var demo = doc.getElementById("voice-demo");
    var topbarInner = doc.querySelector(".topbar-inner");
    var brand = doc.querySelector(".brand");
    var tools = doc.querySelector(".nav-tools");
    var shotSection = doc.getElementById("screenshots");
    var shotFrame = doc.querySelector(".shot-frame");
    var shotImg = doc.getElementById("shot-img");
    var toggleBtn = doc.getElementById("demo-toggle");
    var replayBtn = doc.getElementById("demo-replay");
    var themeBtn = doc.getElementById("theme-toggle");
    var btnEn = doc.querySelector('.lang-seg button[data-lang="en"]');
    var btnZh = doc.querySelector('.lang-seg button[data-lang="zh"]');
    var tabAnn = doc.querySelector('[data-shot-tab="annotate"]');
    var tabWb = doc.querySelector('[data-shot-tab="workbench"]');
    var hudStatus = doc.getElementById("demo-hud-status");

    function rectInfo(el) {
      var r = el.getBoundingClientRect();
      return { x: r.x, top: r.top, width: r.width, height: r.height };
    }

    function recordPhase() {
      var ph = demo.getAttribute("data-demo-phase");
      if (seenPhases.indexOf(ph) === -1) {
        seenPhases.push(ph);
        phaseHeights[ph] = demo.getBoundingClientRect().height;
      }
      if (!phaseDone && seenPhases.length === 4) {
        phaseDone = true;
        observer.disconnect();
        runInteractiveChecks();
      }
    }

    var observer = new MutationObserver(recordPhase);
    observer.observe(demo, { attributes: true, attributeFilter: ["data-demo-phase"] });
    recordPhase();

    function runInteractiveChecks() {
      var report = {
        seenPhases: seenPhases.slice(),
        phaseHeights: phaseHeights
      };

      /* 1. 暂停 / 恢复 / 重播 */
      toggleBtn.click();
      report.pausedAfterClick = demo.getAttribute("data-demo-paused");
      report.togglePressed = toggleBtn.getAttribute("aria-pressed");
      var frozenPhase = demo.getAttribute("data-demo-phase");

      window.setTimeout(function () {
        report.phaseStayedFrozen = demo.getAttribute("data-demo-phase") === frozenPhase;
        toggleBtn.click();
        report.resumedPausedAttr = demo.getAttribute("data-demo-paused");
        replayBtn.click();
        report.phaseAfterReplay = demo.getAttribute("data-demo-phase");

        /* 2. 中 ↔ EN 切换：顶栏与首屏预览窗几何零抖动 + 脚本重启切英文 */
        var topBefore = rectInfo(topbarInner);
        var brandBefore = rectInfo(brand);
        var toolsBefore = rectInfo(tools);
        var demoBefore = rectInfo(demo);

        btnEn.click();
        var topEn = rectInfo(topbarInner);
        var brandEn = rectInfo(brand);
        var toolsEn = rectInfo(tools);
        var demoEn = rectInfo(demo);
        report.enLang = doc.documentElement.lang;
        report.enDemoPhase = demo.getAttribute("data-demo-phase");
        report.enHudStatus = hudStatus.textContent;
        report.topbarHeightDelta = Math.abs(topEn.height - topBefore.height);
        report.brandDeltas = {
          x: Math.abs(brandEn.x - brandBefore.x),
          top: Math.abs(brandEn.top - brandBefore.top),
          height: Math.abs(brandEn.height - brandBefore.height)
        };
        report.toolsDeltas = {
          x: Math.abs(toolsEn.x - toolsBefore.x),
          top: Math.abs(toolsEn.top - toolsBefore.top),
          height: Math.abs(toolsEn.height - toolsBefore.height)
        };
        report.demoLangHeightDelta = Math.abs(demoEn.height - demoBefore.height);

        /* 切回中文并在同一语言内测浅/深切图与 Tab 切图几何稳定 */
        btnZh.click();
        report.zhHudStatus = hudStatus.textContent;

        waitImgLoaded(shotImg, function () {
          report.imgLight = {
            src: shotImg.getAttribute("src"),
            complete: shotImg.complete,
            w: shotImg.naturalWidth,
            h: shotImg.naturalHeight
          };
          var shotSecBase = rectInfo(shotSection).height;
          var shotFrameBase = rectInfo(shotFrame).height;

          themeBtn.click();
          waitImgLoaded(shotImg, function () {
            report.themeAfterToggle = doc.documentElement.getAttribute("data-theme");
            report.imgDark = {
              src: shotImg.getAttribute("src"),
              complete: shotImg.complete,
              w: shotImg.naturalWidth,
              h: shotImg.naturalHeight
            };
            report.shotThemeSecDelta = Math.abs(rectInfo(shotSection).height - shotSecBase);
            report.shotThemeFrameDelta = Math.abs(rectInfo(shotFrame).height - shotFrameBase);

            tabAnn.click();
            waitImgLoaded(shotImg, function () {
              report.imgAnnotate = {
                src: shotImg.getAttribute("src"),
                complete: shotImg.complete,
                w: shotImg.naturalWidth,
                h: shotImg.naturalHeight
              };
              report.shotTabSecDelta = Math.abs(rectInfo(shotSection).height - shotSecBase);
              report.shotTabFrameDelta = Math.abs(rectInfo(shotFrame).height - shotFrameBase);

              tabWb.click();
              themeBtn.click();
              waitImgLoaded(shotImg, function () {
                report.restoredLightSrc = shotImg.getAttribute("src");
                finish(errors.length === 0, report);
              });
            });
          });
        });
      }, 1500);
    }

    window.setTimeout(function () {
      if (!phaseDone) {
        errors.push("timeout waiting for 4 demo phases; seen=" + seenPhases.join(","));
        finish(false, { seenPhases: seenPhases, phaseHeights: phaseHeights });
      }
    }, 9000);
  });
})();
</script>
</body></html>
"""

        # 构造 localStorage getter 抛 SecurityError 的同源探针页（复用真实 index.html 正文）
        sec_inject = (
            "<script>"
            "window.__secErrors = [];"
            "window.addEventListener('error', function(e){ window.__secErrors.push(e.message || 'err'); });"
            "Object.defineProperty(window, 'localStorage', { configurable: true, get: function () { "
            "throw new DOMException('blocked', 'SecurityError'); } });"
            "</script>"
        )
        sec_tail = (
            "<script>"
            "window.addEventListener('DOMContentLoaded', function () {"
            "  window.setTimeout(function () {"
            "    document.getElementById('theme-toggle').click();"
            "    document.querySelector('.lang-seg button[data-lang=\"en\"]').click();"
            "    var pre = document.createElement('pre');"
            "    pre.id = 'sec-report';"
            "    pre.textContent = JSON.stringify({"
            "      errors: window.__secErrors,"
            "      lang: document.documentElement.lang,"
            "      theme: document.documentElement.getAttribute('data-theme'),"
            "      src: document.getElementById('shot-img').getAttribute('src')"
            "    });"
            "    document.body.appendChild(pre);"
            "  }, 80);"
            "});"
            "</script>"
        )
        sec_html = site_html_raw.replace("<head>", "<head>" + sec_inject, 1).replace("</body>", sec_tail + "</body>", 1)

        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
        }

        class SubpathHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                requested_paths.append(path)
                if path == "/voice-input/__probe.html":
                    body = probe_html.encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif path == "/voice-input/__probe_sec.html":
                    body = sec_html.encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif path.startswith("/voice-input/"):
                    rel = path[len("/voice-input/") :] or "index.html"
                    target = (SITE_DIR / rel).resolve()
                    if not str(target).startswith(str(SITE_DIR.resolve())) or not target.is_file():
                        not_found_paths.append(path)
                        self.send_error(404)
                        return
                    body = target.read_bytes()
                    ctype = mime_map.get(target.suffix.lower(), "application/octet-stream")
                else:
                    not_found_paths.append(path)
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        srv = ThreadedServer(("127.0.0.1", 0), SubpathHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            # ② 完整交互、4 阶段动画、几何稳定与切图校验
            p_sub = _prepare_chrome_subprofile(chrome_root, "s1")
            dom_probe = _run_chrome_dump_dom(
                chrome,
                p_sub,
                f"http://127.0.0.1:{port}/voice-input/__probe.html",
                virtual_time_budget_ms=14000,
            )
            assert "<title>PROBE_PASS</title>" in dom_probe, f"同源交互探针失败：\n{dom_probe[-1200:]}"
            m_json = re.search(r'<pre id="probe-result">([\s\S]*?)</pre>', dom_probe)
            assert m_json is not None, "缺少 probe-result 报告"
            payload = json.loads(m_json.group(1))
            assert payload["ok"] is True and not payload["errors"], f"浏览器存在报错：{payload}"
            data = payload["data"]

            assert data["seenPhases"] == ["recording", "polishing", "typed", "dwell"], (
                f"4 阶段推进顺序异常：{data['seenPhases']}"
            )
            heights = list(data["phaseHeights"].values())
            assert max(heights) - min(heights) <= 0.5, f"语音演示 4 阶段高度抖动：{data['phaseHeights']}"

            assert data["pausedAfterClick"] == "true" and data["togglePressed"] == "true"
            assert data["phaseStayedFrozen"] is True, "暂停后阶段未冻结"
            assert data["resumedPausedAttr"] == "false"
            assert data["phaseAfterReplay"] == "recording", "重播未复位至 recording"

            assert data["enLang"] == "en" and data["enDemoPhase"] == "recording"
            assert "REC" in data["enHudStatus"], f"切英文后演示脚本未切换：{data['enHudStatus']!r}"
            assert "录音中" in data["zhHudStatus"], f"切回中文后演示脚本未切换：{data['zhHudStatus']!r}"

            assert data["topbarHeightDelta"] <= 0.5, f"顶栏高度抖动：{data['topbarHeightDelta']}"
            for side in ("brandDeltas", "toolsDeltas"):
                for axis, delta in data[side].items():
                    assert delta <= 0.5, f"顶栏 {side}.{axis} 位移超标：{delta}"
            assert data["demoLangHeightDelta"] <= 0.5, f"中英切换首屏预览窗高度抖动：{data['demoLangHeightDelta']}"

            assert data["themeAfterToggle"] == "dark"
            assert data["imgLight"] == {"src": "./assets/workbench-light.png", "complete": True, "w": 1280, "h": 800}
            assert data["imgDark"] == {"src": "./assets/workbench-dark.png", "complete": True, "w": 1280, "h": 800}
            assert data["imgAnnotate"] == {"src": "./assets/annotate-icons.png", "complete": True, "w": 1200, "h": 750}
            assert data["restoredLightSrc"] == "./assets/workbench-light.png"
            assert data["shotThemeSecDelta"] <= 0.5 and data["shotThemeFrameDelta"] <= 0.5
            assert data["shotTabSecDelta"] <= 0.5 and data["shotTabFrameDelta"] <= 0.5

            # ②b localStorage getter 抛 SecurityError 平滑降级
            p_sec = _prepare_chrome_subprofile(chrome_root, "s2")
            dom_sec = _run_chrome_dump_dom(
                chrome,
                p_sec,
                f"http://127.0.0.1:{port}/voice-input/__probe_sec.html",
                virtual_time_budget_ms=1000,
            )
            m_sec = re.search(r'<pre id="sec-report">([\s\S]*?)</pre>', dom_sec)
            assert m_sec is not None, "SecurityError 降级探针未输出结果"
            sec_data = json.loads(m_sec.group(1))
            assert sec_data == {
                "errors": [],
                "lang": "en",
                "theme": "dark",
                "src": "./assets/workbench-dark.png",
            }, f"localStorage SecurityError 降级异常：{sec_data}"

            # ③ prefers-reduced-motion: reduce 静默停在 typed 态不自动轮播
            p_rm = _prepare_chrome_subprofile(chrome_root, "s3")
            dom_rm = _run_chrome_dump_dom(
                chrome,
                p_rm,
                f"http://127.0.0.1:{port}/voice-input/index.html",
                virtual_time_budget_ms=5000,
                extra_args=("--force-prefers-reduced-motion",),
            )
            assert 'data-demo-phase="typed"' in dom_rm and 'data-demo-paused="true"' in dom_rm, (
                "prefers-reduced-motion 模式下未静态停留在 typed + paused=true"
            )

            # 全部本地请求零 404 且严禁越出 /voice-input/ 子路径
            assert not not_found_paths, f"出现 404 请求：{not_found_paths}"
            assert all(p.startswith("/voice-input/") for p in requested_paths), f"出现越界请求：{requested_paths}"
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        shutil.rmtree(chrome_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. .github/workflows/pages.yml 契约校验（复用 test_packaging_scripts._load_yaml）
# ---------------------------------------------------------------------------
def test_pages_workflow_yaml_contract() -> None:
    doc = _load_yaml(PAGES_WORKFLOW)
    triggers = doc.get("on") or doc.get("True") or doc.get(True)
    assert isinstance(triggers, dict), "pages.yml 缺少触发器配置"
    assert triggers["push"]["branches"] == ["main"]
    assert set(triggers["push"]["paths"]) == {"docs/site/**", ".github/workflows/pages.yml"}
    assert "workflow_dispatch" in triggers

    assert doc.get("permissions") == {"contents": "read", "pages": "write", "id-token": "write"}
    assert doc.get("concurrency") == {"group": "pages", "cancel-in-progress": False}

    jobs = doc["jobs"]
    assert set(jobs) == {"build", "deploy"}
    build = jobs["build"]
    deploy = jobs["deploy"]
    assert build["runs-on"] == "ubuntu-24.04"
    assert deploy["runs-on"] == "ubuntu-24.04"
    assert deploy["needs"] == "build"
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }

    build_steps = build["steps"]
    deploy_steps = deploy["steps"]
    build_uses = [s.get("uses") for s in build_steps if s.get("uses")]
    deploy_uses = [s.get("uses") for s in deploy_steps if s.get("uses")]
    assert build_uses == [
        "actions/checkout@v4",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v3",
    ]
    assert deploy_uses == ["actions/deploy-pages@v4"]
    upload_step = next(s for s in build_steps if s.get("uses") == "actions/upload-pages-artifact@v3")
    assert upload_step["with"]["path"] == "docs/site"
    deploy_step = next(s for s in deploy_steps if s.get("uses") == "actions/deploy-pages@v4")
    assert deploy_step.get("id") == "deployment"

    run_blocks = [s["run"] for s in [*build_steps, *deploy_steps] if s.get("run")]
    assert run_blocks, "pages.yml 缺少静态资产校验步骤"
    combined_run = "\n".join(run_blocks)
    assert "test -f docs/site/.nojekyll" in combined_run
    for path in EXPECTED_SITE_NONEMPTY:
        rel = path.relative_to(ROOT).as_posix()
        assert f"test -s {rel}" in combined_run, f"pages.yml 缺少 test -s {rel}"
    assert not re.search(r"\$\{\{", combined_run), "pages.yml run 块不得内联不可信 ${{ }} 表达式"


# ---------------------------------------------------------------------------
# 5. 中英双语 README 互链、热键、下载直链、依赖对齐与双许可契约
# ---------------------------------------------------------------------------
def test_bilingual_readme_links_hotkeys_downloads_and_license() -> None:
    workflow_assets = _release_asset_names_from_workflow()
    control_text = LINUX_CONTROL.read_text(encoding="utf-8")
    for dep in ("libxcb-cursor0", "libxkbcommon-x11-0", "gir1.2-gtk-3.0", "python3-dbus"):
        assert dep in control_text

    # 全仓非忽略/已追踪非归档文件严禁残留旧仓库账号（排除本测试文件自身）
    self_resolved = Path(__file__).resolve()
    legacy_hits: list[str] = []
    for path in _repo_non_ignored_files():
        if path.resolve() == self_resolved:
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".whl", ".deb", ".zip"}:
            continue
        raw = path.read_bytes()
        if LEGACY_OWNER.encode("ascii") in raw.lower():
            legacy_hits.append(path.relative_to(ROOT).as_posix())
    assert not legacy_hits, f"以下文件仍残留旧仓库地址：{legacy_hits}"

    specs = (
        (
            README_ZH,
            (
                "[English](README.en.md)",
                PAGES_URL,
                "截屏标注",
                "双向自动翻译",
                "未签名且未经 Windows 真机验收",
                "未公证且未经 macOS 真机验收",
            ),
        ),
        (
            README_EN,
            (
                "[简体中文](README.md)",
                f"{PAGES_URL}?lang=en",
                "Screenshot Annotation",
                "Bidirectional Auto-Translate",
                "unsigned and not yet verified on real Windows hardware",
                "unnotarized and not yet verified on real macOS hardware",
            ),
        ),
    )

    for readme_path, lang_tokens in specs:
        text = readme_path.read_text(encoding="utf-8")
        name = readme_path.name
        assert LEGACY_OWNER not in text.lower()
        assert "cd ollama-voice-input" not in text, f"{name} 仍含错误目录名 cd ollama-voice-input"
        assert "cd voice-input" in text, f"{name} 缺少修正后的 cd voice-input"

        # 所有相对链接必须指向真实存在的文件
        rel_targets: set[str] = set()
        for m in re.finditer(r"\]\(([^)#]+)\)", text):
            u = m.group(1).strip()
            if not u.startswith(("http://", "https://", "mailto:", "#")):
                rel_targets.add(u)
        for m in re.finditer(r'(?:src|srcset|href)="([^"#]+)"', text):
            u = m.group(1).strip()
            if not u.startswith(("http://", "https://", "mailto:", "#")):
                rel_targets.add(u)
        assert rel_targets, f"{name} 未检测到任何相对链接"
        for rel_u in sorted(rel_targets):
            assert (ROOT / rel_u).exists(), f"{name} 存在失效相对链接：{rel_u!r}"

        # <picture> 暗/亮实机图与 </summary> 后留空行
        assert "<picture>" in text and "</picture>" in text, f"{name} 缺少 <picture> 自适应块"
        assert 'srcset="docs/site/assets/workbench-dark.png"' in text
        assert 'srcset="docs/site/assets/workbench-light.png"' in text
        assert 'src="docs/site/assets/annotate-icons.png"' in text
        summaries = list(re.finditer(r"</summary>", text))
        assert summaries, f"{name} 应包含 <details><summary> 折叠指引"
        for m in summaries:
            assert text[m.end() :].startswith("\n\n"), f"{name} 存在 </summary> 后未留空行"

        common_tokens = (
            "resources/ollama-voice-input.svg",
            f"Version-{APP_VERSION}",
            f"{REPO_URL}/releases",
            *(f"{REPO_URL}/releases/download/v{APP_VERSION}/{asset}" for asset in workflow_assets),
            "Ctrl+Alt+Z",
            "Ctrl+Alt+X",
            "Ctrl+Alt+A",
            "Ctrl+Alt+O",
            "Ctrl+Alt+R",
            "libxcb-cursor0",
            "libxkbcommon-x11-0",
            "gir1.2-gtk-3.0",
            "python3-dbus",
            "70-ollama-voice-input.rules",
            "AGPL-3.0-only",
            "LICENSE",
            "COMMERCIAL.md",
            "THIRD-PARTY-NOTICES.md",
        )
        for tok in (*common_tokens, *lang_tokens):
            assert tok in text, f"{name} 缺少关键内容 {tok!r}"

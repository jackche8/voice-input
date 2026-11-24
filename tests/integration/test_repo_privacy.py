"""全仓隐私与归属正向白名单护栏（单源仓库文件枚举与文本/PNG 元数据校验）。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from _paths import PROGRAM_ROOT

ROOT = PROGRAM_ROOT
REPO_OWNER = "jackche8"
REPO_NAME = "voice-input"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
PAGES_URL = f"https://{REPO_OWNER}.github.io/{REPO_NAME}/"

# 本项目历史/现用仓库名：一旦出现在任意 GitHub URL 中，owner 必须严格归属 REPO_OWNER 且 slug 必须为 REPO_NAME
OWN_REPO_SLUGS = frozenset({"voice-input", "ollama-voice-input"})

# 全仓允许引用的第三方上游仓库（须显式具名，新增上游依赖需同步登记）
ALLOWED_THIRD_PARTY_GITHUB_REPOS = frozenset(
    {
        "astral-sh/uv",
        "bobotig/python-mss",
        "danbloomberg/leptonica",
        "gvalkov/python-evdev",
        "handy-computer/transcribe.cpp",
        "kludex/python-multipart",
        "lincolnloop/python-qrcode",
        "madmaze/pytesseract",
        "moses-palmer/pynput",
        "ollama/ollama",
        "pyav-org/pyav",
        "python-xlib/python-xlib",
        "qwenlm/qwen3-asr",
        "tesseract-ocr/tessdata_fast",
        "tesseract-ocr/tesseract",
    }
)
ALLOWED_THIRD_PARTY_PAGES_OWNERS = frozenset({"k2-fsa"})
ALLOWED_SINGLE_SEGMENT_GITHUB_BY_FILE = frozenset(
    {
        ("tests/integration/test_production_factory.py", "ollama"),
        ("tests/platform/test_main_window.py", "x"),
    }
)

# 允许的合成占位家目录用户名（仅限测试夹具与设计稿示例，严禁出现真实宿主机用户名）
ALLOWED_SYNTHETIC_HOME_USERS = frozenset({"u", "user"})

# 允许的邮箱白名单：维护者 GitHub noreply + RFC 2606 保留示例域 + 精确具名测试夹具
ALLOWED_NOREPLY_EMAILS = frozenset(
    {
        f"{REPO_OWNER}@users.noreply.github.com",
        f"336174012+{REPO_OWNER}@users.noreply.github.com",
    }
)
ALLOWED_RFC2606_DOMAINS = frozenset({"example.com", "example.org", "example.net", "example.test"})
ALLOWED_EXACT_TEST_EMAILS = frozenset({("tests/logic/test_system_proxy.py", "a", "b.com")})

BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".webp", ".whl", ".deb", ".zip", ".wav", ".mp3", ".mp4"}
)

# 规则正则采用拼串构造，确保本文件纳入自扫时自身 0 命中且不引入多余路径字面量
_GITHUB_COM_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?",
    re.IGNORECASE,
)
_GITHUB_IO_RE = re.compile(
    r"https?://([A-Za-z0-9_.-]+)\.github\.io(?:/([A-Za-z0-9_.-]*))?",
    re.IGNORECASE,
)
_HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9._-])/" + "home/([A-Za-z0-9._-]+)")
_FILE_HOME_RE = re.compile("file:///" + "home/", re.IGNORECASE)
# RFC 3986 authority 边界：仅剥离 scheme://userinfo@，绝不跨越 / ? # 防止 query 邮箱旁路，也不匹配 mailto:
_URL_USERINFO_RE = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>/?#@]+@""")
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

# PNG 原始字节禁令：仅用高熵/长特征串，严禁扫描 3 字节短串以免碰撞压缩像素流
_PNG_FORBIDDEN_RAW_TOKENS = (
    b"/" + b"home/",
    b"file:///" + b"home/",
    b"192.168.",
    b"token=",
    b"@" + b"gmail.com",
    b"@" + b"qq.com",
    b"@" + b"163.com",
)


def repo_tracked_and_unignored_files(root: Path = ROOT) -> list[Path]:
    """单源仓库文件枚举：全部已追踪文件（含已入库 .pp/wiki/）并集未忽略工作区文件（仅排除未追踪 .pp/）。"""
    cached = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    others = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )

    rel_paths: set[str] = set()
    for raw in cached.stdout.split(b"\x00"):
        if raw:
            rel_paths.add(raw.decode("utf-8"))
    for raw in others.stdout.split(b"\x00"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if rel == ".pp" or rel.startswith(".pp/"):
            continue
        rel_paths.add(rel)

    out: list[Path] = []
    for rel in sorted(rel_paths):
        candidate = root / rel
        if candidate.is_file():
            out.append(candidate)
    return out


def validate_text_privacy(
    text: str,
    rel_posix: str,
    *,
    strict_own_site_only: bool = False,
) -> tuple[list[str], dict[str, int]]:
    """按正向白名单校验文本中的 GitHub 归属、本机家目录路径与邮箱地址。"""
    violations: list[str] = []
    counts = {"own_repo_urls": 0, "noreply_emails": 0, "synthetic_homes": 0}

    for lineno, line in enumerate(text.splitlines(), 1):
        loc = f"{rel_posix}:{lineno}"

        # 1. github.com 链接正向白名单
        for m in _GITHUB_COM_RE.finditer(line):
            owner = m.group(1).lower()
            raw_repo = m.group(2)
            if raw_repo is None:
                if owner == REPO_OWNER:
                    continue
                if not strict_own_site_only and (rel_posix, owner) in ALLOWED_SINGLE_SEGMENT_GITHUB_BY_FILE:
                    continue
                violations.append(f"{loc}: 未授权 github.com 账号链接 {m.group(0)!r}")
                continue

            repo = raw_repo.lower()
            if repo.endswith(".git"):
                repo = repo[:-4]
            if repo in OWN_REPO_SLUGS:
                if owner == REPO_OWNER and repo == REPO_NAME:
                    counts["own_repo_urls"] += 1
                else:
                    violations.append(f"{loc}: 项目仓库归属或名称不合规 {m.group(0)!r}")
            else:
                pair = f"{owner}/{repo}"
                if strict_own_site_only or pair not in ALLOWED_THIRD_PARTY_GITHUB_REPOS:
                    violations.append(f"{loc}: 未登记第三方 GitHub 仓库 {m.group(0)!r}")

        # 2. github.io 链接正向白名单
        for m in _GITHUB_IO_RE.finditer(line):
            owner = m.group(1).lower()
            seg = (m.group(2) or "").lower()
            if owner == REPO_OWNER:
                if seg in ("", REPO_NAME):
                    counts["own_repo_urls"] += 1
                else:
                    violations.append(f"{loc}: 本站 github.io 子路径不合规 {m.group(0)!r}")
            elif strict_own_site_only or owner not in ALLOWED_THIRD_PARTY_PAGES_OWNERS:
                violations.append(f"{loc}: 未授权 github.io 站点 {m.group(0)!r}")

        # 3. 本机家目录路径拦截（严格模式全禁；常规模式仅放行合成占位用户名 u/user）
        if _FILE_HOME_RE.search(line):
            violations.append(f"{loc}: 禁止包含本地文件 URI 家目录路径")
        for m in _HOME_PATH_RE.finditer(line):
            user = m.group(1)
            if not strict_own_site_only and user in ALLOWED_SYNTHETIC_HOME_USERS:
                counts["synthetic_homes"] += 1
            else:
                violations.append(f"{loc}: 未授权本机家目录路径 {m.group(0)!r}")

        # 4. 邮箱地址正向白名单（先按 RFC 3986 剥离 URL authority userinfo）
        stripped = _URL_USERINFO_RE.sub("", line)
        for m in _EMAIL_RE.finditer(stripped):
            local_part = m.group(1).lower()
            domain = m.group(2).lower()
            email = f"{local_part}@{domain}"
            if email in ALLOWED_NOREPLY_EMAILS:
                counts["noreply_emails"] += 1
            elif not strict_own_site_only and (
                domain in ALLOWED_RFC2606_DOMAINS
                or (rel_posix, local_part, domain) in ALLOWED_EXACT_TEST_EMAILS
            ):
                continue
            else:
                violations.append(f"{loc}: 未授权邮箱地址 {email!r}")

    return violations, counts


def validate_png_privacy(path: Path, raw: bytes, info: dict[str, Any]) -> None:
    """校验 PNG 原始字节（仅高特征串）与文本元数据块（复用正向白名单）。"""
    lower_raw = raw.lower()
    for forbidden in _PNG_FORBIDDEN_RAW_TOKENS:
        assert forbidden not in lower_raw, f"{path.name} 原始字节含敏感特征 {forbidden!r}"
    for key, val in info.items():
        violations, _ = validate_text_privacy(str(val), f"{path.name}[{key}]", strict_own_site_only=True)
        assert not violations, f"{path.name} 元数据 {key} 含隐私或未授权链接：{violations}"


def test_repo_privacy_positive_whitelist() -> None:
    """全仓（含已追踪 .pp/wiki/ 与本护栏文件自身）满足正向白名单隐私与归属约束。"""
    files = repo_tracked_and_unignored_files(ROOT)
    scanned_files = 0
    totals = {"own_repo_urls": 0, "noreply_emails": 0, "synthetic_homes": 0}
    all_violations: list[str] = []

    for path in files:
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            continue
        scanned_files += 1
        rel_posix = path.relative_to(ROOT).as_posix()
        text = raw.decode("utf-8", errors="replace")
        violations, counts = validate_text_privacy(text, rel_posix)
        all_violations.extend(violations)
        for key in totals:
            totals[key] += counts[key]

    # 非空自检：防止扫描器退化为空遍历导致真空假绿
    assert scanned_files >= 100, f"扫描文件数过少（{scanned_files} < 100），请检查文件枚举"
    assert totals["own_repo_urls"] >= 10, f"未检测到足够本项目 GitHub 链接：{totals}"
    assert totals["noreply_emails"] >= 1, f"未检测到维护者 noreply 邮箱：{totals}"
    assert totals["synthetic_homes"] >= 1, f"未检测到合成占位家目录夹具：{totals}"

    assert not all_violations, "全仓隐私与归属正向白名单检查失败：\n" + "\n".join(all_violations[:30])

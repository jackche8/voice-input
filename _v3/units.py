"""systemd 用户单元的唯一渲染器：模板在 systemd/*.service，install.sh 调用本模块写入用户目录。

本模块不调用 systemctl；测试把输出目录指到临时目录做快照比对。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from _v3.production import CODE_ROOT, ProductionPaths

UNIT_NAMES = (
    "ollama-voice-server.service",
    "ollama-voice-daemon.service",
    "ollama-voice-asr.service",
    "ollama-voice-ollama.service",
)
TEMPLATE_DIR = CODE_ROOT / "systemd"

_EXEC_DIRECTIVES = frozenset(
    {"ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition", "ExecReload", "ExecStop"}
)
_LITERAL_PATH_DIRECTIVES = frozenset({"WorkingDirectory", "ConditionPathExists"})
_EXEC_TOKEN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+")
_SHELL_PLACEHOLDER_WORD_RE = re.compile(r"[^\s'\"]*@[A-Z_]+@[^\s'\"]*")


def _needs_quoting(value: str) -> bool:
    return any(ch.isspace() for ch in value) or '"' in value


def _escape_systemd_dquote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_bash_dquote(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def _replace_plain(text: str, values: Mapping[str, str]) -> str:
    out = text
    for key, value in values.items():
        out = out.replace(key, value)
    return out


def _render_exec_rhs(rhs: str, values: Mapping[str, str]) -> str:
    rendered_tokens: list[str] = []
    for token in _EXEC_TOKEN_RE.findall(rhs):
        if "@" not in token:
            rendered_tokens.append(token)
            continue
        if token.startswith("'") and token.endswith("'") and len(token) >= 2:
            inner = token[1:-1]

            def _sub_shell_word(match: re.Match[str]) -> str:
                subbed = _replace_plain(match.group(0), values)
                if _needs_quoting(subbed) or "'" in subbed or "$" in subbed or "`" in subbed:
                    return f'"{_escape_bash_dquote(subbed)}"'
                return subbed

            rendered_inner = _SHELL_PLACEHOLDER_WORD_RE.sub(_sub_shell_word, inner)
            rendered_tokens.append(f"'{rendered_inner}'")
            continue
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            subbed = _replace_plain(token[1:-1], values)
            rendered_tokens.append(f'"{_escape_systemd_dquote(subbed)}"')
            continue
        subbed = _replace_plain(token, values)
        if _needs_quoting(subbed):
            rendered_tokens.append(f'"{_escape_systemd_dquote(subbed)}"')
        else:
            rendered_tokens.append(subbed)
    return " ".join(rendered_tokens)


def _render_line(line: str, values: Mapping[str, str]) -> str:
    if line.startswith("#") or "@" not in line:
        return line
    key, sep, rhs = line.partition("=")
    if not sep:
        return _replace_plain(line, values)
    directive = key.strip()
    if directive in _LITERAL_PATH_DIRECTIVES:
        return f"{key}={_replace_plain(rhs, values)}"
    if directive == "Environment":
        replaced = _replace_plain(rhs, values)
        if not (replaced.startswith('"') and replaced.endswith('"')) and _needs_quoting(replaced):
            return f'{key}="{_escape_systemd_dquote(replaced)}"'
        return f"{key}={replaced}"
    if directive in _EXEC_DIRECTIVES:
        return f"{key}={_render_exec_rhs(rhs, values)}"
    return _replace_plain(line, values)


def unit_values(*, project_dir: Path, venv: Path, cuda_libs: str = "") -> dict[str, str]:
    paths = ProductionPaths.for_root(project_dir, live=True)
    if cuda_libs:
        cuda_env = (
            f'Environment="LD_LIBRARY_PATH={_escape_systemd_dquote(cuda_libs)}"'
            if _needs_quoting(cuda_libs)
            else f"Environment=LD_LIBRARY_PATH={cuda_libs}"
        )
    else:
        cuda_env = ""
    return {
        "@DIR@": str(paths.data_root),
        "@VENV@": str(venv),
        "@CUDA_ENV@": cuda_env,
        "@ASR_MODEL_DIR@": str(paths.asr_model_dir),
        "@ASR_PYTHON@": str(paths.asr_python),
        "@HF_HOME@": str(paths.hf_home),
        "@OLLAMA_BIN@": str(paths.ollama_binary),
        "@OLLAMA_MODELS@": str(paths.ollama_models),
    }


def render_unit(template: str, values: Mapping[str, str]) -> str:
    lines = [
        _render_line(line, values)
        for line in template.splitlines()
        if not line.startswith("# 模板：")
    ]
    # 空的 @CUDA_ENV@ 只留下空行：去掉以免 unit 中出现无意义空行堆叠
    text = "\n".join(lines).replace("\n\n\n", "\n\n")
    if _placeholders(text):
        raise ValueError(f"模板存在未替换的占位符: {_placeholders(text)}")
    return text.rstrip("\n") + "\n"


def _placeholders(text: str) -> str:
    return " ".join(sorted(set(re.findall(r"@[A-Z_]+@", text))))


def render_all(*, project_dir: Path, venv: Path, cuda_libs: str = "", template_dir: Path = TEMPLATE_DIR) -> dict[str, str]:
    values = unit_values(project_dir=project_dir, venv=venv, cuda_libs=cuda_libs)
    return {name: render_unit((template_dir / name).read_text(encoding="utf-8"), values) for name in UNIT_NAMES}


def write_units(out_dir: Path, rendered: Mapping[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in rendered.items():
        target = out_dir / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染 systemd 用户单元（不调用 systemctl）")
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--cuda-libs", default="")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    for path in write_units(args.out_dir, render_all(project_dir=args.project_dir, venv=args.venv, cuda_libs=args.cuda_libs)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

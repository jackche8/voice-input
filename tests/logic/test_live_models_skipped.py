"""真实模型链路：4b 未下载时必须显式 skip，不得报绿。测试不得在 live 识别服务上建会话（单会话独占，会挡住用户语音）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from _ocr import DEFAULT_OCR_LANG, get_cached_languages
from _ocr_layout import extract_ocr_layout
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient
from _ocr_text import is_cjk_or_fullwidth
from _translate import resolve_translation_direction, translate_blocks
from tests.logic.ocr_samples import all_samples, fonts_missing

ROOT = Path(__file__).resolve().parents[2]


def _4b_present() -> tuple[bool, str]:
    url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as http_client:
            resp = http_client.get(f"{url}/api/tags")
    except httpx.HTTPError as exc:
        return False, f"无法连接 Ollama ({url}): {exc}"
    if resp.status_code != 200:
        return False, f"Ollama /api/tags HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, "Ollama /api/tags 不是合法 JSON"
    names = [m.get("name", "") for m in data.get("models") or []]
    if any(name == "qwen3.5:4b" or name.startswith("qwen3.5:4b") for name in names):
        return True, "present"
    return False, "qwen3.5:4b 未落盘（fake-ip 阻塞官方 pull，本层不改系统代理/DNS/Ollama）"


def test_real_qwen35_4b_skipped_until_downloaded():
    present, reason = _4b_present()
    if not present:
        pytest.skip(reason)
    # 即使将来已下载，本测试也不在 L1 宣称产品验收通过；仅做一次最小 think:false 探活。
    with OllamaClient(timeout_s=30) as client:
        text = client.chat(
            model="qwen3.5:4b",
            messages=[{"role": "user", "content": "只回复：ok"}],
            think=False,
        )
    assert text


DESKTOP_READ_TIMEOUT_S = 130  # 桌面客户端等待 /api/v1/ocr-inplace 的读超时（_desktop/api.py:184）


def _comment(line: str) -> tuple[str, str]:
    code, _sep, comment = line.partition("#")
    return code.rstrip(), comment.strip()


@pytest.mark.parametrize("name", ["fullscreen_article", "fullscreen_traditional", "code"])
def test_real_qwen35_4b_translates_ocr_screens_in_budget_batches(name: str) -> None:
    """真实 tesseract + qwen3.5:4b：整屏分批翻译在桌面读超时内完成；代码行原样、只翻注释。"""
    present, reason = _4b_present()
    if not present:
        pytest.skip(reason)
    missing = [lang for lang in DEFAULT_OCR_LANG.split("+") if lang not in get_cached_languages()]
    if missing or fonts_missing():
        pytest.skip(f"缺 tesseract 语言包 {missing} 或样本字体：{fonts_missing()}")
    replies: list[dict] = []
    started: list[float] = []

    def on_request(_request: httpx.Request) -> None:
        started.append(time.perf_counter())

    def on_response(response: httpx.Response) -> None:
        response.read()
        body = response.json()
        replies.append({
            "seconds": round(time.perf_counter() - started[-1], 2),
            "eval_count": body.get("eval_count"),
            "prompt_eval_count": body.get("prompt_eval_count"),
            "done_reason": body.get("done_reason"),
        })

    url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    http = httpx.Client(base_url=url, timeout=DESKTOP_READ_TIMEOUT_S, trust_env=False,
                        event_hooks={"request": [on_request], "response": [on_response]})
    sample = all_samples()[name]
    t0 = time.perf_counter()
    _w, _h, ocr_text, blocks = extract_ocr_layout(sample.png)
    ocr_s = time.perf_counter() - t0
    direction = resolve_translation_direction(ocr_text)
    with OllamaClient(base_url=url, client=http) as client:
        done = translate_blocks(
            blocks,
            source_lang=direction.source_lang,
            target_lang=direction.target_lang,
            lang_pair=direction.lang_pair,
            client=client,
            model="qwen3.5:4b",
        )
    total_s = time.perf_counter() - t0
    http.close()
    print(f"\n[{name}] words={sample.words} OCR={ocr_s:.2f}s total={total_s:.2f}s batches={len(replies)} {replies}")
    assert total_s < DESKTOP_READ_TIMEOUT_S
    assert all(reply["done_reason"] == "stop" for reply in replies)
    assert all(block.translated_text.strip() for block in done)
    if name == "fullscreen_article":
        assert sample.words >= 400 and len(replies) >= 2
    if name == "code":
        for source, translated in zip(done[0].ocr_text.split("\n"), done[0].translated_text.split("\n")):
            (code, comment), (code_t, comment_t) = _comment(source), _comment(translated)
            # 代码原样：缩进由块译文按原行段补回；行内空白不比（实测 4b 会把 OCR 多认出的空格去掉，如「(path) :」→「(path):」）
            assert code_t[: len(code_t) - len(code_t.lstrip())] == code[: len(code) - len(code.lstrip())]
            assert "".join(code_t.split()) == "".join(code.split()), (source, translated)
            if comment:
                assert comment_t != comment and any(is_cjk_or_fullwidth(ch) for ch in comment_t), (source, translated)

from __future__ import annotations

import json

import httpx
import pytest

from _contracts import (
    DEFAULT_OLLAMA_BASE_URL as CONTRACT_OLLAMA_URL,
    ErrorCode,
    LogicError,
    OcrTextBlock,
    OcrTextLine,
    PolishRequest,
    TranslateDirectionMode,
    TranslateRequest,
)
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient
from _polish import polish_text
from _translate import (
    TRANSLATE_TEXT_CHUNK_TOKEN_BUDGET,
    _estimate_tokens,
    _split_text_chunks,
    has_translatable_language,
    resolve_translation_direction,
    translate_blocks,
    translate_text,
)


def _one_line_block(bbox, text: str, **colors) -> OcrTextBlock:
    return OcrTextBlock(bbox=bbox, lines=[OcrTextLine(bbox=bbox, text=text)], ocr_text=text, **colors)


def _chat_ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": content}},
    )


def test_default_base_url_matches_private_17947_and_disables_env_proxy(monkeypatch):
    assert DEFAULT_OLLAMA_BASE_URL == CONTRACT_OLLAMA_URL == "http://127.0.0.1:17947"
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://192.0.2.1:9999")
    client = OllamaClient()
    try:
        assert client.base_url == "http://127.0.0.1:17947"
        assert client._client._trust_env is False
    finally:
        client.close()


def test_chat_sends_think_false_options_and_returns_content():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_ok("修正后")

    client = OllamaClient(transport=httpx.MockTransport(handler), timeout_s=2)
    out = client.chat(
        model="qwen3.5:4b",
        messages=[{"role": "user", "content": "错字"}],
        think=False,
        options={"temperature": 0.0, "num_ctx": 2048},
    )
    assert out == "修正后"
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["think"] is False
    assert body["stream"] is False
    assert body["model"] == "qwen3.5:4b"
    assert body["options"] == {"temperature": 0.0, "num_ctx": 2048}
    client.close()


def test_think_true_is_rejected_before_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be sent")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[], think=True)
    assert ei.value.code.value == "invalid_request"
    client.close()


def test_timeout_bad_json_http_error_and_empty_content():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    client = OllamaClient(transport=httpx.MockTransport(timeout_handler), timeout_s=0.01)
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert ei.value.code.value == "timeout"
    client.close()

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = OllamaClient(transport=httpx.MockTransport(bad_json))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert "JSON" in ei.value.message
    client.close()

    def http_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = OllamaClient(transport=httpx.MockTransport(http_500))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert ei.value.details["status_code"] == 500
    client.close()

    def empty(request: httpx.Request) -> httpx.Response:
        return _chat_ok("   ")

    client = OllamaClient(transport=httpx.MockTransport(empty))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert "内容" in ei.value.message
    client.close()

    def missing_model(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'qwen3.5:4b' not found"})

    client = OllamaClient(transport=httpx.MockTransport(missing_model))
    with pytest.raises(LogicError) as ei404:
        client.chat(model="qwen3.5:4b", messages=[{"role": "user", "content": "a"}])
    assert ei404.value.code == ErrorCode.dependency_failed
    assert ei404.value.details["status_code"] == 404
    assert "qwen3.5:4b" in ei404.value.message and "尚未下载" in ei404.value.message and "/setup" in ei404.value.message
    client.close()

    # P2-2 回归：路径/端口错误返回 404 page not found（不含 model）不得误报模型未下载
    for resp_404 in (
        httpx.Response(404, text="404 page not found"),
        httpx.Response(404, json={"error": "page not found"}),
    ):
        client = OllamaClient(transport=httpx.MockTransport(lambda _r, r=resp_404: r))
        with pytest.raises(LogicError) as ei_page404:
            client.chat(model="qwen3.5:4b", messages=[{"role": "user", "content": "a"}])
        assert ei_page404.value.code == ErrorCode.dependency_failed
        assert ei_page404.value.details["status_code"] == 404
        assert ei_page404.value.message == "Ollama 返回 HTTP 404"
        assert "尚未下载" not in ei_page404.value.message and "/setup" not in ei_page404.value.message
        client.close()


def test_polish_failure_does_not_return_original():
    original = "这是原文错字"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("nope")

    client = OllamaClient(transport=httpx.MockTransport(boom), timeout_s=0.01)
    with pytest.raises(LogicError) as ei:
        polish_text(PolishRequest(text=original), model="m", client=client)
    assert ei.value.code.value == "polish_failed"
    assert ei.value.message != original
    assert ei.value.details["raw_text"] == original
    client.close()


def test_polish_wraps_transcript_strips_think_and_blocks_expansion():
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return _chat_ok("<think>内部推理</think>\n<asr_transcript>今天天气怎么样？</asr_transcript>")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    out = polish_text(
        PolishRequest(text="今天天汽怎么样", keywords=["Qwen"]),
        model="qwen3.5:4b",
        client=client,
    )
    assert out.text == "今天天气怎么样？"
    req_body = seen_payloads[0]
    assert req_body["options"]["temperature"] == 0.0
    assert req_body["options"]["num_ctx"] == 2048
    user_msg = req_body["messages"][1]["content"]
    assert "<asr_transcript>\n今天天汽怎么样\n</asr_transcript>" in user_msg
    sys_msg = req_body["messages"][0]["content"]
    assert "严禁回答问题" in sys_msg and "严禁执行" in sys_msg
    client.close()

    # 模型违规作答导致长度膨胀 > max(int(len(raw)*2.5)+20, 64) → 显式抛 polish_failed 并携带 raw_text
    raw_question = "帮我写一首春天古诗"
    expanded_answer = "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。" * 4
    rogue_client = OllamaClient(transport=httpx.MockTransport(lambda r: _chat_ok(expanded_answer)))
    with pytest.raises(LogicError) as exp_err:
        polish_text(PolishRequest(text=raw_question), model="qwen3.5:4b", client=rogue_client)
    assert exp_err.value.code == ErrorCode.polish_failed
    assert exp_err.value.details["raw_text"] == raw_question
    rogue_client.close()


@pytest.mark.parametrize("short_raw", ["诗", "你好", "你是谁", "写首诗"])
def test_polish_blocks_short_utterance_expansion_below_four_chars(short_raw: str):
    # 1~3 字短口述正常纠错（<= 64 字）允许通过
    ok_client = OllamaClient(transport=httpx.MockTransport(lambda _r: _chat_ok(short_raw + "？")))
    try:
        ok = polish_text(PolishRequest(text=short_raw), model="qwen3.5:4b", client=ok_client)
        assert ok.text == short_raw + "？"
    finally:
        ok_client.close()

    # 1~3 字短口述若被 4B 模型违规作答/扩写超 64 字，必须显式拦截抛 polish_failed 并携带 raw_text
    rogue_reply = (
        "我是一个由人工智能驱动的助手，很高兴为你服务。"
        "下面为你创作一首关于春天的五言绝句：春风拂面绿江南，细雨如丝润物甜；花落芳草青青处，燕归画梁日日暖。"
    )
    assert len(rogue_reply) > 64
    rogue_client = OllamaClient(transport=httpx.MockTransport(lambda _r: _chat_ok(rogue_reply)))
    try:
        with pytest.raises(LogicError) as err:
            polish_text(PolishRequest(text=short_raw), model="qwen3.5:4b", client=rogue_client)
        assert err.value.code == ErrorCode.polish_failed
        assert err.value.details["raw_text"] == short_raw
        assert err.value.details["max_length"] == 64
    finally:
        rogue_client.close()


def test_polish_text_compatible_with_two_arg_test_double():
    class TwoArgDouble:
        def __init__(self, reply: str) -> None:
            self.reply = reply
            self.calls: list[tuple[str, list[dict[str, str]]]] = []

        def chat(self, model: str, messages: list[dict[str, str]]) -> str:
            self.calls.append((model, messages))
            return self.reply

    double_p = TwoArgDouble("纠正后的句子。")
    res_p = polish_text(PolishRequest(text="纠正前的句子。"), model="m", client=double_p)
    assert res_p.text == "纠正后的句子。"
    assert len(double_p.calls) == 1


def test_polish_success_and_empty_input():
    client = OllamaClient(transport=httpx.MockTransport(lambda r: _chat_ok("纠正后的句子。")))
    out = polish_text(
        PolishRequest(text="纠正前的句子。", keywords=["Qwen"]), model="m", client=client
    )
    assert out.text == "纠正后的句子。"
    client.close()
    with pytest.raises(LogicError) as ei:
        polish_text(PolishRequest(text="  "), model="m")
    assert ei.value.code.value == "invalid_request"


def test_translate_failure_does_not_return_original():
    original = "hello world"

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = OllamaClient(transport=httpx.MockTransport(boom))
    with pytest.raises(LogicError) as ei:
        translate_text(TranslateRequest(text=original, lang_pair="zh-en"), model="m", client=client)
    assert ei.value.code.value == "translate_failed"
    assert original not in ei.value.message or ei.value.code.value != "ok"
    assert not hasattr(ei.value, "text") or ei.value.__class__ is LogicError
    client.close()


def test_translate_success_sends_think_false_and_source_text_boundary():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return _chat_reply("<think>skip</think><source_text>你好</source_text>")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    out = translate_text(
        TranslateRequest(text="hello", lang_pair="zh-en"), model="m", client=client
    )
    assert out.text == "你好"
    assert out.lang_pair == "zh-en"
    assert out.source_lang == "en"
    assert out.target_lang == "zh"
    assert out.direction_mode == TranslateDirectionMode.auto
    assert out.chunks == 1
    assert seen[0]["think"] is False
    assert seen[0]["options"] == {"temperature": 0.0, "num_ctx": 2048}
    user_msg = seen[0]["messages"][1]["content"]
    assert "源语言: 英文 (en)" in user_msg and "目标语言: 中文 (zh)" in user_msg
    assert "<source_text>\nhello\n</source_text>" in user_msg
    client.close()


def test_resolve_translation_direction_auto_mixed_tie_manual_and_errors():
    # 纯中文 / 纯英文 / 中文夹英文术语（6 汉字 × 3 = 18 > 16 ASCII 字母）
    zh_dir = resolve_translation_direction("你好世界")
    assert (zh_dir.source_lang, zh_dir.target_lang, zh_dir.mode) == ("zh", "en", TranslateDirectionMode.auto)

    en_dir = resolve_translation_direction("Hello world")
    assert (en_dir.source_lang, en_dir.target_lang, en_dir.mode) == ("en", "zh", TranslateDirectionMode.auto)

    mixed_dir = resolve_translation_direction("请把 FastAPI 和 WebSocket 修一下")
    assert (mixed_dir.source_lang, mixed_dir.target_lang, mixed_dir.mode) == ("zh", "en", TranslateDirectionMode.auto)

    # 平局（1×3 == 3）、0 字母、纯全角中文标点（不计为汉字）均确定性回退 en → zh
    for fallback_text in ("中 abc", "12345 !!!", "！！！，，，。"):
        fb = resolve_translation_direction(fallback_text)
        assert (fb.source_lang, fb.target_lang, fb.mode) == ("en", "zh", TranslateDirectionMode.auto), fallback_text

    # 显式手动方向覆盖
    manual = resolve_translation_direction("你好世界", lang_pair="zh-en", source_lang="en", target_lang="zh")
    assert (manual.lang_pair, manual.source_lang, manual.target_lang, manual.mode) == (
        "zh-en",
        "en",
        "zh",
        TranslateDirectionMode.manual,
    )

    # 非法语言对 / 仅传单边 / 同语种 / 非配对语种均抛 invalid_request
    for kwargs in (
        {"lang_pair": "fr-de"},
        {"lang_pair": "zh-en", "source_lang": "zh"},
        {"lang_pair": "zh-en", "target_lang": "zh"},
        {"lang_pair": "zh-en", "source_lang": "zh", "target_lang": "zh"},
        {"lang_pair": "zh-en", "source_lang": "zh", "target_lang": "ja"},
    ):
        with pytest.raises(LogicError) as ei:
            resolve_translation_direction("hello", **kwargs)
        assert ei.value.code == ErrorCode.invalid_request


def test_translate_text_chunks_long_input_preserving_separators_and_technical_tokens():
    # 多段长文本：包含空行段落、单换行、含 config.json / v3.5 / 127.0.0.1 的长英文句
    para_a = "第一段介绍本地翻译方案，避免长文本在上下文窗口中发生截断。" * 14
    para_b_line1 = "Check config.json and v3.5 at 127.0.0.1. " * 26 + "Final sentence."
    para_b_line2 = "第二段第二行继续验证单换行分隔符保留，并附带 FastAPI 服务说明。" * 10
    long_text = f"{para_a}\n\n{para_b_line1}\n{para_b_line2}"

    chunk_pairs = _split_text_chunks(long_text, budget=TRANSLATE_TEXT_CHUNK_TOKEN_BUDGET)
    assert len(chunk_pairs) > 2
    assert "".join(chunk + sep for chunk, sep in chunk_pairs) == long_text
    assert all(chunk.strip() for chunk, _sep in chunk_pairs)
    assert all(_estimate_tokens(chunk) <= TRANSLATE_TEXT_CHUNK_TOKEN_BUDGET for chunk, _sep in chunk_pairs)
    # 技术标记不得在句末标点切分时被截成两半
    for token in ("config.json", "v3.5", "127.0.0.1"):
        assert sum(chunk.count(token) for chunk, _sep in chunk_pairs) == long_text.count(token)

    # 单行无标点无空白超长中文触发兜底硬切，且拼回无损
    unbroken_zh = "测" * 520
    hard_pairs = _split_text_chunks(unbroken_zh, budget=TRANSLATE_TEXT_CHUNK_TOKEN_BUDGET)
    assert len(hard_pairs) >= 2
    assert "".join(chunk + sep for chunk, sep in hard_pairs) == unbroken_zh
    assert all(_estimate_tokens(chunk) <= TRANSLATE_TEXT_CHUNK_TOKEN_BUDGET for chunk, _sep in hard_pairs)

    # translate_text 对全文仅判一次方向（中文占优 → 全部段均 zh → en）并按原分隔符精确拼回
    seen_users: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        user_content = json.loads(request.content.decode("utf-8"))["messages"][1]["content"]
        seen_users.append(user_content)
        inner = user_content.split(f"<{_SOURCE_TAG}>\n", 1)[1].rsplit(f"\n</{_SOURCE_TAG}>", 1)[0]
        return _chat_reply(f"[T:{len(seen_users)}]{inner}")

    with OllamaClient(transport=httpx.MockTransport(handler)) as client:
        res = translate_text(TranslateRequest(text=long_text, lang_pair="zh-en"), model="m", client=client)

    assert res.chunks == len(chunk_pairs) > 2
    assert res.source_lang == "zh" and res.target_lang == "en" and res.direction_mode == TranslateDirectionMode.auto
    assert len(seen_users) == res.chunks
    # 含英文段也不得翻转为 en→zh，整篇所有分块保持同一方向
    assert all("源语言: 中文 (zh)" in msg and "目标语言: 英文 (en)" in msg for msg in seen_users)
    expected_rejoined = "".join(f"[T:{i}]{chunk}{sep}" for i, (chunk, sep) in enumerate(chunk_pairs, 1))
    assert res.text == expected_rejoined


def test_translate_text_fails_loudly_on_length_truncation_or_empty_chunk():
    long_text = ("第一段长文本用于触发两段切分验证截断报错。" * 18) + "\n\n" + ("第二段长文本内容。" * 18)
    calls = {"n": 0}

    def truncate_second(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_reply("Part 1 translated.", done_reason="stop")
        return _chat_reply("Part 2 cut off", done_reason="length")

    with OllamaClient(transport=httpx.MockTransport(truncate_second)) as client:
        with pytest.raises(LogicError) as ei:
            translate_text(TranslateRequest(text=long_text, lang_pair="zh-en"), model="m", client=client)
    assert ei.value.code == ErrorCode.translate_failed
    assert "截断" in ei.value.message
    assert ei.value.details and ei.value.details["chunk"] == 2

    # 剥离 think/wrapper 后为空也显式失败
    with OllamaClient(transport=httpx.MockTransport(lambda _r: _chat_reply("<think>only</think>"))) as client:
        with pytest.raises(LogicError) as empty_ei:
            translate_text(TranslateRequest(text="hello", lang_pair="zh-en"), model="m", client=client)
    assert empty_ei.value.code == ErrorCode.translate_failed
    assert "译文为空" in empty_ei.value.message


_SOURCE_TAG = "source_text"


def _chat_reply(content: str, done_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"message": {"role": "assistant", "content": content}, "done_reason": done_reason}
    )


def _batch_items(request: httpx.Request) -> list[str]:
    content = json.loads(request.content.decode("utf-8"))["messages"][1]["content"]
    return json.loads(content.split("<source_text>\n", 1)[1].rsplit("\n</source_text>", 1)[0])


CODE_LINES = [
    OcrTextLine(bbox=(10, 10, 88, 16), text="def main():"),
    OcrTextLine(bbox=(42, 30, 88, 16), text="run()  # go"),
]
PARAGRAPH_LINES = [
    OcrTextLine(bbox=(10, 60, 320, 16), text="The installer downloads the model weights and"),
    OcrTextLine(bbox=(10, 80, 200, 16), text="verifies every checksum.", soft_wrap=True),
    OcrTextLine(bbox=(10, 100, 120, 16), text="Then it starts."),
]


def test_translate_blocks_hard_segments_as_items():
    def forbid_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("没有块时不得调用 Ollama")

    with OllamaClient(transport=httpx.MockTransport(forbid_http)) as no_call_client:
        assert translate_blocks([], source_lang="en", target_lang="zh", client=no_call_client) == []

    # 翻译项 = 硬行段：段落的软换行拼成一项，代码逐行一项；块译文按原缩进逐行拼回
    requests: list[tuple[dict, list[str]]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append((json.loads(request.content.decode("utf-8")), _batch_items(request)))
        return _chat_reply(json.dumps(["def main():", "run()  # 开始", "安装程序下载模型权重并核对每个校验值。", "然后启动。"]))

    blocks = [
        OcrTextBlock(bbox=(10, 10, 96, 36), lines=CODE_LINES, ocr_text="def main():\n    run()  # go"),
        OcrTextBlock(bbox=(10, 60, 320, 56), lines=PARAGRAPH_LINES, ocr_text="…"),
    ]
    with OllamaClient(transport=httpx.MockTransport(reply)) as client:
        done = translate_blocks(blocks, source_lang="en", target_lang="zh", client=client)
    assert len(requests) == 1
    body, items = requests[0]
    assert items == [
        "def main():",
        "run()  # go",
        "The installer downloads the model weights and verifies every checksum.",
        "Then it starts.",
    ]
    assert "源语言: 英文 (en)" in body["messages"][1]["content"]
    assert "目标语言: 中文 (zh)" in body["messages"][1]["content"]
    assert "严禁返回空字符串" in body["messages"][0]["content"]
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4}
    assert body["format"] == schema  # 结构化输出：条数恰为本批项数
    assert [b.translated_text for b in done] == [
        "def main():\n    run()  # 开始",
        "安装程序下载模型权重并核对每个校验值。\n然后启动。",
    ]


def test_translate_blocks_skips_non_language_segments_and_keeps_valid_empty_fail_loud():
    assert has_translatable_language("Hello") is True
    assert has_translatable_language("中文 123") is True
    for non_lang in ("---", "=== ===", "2026-11-21", "12.3%", "   ", "• · | ()"):
        assert has_translatable_language(non_lang) is False, non_lang

    # 全图均无自然语言项时：0 次模型调用，原样保留
    def forbid_http(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("纯符号/纯数字块不得调用 Ollama")

    symbol_only = [
        _one_line_block((10, 10, 80, 16), "---"),
        _one_line_block((10, 30, 80, 16), "2026-11-21"),
    ]
    with OllamaClient(transport=httpx.MockTransport(forbid_http)) as no_call:
        same = translate_blocks(symbol_only, source_lang="en", target_lang="zh", client=no_call)
    assert [b.translated_text for b in same] == ["---", "2026-11-21"]

    # 混合块：仅将含自然语言行段送 Ollama，纯符号/数字段原位保留
    sent_items: list[list[str]] = []

    def reply_mixed(request: httpx.Request) -> httpx.Response:
        items = _batch_items(request)
        sent_items.append(items)
        return _chat_reply(json.dumps(["文件", "设置"]))

    mixed_blocks = [
        OcrTextBlock(
            bbox=(10, 10, 100, 70),
            lines=[
                OcrTextLine(bbox=(10, 10, 60, 16), text="File"),
                OcrTextLine(bbox=(10, 30, 60, 16), text="---"),
                OcrTextLine(bbox=(10, 50, 80, 16), text="2026-11-21"),
            ],
            ocr_text="File\n---\n2026-11-21",
        ),
        _one_line_block((10, 90, 90, 16), "Settings"),
    ]
    with OllamaClient(transport=httpx.MockTransport(reply_mixed)) as client:
        done = translate_blocks(mixed_blocks, source_lang="en", target_lang="zh", client=client)
    assert sent_items == [["File", "Settings"]]
    assert [b.translated_text for b in done] == ["文件\n---\n2026-11-21", "设置"]

    # 非法方向直接抛 invalid_request（不调用 Ollama）
    with OllamaClient(transport=httpx.MockTransport(forbid_http)) as no_call:
        with pytest.raises(LogicError) as bad_dir:
            translate_blocks(mixed_blocks, source_lang="zh", target_lang="zh", client=no_call)
    assert bad_dir.value.code == ErrorCode.invalid_request


def test_translate_blocks_split_into_token_budget_batches_in_order():
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()
    lines = [OcrTextLine(bbox=(10, 10 + 20 * i, 300, 16), text=" ".join(words) + f" {i}") for i in range(60)]
    block = OcrTextBlock(bbox=(10, 10, 300, 1200), lines=lines, ocr_text="…")
    seen: list[list[str]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        items = _batch_items(request)
        seen.append(items)
        assert json.loads(request.content)["format"]["minItems"] == len(items)
        return _chat_reply(json.dumps([f"译{item.rsplit(' ', 1)[1]}" for item in items]))

    with OllamaClient(transport=httpx.MockTransport(reply)) as client:
        done = translate_blocks([block], source_lang="en", target_lang="zh", client=client)
    assert len(seen) >= 2  # 60 项约 1000 token，超出单批预算
    assert [item for batch in seen for item in batch] == [line.text for line in lines]  # 顺序不乱、不丢不重
    assert done[0].translated_text.split("\n") == [f"译{i}" for i in range(60)]


@pytest.mark.parametrize(
    "content, done_reason, needle",
    [
        ("not json", "stop", "不是合法 JSON"),
        ('["只有一项"]', "stop", "数量不匹配"),
        ('["文件", "   "]', "stop", "译文为空"),
        ('["文件", 123]', "stop", "不是字符串"),
        ('["文件", "打开\\n文件夹"]', "stop", "含换行"),
        ('["文件", "打开', "length", "本批输出被截断"),
    ],
)
def test_translate_blocks_batch_failures_are_explicit(content: str, done_reason: str, needle: str):
    src_blocks = [_one_line_block((10, 12, 80, 20), "File"), _one_line_block((10, 40, 120, 20), "Open Folder")]
    with OllamaClient(transport=httpx.MockTransport(lambda _r: _chat_reply(content, done_reason))) as client:
        with pytest.raises(LogicError) as err:
            translate_blocks(src_blocks, source_lang="en", target_lang="zh", client=client)
    assert err.value.code == ErrorCode.translate_failed
    assert needle in err.value.message

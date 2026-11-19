from __future__ import annotations

import json

import httpx
import pytest

from _contracts import (
    DEFAULT_OLLAMA_BASE_URL as CONTRACT_OLLAMA_URL,
    ErrorCode,
    LogicError,
    OcrTextBlock,
    PolishRequest,
    TranslateRequest,
)
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient
from _polish import polish_text
from _translate import translate_blocks, translate_text


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


def test_polish_and_translate_compatible_with_two_arg_test_double():
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

    double_t = TwoArgDouble("你好世界")
    res_t = translate_text(
        TranslateRequest(text="hello world", target_lang="zh"), model="m", client=double_t
    )
    assert res_t.text == "你好世界"

    double_b = TwoArgDouble('["设置", "退出"]')
    blocks = [
        OcrTextBlock(bbox=(0, 0, 10, 10), ocr_text="Settings"),
        OcrTextBlock(bbox=(0, 20, 10, 10), ocr_text="Quit"),
    ]
    res_b = translate_blocks(blocks, target_lang="zh", client=double_b, model="m")
    assert [b.translated_text for b in res_b] == ["设置", "退出"]


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
        translate_text(TranslateRequest(text=original, target_lang="zh"), model="m", client=client)
    assert ei.value.code.value == "translate_failed"
    assert original not in ei.value.message or ei.value.code.value != "ok"
    assert not hasattr(ei.value, "text") or ei.value.__class__ is LogicError
    client.close()


def test_translate_success_sends_think_false_and_source_text_boundary():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return _chat_ok("<think>skip</think><source_text>你好</source_text>")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    out = translate_text(
        TranslateRequest(text="hello", target_lang="zh"), model="m", client=client
    )
    assert out.text == "你好"
    assert out.target_lang == "zh"
    assert seen[0]["think"] is False
    assert seen[0]["options"] == {"temperature": 0.0, "num_ctx": 2048}
    assert "<source_text>\nhello\n</source_text>" in seen[0]["messages"][1]["content"]
    client.close()


def test_translate_blocks_raw_passthrough_and_batch_json_validation():
    src_blocks = [
        OcrTextBlock(
            bbox=(10, 12, 80, 20),
            ocr_text="File",
            bg_color="#1e293b",
            fg_color="#f8fafc",
        ),
        OcrTextBlock(
            bbox=(10, 40, 120, 20),
            ocr_text="Open Folder",
            bg_color="#1e293b",
            fg_color="#f8fafc",
        ),
    ]

    # raw 直通：不发起任何 HTTP 调用
    def forbid_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("raw target_lang must not call Ollama")

    no_call_client = OllamaClient(transport=httpx.MockTransport(forbid_http))
    raw_res = translate_blocks(src_blocks, target_lang="raw", client=no_call_client)
    assert [b.translated_text for b in raw_res] == ["File", "Open Folder"]
    assert raw_res[0].bg_color == "#1e293b" and raw_res[0].fg_color == "#f8fafc"
    assert translate_blocks([], target_lang="zh", client=no_call_client) == []
    no_call_client.close()

    # 多块单次 JSON 数组批量请求（兼容 markdown fence 包裹）
    batch_requests: list[dict] = []

    def batch_ok(request: httpx.Request) -> httpx.Response:
        batch_requests.append(json.loads(request.content.decode("utf-8")))
        return _chat_ok('```json\n["文件", "打开文件夹"]\n```')

    ok_client = OllamaClient(transport=httpx.MockTransport(batch_ok))
    done_blocks = translate_blocks(src_blocks, target_lang="zh", client=ok_client)
    assert len(batch_requests) == 1
    assert [b.translated_text for b in done_blocks] == ["文件", "打开文件夹"]
    assert [b.ocr_text for b in done_blocks] == ["File", "Open Folder"]
    ok_client.close()

    # 异常响应（非 JSON / 数量不符 / 元素为空 / 元素非字符串）必须显式抛 translate_failed
    for bad_reply in ('not json', '["只有一项"]', '["文件", "   "]', '["文件", 123]', '{"a": 1}'):
        bad_client = OllamaClient(
            transport=httpx.MockTransport(lambda _r, text=bad_reply: _chat_ok(text))
        )
        with pytest.raises(LogicError) as err:
            translate_blocks(src_blocks, target_lang="zh", client=bad_client)
        assert err.value.code == ErrorCode.translate_failed
        bad_client.close()

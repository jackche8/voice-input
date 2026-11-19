"""ASR sidecar 根治回归单测：双后端流式、分词器统一、U+FFFD 剥离、短音频补零、
调度槽原子移交、SSE 溢出与终态必达、单会话互斥、lifespan 周期清理、容器解码与 starttime 防误杀。

注意：本文件不导入真实 torch / vllm，保持与 test_protocol_mock 的无运行时污染断言兼容。
"""
from __future__ import annotations

import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient

from _contracts import (
    ErrorCode,
    LogicError,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
)
from runtimes.asr.__main__ import build_parser
from runtimes.asr.engine import (
    OfficialEngine,
    resolve_backend,
    resolve_transformers_device,
    unify_asr_tokenizer,
)
from runtimes.asr.process_guard import (
    ChildProcessGuard,
    parse_stat_with_starttime,
    proc_starttime,
)
from runtimes.asr.protocol import (
    DEFAULT_BACKEND,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_MAX_MODEL_LEN,
    DEFAULT_VLLM_MAX_NUM_SEQS,
    normalize_language,
)
from runtimes.asr.scheduler import EngineScheduler
from runtimes.asr.server import create_sidecar_app
from runtimes.asr.sessions import SidecarSubscriber, SubscriberOverflowError
from runtimes.asr.stream_step import (
    MIN_FINISH_SAMPLES,
    SAMPLE_RATE,
    BoundedOfficialStream,
    decode_whole,
)
from tests.sidecar.fake_engine import FakeEngine
from tests.sidecar.test_stream_step import LIST_PCM, CharTokenizer, _chunks, _speech, _state


# ---------------------------------------------------------------------------
# 1. 默认显存预算、CLI --backend、语言别名与设备选择
# ---------------------------------------------------------------------------


def test_default_vram_constants_and_cli_backend_arg():
    assert DEFAULT_GPU_MEMORY_UTILIZATION == 0.45
    assert DEFAULT_MAX_MODEL_LEN == 4096
    assert DEFAULT_VLLM_MAX_NUM_SEQS == 2
    assert DEFAULT_BACKEND == "auto"

    parser = build_parser()
    args_default = parser.parse_args(["serve"])
    assert args_default.backend == "auto"
    assert args_default.gpu_memory_utilization == 0.45
    assert args_default.max_model_len == 4096

    for choice in ("auto", "vllm", "transformers"):
        parsed = parser.parse_args(["serve", "--backend", choice])
        assert parsed.backend == choice


def test_vllm_load_constrains_encoder_profiling_budget(tmp_path, monkeypatch):
    """回归：vLLM 必须显式传入 max_num_seqs / max_num_batched_tokens / limit_mm_per_prompt，
    防止 0.45 显存配额下默认 profile 21 段音频导致 KV cache 负值 OOM。"""
    import sys
    import types

    captured: dict[str, object] = {}

    class FakeQwen3ASRModel:
        @classmethod
        def LLM(cls, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(backend="vllm", model=SimpleNamespace(get_tokenizer=lambda: None))

    fake_mod = types.ModuleType("qwen_asr")
    fake_mod.Qwen3ASRModel = FakeQwen3ASRModel
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_mod)

    eng = OfficialEngine(model_path=tmp_path, backend="vllm")
    eng.load(warmup=False)
    assert eng.loaded is True
    assert captured["gpu_memory_utilization"] == 0.45
    assert captured["max_model_len"] == 4096
    assert captured["max_num_seqs"] == DEFAULT_VLLM_MAX_NUM_SEQS
    assert captured["max_num_batched_tokens"] == 4096
    assert captured["limit_mm_per_prompt"] == {"audio": 1}
    assert captured["disable_log_stats"] is True

    with pytest.raises(LogicError) as bad_seqs:
        OfficialEngine(model_path=tmp_path, max_num_seqs=0)
    assert bad_seqs.value.code == ErrorCode.invalid_request


def test_resolve_backend_and_transformers_device_selection():
    assert resolve_backend("vllm", vllm_available_fn=lambda: False, cuda_available_fn=lambda: False) == "vllm"
    assert resolve_backend("transformers", vllm_available_fn=lambda: True, cuda_available_fn=lambda: True) == "transformers"
    assert resolve_backend("auto", vllm_available_fn=lambda: True, cuda_available_fn=lambda: True) == "vllm"
    assert resolve_backend("auto", vllm_available_fn=lambda: False, cuda_available_fn=lambda: True) == "transformers"
    assert resolve_backend("auto", vllm_available_fn=lambda: True, cuda_available_fn=lambda: False) == "transformers"

    with pytest.raises(LogicError) as bad_err:
        OfficialEngine(backend="onnx")
    assert bad_err.value.code == ErrorCode.invalid_request

    fake_torch_cuda = SimpleNamespace(
        float16="fp16",
        bfloat16="bf16",
        float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: True),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    assert resolve_transformers_device(torch_module=fake_torch_cuda) == ("cuda", "bf16")

    fake_torch_mps = SimpleNamespace(
        float16="fp16",
        bfloat16="bf16",
        float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: False),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
    )
    assert resolve_transformers_device(torch_module=fake_torch_mps) == ("mps", "fp16")

    fake_torch_cpu = SimpleNamespace(
        float16="fp16",
        bfloat16="bf16",
        float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: False),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    assert resolve_transformers_device(torch_module=fake_torch_cpu) == ("cpu", "fp32")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("  ", None),
        ("auto", None),
        ("AUTO", None),
        ("zh", "Chinese"),
        ("zh-cn", "Chinese"),
        ("zh-CN", "Chinese"),
        ("en", "English"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("yue", "Cantonese"),
    ],
)
def test_language_alias_normalization(raw: str | None, expected: str | None):
    assert normalize_language(raw) == expected


# ---------------------------------------------------------------------------
# 2. Transformers 流式解码、统一分词器、_finish_prefix U+FFFD 与短音频补零
# ---------------------------------------------------------------------------


class _FakeBatchInputs(dict):
    def __init__(self, prompt_ids: list[int], audio_accum):
        super().__init__(
            input_ids=np.asarray([prompt_ids], dtype=np.int64),
            audio_accum=audio_accum,
        )
        self.moved_to: list[object] = []

    def to(self, target):
        self.moved_to.append(target)
        return self


class _FakeTransformersProcessor:
    def __init__(self) -> None:
        self.tokenizer = CharTokenizer()

    def __call__(self, *, text, audio, return_tensors="pt", padding=True):
        prompt_ids = self.tokenizer.encode(text[0])
        return _FakeBatchInputs(prompt_ids, audio[0])

    def batch_decode(self, sequences, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        out: list[str] = []
        for row in sequences:
            ids = [int(x) for x in row if int(x) not in (151643, 151645)]
            out.append(self.tokenizer.decode(ids))
        return out


class _FakeTransformersModel:
    def __init__(self, full_text_at, *, runaway_at: set[int] | None = None):
        self.full_text_at = full_text_at
        self.runaway_at = runaway_at or set()
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        input_ids = kwargs["input_ids"]
        audio = kwargs["audio_accum"]
        max_new = int(kwargs["max_new_tokens"])
        assert kwargs.get("do_sample") is False
        prompt_ids = [int(x) for x in input_ids[0].tolist()]
        prompt_str = "".join(chr(i) for i in prompt_ids)
        prefix = prompt_str[len("P:") :]
        n = len(audio)
        self.calls.append({"prefix": prefix, "samples": n, "max_new_tokens": max_new})
        if n in self.runaway_at:
            gen = [ord("复")] * max_new  # 无 EOS，触发 finish_reason == "length"
        else:
            truth = self.full_text_at(n)
            assert truth.startswith(prefix), (truth, prefix)
            cont = [ord(c) for c in truth[len(prefix) :]]
            if len(cont) >= max_new:
                # 恰好等于 max_new - 1 + EOS 时带 EOS，超出则不带 EOS
                if len(cont) == max_new - 1:
                    gen = cont + [151645]
                else:
                    gen = cont[:max_new]
            else:
                gen = cont + [151645]
        seq = np.asarray([prompt_ids + gen], dtype=np.int64)
        return SimpleNamespace(sequences=seq)


class _FakeTransformersAsr:
    backend = "transformers"
    device = "cpu"
    dtype = "fp32"

    def __init__(self, model: _FakeTransformersModel) -> None:
        self.model = model
        self.processor = _FakeTransformersProcessor()


def test_transformers_backend_streaming_feed_finish_and_runaway_guard():
    pieces = ["跨平台", "流式", "解码", "验证", "通过。"]
    tf_model = _FakeTransformersModel(_speech(pieces))
    stream = BoundedOfficialStream(
        _FakeTransformersAsr(tf_model),
        _state(),
        sampling_params_factory=None,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    for chunk in _chunks(len(pieces)):
        stream.feed(chunk)
    stream.finish()
    assert stream.state.text == "".join(pieces)
    assert all(r.accepted and r.finish_reason == "stop" for r in stream.reports)

    # Transformers 失控步被拒绝且定稿超上界时显式抛 503 asr_unavailable
    runaway_model = _FakeTransformersModel(
        _speech(pieces), runaway_at={n * SAMPLE_RATE // 2 for n in range(3, 6)}
    )
    bad_stream = BoundedOfficialStream(
        _FakeTransformersAsr(runaway_model),
        _state(),
        sampling_params_factory=None,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    with pytest.raises(LogicError) as err:
        decode_whole(bad_stream, [0.0] * (SAMPLE_RATE // 2 * 5))
    assert err.value.code == ErrorCode.asr_unavailable


def test_unify_vllm_tokenizer_eliminates_mismatch():
    vllm_tok = CharTokenizer()
    mismatched_tok = SimpleNamespace(
        audio_token="<|audio_pad|>",
        audio_bos_token="<|audio_start|>",
        audio_eos_token="<|audio_end|>",
        encode=lambda _s: (_ for _ in ()).throw(AssertionError("should not use mismatched processor tokenizer")),
        decode=lambda _ids: "",
    )
    fake_asr = SimpleNamespace(
        backend="vllm",
        model=SimpleNamespace(
            get_tokenizer=lambda: vllm_tok,
            generate=lambda inputs, sampling_params, use_tqdm=False: [],
        ),
        processor=SimpleNamespace(tokenizer=mismatched_tok),
    )
    unify_asr_tokenizer(fake_asr)
    assert fake_asr.processor.tokenizer is vllm_tok
    assert getattr(vllm_tok, "audio_token") == "<|audio_pad|>"


def test_finish_prefix_strips_trailing_ufffd_from_split_utf8_tokens():
    class ByteLevelTokenizer:
        def encode(self, text: str) -> list[int]:
            return list(text.encode("utf-8"))

        def decode(self, ids: list[int]) -> str:
            return bytes(ids).decode("utf-8", errors="replace")

    # 3 字节汉字在回退 1 个字节 token 时末尾出现残缺 UTF-8 -> U+FFFD
    state = _state(chunk_s=0.4, unfixed_chunk=1, unfixed_token=1)
    state.chunk_id = 3
    state._raw_decoded = "你好世界"  # 4 * 3 = 12 byte tokens; rollback 1 -> 11 bytes -> "你好世\ufffd"
    fake_asr = SimpleNamespace(
        backend="vllm",
        model=SimpleNamespace(),
        processor=SimpleNamespace(tokenizer=ByteLevelTokenizer()),
    )
    stream = BoundedOfficialStream(
        fake_asr,
        state,
        sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    prefix, rollback = stream._finish_prefix()
    assert "\ufffd" not in prefix
    assert prefix == "你好世"
    assert rollback == 3  # 整个被截断汉字的 3 个字节均计入重生成回退预算


def test_finish_pads_short_non_empty_audio_to_8000_samples_but_skips_empty():
    tf_model = _FakeTransformersModel(lambda n: "短词" if n >= MIN_FINISH_SAMPLES else "未补齐")
    # 1) 空流不补零、不调模型
    empty_stream = BoundedOfficialStream(
        _FakeTransformersAsr(tf_model),
        _state(chunk_s=0.4, unfixed_chunk=5, unfixed_token=5),
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    empty_stream.finish()
    assert tf_model.calls == []
    assert len(empty_stream.state.audio_accum) == 0

    # 2) 0.2s (3200 < 8000) 非空尾音频在 finish() 自动补静音至 8000 采样点 (0.5s)
    short_stream = BoundedOfficialStream(
        _FakeTransformersAsr(tf_model),
        _state(chunk_s=0.4, unfixed_chunk=5, unfixed_token=5),
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    short_stream.feed([0.1] * 3200)
    short_stream.finish()
    assert len(short_stream.state.audio_accum) == MIN_FINISH_SAMPLES == 8000
    assert short_stream.state.text == "短词"


# ---------------------------------------------------------------------------
# 3. EngineScheduler 槽位原子移交、SessionManager 溢出/终态必达/互斥锁、Server 鲁棒性
# ---------------------------------------------------------------------------


def test_scheduler_atomic_handoff_prevents_slot_steal_and_second_stream_waiter():
    sched = EngineScheduler()
    sched.acquire_batch()

    stream_ready = threading.Event()
    stream_acquired = threading.Event()
    errors: list[BaseException] = []

    def stream_one() -> None:
        try:
            stream_ready.set()
            sched.acquire_stream("s-priority", timeout_s=3.0)
            stream_acquired.set()
        except BaseException as exc:
            errors.append(exc)

    t_stream = threading.Thread(target=stream_one, daemon=True)
    t_stream.start()
    assert stream_ready.wait(timeout=1.0)
    time.sleep(0.03)

    # 第一条 stream 仍在排队等 batch 让位时，第二条 stream 必须立即 409 conflict
    with pytest.raises(LogicError) as dup_err:
        sched.acquire_stream("s-dup", timeout_s=0.1)
    assert dup_err.value.code == ErrorCode.conflict

    # 同时发起多条并发 batch 争抢，release() 必须把槽位原子移交给排在队首的 stream，零误抛 conflict
    batch_done: list[int] = []

    def contender(idx: int) -> None:
        try:
            sched.acquire_batch(timeout_s=3.0)
            try:
                batch_done.append(idx)
            finally:
                sched.release()
        except BaseException as exc:
            errors.append(exc)

    contenders = [threading.Thread(target=contender, args=(i,), daemon=True) for i in range(4)]
    for t in contenders:
        t.start()
    time.sleep(0.03)

    # 释放初始 batch：stream 优先直接拿到槽位
    sched.release()
    assert stream_acquired.wait(timeout=1.0)
    assert sched.active_stream_id == "s-priority"
    assert batch_done == []
    sched.release()

    for t in contenders:
        t.join(timeout=2.0)
    t_stream.join(timeout=1.0)
    assert not errors
    assert sorted(batch_done) == [0, 1, 2, 3]
    assert sched.active_stream_id is None


def test_scheduler_release_stream_is_atomic_and_second_release_does_not_corrupt_batch():
    sched = EngineScheduler()
    sched.acquire_stream("s1")
    assert sched.active_stream_id == "s1"

    batch_ready = threading.Event()
    batch_acquired = threading.Event()
    errors: list[BaseException] = []

    def batch_waiter() -> None:
        try:
            batch_ready.set()
            sched.acquire_batch(timeout_s=3.0)
            batch_acquired.set()
        except BaseException as exc:
            errors.append(exc)

    t_batch = threading.Thread(target=batch_waiter, daemon=True)
    t_batch.start()
    assert batch_ready.wait(timeout=1.0)
    time.sleep(0.03)

    # 第 1 次 release_stream("s1") 将槽位原子移交给等待中的 batch
    assert sched.release_stream("s1") is True
    assert batch_acquired.wait(timeout=1.0)
    assert sched.active_stream_id is None

    # 第 2 次 release_stream("s1")（模拟并发双关闭）必须返回 False，且绝不破坏运行中的 batch 槽位
    assert sched.release_stream("s1") is False
    assert sched.release_stream("other") is False
    with pytest.raises(LogicError) as blocked_err:
        sched.acquire_stream("s2", timeout_s=0.05)
    assert blocked_err.value.code == ErrorCode.timeout
    assert sched.active_stream_id is None

    # 仅当 batch 自己释放后，新流式会话 s2 才能拿到槽位
    assert sched.release_batch() is True
    assert sched.release_batch() is False
    sched.acquire_stream("s2", timeout_s=0.1)
    assert sched.active_stream_id == "s2"
    assert sched.release_batch() is False
    assert sched.active_stream_id == "s2"
    assert sched.release_stream("s2") is True
    assert sched.active_stream_id is None
    t_batch.join(timeout=1.0)
    assert not errors


def test_subscriber_overflow_flag_and_terminal_eviction_delivery():
    # 场景 A：仅 partial 溢出时，poll 在消费完已有项后显式抛 SubscriberOverflowError
    sub_open = SidecarSubscriber(maxsize=2)
    for i in range(4):
        sub_open.publish(SessionPartialEvent(session_id="s1", tail_text=f"p{i}"))
    assert sub_open.overflow is True
    assert sub_open.poll(0.01).tail_text == "p0"
    assert sub_open.poll(0.01).tail_text == "p1"
    with pytest.raises(SubscriberOverflowError):
        sub_open.poll(0.01)

    # 场景 B：队列被旧 partial 塞满后到达 final + closed，必须淘汰旧 partial 保证终态必达
    sub_fin = SidecarSubscriber(maxsize=2)
    for i in range(5):
        sub_fin.publish(SessionPartialEvent(session_id="s2", tail_text=f"old-{i}"))
    sub_fin.publish(SessionFinalEvent(session_id="s2", text="最终定稿"))
    sub_fin.publish(SessionClosedEvent(session_id="s2", reason=SessionClosedReason.finished))
    ev1 = sub_fin.poll(0.01)
    ev2 = sub_fin.poll(0.01)
    assert isinstance(ev1, SessionFinalEvent) and ev1.text == "最终定稿"
    assert isinstance(ev2, SessionClosedEvent) and ev2.reason == SessionClosedReason.finished


def test_server_finish_unexpected_runtime_error_releases_slot_and_lifespan_sweeps():
    class CrashingFinishEngine(FakeEngine):
        def finish_stream(self, session_id: str):
            self.streams.pop(session_id, None)
            raise RuntimeError("simulated CUDA kernel crash")

    app = create_sidecar_app(CrashingFinishEngine(), idle_s=0.05, sweep_interval_s=0.05)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={}).json()["session_id"]
        resp = client.post(f"/v1/sessions/{sid}/finish")
        assert resp.status_code == 503
        assert app.state.scheduler.active_stream_id is None
        assert sid not in app.state.sessions

        # 验证 lifespan 定时后台 sweep 自动清理空闲超时的会话并释放槽位
        idle_sid = client.post("/v1/sessions", json={}).json()["session_id"]
        assert app.state.scheduler.active_stream_id == idle_sid
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and idle_sid in app.state.sessions:
            time.sleep(0.03)
        assert idle_sid not in app.state.sessions
        assert app.state.scheduler.active_stream_id is None


def test_per_session_infer_mutex_serializes_concurrent_chunks_and_finish():
    class SerialAssertEngine(FakeEngine):
        def __init__(self) -> None:
            super().__init__(text="ok")
            self._in_flight = 0
            self.max_concurrent = 0
            self._mu = threading.Lock()

        def _enter(self) -> None:
            with self._mu:
                self._in_flight += 1
                self.max_concurrent = max(self.max_concurrent, self._in_flight)

        def _exit(self) -> None:
            with self._mu:
                self._in_flight -= 1

        def push_pcm(self, session_id: str, pcm: bytes):
            self._enter()
            try:
                time.sleep(0.03)
                return super().push_pcm(session_id, pcm)
            finally:
                self._exit()

        def finish_stream(self, session_id: str):
            self._enter()
            try:
                time.sleep(0.02)
                return super().finish_stream(session_id)
            finally:
                self._exit()

    eng = SerialAssertEngine()
    app = create_sidecar_app(eng)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={}).json()["session_id"]

        def send_chunk(idx: int):
            return client.post(
                f"/v1/sessions/{sid}/chunks",
                content=b"\x01\x00" * 80,
                headers={"X-Chunk-Index": str(idx)},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(send_chunk, i) for i in range(4)]
            for f in futs:
                assert f.result().status_code == 200
        assert client.post(f"/v1/sessions/{sid}/finish").status_code == 200
        assert eng.max_concurrent == 1


def test_sidecar_transcribe_endpoint_decodes_webm_and_rejects_corrupt_container():
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48000, layout="mono")
        tone = (np.sin(np.linspace(0, 20, 9600)) * 12000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(tone, format="s16", layout="mono")
        frame.sample_rate = 48000
        for pkt in stream.encode(frame):
            out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    webm_bytes = buf.getvalue()

    received_pcm: list[bytes] = []

    class InspectEngine(FakeEngine):
        def transcribe(self, pcm: bytes, *, language=None, context=None):
            received_pcm.append(pcm)
            return super().transcribe(pcm, language=language, context=context)

    app = create_sidecar_app(InspectEngine(text="webm-ok"))
    with TestClient(app) as client:
        ok = client.post(
            "/v1/transcribe",
            files={"audio": ("voice.webm", webm_bytes, "audio/webm")},
            data={"language": "auto"},
        )
        assert ok.status_code == 200
        assert ok.json()["text"] == "webm-ok"
        assert received_pcm and received_pcm[0] != webm_bytes
        assert len(received_pcm[0]) % 2 == 0

        bad = client.post(
            "/v1/transcribe",
            files={"audio": ("broken.webm", b"\x1a\x45\xdf\xa3corrupt", "audio/webm")},
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == ErrorCode.invalid_request.value


# ---------------------------------------------------------------------------
# 4. process_guard 校验 /proc/{pid}/stat starttime 防止误杀复用 PID
# ---------------------------------------------------------------------------


def test_process_guard_starttime_prevents_signalling_recycled_pid(monkeypatch):
    self_stat = parse_stat_with_starttime(os.getpid())
    assert self_stat is not None
    assert len(self_stat) == 4 and self_stat[3] > 0
    assert proc_starttime(os.getpid()) == self_stat[3]

    current_starttimes = {100: 1000, 200: 2000, 201: 2010}
    guard = ChildProcessGuard(
        owner_pid=100,
        starttime_reader=lambda pid: current_starttimes.get(pid),
    )
    # 初次登记：200 与 201 都是 owner=100 的子进程
    initial_table = {
        100: (1, 100, 100, 1000),
        200: (100, 100, 100, 2000),
        201: (100, 100, 100, 2010),
    }
    assert guard.remember(initial_table) == {200, 201}

    # 模拟 PID 200 已退出，被无关进程复用（starttime 变为 9999，ppid=1, pgrp=888）
    current_starttimes[200] = 9999
    recycled_table = {
        100: (1, 100, 100, 1000),
        200: (1, 888, 888, 9999),
        201: (100, 100, 100, 2010),
    }
    targets = guard.reclaimable(recycled_table)
    assert targets == {201}
    assert 200 not in guard.owned

    # 再验证 reap 发送信号前瞬间 PID 201 被复用时，_signal_verified 同样拦截绝不误发信号
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, int(sig))))
    current_starttimes[201] = 77777  # 发送信号前 201 已被内核复用
    assert guard._signal_verified(201, 15) is False
    assert killed == []
    assert 201 not in guard.owned

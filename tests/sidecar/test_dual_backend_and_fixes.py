"""ASR sidecar 根治回归单测：双后端流式、分词器统一、U+FFFD 剥离、短音频补零、
调度槽原子移交、SSE 溢出与终态必达、单会话互斥、lifespan 周期清理、容器解码与 starttime 防误杀。

注意：本文件不导入真实 torch / vllm，保持与 test_protocol_mock 的无运行时污染断言兼容。
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import types
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
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
from _procfs import parse_stat_with_starttime, proc_starttime
from runtimes.asr.__main__ import build_parser
from runtimes.asr.engine import (
    OfficialEngine,
    resolve_backend,
    resolve_transformers_device,
    unify_asr_tokenizer,
)
from runtimes.asr.process_guard import ChildProcessGuard
from runtimes.asr.protocol import (
    DEFAULT_BACKEND,
    DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES,
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
    is_cuda_reclaim_target,
    reclaim_cuda_cache,
)
from tests.sidecar.fake_engine import FakeEngine
from tests.sidecar.test_stream_step import LIST_PCM, CharTokenizer, FakeAsr, FakeLLM, _chunks, _speech, _state


# ---------------------------------------------------------------------------
# 1. 默认显存预算、CLI --backend、语言别名与设备选择
# ---------------------------------------------------------------------------


def test_default_vram_constants_and_cli_backend_arg():
    assert DEFAULT_GPU_MEMORY_UTILIZATION == 0.45
    assert DEFAULT_MAX_MODEL_LEN == 4096
    assert DEFAULT_VLLM_MAX_NUM_SEQS == 2
    assert DEFAULT_BACKEND == "auto"

    parser = build_parser()
    with pytest.raises(SystemExit):  # 模型目录由看护层显式给，无默认位置
        parser.parse_args(["serve"])
    args_default = parser.parse_args(["serve", "--model", "m"])
    assert args_default.backend == "auto"
    assert args_default.gpu_memory_utilization == 0.45
    assert args_default.max_model_len == 4096

    for choice in ("auto", "vllm", "transformers"):
        parsed = parser.parse_args(["serve", "--model", "m", "--backend", choice])
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
        OfficialEngine(model_path=Path("unused-model"), backend="onnx")
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


# ---------------------------------------------------------------------------
# 5. Transformers + CUDA 显存缓存段按需阈值回收与全生命周期清理（零真实 torch 导入）
# ---------------------------------------------------------------------------


class _FakeCudaAllocator:
    def __init__(
        self,
        *,
        allocated: int = 4 * 1024 * 1024 * 1024,
        reserved: int = 4 * 1024 * 1024 * 1024,
        available: bool = True,
    ) -> None:
        self.allocated = allocated
        self.reserved = reserved
        self.available = available
        self.empty_cache_calls = 0
        self.reserved_queries: list[object] = []
        self.allocated_queries: list[object] = []
        self.on_empty_cache = None

    def set_gap(self, gap_bytes: int) -> None:
        self.reserved = self.allocated + gap_bytes

    def build_torch_module(self) -> types.ModuleType:
        mod = types.ModuleType("torch")
        mod.float16 = "fp16"
        mod.bfloat16 = "bf16"
        mod.float32 = "fp32"

        def _device(spec):
            if isinstance(spec, str):
                kind = spec.strip().lower().split(":", 1)[0]
                return SimpleNamespace(type=kind, spec=spec)
            return spec

        def _memory_reserved(device=None):
            self.reserved_queries.append(device)
            return self.reserved

        def _memory_allocated(device=None):
            self.allocated_queries.append(device)
            return self.allocated

        def _empty_cache():
            self.empty_cache_calls += 1
            if callable(self.on_empty_cache):
                self.on_empty_cache()

        mod.device = _device
        mod.cuda = SimpleNamespace(
            is_available=lambda: self.available,
            is_bf16_supported=lambda: True,
            memory_reserved=_memory_reserved,
            memory_allocated=_memory_allocated,
            empty_cache=_empty_cache,
        )
        mod.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
        return mod


def test_reclaim_cuda_cache_threshold_force_and_backend_device_isolation(monkeypatch):
    threshold = DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES
    assert threshold == 512 * 1024 * 1024

    alloc = _FakeCudaAllocator()
    fake_torch = alloc.build_torch_module()

    # 1) 阈值边界：gap == threshold - 1 不回收；gap == threshold 回收；" CUDA:0 " 规范化传 "cuda:0"
    alloc.set_gap(threshold - 1)
    assert reclaim_cuda_cache(backend="transformers", device=" CUDA:0 ", force=False, torch_module=fake_torch) is False
    assert alloc.empty_cache_calls == 0
    assert alloc.reserved_queries == ["cuda:0"]
    assert alloc.allocated_queries == ["cuda:0"]

    alloc.set_gap(threshold)
    assert reclaim_cuda_cache(backend="transformers", device=" CUDA:0 ", force=False, torch_module=fake_torch) is True
    assert alloc.empty_cache_calls == 1
    assert alloc.reserved_queries[-1] == "cuda:0"
    assert alloc.allocated_queries[-1] == "cuda:0"

    # 2) torch.device-like 对象与 force=True（gap == 0 也强制回收）
    cuda_dev_obj = SimpleNamespace(type="cuda", index=0)
    alloc.set_gap(0)
    assert reclaim_cuda_cache(backend="transformers", device=cuda_dev_obj, force=True, torch_module=fake_torch) is True
    assert alloc.empty_cache_calls == 2

    # 3) 畸形 CUDA 字符串与非目标后端/设备全部拦截，零调用 memory_reserved / empty_cache
    q_before = len(alloc.reserved_queries)
    for bad_dev in ("cuda:", "cuda:-1", "cuda:abc", "cuda:０", "cuda_fake", "cpu", "mps", "", None, SimpleNamespace(type="mps")):
        assert is_cuda_reclaim_target("transformers", bad_dev) is False
        assert reclaim_cuda_cache(backend="transformers", device=bad_dev, force=True, torch_module=fake_torch) is False
    for non_tf in ("vllm", "auto", "", None):
        assert is_cuda_reclaim_target(non_tf, "cuda") is False
        assert reclaim_cuda_cache(backend=non_tf, device="cuda", force=True, torch_module=fake_torch) is False
    alloc.available = False
    assert reclaim_cuda_cache(backend="transformers", device="cuda", force=True, torch_module=fake_torch) is False
    assert alloc.empty_cache_calls == 2
    assert len(alloc.reserved_queries) == q_before

    # 4) sys.modules 无 torch 且 torch_module=None：直接返回 False，绝不触发 import torch
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert reclaim_cuda_cache(backend="transformers", device="cuda", force=True) is False
    assert "torch" not in sys.modules


def test_bounded_stream_transformers_cuda_reclaims_on_threshold_and_step_exception(monkeypatch):
    threshold = DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES
    alloc = _FakeCudaAllocator()
    monkeypatch.setitem(sys.modules, "torch", alloc.build_torch_module())

    class _FakeCudaAsr(_FakeTransformersAsr):
        device = " CUDA:0 "

    pieces = ["第一", "第二", "第三", "第四"]
    stream = BoundedOfficialStream(
        _FakeCudaAsr(_FakeTransformersModel(_speech(pieces))),
        _state(),
        sampling_params_factory=None,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    chunks = _chunks(len(pieces))

    # 第 1 步 gap == threshold - 1 不触发；第 2 步 gap == threshold 触发一次并传入规范化 "cuda:0"
    alloc.set_gap(threshold - 1)
    stream.feed(chunks[0])
    assert alloc.empty_cache_calls == 0
    assert alloc.reserved_queries == ["cuda:0"]

    alloc.set_gap(threshold)
    stream.feed(chunks[1])
    assert alloc.empty_cache_calls == 1
    assert alloc.reserved_queries[-1] == "cuda:0"

    # failed=False 正常步：empty_cache() 自身抛错绝不被吞（响亮暴露接线错误）
    def _boom_normal():
        raise RuntimeError("normal-step empty_cache wiring bug")

    alloc.on_empty_cache = _boom_normal
    with pytest.raises(RuntimeError, match="normal-step empty_cache wiring bug"):
        stream.feed(chunks[2])
    assert alloc.empty_cache_calls == 2
    alloc.on_empty_cache = None

    # failed=True 步内异常：先 _clear_exception_frames + gc.collect 释放 generate() 内层局部对象，再强回收并保原异常
    class _HeldActivation:
        pass

    held_refs: list[weakref.ref] = []
    alive_at_flush: list[bool] = []

    class _OomTransformersModel:
        def generate(self, **kwargs):
            local_activation = _HeldActivation()
            held_refs.append(weakref.ref(local_activation))
            raise RuntimeError("simulated CUDA OOM in generate")

    alloc.set_gap(0)
    alloc.on_empty_cache = lambda: alive_at_flush.append(held_refs[-1]() is not None)
    oom_stream = BoundedOfficialStream(
        _FakeCudaAsr(_OomTransformersModel()),
        _state(),
        sampling_params_factory=None,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    with pytest.raises(RuntimeError, match="simulated CUDA OOM in generate") as oom_err:
        oom_stream.feed(chunks[0])
    assert oom_err.value.__traceback__ is not None
    assert alloc.empty_cache_calls == 3
    assert alive_at_flush == [False], "generate() 内层帧局部对象必须在 empty_cache() 前释放"

    # failed=True 且 empty_cache() 二次抛 CUDA 粘滞错：二次错被抑制，原 OOM 异常完整保留
    def _sticky_cuda_err():
        alive_at_flush.append(held_refs[-1]() is not None)
        raise RuntimeError("secondary sticky CUDA error")

    alloc.on_empty_cache = _sticky_cuda_err
    oom_stream2 = BoundedOfficialStream(
        _FakeCudaAsr(_OomTransformersModel()),
        _state(),
        sampling_params_factory=None,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    with pytest.raises(RuntimeError, match="simulated CUDA OOM in generate"):
        oom_stream2.feed(chunks[0])
    assert alloc.empty_cache_calls == 4
    assert alive_at_flush[-1] is False
    alloc.on_empty_cache = None

    # 负向接线：vllm / transformers+cpu / transformers+mps 均零调用 empty_cache
    alloc.set_gap(threshold * 2)
    before_non_cuda = alloc.empty_cache_calls

    vllm_asr = FakeAsr(FakeLLM(_speech(pieces)))
    vllm_asr.backend = "vllm"
    vllm_asr.device = "cuda"
    vllm_stream = BoundedOfficialStream(
        vllm_asr,
        _state(),
        sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )
    for c in chunks[:2]:
        vllm_stream.feed(c)
    vllm_stream.finish()

    for dev_name in ("cpu", "mps"):
        non_cuda_asr = _FakeTransformersAsr(_FakeTransformersModel(_speech(pieces)))
        non_cuda_asr.device = dev_name
        non_cuda_stream = BoundedOfficialStream(
            non_cuda_asr,
            _state(),
            sampling_params_factory=None,
            parse_output=lambda raw, lang: (lang or "", raw),
            pcm=LIST_PCM,
        )
        for c in chunks[:2]:
            non_cuda_stream.feed(c)
        non_cuda_stream.finish()

    assert alloc.empty_cache_calls == before_non_cuda


def test_official_engine_cuda_lifecycle_reclaims_on_success_and_error_without_swallowing(tmp_path, monkeypatch):
    threshold = DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES
    alloc = _FakeCudaAllocator()
    monkeypatch.setitem(sys.modules, "torch", alloc.build_torch_module())

    @dataclass
    class _FakeASRStreamingState:
        unfixed_chunk_num: int
        unfixed_token_num: int
        chunk_size_sec: float
        chunk_size_samples: int
        chunk_id: int
        buffer: object
        audio_accum: object
        prompt_raw: str
        context: str
        force_language: str | None
        language: str
        text: str
        _raw_decoded: str

    def _norm_lang(name: str) -> str:
        return name[:1].upper() + name[1:].lower()

    def _validate_lang(name: str) -> None:
        if name not in {"Chinese", "English"}:
            raise ValueError(f"unsupported {name}")

    class _FakeLoadModel(_FakeTransformersModel):
        def __init__(self, speech_fn, *, runaway_at=None, fail_on_to=False):
            super().__init__(speech_fn, runaway_at=runaway_at)
            self._fail_on_to = fail_on_to

        def to(self, dev):
            if self._fail_on_to:
                raise RuntimeError("OOM during model.to")
            self.device = dev
            return self

        def eval(self):
            return self

    class _FakeTfLoadedAsr:
        backend = "transformers"

        def __init__(self, model):
            self.model = model
            self.processor = _FakeTransformersProcessor()
            self.device = None
            self.dtype = None

        def _build_text_prompt(self, context: str, force_language: str | None) -> str:
            return "P:"

    class _FakeVllmLoadedAsr:
        backend = "vllm"

        def __init__(self, llm):
            self.model = llm
            self.processor = SimpleNamespace(tokenizer=CharTokenizer())

        def init_streaming_state(self, *, context, language, unfixed_chunk_num, unfixed_token_num, chunk_size_sec):
            return _FakeASRStreamingState(
                unfixed_chunk_num=unfixed_chunk_num,
                unfixed_token_num=unfixed_token_num,
                chunk_size_sec=chunk_size_sec,
                chunk_size_samples=max(1, int(round(chunk_size_sec * SAMPLE_RATE))),
                chunk_id=0,
                buffer=np.zeros((0,), dtype=np.float32),
                audio_accum=np.zeros((0,), dtype=np.float32),
                prompt_raw="P:",
                context=context,
                force_language=language,
                language="",
                text="",
                _raw_decoded="",
            )

    load_cfg = {"fail_on_to": False, "runaway_at": set(), "last_model_ref": None}
    step_samples = int(round(0.4 * SAMPLE_RATE))  # 6400
    pieces = [f"词{i}，" for i in range(6)]

    def _speech_step(n: int) -> str:
        k = max(1, -(-n // step_samples))
        return "".join(pieces[:k])

    class _FakeQwen3ASRModel:
        @classmethod
        def from_pretrained(cls, _path, dtype=None):
            m = _FakeLoadModel(_speech_step, runaway_at=set(load_cfg["runaway_at"]), fail_on_to=load_cfg["fail_on_to"])
            load_cfg["last_model_ref"] = weakref.ref(m)
            return _FakeTfLoadedAsr(m)

        @classmethod
        def LLM(cls, **kwargs):
            return _FakeVllmLoadedAsr(FakeLLM(_speech_step))

    qwen_mod = types.ModuleType("qwen_asr")
    qwen_mod.Qwen3ASRModel = _FakeQwen3ASRModel
    qwen_inf = types.ModuleType("qwen_asr.inference")
    qwen_q3 = types.ModuleType("qwen_asr.inference.qwen3_asr")
    qwen_q3.ASRStreamingState = _FakeASRStreamingState
    qwen_utils = types.ModuleType("qwen_asr.inference.utils")
    qwen_utils.normalize_language_name = _norm_lang
    qwen_utils.validate_language = _validate_lang
    vllm_mod = types.ModuleType("vllm")
    vllm_mod.SamplingParams = lambda temperature=0.0, max_tokens=50: int(max_tokens)

    monkeypatch.setitem(sys.modules, "qwen_asr", qwen_mod)
    monkeypatch.setitem(sys.modules, "qwen_asr.inference", qwen_inf)
    monkeypatch.setitem(sys.modules, "qwen_asr.inference.qwen3_asr", qwen_q3)
    monkeypatch.setitem(sys.modules, "qwen_asr.inference.utils", qwen_utils)
    monkeypatch.setitem(sys.modules, "vllm", vllm_mod)

    pcm_0_4s = b"\x00\x00" * step_samples

    # 1) load(warmup=True) 经 transcribe finally 强制回收一次；load(warmup=False) 结束也强制回收一次
    alloc.set_gap(0)
    eng = OfficialEngine(model_path=tmp_path, backend="transformers", device="cuda")
    eng.load(warmup=True)
    assert eng.loaded is True and eng.resolved_backend == "transformers" and eng.resolved_device == "cuda"
    assert alloc.empty_cache_calls == 1

    eng_no_warm = OfficialEngine(model_path=tmp_path, backend="transformers", device="cuda")
    eng_no_warm.load(warmup=False)
    assert alloc.empty_cache_calls == 2
    eng_no_warm.unload()
    assert alloc.empty_cache_calls == 3

    # 2) load() 失败（model.to 抛 OOM）：半加载模型 weakref 在 empty_cache 前已释放、resolved_* 清零、二次清理错不吞原错
    load_cfg["fail_on_to"] = True
    fail_model_alive: list[bool] = []

    def _check_fail_load_flush():
        fail_model_alive.append(load_cfg["last_model_ref"]() is not None)
        raise RuntimeError("secondary cleanup error after load OOM")

    alloc.on_empty_cache = _check_fail_load_flush
    bad_eng = OfficialEngine(model_path=tmp_path, backend="transformers", device="cuda")
    with pytest.raises(RuntimeError, match="OOM during model.to"):
        bad_eng.load(warmup=True)
    assert alloc.empty_cache_calls == 4
    assert fail_model_alive == [False], "半搬移模型必须在 empty_cache() 执行前被彻底释放"
    assert bad_eng._asr is None and bad_eng.loaded is False
    assert bad_eng.resolved_backend is None and bad_eng.resolved_device is None
    alloc.on_empty_cache = None
    load_cfg["fail_on_to"] = False

    # 未加载态 drop_all_streams() 与重复 unload() 为安全幂等 no-op
    bad_eng.drop_all_streams()
    bad_eng.unload()
    assert alloc.empty_cache_calls == 4

    # 3) transcribe() 成功强制回收 + 失控失败在二次清理错下仍保原 LogicError
    alloc.set_gap(0)
    res = eng.transcribe(pcm_0_4s * 2, language="zh")
    assert res.text == pieces[0] + pieces[1]
    assert alloc.empty_cache_calls == 5

    eng._asr.model.runaway_at = {step_samples * 2}
    alloc.on_empty_cache = lambda: (_ for _ in ()).throw(RuntimeError("secondary transcribe cleanup err"))
    with pytest.raises(LogicError) as tr_err:
        eng.transcribe(pcm_0_4s * 2, language="zh")
    assert tr_err.value.code == ErrorCode.asr_unavailable
    assert alloc.empty_cache_calls == 6
    alloc.on_empty_cache = None
    eng._asr.model.runaway_at = set()

    # 4) finish_stream() 整块对齐（tail_n == 0 跳过 _decode）仍强回收；失控失败在二次清理错下仍保原 LogicError
    alloc.set_gap(0)
    eng.start_stream("s-aligned", language="zh")
    eng.push_pcm("s-aligned", pcm_0_4s)
    eng.push_pcm("s-aligned", pcm_0_4s)
    assert alloc.empty_cache_calls == 6  # gap=0，两步 push_pcm 均未触发阈值
    fin = eng.finish_stream("s-aligned")
    assert fin.text == pieces[0] + pieces[1]
    assert len(eng.last_finished_reports) == 2 and not eng.last_finished_reports[-1].final  # 确已跳过 _decode
    assert alloc.empty_cache_calls == 7

    eng.start_stream("s-runaway", language="zh")
    eng._asr.model.runaway_at = {step_samples * 2}
    eng.push_pcm("s-runaway", pcm_0_4s * 2)
    alloc.on_empty_cache = lambda: (_ for _ in ()).throw(RuntimeError("secondary finish cleanup err"))
    with pytest.raises(LogicError) as fin_err:
        eng.finish_stream("s-runaway")
    assert fin_err.value.code == ErrorCode.asr_unavailable
    assert alloc.empty_cache_calls == 8
    alloc.on_empty_cache = None
    eng._asr.model.runaway_at = set()

    # 5) cancel_stream() 命中活动会话强回收；404 不触发
    eng.start_stream("s-cancel", language="zh")
    eng.cancel_stream("s-cancel")
    assert alloc.empty_cache_calls == 9
    with pytest.raises(LogicError) as nf_err:
        eng.cancel_stream("s-missing")
    assert nf_err.value.code == ErrorCode.not_found
    assert alloc.empty_cache_calls == 9

    # 6) drop_all_streams() 强回收
    eng.start_stream("s-drop", language="zh")
    eng.drop_all_streams()
    assert alloc.empty_cache_calls == 10

    # 7) unload() 先置空 _asr 再强回收，且二次 unload() 幂等 no-op
    asr_none_at_flush: list[bool] = []
    alloc.on_empty_cache = lambda: asr_none_at_flush.append(eng._asr is None)
    eng.unload()
    assert alloc.empty_cache_calls == 11
    assert asr_none_at_flush == [True]
    assert eng.loaded is False and eng.resolved_backend is None and eng.resolved_device is None
    eng.unload()
    assert alloc.empty_cache_calls == 11
    alloc.on_empty_cache = None

    # 8) Linux vLLM 全生命周期零调用 empty_cache（即便 gap >= 2 * threshold）
    alloc.set_gap(threshold * 2)
    vllm_before = alloc.empty_cache_calls
    vllm_eng = OfficialEngine(model_path=tmp_path, backend="vllm")
    vllm_eng.load(warmup=False)
    vllm_eng.transcribe(pcm_0_4s * 2, language="zh")
    vllm_eng.start_stream("v1", language="zh")
    vllm_eng.push_pcm("v1", pcm_0_4s)
    vllm_eng.finish_stream("v1")
    vllm_eng.start_stream("v2", language="zh")
    vllm_eng.cancel_stream("v2")
    vllm_eng.drop_all_streams()
    vllm_eng.unload()
    assert alloc.empty_cache_calls == vllm_before

"""真实动作/探测的协议与失败路径：全部用 httpx.MockTransport、假进程、临时目录，不触网、不调 live systemctl。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from _contracts import ErrorCode, LogicError, SetupProgress
from _platform import child_process as cp_mod
from _v3.lan_access import list_physical_lan_ipv4, load_or_create_token
from _v3.release_download import (
    RELEASE_HTTP_TIMEOUT,
    build_release_proxy_mounts,
    create_release_client,
)
from _v3.setup_actions import (
    FAKEIP_RETRY_STAGE,
    asr_download_action,
    format_ollama_pull_error,
    is_ollama_fakeip_redirect_error,
    ollama_asset,
    ollama_install_action,
    ollama_pull_action,
)
from _v3.setup_probe import probe_asr_sidecar, probe_asr_weights, probe_ollama, session_type


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))


def _pull_handler(lines: list[dict], *, tags: list[str], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            body = json.loads(request.content)
            assert body == {"model": "qwen3.5:4b", "stream": True}
            payload = "\n".join(json.dumps(item) for item in lines) + "\n"
            return httpx.Response(status, content=payload.encode())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in tags]})
        return httpx.Response(404)

    return handler


def test_ollama_pull_reports_aggregated_progress_and_verifies_tags():
    seen: list[SetupProgress] = []
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 50},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 0},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 100},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 300},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ]
    action = ollama_pull_action(client_factory=lambda: _client(_pull_handler(lines, tags=["qwen3.5:4b"])), model="qwen3.5:4b")
    assert "qwen3.5:4b" in action(seen.append)
    assert seen[3].completed_bytes == 100 and seen[3].total_bytes == 400 and seen[3].percent == 25.0
    assert seen[-1].stage == "success" and seen[-1].percent == 100.0


@pytest.mark.parametrize(
    "lines,tags,status,needle",
    [
        ([{"status": "pulling manifest"}, {"error": "pull model manifest: 412"}], [], 200, "412"),
        ([{"error": "boom"}], [], 500, "HTTP 500"),
        ([{"status": "pulling manifest"}], [], 200, "未完成"),
        ([{"status": "success"}], ["other:1b"], 200, "/api/tags"),
    ],
)
def test_ollama_pull_failures_are_visible(lines, tags, status, needle):
    action = ollama_pull_action(client_factory=lambda: _client(_pull_handler(lines, tags=tags, status=status)), model="qwen3.5:4b")
    with pytest.raises(LogicError) as ei:
        action(lambda _p: None)
    assert needle in ei.value.message


def test_ollama_pull_connection_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LogicError) as ei:
        ollama_pull_action(client_factory=lambda: _client(handler), model="qwen3.5:4b")(lambda _p: None)
    assert "无法连接" in ei.value.message


def _long_presigned_url() -> str:
    return (
        "https://dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com/"
        "ollama/docker/registry/v2/blobs/sha256/abcdef?"
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=cred/20261123/auto/s3/aws4_request&"
        "X-Amz-Date=20261123T010000Z&X-Amz-Expires=1200&X-Amz-SignedHeaders=host&"
        "X-Amz-Signature=" + ("a" * 600)
    )


def test_ollama_pull_fakeip_retries_insecure_once_and_succeeds(capsys):
    url = _long_presigned_url()
    # 1. 流式 item["error"] 命中 resolves to non-public：清空首轮 layers，上报兼容重试 stage，insecure:true 重试成功并复查 /api/tags
    stream_payloads: list[dict] = []
    stream_raw_err = (
        f'max retries exceeded: Get "{url}": redirect target not allowed: '
        'host "dd20bb.r2.cloudflarestorage.com" resolves to non-public 198.18.75.191'
    )

    def stream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            body = json.loads(request.content)
            stream_payloads.append(body)
            if len(stream_payloads) == 1:
                lines = [
                    {"status": "pulling stale", "digest": "sha256:stale", "total": 1000, "completed": 500},
                    {"error": stream_raw_err},
                ]
            else:
                lines = [
                    {"status": "pulling fresh", "digest": "sha256:fresh", "total": 200, "completed": 100},
                    {"status": "pulling fresh", "digest": "sha256:fresh", "total": 200, "completed": 200},
                    {"status": "success"},
                ]
            return httpx.Response(200, content=("\n".join(json.dumps(x) for x in lines) + "\n").encode())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b", "model": "qwen3.5:4b"}]})
        return httpx.Response(404)

    seen: list[SetupProgress] = []
    res = ollama_pull_action(client_factory=lambda: _client(stream_handler), model="qwen3.5:4b")(seen.append)
    assert res == "qwen3.5:4b 已下载"
    assert stream_payloads == [
        {"model": "qwen3.5:4b", "stream": True},
        {"model": "qwen3.5:4b", "stream": True, "insecure": True},
    ]
    retry_prog = SetupProgress(completed_bytes=None, total_bytes=None, percent=None, stage=FAKEIP_RETRY_STAGE)
    assert retry_prog in seen
    after_retry = seen[seen.index(retry_prog) + 1]
    assert after_retry.completed_bytes == 100 and after_retry.total_bytes == 200 and after_retry.percent == 50.0
    assert "X-Amz-Signature=" in capsys.readouterr().err

    # 2. HTTP>=400 且长预签名 URL 在前、is not a public address 在后（完整读体不得被旧 [:300] 截断）
    http_payloads: list[dict] = []
    http_raw_err = f'Get "{url}": redirect target not allowed: 198.18.0.9 is not a public address'

    def http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            body = json.loads(request.content)
            http_payloads.append(body)
            if len(http_payloads) == 1:
                return httpx.Response(500, json={"error": http_raw_err})
            return httpx.Response(200, content=(json.dumps({"status": "success"}) + "\n").encode())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b"}]})
        return httpx.Response(404)

    assert is_ollama_fakeip_redirect_error(http_raw_err)
    assert ollama_pull_action(client_factory=lambda: _client(http_handler), model="qwen3.5:4b")(lambda _p: None) == "qwen3.5:4b 已下载"
    assert http_payloads == [
        {"model": "qwen3.5:4b", "stream": True},
        {"model": "qwen3.5:4b", "stream": True, "insecure": True},
    ]
    assert "is not a public address" in capsys.readouterr().err


@pytest.mark.parametrize(
    "retry_mode,expected_brief",
    [
        ("still_fakeip", "Fake-IP"),
        ("stream_timeout", "超时"),
        ("http_502", "HTTP 502"),
        ("incomplete", "未完成"),
        ("connect_error", "无法连接"),
    ],
)
def test_ollama_pull_fakeip_retry_failure_is_concise_and_logged(capsys, retry_mode, expected_brief):
    url = _long_presigned_url()
    first_err = f'max retries exceeded: Get "{url}": redirect target not allowed: host "r2" resolves to non-public 198.18.75.191'
    pull_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/pull":
            return httpx.Response(404)
        body = json.loads(request.content)
        pull_calls.append(body)
        if len(pull_calls) == 1:
            return httpx.Response(200, content=(json.dumps({"error": first_err}) + "\n").encode())
        if retry_mode == "still_fakeip":
            return httpx.Response(200, content=(json.dumps({"error": first_err}) + "\n").encode())
        if retry_mode == "stream_timeout":
            err2 = f'max retries exceeded: Get "{url}": context deadline exceeded'
            return httpx.Response(200, content=(json.dumps({"error": err2}) + "\n").encode())
        if retry_mode == "http_502":
            err2 = f'Get "{url}": proxyconnect tcp: 502 Bad Gateway'
            return httpx.Response(502, json={"error": err2})
        if retry_mode == "incomplete":
            return httpx.Response(200, content=(json.dumps({"status": "pulling manifest"}) + "\n").encode())
        raise httpx.ConnectError("connection refused", request=request)

    def factory() -> httpx.Client:
        return httpx.Client(base_url="http://127.0.0.1:18947", transport=httpx.MockTransport(handler))

    with pytest.raises(LogicError) as ei:
        ollama_pull_action(client_factory=factory, model="qwen3.5:4b")(lambda _p: None)
    assert len(pull_calls) == 2
    assert pull_calls[1] == {"model": "qwen3.5:4b", "stream": True, "insecure": True}
    msg = ei.value.message
    assert "Fake-IP" in msg and expected_brief in msg
    assert "OLLAMA_HOST=127.0.0.1:18947 ollama pull --insecure qwen3.5:4b" in msg
    assert "X-Amz-" not in msg and "https://" not in msg and "max retries exceeded" not in msg
    err_log = capsys.readouterr().err
    assert "attempt=1" in err_log and "attempt=2" in err_log and "X-Amz-Signature=" in err_log


def test_ollama_pull_non_fakeip_errors_do_not_retry_and_strip_urls(capsys):
    url = _long_presigned_url()
    cases = [
        (f'max retries exceeded: Get "{url}": x509: certificate signed by unknown authority', 200, "TLS"),
        (f'Get "{url}": dial tcp: lookup registry.ollama.ai: no such host', 200, "域名解析"),
        (f'Get "{url}": proxyconnect tcp: EOF', 200, "代理"),
        (f'write /models/blob: no space left on device (see {url})', 500, "磁盘空间不足"),
    ]
    for raw_err, status, expected in cases:
        calls: list[dict] = []

        def handler(request: httpx.Request, _raw=raw_err, _status=status, _calls=calls) -> httpx.Response:
            _calls.append(json.loads(request.content))
            if _status >= 400:
                return httpx.Response(_status, json={"error": _raw})
            return httpx.Response(200, content=(json.dumps({"error": _raw}) + "\n").encode())

        with pytest.raises(LogicError) as ei:
            ollama_pull_action(
                client_factory=lambda _h=handler: _client(_h),
                model="qwen3.5:4b",
                ollama_host="http://127.0.0.1:17947",
            )(lambda _p: None)
        assert len(calls) == 1, raw_err
        msg = ei.value.message
        assert expected in msg
        assert "OLLAMA_HOST=127.0.0.1:17947 ollama pull qwen3.5:4b" in msg
        assert "--insecure" not in msg
        assert "X-Amz-" not in msg and "https://" not in msg and "max retries exceeded" not in msg
        assert "X-Amz-Signature=" in capsys.readouterr().err

    # 未知超长残余上限 ≤160 字并带省略号，剥离裸 URL
    huge = f"unexpected failure at {url} " + ("detail-" * 50)
    formatted = format_ollama_pull_error(huge, model="qwen3.5:4b", ollama_host="http://127.0.0.1:17947")
    assert "https://" not in formatted and "X-Amz-" not in formatted and "…" in formatted
    assert "OLLAMA_HOST=127.0.0.1:17947 ollama pull qwen3.5:4b" in formatted


class FakeRun:
    def __init__(self, returncode=0, stderr="", effect=None):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.effect = effect

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if self.effect:
            self.effect(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)


def _release_handler(blob: bytes, digest: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sha256sum.txt"):
            return httpx.Response(200, text=f"{digest}  ./ollama-linux-amd64.tar.zst\nffff  ./other.tgz\n")
        if request.url.path.endswith("/ollama-linux-amd64.tar.zst"):
            assert "/v0.40.16/" in request.url.path
            return httpx.Response(200, content=blob, headers={"content-length": str(len(blob))})
        return httpx.Response(404)

    return handler


def test_ollama_install_verifies_sha_and_extracts_to_user_dir(tmp_path):
    blob = b"fake-tar-zst-bytes" * 100
    digest = hashlib.sha256(blob).hexdigest()

    def extract(cmd):
        staging = Path(cmd[cmd.index("-C") + 1])
        (staging / "bin").mkdir(parents=True)
        (staging / "bin" / "ollama").write_text("#!/bin/sh\n")

    run = FakeRun(effect=extract)
    install_dir = tmp_path / "ollama-bin"
    progress: list[SetupProgress] = []
    action = ollama_install_action(version="0.40.16", install_dir=install_dir, binary=install_dir / "bin" / "ollama",
                                   asset="ollama-linux-amd64.tar.zst", client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(blob, digest))),
                                   run=run)
    assert "已安装" in action(progress.append)
    assert (install_dir / "bin" / "ollama").is_file()
    assert run.calls[0][:3] == ["tar", "--zstd", "-xf"]
    assert progress[-2].percent == 100.0
    assert not list(tmp_path.glob(".*download")) and not list(tmp_path.glob(".*staging"))
    with pytest.raises(LogicError):
        action(lambda _p: None)


def test_ollama_install_rejects_checksum_mismatch(tmp_path):
    run = FakeRun()
    action = ollama_install_action(version="0.40.16", install_dir=tmp_path / "ob", binary=tmp_path / "ob" / "bin" / "ollama",
                                   asset="ollama-linux-amd64.tar.zst", client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(b"x", "0" * 64))),
                                   run=run)
    with pytest.raises(LogicError) as ei:
        action(lambda _p: None)
    assert "sha256" in ei.value.message
    assert run.calls == [] and not (tmp_path / "ob").exists()


def test_ollama_release_asset_per_platform():
    assert ollama_asset("linux", "x86_64") == "ollama-linux-amd64.tar.zst"
    assert ollama_asset("linux", "aarch64") == "ollama-linux-arm64.tar.zst"
    assert ollama_asset("windows", "AMD64") == "ollama-windows-amd64.zip"
    assert ollama_asset("macos", "arm64") == "Ollama-darwin.zip"
    with pytest.raises(LogicError):
        ollama_asset("linux", "riscv64")


class FakePopen:
    def __init__(self, *, model_dir: Path, returncode: int, write_weights: bool, stderr_text: str = ""):
        self.model_dir = model_dir
        self._rc = returncode
        self.write_weights = write_weights
        self.stderr_text = stderr_text
        self.polls = 0
        self.args = None
        self.kwargs = None

    def __call__(self, args, **kw):
        self.args = args
        self.kwargs = dict(kw)
        kw["stderr"].write(self.stderr_text)
        self.stdout = io.StringIO(json.dumps({"total_bytes": 1000}) + "\n")
        return self

    def poll(self):
        self.polls += 1
        if self.polls < 3:
            return None
        if self.write_weights:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            (self.model_dir / "config.json").write_text("{}")
            (self.model_dir / "model.safetensors").write_bytes(b"0" * 10)
        self.returncode = self._rc
        return self._rc


def test_asr_download_runs_worker_in_isolated_env_and_rechecks(tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    model_dir = tmp_path / "models" / "Qwen3-ASR-1.7B"
    popen = FakePopen(model_dir=model_dir, returncode=0, write_weights=True)
    progress: list[SetupProgress] = []
    action = asr_download_action(python=python, worker=Path("/w.py"), repo_id="Qwen/Qwen3-ASR-1.7B",
                                 model_dir=model_dir, log_path=tmp_path / "log" / "a.log",
                                 env={"HF_HOME": str(tmp_path / "hf")}, popen=popen, sleep=lambda _s: None)
    assert "已下载" in action(progress.append)
    assert popen.args == [str(python), "/w.py", "Qwen/Qwen3-ASR-1.7B", str(model_dir)]
    assert progress and progress[0].stage == "下载识别模型权重"


def test_asr_download_failures_visible(tmp_path):
    model_dir = tmp_path / "m"
    missing_env = asr_download_action(python=tmp_path / "nope", worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                      log_path=tmp_path / "l.log", env={}, popen=FakePopen(model_dir=model_dir, returncode=0, write_weights=True))
    with pytest.raises(LogicError) as ei:
        missing_env(lambda _p: None)
    assert "未安装" in ei.value.message
    python = tmp_path / "py"
    python.write_text("")
    failing = asr_download_action(python=python, worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                  log_path=tmp_path / "l.log", env={}, sleep=lambda _s: None,
                                  popen=FakePopen(model_dir=model_dir, returncode=1, write_weights=False, stderr_text="HTTP 403 fixture"))
    with pytest.raises(LogicError) as ei:
        failing(lambda _p: None)
    assert "403 fixture" in ei.value.message
    incomplete = asr_download_action(python=python, worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                     log_path=tmp_path / "l.log", env={}, sleep=lambda _s: None,
                                     popen=FakePopen(model_dir=model_dir, returncode=0, write_weights=False))
    with pytest.raises(LogicError) as ei:
        incomplete(lambda _p: None)
    assert "不完整" in ei.value.message


def test_probe_asr_weights_complete_and_partial(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "s1.safetensors", "b": "s2.safetensors"}}))
    (d / "s1.safetensors").write_bytes(b"1")
    probe = probe_asr_weights(d)
    assert not probe.complete and "s2.safetensors" in probe.missing
    (d / "s2.safetensors").write_bytes(b"2")
    assert probe_asr_weights(d).complete
    (d / ".cache").mkdir()
    (d / ".cache" / "x.incomplete").write_bytes(b"")
    assert not probe_asr_weights(d).complete
    assert not probe_asr_weights(tmp_path / "none").complete


def test_probe_ollama_and_sidecar(tmp_path):
    binary = tmp_path / "ollama"
    binary.write_text("")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.16"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b", "size": 3_400_000_000}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "loaded": True})
        return httpx.Response(404)

    with _client(handler) as client:
        probe = probe_ollama(binary=binary, client=client, model="qwen3.5:4b")
        assert probe.installed and probe.running and probe.has_model and probe.version == "0.40.16"
        side = probe_asr_sidecar(client)
        assert side.running and side.loaded

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with _client(down) as client:
        probe = probe_ollama(binary=tmp_path / "missing", client=client, model="qwen3.5:4b")
        assert not probe.installed and not probe.running and "refused" in probe.error
        assert not probe_asr_sidecar(client).running


def test_lan_ip_listing_skips_virtual_bridges(tmp_path):
    for name in ("wlp1s0", "eth0"):
        (tmp_path / name / "device").mkdir(parents=True)
    payload = [
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
        {"ifname": "wlp1s0", "addr_info": [{"local": "10.0.0.110"}]},
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]},
        {"ifname": "eth0", "addr_info": [{"local": "198.18.0.5"}, {"local": "192.168.1.9"}, {"local": "169.254.1.1"}]},
    ]
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    assert list_physical_lan_ipv4(run=run, sys_class_net=tmp_path) == ["10.0.0.110", "192.168.1.9"]
    bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no ip")
    with pytest.raises(LogicError):
        list_physical_lan_ipv4(run=bad, sys_class_net=tmp_path)


def test_token_file_created_0600_and_stable(tmp_path):
    path = tmp_path / "token"
    first = load_or_create_token(path)
    assert len(first) >= 24
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_or_create_token(path) == first


def test_lan_ip_listing_windows_filters_virtual_adapters_and_falls_back_only_on_error(monkeypatch):
    ipconfig_out = (
        "\nWindows IP 配置\n\n"
        "无线局域网适配器 WLAN:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 192.168.1.25(首选)\n"
        "   自动配置 IPv4 地址 . . . . . . . : 169.254.10.2(首选)\n"
        "   默认网关. . . . . . . . . . . . . : 192.168.1.1\n\n"
        "Ethernet adapter Ethernet:\n"
        "   IPv4 Address. . . . . . . . . . . : 10.0.0.9(Preferred)\n\n"
        "以太网适配器 有线:\n"
        "   IPv4 地址 . . . . . . . . . . . . ： 172.16.8.4(首选)\n\n"
        "以太网适配器 vEthernet (WSL (Hyper-V firewall)):\n"
        "   IPv4 地址 . . . . . . . . . . . . : 172.24.16.1(首选)\n\n"
        "Ethernet adapter VMware Network Adapter VMnet8:\n"
        "   IPv4 Address. . . . . . . . . . . : 192.168.152.1\n\n"
        "Ethernet adapter VirtualBox Host-Only Network:\n"
        "   IPv4 Address. . . . . . . . . . . : 192.168.56.1\n\n"
        "以太网适配器 DockerNAT:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 10.0.75.1\n\n"
        "未知适配器 Tailscale:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 100.64.0.1\n\n"
        "以太网适配器 TAP-Windows Adapter V9:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 10.9.0.5\n\n"
        "以太网适配器 蓝牙网络连接:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 192.168.137.1\n\n"
        "隧道适配器 Teredo Tunneling Pseudo-Interface:\n"
        "   IPv4 地址 . . . . . . . . . . . . : 10.20.30.40\n"
    )
    seen_kw: list[dict] = []

    def win_run(cmd, **kw):
        seen_kw.append(dict(kw))
        assert cmd == ["ipconfig"]
        return subprocess.CompletedProcess(cmd, 0, stdout=ipconfig_out, stderr="")

    monkeypatch.setattr(cp_mod.os, "name", "nt")
    probe_called = {"n": 0}
    ips = list_physical_lan_ipv4(
        run=win_run,
        os_name="windows",
        socket_probe=lambda: (probe_called.__setitem__("n", probe_called["n"] + 1) or ["192.168.99.9"]),
    )
    assert ips == ["192.168.1.25", "10.0.0.9", "172.16.8.4"]
    assert probe_called["n"] == 0
    assert seen_kw[0]["creationflags"] == cp_mod.CREATE_NO_WINDOW
    assert seen_kw[0]["encoding"] == "oem" and seen_kw[0]["errors"] == "replace"

    # returncode == 0 但全部为虚拟网卡或离线 → 返回 []，严禁触发 socket_probe 回退
    virtual_only = "以太网适配器 vEthernet (WSL):\n   IPv4 地址 . . . : 172.24.16.1(首选)\n"
    offline = list_physical_lan_ipv4(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=virtual_only, stderr=""),
        os_name="windows",
        socket_probe=lambda: (probe_called.__setitem__("n", probe_called["n"] + 1) or ["192.168.99.9"]),
    )
    assert offline == [] and probe_called["n"] == 0

    # 命令失败（FileNotFoundError / returncode != 0）时才回退 socket_probe 且走同一过滤规则
    def missing_ipconfig(cmd, **kw):
        raise FileNotFoundError("ipconfig")

    fb1 = list_physical_lan_ipv4(
        run=missing_ipconfig,
        os_name="windows",
        socket_probe=lambda: ["127.0.0.1", "169.254.1.2", "198.18.0.1", "192.168.77.8"],
    )
    assert fb1 == ["192.168.77.8"]
    fb2 = list_physical_lan_ipv4(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"),
        os_name="windows",
        socket_probe=lambda: ["10.1.1.2"],
    )
    assert fb2 == ["10.1.1.2"]


def test_lan_ip_listing_macos_filters_virtual_and_down_and_falls_back_only_on_error():
    ifconfig_out = (
        "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
        "\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet6 fe80::1%en0 prefixlen 64\n"
        "\tinet 192.168.50.12 netmask 0xffffff00 broadcast 192.168.50.255\n"
        "en1: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet 192.168.50.99 netmask 0xffffff00 broadcast 192.168.50.255\n"
        "utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n"
        "\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff\n"
        "bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255\n"
        "awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet 10.99.0.1 netmask 0xffffff00\n"
        "en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet 169.254.2.3 netmask 0xffff0000\n"
        "\tinet 10.1.2.3 netmask 0xffffff00 broadcast 10.1.2.255\n"
    )
    probe_called = {"n": 0}
    ips = list_physical_lan_ipv4(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=ifconfig_out, stderr=""),
        os_name="macos",
        socket_probe=lambda: (probe_called.__setitem__("n", probe_called["n"] + 1) or ["192.168.1.1"]),
    )
    assert ips == ["192.168.50.12", "10.1.2.3"] and probe_called["n"] == 0

    # returncode == 0 但无可用物理网卡 → 返回 []，不触发 socket_probe
    empty = list_physical_lan_ipv4(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="lo0: flags=8049<UP,LOOPBACK>\n\tinet 127.0.0.1\n", stderr=""),
        os_name="macos",
        socket_probe=lambda: (probe_called.__setitem__("n", probe_called["n"] + 1) or ["192.168.1.1"]),
    )
    assert empty == [] and probe_called["n"] == 0

    # ifconfig 命令失败时才回退
    fb = list_physical_lan_ipv4(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 127, stdout="", stderr="not found"),
        os_name="macos",
        socket_probe=lambda: ["172.16.0.9", "169.254.0.1"],
    )
    assert fb == ["172.16.0.9"]


def test_session_type_cross_platform():
    assert session_type({}, os_name="windows") == "windows"
    assert session_type({}, os_name="macos") == "macos"
    assert session_type({"XDG_SESSION_TYPE": "Wayland"}, os_name="linux") == "wayland"
    assert session_type({}, os_name="linux") == "unknown"


def test_ollama_install_replaces_existing_partial_dir_or_file(tmp_path):
    blob = b"fake-tar-zst-bytes" * 10
    digest = hashlib.sha256(blob).hexdigest()

    def extract(cmd):
        staging = Path(cmd[cmd.index("-C") + 1])
        (staging / "bin").mkdir(parents=True)
        (staging / "bin" / "ollama").write_text("#!/bin/sh\n")

    # 1. 残留半成品目录（缺少 binary）
    install_dir = tmp_path / "ollama-partial-dir"
    (install_dir / "leftover").mkdir(parents=True)
    (install_dir / "leftover" / "stale.txt").write_text("stale")
    action_dir = ollama_install_action(
        version="0.40.16",
        install_dir=install_dir,
        binary=install_dir / "bin" / "ollama",
        asset="ollama-linux-amd64.tar.zst",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(blob, digest))),
        run=FakeRun(effect=extract),
    )
    assert "已安装" in action_dir(lambda _p: None)
    assert (install_dir / "bin" / "ollama").is_file()
    assert not (install_dir / "leftover").exists()

    # 2. 残留普通文件占用了 install_dir 路径
    file_dir = tmp_path / "ollama-file"
    file_dir.write_text("stray file")
    action_file = ollama_install_action(
        version="0.40.16",
        install_dir=file_dir,
        binary=file_dir / "bin" / "ollama",
        asset="ollama-linux-amd64.tar.zst",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(blob, digest))),
        run=FakeRun(effect=extract),
    )
    assert "已安装" in action_file(lambda _p: None)
    assert (file_dir / "bin" / "ollama").is_file()

    # 3. 残留悬空符号链接占用了 install_dir 路径
    link_dir = tmp_path / "ollama-broken-link"
    link_dir.symlink_to(tmp_path / "nonexistent-target")
    action_link = ollama_install_action(
        version="0.40.16",
        install_dir=link_dir,
        binary=link_dir / "bin" / "ollama",
        asset="ollama-linux-amd64.tar.zst",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(blob, digest))),
        run=FakeRun(effect=extract),
    )
    assert "已安装" in action_link(lambda _p: None)
    assert (link_dir / "bin" / "ollama").is_file()


def test_asr_download_passes_utf8_and_windows_no_window_flags(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("")
    model_dir = tmp_path / "models" / "Qwen3-ASR-1.7B"
    popen = FakePopen(model_dir=model_dir, returncode=0, write_weights=True)
    monkeypatch.setattr(cp_mod.os, "name", "nt")
    action = asr_download_action(
        python=python,
        worker=Path("/w.py"),
        repo_id="Qwen/Qwen3-ASR-1.7B",
        model_dir=model_dir,
        log_path=tmp_path / "log" / "a.log",
        env={"HF_HOME": str(tmp_path / "hf")},
        popen=popen,
        sleep=lambda _s: None,
    )
    assert "已下载" in action(lambda _p: None)
    assert popen.kwargs["env"]["PYTHONUTF8"] == "1"
    assert popen.kwargs["encoding"] == "utf-8" and popen.kwargs["errors"] == "replace"
    assert popen.kwargs["creationflags"] == cp_mod.CREATE_NO_WINDOW


def test_build_release_proxy_mounts_and_create_release_client(monkeypatch):
    # 1. 有限读超时常量
    assert RELEASE_HTTP_TIMEOUT.connect == 30.0 and RELEASE_HTTP_TIMEOUT.read == 120.0

    # 2. 注入 transport 时不传 mounts 且 trust_env=False，绝不绕过测试 MockTransport
    mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    with create_release_client(
        environ={"HTTPS_PROXY": "http://127.0.0.1:7890"},
        os_name="windows",
        read_system_proxy=lambda _os: ({"http": "http://os:1", "https": "http://os:2"}, []),
        transport=mock_transport,
    ) as client:
        assert client._trust_env is False
        assert client._mounts == {}
        assert client.timeout == RELEASE_HTTP_TIMEOUT
        assert client.get("https://example.invalid/ping").text == "ok"

    # 3. transport=None：Windows 分协议代理 + 全形状 NO_PROXY（:// 透传、IPv6 加括号、host:port 不误加括号、*.corp/.org/裸域名、畸形项 fail-open）
    mounts = build_release_proxy_mounts(
        {
            "NO_PROXY": "http://bypass.local:9000,::1,[::1],127.0.0.1,localhost,corp.local:8080,*.corp,.example.org,bare.internal,bad:notaport,<local>"
        },
        os_name="windows",
        read_system_proxy=lambda _os: (
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7891"},
            [],
        ),
    )
    assert isinstance(mounts["http://"], httpx.HTTPTransport)
    assert isinstance(mounts["https://"], httpx.HTTPTransport)
    for key in (
        "http://bypass.local:9000",
        "all://[::1]",
        "all://127.0.0.1",
        "all://localhost",
        "all://corp.local:8080",
        "all://*.corp",
        "all://*.example.org",
        "all://bare.internal",
        "all://*.bare.internal",
    ):
        assert key in mounts and mounts[key] is None, key
    assert "all://[corp.local:8080]" not in mounts
    assert "all://bad:notaport" not in mounts
    assert all("<local>" not in k.lower() for k in mounts)

    # 实际路由核验：create_release_client(transport=None) trust_env=False、公网走代理、NO_PROXY 命中直连
    with create_release_client(
        environ={"NO_PROXY": "corp.local:8080,bare.internal,*.corp"},
        os_name="windows",
        read_system_proxy=lambda _os: ({"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7891"}, []),
    ) as live_client:
        assert live_client._trust_env is False
        assert live_client._transport_for_url(httpx.URL("https://github.com/astral-sh/uv")) is not live_client._transport
        assert live_client._transport_for_url(httpx.URL("https://corp.local:8080/x")) is live_client._transport
        assert live_client._transport_for_url(httpx.URL("https://sub.bare.internal/x")) is live_client._transport
        assert live_client._transport_for_url(httpx.URL("https://bare.internal/x")) is live_client._transport
        assert live_client._transport_for_url(httpx.URL("https://a.corp/x")) is live_client._transport
        assert live_client._transport_for_url(httpx.URL("http://[::1]:17945/x")) is live_client._transport

    # 4. ALL_PROXY / all_proxy 生效且仍保底回环 NO_PROXY；NO_PROXY=* 短路返回空 mounts
    all_mounts = build_release_proxy_mounts(
        {"all_proxy": "http://127.0.0.1:7890"},
        os_name="linux",
        read_system_proxy=lambda _os: ({}, []),
    )
    assert isinstance(all_mounts["http://"], httpx.HTTPTransport) and isinstance(all_mounts["https://"], httpx.HTTPTransport)
    assert all_mounts["all://127.0.0.1"] is None and all_mounts["all://localhost"] is None and all_mounts["all://[::1]"] is None
    assert (
        build_release_proxy_mounts(
            {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "corp.local,*"},
            os_name="linux",
            read_system_proxy=lambda _os: ({}, []),
        )
        == {}
    )

    # 5. SOCKS5 缺 socksio 显式抛脱敏 LogicError；非法代理 URL 同时脱敏 url 与 exc 文本
    with pytest.raises(LogicError) as socks_err:
        build_release_proxy_mounts(
            {"HTTPS_PROXY": "socks5://alice:topsecret@127.0.0.1:1080"},
            os_name="windows",
            read_system_proxy=lambda _os: ({}, []),
        )
    assert socks_err.value.code == ErrorCode.dependency_failed
    assert "SOCKS" in socks_err.value.message and "socks5://***@127.0.0.1:1080" in socks_err.value.message
    assert "topsecret" not in socks_err.value.message

    for bad_proxy in ("http://alice:topsecret@127.0.0.1:abc", "ftp://alice:topsecret@127.0.0.1:21"):
        with pytest.raises(LogicError) as bad_err:
            build_release_proxy_mounts(
                {"HTTPS_PROXY": bad_proxy},
                os_name="linux",
                read_system_proxy=lambda _os: ({}, []),
            )
        assert bad_err.value.code == ErrorCode.dependency_failed
        assert "代理地址无效" in bad_err.value.message and "topsecret" not in bad_err.value.message and "***@" in bad_err.value.message

    # 异常文本本身携带凭证时同样必须经 redact_url_credentials(str(exc)) 脱敏
    monkeypatch.setattr(
        httpx,
        "HTTPTransport",
        lambda **kw: (_ for _ in ()).throw(ValueError("bad proxy http://bob:excsecret@127.0.0.1:7890")),
    )
    with pytest.raises(LogicError) as exc_redact:
        build_release_proxy_mounts(
            {"HTTPS_PROXY": "http://alice:urlsecret@127.0.0.1:7890"},
            os_name="linux",
            read_system_proxy=lambda _os: ({}, []),
        )
    assert "urlsecret" not in exc_redact.value.message and "excsecret" not in exc_redact.value.message
    assert "http://***@127.0.0.1:7890" in exc_redact.value.message


def test_asr_download_none_log_path_concurrent_stderr_tee_and_tail(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    model_dir = tmp_path / "models" / "Qwen3-ASR-1.7B"

    class _PipeProc:
        def __init__(self, *, rc: int, stderr_text: str | None, write_weights: bool) -> None:
            self._rc = rc
            self._write_weights = write_weights
            self._polls = 0
            self.returncode = None
            self.stdout = io.StringIO(json.dumps({"total_bytes": 100}) + "\n")
            if stderr_text is not None:
                self.stderr = io.StringIO(stderr_text)

        def poll(self):
            self._polls += 1
            if self._polls < 2:
                return None
            if self._write_weights:
                model_dir.mkdir(parents=True, exist_ok=True)
                (model_dir / "config.json").write_text("{}")
                (model_dir / "model.safetensors").write_bytes(b"1")
            self.returncode = self._rc
            return self._rc

    # 1) log_path=None：stderr=subprocess.PIPE 并发排空并安全 tee 到 sys.stderr
    captured_err = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured_err)
    seen_kw: list[dict] = []

    def popen_ok(_argv, **kw):
        seen_kw.append(dict(kw))
        return _PipeProc(rc=0, stderr_text="hf progress line\n", write_weights=True)

    ok_action = asr_download_action(
        python=python,
        worker=Path("/w.py"),
        repo_id="Qwen/Qwen3-ASR-1.7B",
        model_dir=model_dir,
        log_path=None,
        env={},
        popen=popen_ok,
        sleep=lambda _s: None,
    )
    assert "已下载" in ok_action(lambda _p: None)
    assert seen_kw[-1]["stderr"] == subprocess.PIPE
    assert "hf progress line" in captured_err.getvalue()

    # 2) sys.stderr=None（pythonw/冻结无控制台）且非零退出：保留末尾 400 字尾缓冲不崩
    monkeypatch.setattr("sys.stderr", None)
    long_err = ("X" * 300) + "TAIL_MARKER_400"
    fail_action = asr_download_action(
        python=python,
        worker=Path("/w.py"),
        repo_id="r",
        model_dir=model_dir,
        log_path=None,
        env={},
        popen=lambda _a, **_k: _PipeProc(rc=2, stderr_text=long_err, write_weights=False),
        sleep=lambda _s: None,
    )
    with pytest.raises(LogicError) as err:
        fail_action(lambda _p: None)
    assert "退出码 2" in err.value.message and "TAIL_MARKER_400" in err.value.message

    # 3) 容忍 getattr(proc, "stderr", None) is None（如 _DoneProc）
    no_stderr_action = asr_download_action(
        python=python,
        worker=Path("/w.py"),
        repo_id="r",
        model_dir=model_dir,
        log_path=None,
        env={},
        popen=lambda _a, **_k: _PipeProc(rc=0, stderr_text=None, write_weights=True),
        sleep=lambda _s: None,
    )
    assert "已下载" in no_stderr_action(lambda _p: None)

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from _contracts import (
    DEFAULT_LOG_MAX_LINES,
    AboutAsrVariant,
    ComponentName,
    ComponentState,
    ComponentStatus,
    ErrorCode,
    LogGroup,
    LogLevel,
    LogicError,
    RunMode,
)
from _logs import (
    ABOUT_LINKS,
    CANONICAL_LOG_SOURCES,
    LICENSE_NOTE,
    LICENSE_TITLE,
    LogService,
    build_about_info,
    build_diagnostic_summary,
    classify_log_level,
    clear_log,
    detect_run_mode,
    list_logs,
    read_log,
    read_log_tail,
    redact_log_line,
    strip_ansi,
)
from _paths import DEB_PROGRAM_ROOT, AppPaths
from _v3 import uv_runner


def _sandbox_paths(tmp_path: Path, *, os_name: str = "linux") -> AppPaths:
    return AppPaths.for_roots(
        config_root=tmp_path / "cfg",
        data_root=tmp_path / "data",
        runtime_dir=tmp_path / "run",
        os_name=os_name,  # type: ignore[arg-type]
    )


def test_strip_ansi_single_source_shared_with_uv_runner():
    assert uv_runner.strip_ansi is strip_ansi
    assert strip_ansi("\x1b[31mERROR\x1b[0m: \x1b]0;win title\x07ready\x1b]8;;https://x\x1b\\ok") == "ERROR: readyok"
    assert strip_ansi("") == ""


def test_redact_log_line_covers_secrets_proxies_r2_auth_and_kv():
    # 1) 显式令牌：len>=4 同时脱敏原文与 percent-encoded；<4 短串不得误伤普通文本
    line = "req tok+a/b url=tok%2Ba%2Fb keep=abc"
    redacted = redact_log_line(line, secret_tokens=("tok+a/b", "abc", "", "   "))
    assert "tok+a/b" not in redacted
    assert "tok%2Ba%2Fb" not in redacted
    assert "keep=abc" in redacted

    # 2) URL 凭证与裸 Windows 代理凭证
    proxy_line = (
        "HTTPS_PROXY=http://alice:p@ss@127.0.0.1:7890 "
        "win=https=bob:sec@10.0.0.1:8080;http=carol:p@w@10.0.0.2:8081"
    )
    proxy_out = redact_log_line(proxy_line)
    assert proxy_out == (
        "HTTPS_PROXY=http://***@127.0.0.1:7890 "
        "win=https=***@10.0.0.1:8080;http=***@10.0.0.2:8081"
    )

    # 3) R2 / S3 预签名查询参数（含空值不吞后续字段）
    r2_url = "https://r2.example/a.whl?X-Amz-Credential=AKIA%2F2026&X-Amz-Signature=deadbeef&next=1"
    assert redact_log_line(r2_url) == "https://r2.example/a.whl?X-Amz-Credential=***&X-Amz-Signature=***&next=1"
    assert redact_log_line("?X-Amz-Signature=&next=1") == "?X-Amz-Signature=&next=1"

    # 4) Authorization / Proxy-Authorization 全 scheme（JSON 闭合引号止步、空值不吞后续）
    assert redact_log_line("Authorization: Bearer eyJ.abc.123 status=200") == "Authorization: Bearer *** status=200"
    assert redact_log_line("Proxy-Authorization: Basic dXNlcjpwYXNz host=a") == "Proxy-Authorization: Basic *** host=a"
    assert redact_log_line("Authorization: Token raw_tok_9") == "Authorization: Token ***"
    assert redact_log_line('Authorization: Digest username="u", response="abc"') == "Authorization: Digest ***"
    assert redact_log_line("Authorization: opaque_secret status=200") == "Authorization: *** status=200"
    assert (
        redact_log_line('{"Authorization": "Bearer jwt123", "host": "127.0.0.1"}')
        == '{"Authorization": "Bearer ***", "host": "127.0.0.1"}'
    )
    assert (
        redact_log_line("{'Proxy-Authorization': 'Basic dXNlcjpwYXNz', 'ok': True}")
        == "{'Proxy-Authorization': 'Basic ***', 'ok': True}"
    )
    assert redact_log_line('{"Authorization": "", "next": 1}') == '{"Authorization": "", "next": 1}'

    # 5) 敏感键（= 与 JSON/dict :，空值不吞后续，不误伤 max_tokens/tokenizer）
    kv = (
        '{"token": "s3cr3t", "web_input_token": "", "hf_token": "hf_abc", "ok": true} '
        "GET /?token=&next=1 X-Voice-Token: vtok_99 baidu_appkey=bk_123 "
        "api_key=ak_1 password=pw1 passwd=pw2 secret=sec1 max_tokens=100 tokenizer=qwen"
    )
    kv_out = redact_log_line(kv)
    assert '"token": "***"' in kv_out
    assert '"web_input_token": ""' in kv_out
    assert '"hf_token": "***"' in kv_out
    assert '"ok": true' in kv_out
    assert "GET /?token=&next=1" in kv_out
    assert "X-Voice-Token: ***" in kv_out
    assert "baidu_appkey=***" in kv_out
    assert "api_key=***" in kv_out
    assert "password=***" in kv_out
    assert "passwd=***" in kv_out
    assert "secret=***" in kv_out
    assert "max_tokens=100" in kv_out and "tokenizer=qwen" in kv_out


def test_classify_log_level_precision():
    for err_line in (
        "2026-11-23 [ERROR] crash",
        "ERROR:    Exception in ASGI application",
        "time=2026 level=ERROR msg=boom",
        "Traceback (most recent call last):",
        "error: Failed to download wheel",
        "× Failed to build package",
    ):
        assert classify_log_level(err_line) == LogLevel.error, err_line

    for warn_line in (
        "2026-11-23 [WARN] slow",
        "2026-11-23 [WARNING] retry",
        "WARNING: deprecated flag",
        "time=2026 level=WARN msg=retry",
        "warning: cache miss",
    ):
        assert classify_log_level(warn_line) == LogLevel.warn, warn_line

    for info_line in (
        "",
        "2026-11-23 [INFO] status error=None ok=True",
        '{"ok": true, "error": null, "has_error": false, "has_warn": false}',
        "2026-11-23 [INFO] error_count=0 warn_count=0",
        "2026-11-23 [INFO] ready",
    ):
        assert classify_log_level(info_line) == LogLevel.info, info_line


def test_read_log_tail_normalization_crlf_inline_cr_nul_ansi_and_truncation(tmp_path: Path):
    log = tmp_path / "app.log"
    payload = (
        b"line0_old\r\n"
        b"crlf_preserved\r\n"
        b"dl 10%\rdl 50%\rdl 100%\r\n"
        b"\x00\x00\x1b[31mERROR: secret=abc123\x1b[0m\r\n"
        b"2026 [WARN] ok\n"
    )
    log.write_bytes(payload)

    tail = read_log_tail(log, max_lines=4)
    assert tail.exists is True
    assert tail.bytes == len(payload)
    assert tail.truncated is True
    assert tail.has_error is True
    assert tail.has_warn is True
    assert [(e.line_no, e.text, e.level) for e in tail.entries] == [
        (1, "crlf_preserved", LogLevel.info),
        (2, "dl 100%", LogLevel.info),
        (3, "ERROR: secret=***", LogLevel.error),
        (4, "2026 [WARN] ok", LogLevel.warn),
    ]

    # 字节上限截断：在多字节 UTF-8 中间切分时，先按 bytes 丢首半行再解码，不产生乱码且标记 truncated=True
    big = tmp_path / "big.log"
    big.write_bytes(("中文日志" * 20 + "\n").encode("utf-8") * 50)
    capped = read_log_tail(big, max_lines=500, max_bytes=300)
    assert capped.truncated is True
    assert len(capped.entries) >= 1
    assert all("\ufffd" not in e.text for e in capped.entries)

    # 字节上限内无任何 \n（或仅末尾单一 \n）：保留截断内容而非清空
    no_nl = tmp_path / "no_nl.log"
    no_nl.write_bytes(b"A" * 500)
    res_no_nl = read_log_tail(no_nl, max_lines=10, max_bytes=128)
    assert res_no_nl.truncated is True
    assert len(res_no_nl.entries) == 1 and res_no_nl.entries[0].text == "A" * 128

    single_nl = tmp_path / "single_nl.log"
    single_nl.write_bytes(b"B" * 500 + b"\n")
    res_single_nl = read_log_tail(single_nl, max_lines=10, max_bytes=128)
    assert res_single_nl.truncated is True
    assert len(res_single_nl.entries) == 1 and res_single_nl.entries[0].text == "B" * 127


def test_canonical_allowlist_and_legacy_filename_rejected(tmp_path: Path):
    assert len(CANONICAL_LOG_SOURCES) == 9
    names = [name for name, _ in CANONICAL_LOG_SOURCES]
    assert "setup-asr_download.log" in names
    assert "setup-asr-download.log" not in names

    # 同时落盘规范下划线名与旧连字符名：仅规范 9 份可见，旧名必须 404
    (tmp_path / "setup-asr_download.log").write_text("2026 [INFO] canon ok\n", encoding="utf-8")
    (tmp_path / "setup-asr-download.log").write_text("2026 [INFO] legacy ignored\n", encoding="utf-8")

    listed = list_logs(tmp_path, max_lines=2000)
    assert listed.log_max_lines == 2000
    assert listed.allowed_max_lines == [500, 2000, 5000]
    assert [s.name for s in listed.sources] == names
    dl = next(s for s in listed.sources if s.name == "setup-asr_download.log")
    assert dl.group == LogGroup.setup and dl.exists is True and dl.line_count == 1

    for bad_name in ("setup-asr-download.log", "../secret.log", "app.log.1", "unknown.log"):
        with pytest.raises(LogicError) as err:
            read_log(tmp_path, bad_name)
        assert err.value.code == ErrorCode.not_found

    for bad_lines in (0, 5001):
        with pytest.raises(LogicError) as err:
            read_log(tmp_path, "app.log", max_lines=bad_lines)
        assert err.value.code == ErrorCode.invalid_request


def test_symlink_and_non_regular_rejected_and_clear_log_behavior(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n", encoding="utf-8")
    (logs_dir / "app.log").symlink_to(outside)

    for fn in (
        lambda: read_log(logs_dir, "app.log"),
        lambda: list_logs(logs_dir),
        lambda: clear_log(logs_dir, "app.log"),
    ):
        with pytest.raises(LogicError) as err:
            fn()
        assert err.value.code == ErrorCode.logs_io
    assert outside.read_text(encoding="utf-8") == "do not touch\n"

    # 目录伪装日志文件 → logs_io
    (logs_dir / "server.log").mkdir()
    with pytest.raises(LogicError) as dir_err:
        read_log(logs_dir, "server.log")
    assert dir_err.value.code == ErrorCode.logs_io

    # 活跃安装任务日志拒绝清空 → conflict
    setup_log = logs_dir / "setup-ollama_pull.log"
    setup_log.write_text("pulling...\n", encoding="utf-8")
    with pytest.raises(LogicError) as conflict_err:
        clear_log(logs_dir, "setup-ollama_pull.log", active_setup_logs={"setup-ollama_pull.log"})
    assert conflict_err.value.code == ErrorCode.conflict
    assert setup_log.read_text(encoding="utf-8") == "pulling...\n"

    # 缺失日志幂等成功
    missing_res = clear_log(logs_dir, "window.log")
    assert missing_res.cleared is True and missing_res.bytes_before == 0

    # 普通文件原地 truncate(0) 保留 inode 并删除 .1 归档
    asr_log = logs_dir / "asr.log"
    asr_log.write_text("line1\nline2\n", encoding="utf-8")
    backup = logs_dir / "asr.log.1"
    backup.write_text("old backup\n", encoding="utf-8")
    ino_before = asr_log.stat().st_ino
    size_before = asr_log.stat().st_size

    cleared = clear_log(logs_dir, "asr.log")
    assert cleared.bytes_before == size_before
    assert asr_log.stat().st_size == 0
    assert asr_log.stat().st_ino == ino_before
    assert not backup.exists()

    # .1 备份是符号链接时拒绝删除
    (logs_dir / "asr.log.1").symlink_to(outside)
    with pytest.raises(LogicError) as sym_backup_err:
        clear_log(logs_dir, "asr.log")
    assert sym_backup_err.value.code == ErrorCode.logs_io
    assert outside.exists()


def test_build_about_info_non_blocking_variants_and_run_modes(tmp_path: Path):
    paths = _sandbox_paths(tmp_path, os_name="linux")

    # 1) 清单缺失 → None
    about_missing = build_about_info(paths, environ={"XDG_SESSION_TYPE": "wayland"}, frozen=False)
    assert about_missing.asr_variant is None
    assert "尚未安装" in about_missing.asr_detail
    assert about_missing.session_type == "wayland"
    assert about_missing.run_mode == RunMode.source
    assert about_missing.license_title == LICENSE_TITLE
    assert about_missing.license_note == LICENSE_NOTE
    assert about_missing.links == ABOUT_LINKS

    # 2) 合法清单 + 硬件说明
    paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.asr_manifest.write_text(json.dumps({"variant": "cuda"}), encoding="utf-8")
    about_cuda = build_about_info(paths, hardware_detail="NVIDIA RTX 4070", frozen=True)
    assert about_cuda.asr_variant == AboutAsrVariant.cuda
    assert "NVIDIA RTX 4070" in about_cuda.asr_detail
    assert about_cuda.run_mode == RunMode.frozen

    # 3) 损坏 JSON & 非法 variant → corrupt（不抛异常）
    paths.asr_manifest.write_text("{corrupt", encoding="utf-8")
    about_corrupt = build_about_info(paths)
    assert about_corrupt.asr_variant == AboutAsrVariant.corrupt

    paths.asr_manifest.write_text(json.dumps({"variant": "rocm"}), encoding="utf-8")
    about_bad_var = build_about_info(paths)
    assert about_bad_var.asr_variant == AboutAsrVariant.corrupt

    # 4) detect_run_mode 覆盖 deb / frozen / source
    assert detect_run_mode(os_name="linux", program_root=DEB_PROGRAM_ROOT, frozen=False) == RunMode.deb
    assert detect_run_mode(os_name="windows", program_root=DEB_PROGRAM_ROOT, frozen=False) == RunMode.source
    assert detect_run_mode(os_name="macos", program_root=tmp_path, frozen=True) == RunMode.frozen


def test_diagnostic_summary_and_log_service_redaction_and_cap(tmp_path: Path):
    paths = _sandbox_paths(tmp_path, os_name="linux")
    paths.logs.mkdir(parents=True, exist_ok=True)
    secret = "super_secret_token_999"
    (paths.logs / "app.log").write_text(f"2026 [INFO] boot token={secret}\n", encoding="utf-8")
    (paths.logs / "server.log").write_text(f"2026 [ERROR] leaked {secret}\n", encoding="utf-8")

    comp = ComponentStatus(
        name=ComponentName.ollama,
        state=ComponentState.backing_off,
        reason=f"proxy http://alice:pw@127.0.0.1:7890 failed with {secret}",
        failures=2,
        since="2026-11-23T00:00:00+00:00",
    )
    service = LogService(
        paths=paths,
        get_log_max_lines=lambda: 500,
        # 故意传生成器，验证多文件遍历不会在首个文件耗尽后漏脱敏
        get_secret_tokens=lambda: (t for t in (secret,)),
        get_components=lambda: [comp],
        get_hardware_detail=lambda: f"GPU hint {secret}",
        session_type="x11",
    )

    listed = service.list_sources()
    assert listed.log_max_lines == 500
    read_srv = service.read("server.log")
    assert secret not in read_srv.entries[0].text

    summary = service.diagnostic_summary()
    assert summary.log_max_lines == 500
    assert summary.included_files == ["app.log", "server.log"]
    assert secret not in summary.text
    assert "alice:pw" not in summary.text
    assert "http://***@127.0.0.1:7890" in summary.text

    # 紧凑字节上限触发 truncated=True 与省略提示
    (paths.logs / "ollama.log").write_text(("2026 [INFO] " + "x" * 120 + "\n") * 20, encoding="utf-8")
    tight = build_diagnostic_summary(
        paths.logs,
        about=service.about(),
        components=[comp],
        log_max_lines=DEFAULT_LOG_MAX_LINES,
        secret_tokens=(secret,),
        max_summary_bytes=900,
        now=datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert tight.truncated is True
    assert "体积上限" in tight.text
    assert secret not in tight.text

    # 可选探针抛异常时 LogService.about 与 list_sources 均 fail-open
    broken_service = LogService(
        paths=paths,
        get_log_max_lines=lambda: (_ for _ in ()).throw(RuntimeError("cfg err")),
        get_hardware_detail=lambda: (_ for _ in ()).throw(RuntimeError("gpu err")),
        get_ollama_version=lambda: (_ for _ in ()).throw(RuntimeError("ver err")),
    )
    assert broken_service.list_sources().log_max_lines == DEFAULT_LOG_MAX_LINES
    assert broken_service.about().ok is True

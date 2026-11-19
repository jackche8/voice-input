"""L4：systemd 单元渲染路径转义与隔离组合保留端口回归测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from _v3.isolated import FORBIDDEN_PORTS, compose_isolated_candidate
from _v3.production import ProductionPaths
from _v3.units import UNIT_NAMES, render_all, render_unit, unit_values


def test_units_render_quotes_exec_and_env_with_spaces_but_not_literal_path_directives():
    project_dir = Path("/home/user/my projects/ollama voice input")
    venv = Path("/home/user/my venv")
    cuda_libs = "/opt/cuda libs/lib64:/usr/local/my cuda/lib"
    paths = ProductionPaths.for_root(project_dir, live=True)

    rendered = render_all(project_dir=project_dir, venv=venv, cuda_libs=cuda_libs)
    assert set(rendered) == set(UNIT_NAMES)

    for name, text in rendered.items():
        assert "@" not in text, f"{name} 存在未替换占位符:\n{text}"

    asr = rendered["ollama-voice-asr.service"]
    assert f"ConditionPathExists={paths.asr_model_dir}/config.json" in asr
    assert f'ConditionPathExists="{paths.asr_model_dir}' not in asr
    assert f"ConditionPathExists={paths.asr_python}" in asr
    assert f'ConditionPathExists="{paths.asr_python}"' not in asr
    assert f"WorkingDirectory={paths.data_root}" in asr
    assert f'WorkingDirectory="{paths.data_root}"' not in asr
    assert f'Environment="HF_HOME={paths.hf_home}"' in asr
    assert f'ExecCondition="{venv}/bin/python3" "{paths.data_root}/_activation.py" check' in asr
    assert (
        f'ExecStart="{paths.asr_python}" -m runtimes.asr serve '
        f"--host 127.0.0.1 --port 17946 --backend auto --gpu-memory-utilization 0.45 "
        f'--max-model-len 4096 --load --warmup --model "{paths.asr_model_dir}"'
    ) in asr

    daemon = rendered["ollama-voice-daemon.service"]
    assert f"WorkingDirectory={paths.data_root}" in daemon
    assert f'WorkingDirectory="{paths.data_root}"' not in daemon
    assert f'Environment="LD_LIBRARY_PATH={cuda_libs}"' in daemon
    assert f'ExecCondition="{venv}/bin/python3" "{paths.data_root}/_activation.py" check' in daemon
    assert f'ExecStart="{venv}/bin/python3" "{paths.data_root}/daemon.py"' in daemon

    ollama = rendered["ollama-voice-ollama.service"]
    assert f"ConditionPathExists={paths.ollama_binary}" in ollama
    assert f'ConditionPathExists="{paths.ollama_binary}"' not in ollama
    assert f'Environment="OLLAMA_MODELS={paths.ollama_models}"' in ollama
    assert f'ExecCondition="{venv}/bin/python3" "{paths.data_root}/_activation.py" check' in ollama
    assert f'ExecStart="{paths.ollama_binary}" serve' in ollama

    server = rendered["ollama-voice-server.service"]
    assert f"WorkingDirectory={paths.data_root}" in server
    assert f'WorkingDirectory="{paths.data_root}"' not in server
    assert f'Environment="LD_LIBRARY_PATH={cuda_libs}"' in server
    assert f'ExecCondition="{venv}/bin/python3" "{paths.data_root}/_activation.py" check' in server
    exec_start_pre_lines = [
        line for line in server.splitlines() if line.startswith("ExecStartPre=") and "certs" in line
    ]
    assert len(exec_start_pre_lines) == 1
    assert exec_start_pre_lines[0].startswith(f'ExecStartPre="{venv}/bin/python3" ')
    assert f'"{paths.data_root}/certs"' in exec_start_pre_lines[0]
    assert (
        f'ExecStart="{venv}/bin/uvicorn" --factory app:create_production_app '
        f"--host 0.0.0.0 --port 17945 "
        f'--ssl-keyfile "{paths.data_root}/certs/key.pem" '
        f'--ssl-certfile "{paths.data_root}/certs/cert.pem"'
    ) in server


def test_units_render_plain_paths_without_spurious_quotes_and_escapes_dquotes():
    plain = render_all(
        project_dir=Path("/opt/voice"),
        venv=Path("/opt/voice/.venv"),
        cuda_libs="/opt/cuda/lib64",
    )
    assert "Environment=LD_LIBRARY_PATH=/opt/cuda/lib64" in plain["ollama-voice-server.service"]
    assert "ExecStart=/opt/voice/.venv/bin/python3 /opt/voice/daemon.py" in plain["ollama-voice-daemon.service"]
    assert "WorkingDirectory=/opt/voice" in plain["ollama-voice-daemon.service"]

    vals = unit_values(
        project_dir=Path('/opt/my "quoted" dir'),
        venv=Path("/opt/venv"),
        cuda_libs='/opt/cuda "a"/lib',
    )
    snippet = render_unit(
        "[Service]\nWorkingDirectory=@DIR@\n@CUDA_ENV@\nExecStart=@VENV@/bin/python3 @DIR@/daemon.py\n",
        vals,
    )
    assert 'WorkingDirectory=/opt/my "quoted" dir' in snippet
    assert 'Environment="LD_LIBRARY_PATH=/opt/cuda \\"a\\"/lib"' in snippet
    assert 'ExecStart=/opt/venv/bin/python3 "/opt/my \\"quoted\\" dir/daemon.py"' in snippet


def test_isolated_forbids_private_ollama_port_17947(tmp_path: Path):
    assert 17947 in FORBIDDEN_PORTS
    assert {17945, 17946, 17947, 11434}.issubset(FORBIDDEN_PORTS)
    with pytest.raises(ValueError, match="保留端口"):
        compose_isolated_candidate(root=tmp_path / "sb1", token="tok", bind_port=17947)
    with pytest.raises(ValueError, match="保留端口"):
        compose_isolated_candidate(
            root=tmp_path / "sb2",
            token="tok",
            ollama_base_url="http://127.0.0.1:17947",
        )

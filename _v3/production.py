"""生产运行时组合：显式路径 + 显式令牌 + 真实能力适配器。

只有生产入口（`_paths.default_app_paths` 给出的 live 路径与已读取的令牌）才会绑定 live 文件；
测试用隔离根目录 + 假令牌 + 假传输/假进程装配同一函数，验证接线而不碰用户数据。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from _asr import AsrSidecarClient
from _asr_provider import SidecarStreamProvider
from _audio_codec import decode_audio_to_pcm16k
from _config import ConfigStore, migrate_to_product_config, read_log_max_lines
from _contracts import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_TRANSLATE_LANG_PAIR,
    OLLAMA_VERSION,
    PRODUCT_ASR_MODEL,
    SIDECAR_DEFAULT_BASE_URL,
    AudioInputListResponse,
    OcrTextBlock,
    PolishRequest,
    PolishResponse,
    SetupTaskKind,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
)
from _events import EventBus
from _hotkey import HotkeyBindings
from _history import HistoryStore
from _history_service import HistoryService
from _jobs import JobStore
from _jobs_service import JobsService
from _linux.conflicts import scan_hotkey_conflicts
from _linux.evdev_keyboards import readable_keyboard_paths
from _logs import LogService
from _ocr import list_tesseract_languages
from _ocr_layout import extract_ocr_layout_with_meta, extract_ocr_text_with_meta
from _ocr_preprocess import render_ocr_preprocess_preview
from _ollama import OllamaClient
from _paths import INTERNAL_SETUP_WORKER_FLAG, PROGRAM_ROOT, STATIC_V3, AppPaths, default_app_paths, internal_subcommand
from _platform._common import list_audio_input_devices
from _platform.child_process import set_env_var
from _platform.system_proxy import SystemProxyReader, merge_proxy_env, read_os_system_proxy
from _polish import polish_text
from _ports import DEFAULT_SERVER_PORT, local_ports
from _session_finals import SessionFinalIndex
from _translate import translate_blocks as translate_ocr_blocks, translate_text
from _utterance import UtteranceService
from _v3.auth import AuthSettings
from _v3.component_routes import ComponentRegistry
from _v3.lan_access import LanAccessService, list_physical_lan_ipv4
from _v3.logs_routes import active_setup_log_names_for
from _v3.release_download import create_release_client
from _v3.runtime import Runtime
from _v3.runtime_install import (
    CachedHardwareProbe,
    asr_runtime_cleanup,
    asr_runtime_disk_budget,
    asr_runtime_install_action,
    detect_variant,
    probe_asr_hardware,
)
from _v3.setup_actions import (
    asr_download_action,
    ollama_asset,
    ollama_install_action,
    ollama_install_cleanup,
    ollama_pull_action,
)
from _v3.setup_probe import (
    asr_variant,
    dbus_portal_version,
    hotkey_conflicts_capability,
    probe_asr_weights,
    probe_capabilities,
    probe_ollama,
    session_type,
)
from _v3.setup_sequence import InstallSequence, SequenceFacts
from _v3.setup_service import SetupProbes, SetupService
from _v3.setup_tasks import GIB, SetupAction, SetupTaskManager, Spawn, TaskKindSpec, disk_shortfall, thread_spawn

ASR_DOWNLOAD_WORKER = PROGRAM_ROOT / "_v3" / "asr_download_worker.py"


@dataclass(frozen=True)
class ProductionEndpoints:
    port: int = DEFAULT_SERVER_PORT
    sidecar_url: str = SIDECAR_DEFAULT_BASE_URL
    ollama_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_version: str = OLLAMA_VERSION


@dataclass(frozen=True)
class SystemAdapters:
    """所有会碰系统的边界，生产给真实实现，测试给夹具。"""

    environ: Mapping[str, str]
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    which: Callable[[str], str | None] = shutil.which
    portal_version: Callable[[str], tuple[int | None, str | None]] = dbus_portal_version
    ocr_languages: Callable[[], list[str]] | None = None
    keyboard_devices: Callable[[], list[str]] = readable_keyboard_paths
    list_ips: Callable[[], list[str]] | None = None
    list_audio_inputs: Callable[[], AudioInputListResponse] | None = None
    http_transport: httpx.BaseTransport | None = None
    read_system_proxy: SystemProxyReader = read_os_system_proxy
    spawn: Spawn = thread_spawn


def _client(base_url: str, adapters: SystemAdapters, *, timeout: httpx.Timeout | float) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        transport=adapters.http_transport,
        follow_redirects=True,
        trust_env=False,
    )


def _model(config: ConfigStore) -> str:
    return config.load().ollama_model


def _ollama_probe(paths: AppPaths, endpoints: ProductionEndpoints, adapters: SystemAdapters, config: ConfigStore):
    def probe():
        with _client(endpoints.ollama_url, adapters, timeout=3.0) as client:
            return probe_ollama(binary=paths.ollama_binary, client=client, model=_model(config))

    return probe


def _noop() -> None:
    return None


def setup_task_actions(
    paths: AppPaths,
    endpoints: ProductionEndpoints,
    adapters: SystemAdapters,
    config: ConfigStore,
    *,
    on_replace_start: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[SetupTaskKind, SetupAction]:
    """安装 worker 进程里执行的真实动作表（服务端进程不执行动作，只拉起 worker）。"""
    download_env = merge_proxy_env(
        adapters.environ,
        os_name=paths.os_name,
        read_system_proxy=adapters.read_system_proxy,
    )
    set_env_var(download_env, "HF_HOME", str(paths.hf_home), os_name=paths.os_name, overwrite=True)
    set_env_var(download_env, "HF_HUB_DISABLE_PROGRESS_BARS", "1", os_name=paths.os_name, overwrite=True)
    set_env_var(download_env, "PYTHONUTF8", "1", os_name=paths.os_name, overwrite=True)
    long_read = httpx.Timeout(30.0, read=None)

    def release_client() -> httpx.Client:
        return create_release_client(
            environ=adapters.environ,
            os_name=paths.os_name,
            read_system_proxy=adapters.read_system_proxy,
            transport=adapters.http_transport,
        )

    return {
        SetupTaskKind.ollama_pull: lambda report: ollama_pull_action(
            client_factory=lambda: _client(endpoints.ollama_url, adapters, timeout=long_read),
            model=_model(config),
            ollama_host=endpoints.ollama_url,
        )(report),
        SetupTaskKind.ollama_install: ollama_install_action(
            version=endpoints.ollama_version,
            install_dir=paths.ollama_install_dir,
            binary=paths.ollama_binary,
            asset=ollama_asset(paths.os_name, platform.machine()),
            client_factory=release_client,
            run=adapters.run,
        ),
        SetupTaskKind.asr_runtime_install: asr_runtime_install_action(
            paths=paths,
            os_name=paths.os_name,
            machine=platform.machine(),
            variant=lambda: detect_variant(
                paths.os_name,
                platform.machine(),
                run=adapters.run,
                which=adapters.which,
                environ=adapters.environ,
            ),
            environ=adapters.environ,
            client_factory=release_client,
            run=adapters.run,
            popen=adapters.popen,
            read_system_proxy=adapters.read_system_proxy,
            on_replace_start=on_replace_start,
            cancel_event=cancel_event,
        ),
        SetupTaskKind.asr_download: asr_download_action(
            python=paths.asr_python,
            worker=ASR_DOWNLOAD_WORKER,
            repo_id=PRODUCT_ASR_MODEL,
            model_dir=paths.asr_model_dir,
            log_path=None,
            env=download_env,
            popen=adapters.popen,
        ),
    }


def setup_task_kinds(paths: AppPaths, adapters: SystemAdapters) -> dict[SetupTaskKind, TaskKindSpec]:
    """每类任务的磁盘预检（目标目录、所需字节）与服务端重试前清理（asr_runtime_install 清理下沉至 worker 替换阶段）。"""

    def runtime_disk() -> tuple[Path, int]:
        chosen, _why = detect_variant(
            paths.os_name,
            platform.machine(),
            run=adapters.run,
            which=adapters.which,
            environ=adapters.environ,
        )
        return asr_runtime_disk_budget(paths, paths.os_name, chosen)

    return {
        SetupTaskKind.ollama_install: TaskKindSpec(disk=lambda: (paths.ollama_install_dir, 2 * GIB),
                                                   cleanup=lambda: ollama_install_cleanup(paths.ollama_install_dir)),
        SetupTaskKind.ollama_pull: TaskKindSpec(disk=lambda: (paths.ollama_models, 4 * GIB)),
        SetupTaskKind.asr_runtime_install: TaskKindSpec(disk=runtime_disk, cleanup=_noop),
        SetupTaskKind.asr_download: TaskKindSpec(disk=lambda: (paths.asr_model_dir, 5 * GIB)),
    }


def setup_worker_kind(
    kind: SetupTaskKind,
    environ: Mapping[str, str],
) -> tuple[SetupAction, Callable[[], None], threading.Event]:
    """安装 worker 进程入口用：按与服务端相同的 environ 定位 live 路径与端口，取该类任务的动作、条件清理与取消事件。"""
    paths = default_app_paths(environ)
    ports = local_ports(environ)
    adapters = SystemAdapters(environ=environ)
    endpoints = ProductionEndpoints(port=ports.server, sidecar_url=ports.asr_url, ollama_url=ports.ollama_url)
    cancel_event = threading.Event()
    if kind == SetupTaskKind.asr_runtime_install:
        replacing = threading.Event()
        action = setup_task_actions(
            paths,
            endpoints,
            adapters,
            ConfigStore(paths.config),
            on_replace_start=replacing.set,
            cancel_event=cancel_event,
        )[kind]
        return action, (lambda: asr_runtime_cleanup(paths) if replacing.is_set() else None), cancel_event
    action = setup_task_actions(paths, endpoints, adapters, ConfigStore(paths.config))[kind]
    return action, setup_task_kinds(paths, adapters)[kind].cleanup, cancel_event


def _setup_service(
    paths: AppPaths,
    endpoints: ProductionEndpoints,
    adapters: SystemAdapters,
    config: ConfigStore,
    components: ComponentRegistry,
    events: EventBus | None = None,
    cached_hardware: CachedHardwareProbe | None = None,
    raw_hardware_probe: Callable[[], object] | None = None,
) -> SetupService:
    ollama_probe = _ollama_probe(paths, endpoints, adapters, config)

    def hotkey_scan():
        cfg = config.load()
        bindings = HotkeyBindings(
            voice=cfg.hotkey_voice,
            screenshot=cfg.hotkey_screenshot,
            repeat=cfg.hotkey_repeat,
            ocr=cfg.hotkey_ocr,
            annotate=cfg.hotkey_annotate,
        )
        return scan_hotkey_conflicts(bindings, run=adapters.run, which=adapters.which)

    ocr_languages = adapters.ocr_languages or list_tesseract_languages

    def capabilities(session: str):
        if paths.os_name == "windows":
            from _platform.windows import WindowsPlatformAdapter

            return WindowsPlatformAdapter(which=adapters.which, ocr_languages=ocr_languages).capabilities()
        if paths.os_name == "macos":
            from _platform.macos import MacOSPlatformAdapter

            return MacOSPlatformAdapter(temp_dir=paths.temp_dir, ocr_languages=ocr_languages).capabilities()
        return [
            *probe_capabilities(
                session=session,
                which=adapters.which,
                portal_version=adapters.portal_version,
                ocr_languages=ocr_languages,
                keyboard_devices=adapters.keyboard_devices,
            ),
            hotkey_conflicts_capability(hotkey_scan),
        ]

    if raw_hardware_probe is None:
        def raw_hardware_probe():
            return probe_asr_hardware(
                paths.os_name,
                platform.machine(),
                run=adapters.run,
                which=adapters.which,
                environ=adapters.environ,
            )

    if cached_hardware is None:
        cached_hardware = CachedHardwareProbe(raw_hardware_probe)
    probes = SetupProbes(
        session=lambda: session_type(adapters.environ, os_name=paths.os_name),
        ollama=ollama_probe,
        asr_weights=lambda: probe_asr_weights(paths.asr_model_dir),
        capabilities=capabilities,
        asr_variant=lambda: asr_variant(paths),
        asr_hardware=cached_hardware.get,
    )
    tasks = SetupTaskManager(
        kinds=setup_task_kinds(paths, adapters),
        tasks_dir=paths.setup_tasks,
        log_dir=paths.logs,
        worker_argv=internal_subcommand(INTERNAL_SETUP_WORKER_FLAG, ("-m", "_v3.setup_worker")),
        owner_pid=os.getppid(),  # 看护层拉起的服务端的父进程即应用主进程（macOS 为随其退出的包装进程）
        environ=adapters.environ,
        popen=adapters.popen,
        log_max_lines=lambda: read_log_max_lines(paths.config),
    )
    facts = SequenceFacts(
        ollama_installed=paths.ollama_binary.is_file,
        ollama_has_model=lambda: ollama_probe().has_model,
        asr_runtime_installed=paths.asr_manifest.is_file,
        asr_weights_complete=lambda: probe_asr_weights(paths.asr_model_dir).complete,
        asr_hardware_probe=raw_hardware_probe,
        asr_installed_variant=lambda: asr_variant(paths),
        asr_runtime_on_disk=lambda: paths.asr_manifest.exists() or paths.asr_venv.exists(),
        asr_disk_shortfall=lambda target: disk_shortfall(*asr_runtime_disk_budget(paths, paths.os_name, target)),
        invalidate_hardware_cache=cached_hardware.invalidate,
    )
    sequence = InstallSequence(
        tasks=tasks,
        facts=facts,
        components=components,
        events=events,
        state_file=paths.data_root / "setup" / "install_all.json",
        spawn=adapters.spawn,
    )
    return SetupService(probes=probes, tasks=tasks, sequence=sequence, components=components)


def compose_production_runtime(
    *,
    paths: AppPaths,
    token: str,
    adapters: SystemAdapters,
    endpoints: ProductionEndpoints = ProductionEndpoints(),
) -> Runtime:
    """组合生产运行时并执行启动恢复（claimed → delivery_unknown，不重投）。不启动 HTTP 服务。"""
    if not isinstance(paths, AppPaths):
        raise TypeError("必须显式传入 AppPaths")
    if not token or not token.strip():
        raise ValueError("必须显式传入访问令牌")
    paths.upload_tmp.mkdir(parents=True, exist_ok=True)
    components = ComponentRegistry()  # 看护层上报的唯一缓存：设置检查与一键装齐都读它
    config = ConfigStore(paths.config)
    migrate_to_product_config(config)
    events = EventBus()
    history = HistoryService(HistoryStore(paths.history), environ=adapters.environ)
    jobs = JobsService(JobStore(paths.jobs), events, claim_lease_s=60.0)
    jobs.recover()
    finals = SessionFinalIndex()

    asr = AsrSidecarClient(base_url=endpoints.sidecar_url, transport=adapters.http_transport)
    ollama = OllamaClient(base_url=endpoints.ollama_url, transport=adapters.http_transport)

    def transcribe(audio: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
        return asr.transcribe(
            audio,
            language=language,
            context=context,
            filename="audio.pcm",
            content_type="audio/pcm",
        )

    def polish(request: PolishRequest, *, model: str | None = None) -> PolishResponse:
        return polish_text(request, model=model or config.load().ollama_model, client=ollama)

    def translate(request: TranslateRequest) -> TranslateResponse:
        return translate_text(request, model=config.load().ollama_model, client=ollama)

    def translate_blocks(
        blocks: list[OcrTextBlock],
        *,
        source_lang: str,
        target_lang: str,
        lang_pair: str = DEFAULT_TRANSLATE_LANG_PAIR,
    ) -> list[OcrTextBlock]:
        return translate_ocr_blocks(
            blocks,
            source_lang=source_lang,
            target_lang=target_lang,
            lang_pair=lang_pair,
            model=config.load().ollama_model,
            client=ollama,
        )

    utterance = UtteranceService(
        jobs=jobs,
        history=history,
        events=events,
        config=config,
        finals=finals,
        polish=polish,
        transcribe=transcribe,
    )
    list_ips = adapters.list_ips or (lambda: list_physical_lan_ipv4(run=adapters.run, os_name=paths.os_name))
    list_audio_inputs = adapters.list_audio_inputs or (
        lambda: list_audio_input_devices(refresh=True, os_name=paths.os_name)
    )

    def raw_hardware_probe():
        return probe_asr_hardware(
            paths.os_name,
            platform.machine(),
            run=adapters.run,
            which=adapters.which,
            environ=adapters.environ,
        )

    cached_hardware = CachedHardwareProbe(raw_hardware_probe)
    setup = _setup_service(
        paths,
        endpoints,
        adapters,
        config,
        components,
        events=events,
        cached_hardware=cached_hardware,
        raw_hardware_probe=raw_hardware_probe,
    )
    logs = LogService(
        paths=paths,
        get_log_max_lines=lambda: read_log_max_lines(paths.config),
        get_secret_tokens=lambda: (token,),
        get_components=components.snapshot,
        get_active_setup_logs=lambda: active_setup_log_names_for(setup),
        get_hardware_detail=lambda: cached_hardware.get().detail,
        get_ollama_version=lambda: endpoints.ollama_version,
        get_ollama_model=lambda: _model(config),
        session_type=session_type(adapters.environ, os_name=paths.os_name),
    )
    return Runtime(
        config=config,
        history=history,
        jobs=jobs,
        events=events,
        finals=finals,
        utterance=utterance,
        auth=AuthSettings(token=token, port=endpoints.port),
        transcribe=transcribe,
        polish=polish,
        translate=translate,
        translate_blocks=translate_blocks,
        ocr=extract_ocr_text_with_meta,
        ocr_layout=extract_ocr_layout_with_meta,
        ocr_preview=render_ocr_preprocess_preview,
        decode_audio=decode_audio_to_pcm16k,
        stream_provider=SidecarStreamProvider(asr),
        setup=setup,
        components=components,
        lan=LanAccessService(token=token, list_ips=list_ips, port=endpoints.port),
        list_audio_inputs=list_audio_inputs,
        logs=logs,
        about=logs.about,
        upload_tmp=paths.upload_tmp,
    )


def build_production_app(
    *,
    environ: Mapping[str, str],
    static_v3: Path = STATIC_V3,
):
    """唯一生产装配：按 environ 定位的 live 路径 + 读/建令牌 + 真实适配器 + 唯一界面 static/v3。"""
    from _v3.lan_access import load_or_create_token
    from _v3_app import create_app

    paths = default_app_paths(environ)
    ports = local_ports(environ)
    paths.config_root.mkdir(mode=0o700, parents=True, exist_ok=True)  # 首次运行：令牌与证书所在目录只给本用户
    token = load_or_create_token(paths.token)
    adapters = SystemAdapters(environ=environ, ocr_languages=list_tesseract_languages)
    endpoints = ProductionEndpoints(port=ports.server, sidecar_url=ports.asr_url, ollama_url=ports.ollama_url)
    runtime = compose_production_runtime(paths=paths, token=token, adapters=adapters, endpoints=endpoints)
    return create_app(runtime, static_v3=static_v3), paths

"""跨平台适配层公共原子能力（Logic 层，严禁依赖 `_linux.*` 或任何平台专属库）。

提供：
- `png_dimensions`：无 PIL 读取 PNG 文件头宽高；
- `refresh_audio_devices` / `list_audio_input_devices` / `resolve_audio_input_device`：
  跨平台热插拔 PortAudio 刷新、Host API 择优列举与 3 级设备解析；
- `SoundDeviceSource` & `play_beep`：跨平台线程安全麦克风源与提示音（严格锁序 `_BEEP_LOCK -> _AUDIO_LOCK`）；
- `check_focus_and_self_guard`：三端统一的焦点/自窗口注入护栏；
- `probe_ocr_capabilities`：三端统一的 OCR 语言包能力自检。
"""
from __future__ import annotations

import struct
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from _capture import SAMPLE_RATE, PcmSink
from _contracts import (
    AudioInputDevice,
    AudioInputListResponse,
    ErrorCode,
    LogicError,
    SetupCapability,
)
from _ocr import default_ocr_languages
from _platform.detect import detect_os
from _platform.protocol import FocusTarget, PasteResult

DEFAULT_BEEP_SAMPLE_RATE = SAMPLE_RATE
_AUDIO_LOCK = threading.Lock()
_BEEP_LOCK = threading.Lock()
_ACTIVE_INPUT_STREAMS = 0

_PREFIX_MATCH_LEN = 31
_EXCLUDED_HOSTAPIS = frozenset({"windows wdm-ks"})
_PSEUDO_INPUT_NAMES = frozenset(
    {
        "microsoft sound mapper - input",
        "primary sound capture driver",
        "default source",
        "default",
        "sysdefault",
        "pulse",
        "pipewire",
    }
)
_ALSA_VIRTUAL_PLUGINS = frozenset(
    {
        "lavrate",
        "samplerate",
        "speexrate",
        "speex",
        "upmix",
        "vdownmix",
        "dmix",
        "dsnoop",
        "null",
    }
)


def _load_sounddevice(sounddevice_mod=None):
    if sounddevice_mod is not None:
        return sounddevice_mod
    import sounddevice as sd

    return sd


def _is_valid_index(idx: object, total: int) -> bool:
    return isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < total


def _safe_default_input_index(sd) -> int | None:
    default_obj = getattr(sd, "default", None)
    pair = getattr(default_obj, "device", None)
    if pair is None:
        return None
    try:
        idx = pair[0]
    except (TypeError, IndexError, KeyError):
        return None
    if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
        return idx
    return None


def _safe_default_hostapi_index(sd) -> int | None:
    default_obj = getattr(sd, "default", None)
    idx = getattr(default_obj, "hostapi", None)
    if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0:
        return idx
    return None


def _hostapi_name(hostapis: list, ha_idx: object) -> str:
    if not _is_valid_index(ha_idx, len(hostapis)):
        return ""
    return str(hostapis[int(ha_idx)].get("name") or "").strip()


def _is_excluded_hostapi(name: str) -> bool:
    return name.strip().lower() in _EXCLUDED_HOSTAPIS


def _is_input_device(dev: dict) -> bool:
    try:
        return int(dev.get("max_input_channels") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_real_input_device(dev: dict, *, os_name: str, hostapi_name: str) -> bool:
    if not _is_input_device(dev):
        return False
    name = str(dev.get("name") or "").strip()
    if not name:
        return False
    low = name.lower()
    if low in _PSEUDO_INPUT_NAMES:
        return False
    if os_name == "linux":
        if low.endswith(".monitor"):
            return False
        if hostapi_name.strip().lower() == "alsa" and low in _ALSA_VIRTUAL_PLUGINS:
            return False
    return True


def _hostapi_has_raw_devices(ha: dict, ha_idx: int, devices: list) -> bool:
    if "devices" in ha:
        return bool(ha.get("devices"))
    return any(dev.get("hostapi") == ha_idx for dev in devices)


def _ordered_hostapi_indices(hostapis: list, default_ha: int | None, *, os_name: str) -> list[int]:
    """返回排除 WDM-KS 后的偏好 Host API 索引顺序（无重复）。"""
    order: list[int] = []
    seen: set[int] = set()

    def _add(idx: int | None) -> None:
        if idx is None or idx in seen or not _is_valid_index(idx, len(hostapis)):
            return
        if _is_excluded_hostapi(_hostapi_name(hostapis, idx)):
            return
        seen.add(idx)
        order.append(idx)

    def _add_by_name(target_lower: str) -> None:
        for i, ha in enumerate(hostapis):
            if str(ha.get("name") or "").strip().lower() == target_lower:
                _add(i)

    if os_name == "windows":
        _add_by_name("windows directsound")
        _add_by_name("mme")
        _add(default_ha)
        _add_by_name("windows wasapi")
    elif os_name == "macos":
        _add_by_name("core audio")
        _add(default_ha)
    else:
        _add_by_name("pulseaudio")
        _add(default_ha)
        _add_by_name("alsa")

    for i in range(len(hostapis)):
        _add(i)
    return order


def _real_inputs_in_hostapi(
    devices: list, ha_idx: int, hostapi_name: str, *, os_name: str
) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for dev_idx, dev in enumerate(devices):
        if dev.get("hostapi") != ha_idx:
            continue
        if _is_real_input_device(dev, os_name=os_name, hostapi_name=hostapi_name):
            items.append((dev_idx, str(dev.get("name") or "").strip()))
    return items


def _select_hostapi_and_inputs(
    devices: list, hostapis: list, default_ha: int | None, *, os_name: str
) -> tuple[int | None, list[tuple[int, str]], list[int]]:
    ha_order = _ordered_hostapi_indices(hostapis, default_ha, os_name=os_name)
    if os_name == "linux":
        for ha_idx in ha_order:
            ha_name = _hostapi_name(hostapis, ha_idx)
            if ha_name.lower() == "pulseaudio" and _hostapi_has_raw_devices(hostapis[ha_idx], ha_idx, devices):
                return ha_idx, _real_inputs_in_hostapi(devices, ha_idx, ha_name, os_name=os_name), ha_order

    for ha_idx in ha_order:
        items = _real_inputs_in_hostapi(devices, ha_idx, _hostapi_name(hostapis, ha_idx), os_name=os_name)
        if items:
            return ha_idx, items, ha_order
    return (ha_order[0] if ha_order else None), [], ha_order


def _refresh_audio_devices_locked(sd) -> bool:
    """刷新 PortAudio 设备表与系统默认设备；调用方必须已按 `_BEEP_LOCK -> _AUDIO_LOCK` 持锁。"""
    if _ACTIVE_INPUT_STREAMS > 0:
        return False
    stop_fn = getattr(sd, "stop", None)
    if callable(stop_fn):
        try:
            stop_fn()
        except Exception:
            pass
    terminate_fn = getattr(sd, "_terminate", None)
    initialize_fn = getattr(sd, "_initialize", None)
    if not (callable(terminate_fn) and callable(initialize_fn)):
        return False
    # 兼容真实 sounddevice（带 _initialized 引用计数）与无计数测试替身，防止 _terminate 抛错时计数泄漏
    has_counter = hasattr(sd, "_initialized")
    terminated = False
    try:
        if not has_counter or int(getattr(sd, "_initialized", 0)) > 0:
            terminate_fn()
            terminated = True
    finally:
        if has_counter:
            if int(getattr(sd, "_initialized", 0)) <= 0:
                initialize_fn()
        elif terminated:
            initialize_fn()
    return True


def refresh_audio_devices(sd=None) -> bool:
    """无活跃录音流时安全重扫 PortAudio 设备表与系统默认设备。"""
    mod = _load_sounddevice(sd)
    with _BEEP_LOCK:
        with _AUDIO_LOCK:
            return _refresh_audio_devices_locked(mod)


def _resolve_display_default_name(
    sd,
    devices: list,
    hostapis: list,
    selected_ha_idx: int | None,
    *,
    os_name: str,
) -> str | None:
    candidate_indices: list[int | None] = []
    if selected_ha_idx is not None and _is_valid_index(selected_ha_idx, len(hostapis)):
        ha_def = hostapis[selected_ha_idx].get("default_input_device")
        if isinstance(ha_def, int) and not isinstance(ha_def, bool) and ha_def >= 0:
            candidate_indices.append(ha_def)
    candidate_indices.append(_safe_default_input_index(sd))
    if os_name == "windows":
        for ha in hostapis:
            if str(ha.get("name") or "").strip().lower() == "windows wasapi":
                wasapi_def = ha.get("default_input_device")
                if isinstance(wasapi_def, int) and not isinstance(wasapi_def, bool) and wasapi_def >= 0:
                    candidate_indices.append(wasapi_def)

    for idx in candidate_indices:
        if idx is None or not _is_valid_index(idx, len(devices)):
            continue
        dev = devices[idx]
        dev_ha_name = _hostapi_name(hostapis, dev.get("hostapi"))
        if _is_excluded_hostapi(dev_ha_name):
            continue
        if _is_real_input_device(dev, os_name=os_name, hostapi_name=dev_ha_name):
            return str(dev.get("name") or "").strip()
    return None


def _list_audio_input_devices_locked(sd, *, target_os: str) -> AudioInputListResponse:
    devices = list(sd.query_devices())
    query_hostapis = getattr(sd, "query_hostapis", None)
    hostapis = list(query_hostapis()) if callable(query_hostapis) else []
    default_ha = _safe_default_hostapi_index(sd)

    if hostapis:
        selected_ha_idx, selected_items, _ = _select_hostapi_and_inputs(
            devices, hostapis, default_ha, os_name=target_os
        )
        selected_ha_name = _hostapi_name(hostapis, selected_ha_idx) if selected_ha_idx is not None else ""
    else:
        selected_ha_idx = None
        selected_ha_name = ""
        selected_items = [
            (idx, str(dev.get("name") or "").strip())
            for idx, dev in enumerate(devices)
            if _is_real_input_device(dev, os_name=target_os, hostapi_name="")
        ]

    if not selected_items:
        return AudioInputListResponse(default_device=None, devices=[])

    selected_ha_default_idx = (
        hostapis[selected_ha_idx].get("default_input_device")
        if selected_ha_idx is not None and _is_valid_index(selected_ha_idx, len(hostapis))
        else None
    )
    dedup_order: list[str] = []
    dedup_default: dict[str, bool] = {}
    for dev_idx, clean_name in selected_items:
        is_direct_default = (
            isinstance(selected_ha_default_idx, int)
            and not isinstance(selected_ha_default_idx, bool)
            and dev_idx == selected_ha_default_idx
        )
        if clean_name not in dedup_default:
            dedup_order.append(clean_name)
            dedup_default[clean_name] = is_direct_default
        elif is_direct_default:
            dedup_default[clean_name] = True

    default_name = _resolve_display_default_name(
        sd, devices, hostapis, selected_ha_idx, os_name=target_os
    )
    if default_name is not None:
        if default_name in dedup_default:
            dedup_default[default_name] = True
        elif len(default_name) >= _PREFIX_MATCH_LEN:
            prefix = default_name[:_PREFIX_MATCH_LEN]
            prefix_hits = [
                name for name in dedup_order if len(name) >= _PREFIX_MATCH_LEN and name[:_PREFIX_MATCH_LEN] == prefix
            ]
            if len(prefix_hits) == 1:
                dedup_default[prefix_hits[0]] = True

    out_devices = [
        AudioInputDevice(name=name, hostapi=selected_ha_name, is_default=dedup_default[name])
        for name in dedup_order
    ]
    return AudioInputListResponse(default_device=default_name, devices=out_devices)


def list_audio_input_devices(
    *,
    refresh: bool = True,
    sounddevice_mod=None,
    os_name: str | None = None,
) -> AudioInputListResponse:
    """跨平台枚举当前可用麦克风（择优单一 Host API、过滤伪默认与回环源）；异常显式抛 LogicError。"""
    try:
        sd = _load_sounddevice(sounddevice_mod)
        target_os = detect_os() if os_name is None else os_name
        with _BEEP_LOCK:
            with _AUDIO_LOCK:
                if refresh:
                    _refresh_audio_devices_locked(sd)
                return _list_audio_input_devices_locked(sd, target_os=target_os)
    except LogicError:
        raise
    except Exception as exc:
        raise LogicError(
            ErrorCode.dependency_failed,
            f"枚举音频输入设备失败: {exc}",
        ) from exc


def _resolve_audio_input_device_locked(
    sd,
    device_name: str,
    *,
    os_name: str | None = None,
) -> int | None:
    target = (device_name or "").strip()
    target_os = detect_os() if os_name is None else os_name
    query_devices = getattr(sd, "query_devices", None)
    if not callable(query_devices):
        if not target:
            return None
        raise RuntimeError(f"已选麦克风未连接（{target[:32]}），请重新插上或切回「跟随系统默认」")

    devices = list(query_devices())
    query_hostapis = getattr(sd, "query_hostapis", None)
    hostapis = list(query_hostapis()) if callable(query_hostapis) else []
    default_ha = _safe_default_hostapi_index(sd)

    if not target:
        default_in = _safe_default_input_index(sd)
        if default_in is not None and _is_valid_index(default_in, len(devices)) and _is_input_device(devices[default_in]):
            return None
        if hostapis:
            selected_ha_idx, selected_items, _ = _select_hostapi_and_inputs(
                devices, hostapis, default_ha, os_name=target_os
            )
            if selected_ha_idx is not None and selected_items:
                ha_def = hostapis[selected_ha_idx].get("default_input_device")
                if _is_valid_index(ha_def, len(devices)) and _is_input_device(devices[ha_def]):
                    return int(ha_def)
                return selected_items[0][0]
        else:
            for dev_idx, dev in enumerate(devices):
                if _is_real_input_device(dev, os_name=target_os, hostapi_name=""):
                    return dev_idx
        raise RuntimeError("未检测到可用麦克风，请插入麦克风后重试")

    groups: list[list[tuple[int, str]]] = []
    if hostapis:
        selected_ha_idx, _, ha_order = _select_hostapi_and_inputs(
            devices, hostapis, default_ha, os_name=target_os
        )
        ordered_has: list[int] = []
        if selected_ha_idx is not None:
            ordered_has.append(selected_ha_idx)
        for ha_idx in ha_order:
            if ha_idx != selected_ha_idx:
                ordered_has.append(ha_idx)
        for ha_idx in ordered_has:
            groups.append(_real_inputs_in_hostapi(devices, ha_idx, _hostapi_name(hostapis, ha_idx), os_name=target_os))
    else:
        groups.append(
            [
                (dev_idx, str(dev.get("name") or "").strip())
                for dev_idx, dev in enumerate(devices)
                if _is_real_input_device(dev, os_name=target_os, hostapi_name="")
            ]
        )

    # ① 偏好 Host API 精确匹配 -> ② 其余非 WDM-KS Host API 精确匹配
    for group in groups:
        for dev_idx, cand_name in group:
            if cand_name == target:
                return dev_idx

    # ③ 逐个 Host API 独立唯一 31 字前缀匹配（应对 Windows MME 31 字截断，同 Host API 内 ≥2 歧义显式报错）
    if len(target) >= _PREFIX_MATCH_LEN:
        prefix = target[:_PREFIX_MATCH_LEN]
        for group in groups:
            hits = [
                dev_idx
                for dev_idx, cand_name in group
                if len(cand_name) >= _PREFIX_MATCH_LEN and cand_name[:_PREFIX_MATCH_LEN] == prefix
            ]
            if len(hits) == 1:
                return hits[0]
            if len(hits) >= 2:
                raise RuntimeError(
                    f"已选麦克风名称前31字存在歧义（{target[:28]}），请改选唯一设备或切回「跟随系统默认」"
                )

    raise RuntimeError(f"已选麦克风未连接（{target[:32]}），请重新插上或切回「跟随系统默认」")


def resolve_audio_input_device(
    device_name: str = "",
    *,
    sounddevice_mod=None,
    os_name: str | None = None,
) -> int | None:
    """将配置中的麦克风名称解析为 PortAudio 设备索引（空串跟随系统默认返回 None 或偏好默认索引）。"""
    sd = _load_sounddevice(sounddevice_mod)
    with _AUDIO_LOCK:
        return _resolve_audio_input_device_locked(sd, device_name, os_name=os_name)


class SoundDeviceSource:
    """跨平台 16k 单声道 int16 麦克风输入流；与 `play_beep` 共享锁避免 PortAudio 冲突并支持热插拔刷新。"""

    def __init__(
        self,
        *,
        samplerate: int = SAMPLE_RATE,
        sounddevice_mod=None,
        device: str | None = None,
        device_provider: Callable[[], str] | None = None,
        os_name: str | None = None,
    ) -> None:
        self.samplerate = samplerate
        self._sd = sounddevice_mod
        self._device = device
        self._device_provider = device_provider
        self._os_name = os_name
        self._stream = None

    def _module(self):
        return _load_sounddevice(self._sd)

    def _desired_device_name(self) -> str:
        if self._device_provider is not None:
            raw = self._device_provider()
            return "" if raw is None else str(raw).strip()
        return "" if self._device is None else str(self._device).strip()

    def start(self, sink: PcmSink) -> None:
        global _ACTIVE_INPUT_STREAMS
        # 锁外读取目标设备名，防止配置锁与音频锁交叉嵌套
        desired_device = self._desired_device_name()
        sd = self._module()

        def callback(indata, frames, time_info, status):
            del frames, time_info, status
            sink(bytes(indata))

        with _BEEP_LOCK:
            with _AUDIO_LOCK:
                _refresh_audio_devices_locked(sd)
                resolved = _resolve_audio_input_device_locked(
                    sd, desired_device, os_name=self._os_name
                )
                stream = sd.RawInputStream(
                    samplerate=self.samplerate,
                    channels=1,
                    dtype="int16",
                    device=resolved,
                    callback=callback,
                )
                try:
                    stream.start()
                except Exception:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise
                self._stream = stream
                _ACTIVE_INPUT_STREAMS += 1

    def stop(self) -> None:
        global _ACTIVE_INPUT_STREAMS
        with _AUDIO_LOCK:
            stream = self._stream
            self._stream = None
            if stream is None:
                return
            # 流关闭异常（设备热拔 / PipeWire 重启）绝不能向上抛：否则调用方其后的修饰键释放被跳过
            try:
                try:
                    stream.stop()
                finally:
                    stream.close()
            except Exception as exc:
                print(f"[mic] 关闭音频流异常: {exc}", file=sys.stderr)
            finally:
                _ACTIVE_INPUT_STREAMS = max(0, _ACTIVE_INPUT_STREAMS - 1)


def noop() -> None:
    return None


def always_false() -> bool:
    return False


def png_dimensions(path: str | Path) -> tuple[int, int]:
    """从 PNG 文件头读取图片宽高（无需 PIL），无效 PNG 文件头显式抛出 ValueError。"""
    with open(path, "rb") as fh:
        header = fh.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"非有效 PNG 文件头: {path}")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def play_beep(
    freq: float = 440,
    duration: float = 0.1,
    volume: float = 0.3,
    *,
    background: bool = True,
    sounddevice_mod=None,
    sample_rate: int = DEFAULT_BEEP_SAMPLE_RATE,
) -> None:
    """跨平台线程安全提示音（基于 `sounddevice` + `numpy` + 串行锁，不依赖 `_linux`）。"""

    def _worker() -> None:
        try:
            import numpy as np

            sd = sounddevice_mod
            if sd is None:
                import sounddevice as sd

            frames = max(1, int(sample_rate * duration))
            t = np.linspace(0, duration, frames, endpoint=False)
            wave = volume * np.sin(2 * np.pi * freq * t).astype(np.float32)
            fade = min(int(sample_rate * 0.01), len(wave) // 4)
            if fade > 0:
                wave[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
                wave[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
            with _BEEP_LOCK:
                with _AUDIO_LOCK:
                    stop_fn = getattr(sd, "stop", None)
                    if callable(stop_fn):
                        try:
                            stop_fn()
                        except Exception:
                            pass
                    sd.play(wave, sample_rate)
                wait_fn = getattr(sd, "wait", None)
                if callable(wait_fn):
                    try:
                        wait_fn()
                    except Exception:
                        pass
        except Exception:
            pass  # 提示音属非关键辅助反馈，失败不中断主链路

    if background:
        threading.Thread(target=_worker, daemon=True).start()
    else:
        _worker()


def check_focus_and_self_guard(
    target: FocusTarget | None,
    live: FocusTarget | None = None,
) -> PasteResult | None:
    """公共焦点/自窗口注入护栏：命中阻断返回 `PasteResult(ok=False, uncertain=False)`，放行返回 `None`。"""
    if target is not None and target.is_self:
        return PasteResult(
            ok=False,
            method="self_window_blocked",
            detail="目标焦点为自身窗口，已拒绝注入",
            uncertain=False,
        )
    if live is None:
        return None
    if live.is_self:
        return PasteResult(
            ok=False,
            method="self_window_blocked",
            detail="当前焦点位于自身窗口，已拒绝注入",
            uncertain=False,
        )
    if target is not None and target.focus_id and live.focus_id and target.focus_id != live.focus_id:
        return PasteResult(
            ok=False,
            method="focus_mismatch",
            detail="焦点与任务绑定时不一致，未注入",
            uncertain=False,
        )
    return None


def probe_ocr_capabilities(
    ocr_languages: Callable[[], Iterable[str]] | None,
    *,
    missing_prefix: str = "tesseract 缺少",
) -> list[SetupCapability]:
    """公共 OCR 语言包能力自检（默认识别语言逐项，见 `_ocr.default_ocr_languages`）。"""
    if ocr_languages is None:
        return []
    try:
        langs = set(ocr_languages())
        err: str | None = None
    except Exception as exc:
        langs, err = set(), str(exc)
    caps: list[SetupCapability] = []
    for code, label in default_ocr_languages():
        caps.append(
            SetupCapability(
                id=f"ocr_{code}",
                label=label,
                available=code in langs,
                required=True,
                detail=err or (None if code in langs else f"{missing_prefix} {code} 语言包"),
            )
        )
    return caps

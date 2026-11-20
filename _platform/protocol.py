"""跨平台桌面适配层统一协议与数据模型（Logic 层，无业务规则与 UI 依赖）。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from _contracts import SetupCapability
from _hotkey import HotkeyBindings

OsName = Literal["linux", "windows", "macos"]
VirtualRect = tuple[int, int, int, int]


@dataclass(frozen=True)
class ScreenCapture:
    """整屏（含多显示器虚拟桌面）抓屏快照：`virtual_rect` 为 `(left, top, width, height)`。"""

    png_bytes: bytes
    virtual_rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class FocusTarget:
    """焦点窗口快照。`focus_id` 为空串代表当前会话无法精确读取窗口句柄（如纯原生 Wayland）。"""

    focus_id: str
    app_id: str
    is_terminal: bool
    is_self: bool


@dataclass(frozen=True)
class PasteResult:
    """文本注入/粘贴结果。

    - `ok=True`：已证明文本送达输入法或粘贴热键完整发出（对应 `complete`）。
    - `ok=False, uncertain=False`：已证明未发生任何输出（如自窗口拦截、焦点变化或剪贴板写失败，对应 `none`）。
    - `ok=False, uncertain=True`：按键发送途中异常，不能证明未输出（对应 `partial_or_unknown`）。
    """

    ok: bool
    method: str
    detail: str
    uncertain: bool = False

    @property
    def ack_result(self) -> Literal["complete", "none", "partial_or_unknown"]:
        if self.ok:
            return "complete"
        if self.uncertain:
            return "partial_or_unknown"
        return "none"


@dataclass(frozen=True)
class HotkeyCallbacks:
    on_voice_start: Callable[[], bool]
    on_voice_stop: Callable[[], None]
    on_screenshot: Callable[[], None]
    on_ocr: Callable[[], None] = lambda: None
    on_repeat: Callable[[], None] = lambda: None
    overlay_active: Callable[[], bool] = lambda: False
    close_overlay: Callable[[], bool] = lambda: False


@runtime_checkable
class HotkeyRig(Protocol):
    backend_name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def reset_voice(self) -> None: ...


@runtime_checkable
class PlatformAdapter(Protocol):
    os_name: OsName
    session_type: str

    def capture_region(
        self,
        region: VirtualRect,
        *,
        virtual_rect: VirtualRect | None = None,
    ) -> bytes: ...

    def current_focus(self) -> FocusTarget: ...

    def read_clipboard_text(self) -> str: ...

    def write_clipboard_text(self, text: str) -> str | None: ...

    def inject_text(
        self,
        text: str,
        *,
        target: FocusTarget | None = None,
        terminal: bool | None = None,
    ) -> PasteResult: ...

    def create_hotkey_rig(
        self,
        bindings: HotkeyBindings,
        *,
        on_voice_start: Callable[[], bool],
        on_voice_stop: Callable[[], None],
        on_screenshot: Callable[[], None],
        on_ocr: Callable[[], None] = ...,
        on_repeat: Callable[[], None] = ...,
        overlay_active: Callable[[], bool] = ...,
        close_overlay: Callable[[], bool] = ...,
        on_error: Callable[[str | None], None],
    ) -> HotkeyRig: ...

    def capabilities(self) -> list[SetupCapability]: ...

    def configure_autostart(self, *, enabled: bool, command: Sequence[str]) -> None:
        """当前用户登录后自动运行 command（开 → 写入/覆盖；关 → 删除）。三端同一签名。"""
        ...

    def play_beep(
        self,
        freq: float = 440,
        duration: float = 0.1,
        volume: float = 0.3,
    ) -> None: ...

    def close(self) -> None: ...


def capture_region_mss(
    region: VirtualRect,
    mss_factory=None,
    *,
    virtual_rect: VirtualRect | None = None,
) -> bytes:
    """使用 `mss` 直接截取目标矩形 `(left, top, width, height)` 并编码为 PNG（不先全屏截图）。

    当 `sct.monitors[0]` 宽高有效时：
    - 若传入 `virtual_rect`，按 `monitors[0]` 与 `virtual_rect` 的宽高比例及原点自动将逻辑坐标映射到
      `mss` 物理坐标系（Windows / Linux X11 HiDPI 自动缩放，macOS 逻辑点 1:1 等价）；
    - 无论是否传入 `virtual_rect`，最终抓取框均严格钳制在 `monitors[0]` 边界内且保证宽/高 >= 1，
      防止 X11 越界 1px 触发 `BadMatch`。
    """
    import mss
    import mss.tools

    rx, ry, rw, rh = (int(v) for v in region)
    if rw <= 0 or rh <= 0:
        raise ValueError(f"选区宽高必须大于 0，收到 {region!r}")

    vrect_parsed: VirtualRect | None = None
    if virtual_rect is not None:
        vx, vy, vw, vh = (int(v) for v in virtual_rect)
        if vw <= 0 or vh <= 0:
            raise ValueError(f"virtual_rect 宽高必须大于 0，收到 {virtual_rect!r}")
        vrect_parsed = (vx, vy, vw, vh)

    factory = mss.mss if mss_factory is None else mss_factory
    with factory() as sct:
        monitors = getattr(sct, "monitors", None)
        mon = monitors[0] if monitors else None
        mon_left = int(mon.get("left", 0)) if isinstance(mon, dict) else 0
        mon_top = int(mon.get("top", 0)) if isinstance(mon, dict) else 0
        mon_w = int(mon.get("width", 0)) if isinstance(mon, dict) else 0
        mon_h = int(mon.get("height", 0)) if isinstance(mon, dict) else 0

        if mon_w > 0 and mon_h > 0:
            if vrect_parsed is not None:
                vx, vy, vw, vh = vrect_parsed
                scale_x = mon_w / float(vw)
                scale_y = mon_h / float(vh)
                raw_x0 = mon_left + int(round((rx - vx) * scale_x))
                raw_y0 = mon_top + int(round((ry - vy) * scale_y))
                raw_x1 = mon_left + int(round((rx - vx + rw) * scale_x))
                raw_y1 = mon_top + int(round((ry - vy + rh) * scale_y))
            else:
                raw_x0, raw_y0 = rx, ry
                raw_x1, raw_y1 = rx + rw, ry + rh

            mon_right = mon_left + mon_w
            mon_bottom = mon_top + mon_h
            x0 = max(mon_left, min(mon_right - 1, raw_x0))
            y0 = max(mon_top, min(mon_bottom - 1, raw_y0))
            x1 = max(x0 + 1, min(mon_right, raw_x1))
            y1 = max(y0 + 1, min(mon_bottom, raw_y1))
            box = {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}
        else:
            box = {"left": rx, "top": ry, "width": rw, "height": rh}

        shot = sct.grab(box)
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
        if not png_bytes:
            raise RuntimeError("mss 区域截图编码 PNG 返回空字节")
        return png_bytes


def crop_png_region(
    png_bytes: bytes,
    virtual_rect: VirtualRect,
    region: VirtualRect,
) -> bytes:
    """按逻辑虚拟桌面矩形与物理 PNG 尺寸比例，精确裁切选区 `(x, y, w, h)` 为 PNG 字节。"""
    import io

    from PIL import Image

    if not png_bytes:
        raise ValueError("待裁切截图 PNG 不能为空")
    vx, vy, vw, vh = (int(v) for v in virtual_rect)
    rx, ry, rw, rh = (int(v) for v in region)
    if vw <= 0 or vh <= 0:
        raise ValueError(f"virtual_rect 宽高必须大于 0，收到 {virtual_rect!r}")
    if rw <= 0 or rh <= 0:
        raise ValueError(f"region 宽高必须大于 0，收到 {region!r}")

    with Image.open(io.BytesIO(png_bytes)) as img:
        img_w, img_h = img.size
        if img_w <= 0 or img_h <= 0:
            raise RuntimeError("截图图像尺寸非法")
        scale_x = img_w / float(vw)
        scale_y = img_h / float(vh)
        lx0 = rx - vx
        ly0 = ry - vy
        px0 = max(0, min(img_w - 1, int(round(lx0 * scale_x))))
        py0 = max(0, min(img_h - 1, int(round(ly0 * scale_y))))
        px1 = max(px0 + 1, min(img_w, int(round((lx0 + rw) * scale_x))))
        py1 = max(py0 + 1, min(img_h, int(round((ly0 + rh) * scale_y))))
        cropped = img.crop((px0, py0, px1, py1))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        out = buf.getvalue()
        if not out:
            raise RuntimeError("裁切区域编码 PNG 返回空字节")
        return out

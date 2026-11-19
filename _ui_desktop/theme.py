"""方案 C（Studio 工作站）跨平台桌面 UI 视觉令牌与字体工具。

所有桌面浮窗、红十字框选器、原位遮挡译文层、语音胶囊与双行大字幕条、系统托盘图标
均从本模块读取颜色、字号与圆角常量，禁止在各组件内散落硬编码色值。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication

# ---------------------------------------------------------------------------
# 1. 红色十字定位框选器（截屏原位翻译 & 纯 OCR 100% 复用）
# ---------------------------------------------------------------------------
CROSSHAIR_HEX = "#ff2d2d"
CROSSHAIR_GLOW_RGBA = (255, 45, 45, 204)
SELECTION_FILL_RGBA = (255, 45, 45, 20)
DIM_MASK_RGBA = (0, 0, 0, 108)

COORD_TAG_BG_RGBA = (13, 17, 23, 235)
COORD_TAG_TEXT_HEX = "#ff6b6b"
COORD_TAG_BORDER_HEX = CROSSHAIR_HEX

TOP_HINT_BG_RGBA = (13, 17, 23, 230)
TOP_HINT_TEXT_HEX = "#f0f6fc"
TOP_HINT_BORDER_HEX = "#30363d"

# ---------------------------------------------------------------------------
# 2. Studio 深色工作站面板与悬浮工具栏令牌
# ---------------------------------------------------------------------------
BG_DARK_HEX = "#0d1117"
RAIL_HEX = "#131820"
SURFACE_HEX = "#161b22"
SURFACE_2_HEX = "#1f2630"
BORDER_HEX = "#30363d"

TEXT_PRIMARY_HEX = "#e6edf3"
TEXT_MUTED_HEX = "#8b949e"

ACCENT_HEX = "#58a6ff"
ACCENT_HOVER_HEX = "#79c0ff"
ACCENT_SOFT_RGBA = (56, 139, 253, 41)

OK_HEX = "#3fb950"
OK_SOFT_RGBA = (63, 185, 80, 41)
WARN_HEX = "#d29922"
WARN_SOFT_RGBA = (210, 153, 34, 46)
ERROR_HEX = "#f85149"
ERROR_SOFT_RGBA = (248, 81, 73, 46)

# 原位遮挡默认回退前背景色（均按 alpha=255 100% 不透明使用）
DEFAULT_BLOCK_BG_HEX = "#ffffff"
DEFAULT_BLOCK_FG_HEX = "#0f172a"
OVERLAY_OUTLINE_HEX = ACCENT_HEX
OVERLAY_BANNER_BG_RGBA = (13, 17, 23, 224)

# ---------------------------------------------------------------------------
# 3. 左下角状态胶囊与底部居中双行大字幕条令牌
# ---------------------------------------------------------------------------
CAPSULE_BG_RGBA = (13, 17, 23, 232)
CAPSULE_BORDER_HEX = "#30363d"
CAPSULE_TEXT_HEX = "#e6edf3"
CAPSULE_MUTED_HEX = "#8b949e"

STATE_IDLE_HEX = "#8b949e"
STATE_RECORDING_HEX = "#f85149"
STATE_POLISHING_HEX = "#58a6ff"
STATE_BUSY_HEX = "#d29922"
STATE_OK_HEX = "#3fb950"
STATE_WARN_HEX = "#d29922"
STATE_ERROR_HEX = "#f85149"

CAPTION_BG_RGBA = (13, 17, 23, 240)
CAPTION_BORDER_RGBA = (88, 166, 255, 102)
STABLE_TEXT_HEX = "#f8fafc"
TAIL_TEXT_HEX = "#94a3b8"
# 语义别名，便于不同模块直观引用
CAPTION_STABLE_TEXT_HEX = STABLE_TEXT_HEX
CAPTION_TAIL_TEXT_HEX = TAIL_TEXT_HEX

# ---------------------------------------------------------------------------
# 4. 字体族、像素字号与圆角几何常量
# ---------------------------------------------------------------------------
FONT_FAMILIES_SANS: tuple[str, ...] = (
    "Noto Sans CJK SC",
    "PingFang SC",
    "Microsoft YaHei",
    "Segoe UI",
    "Sans Serif",
)
FONT_FAMILIES_MONO: tuple[str, ...] = (
    "JetBrains Mono",
    "Noto Sans Mono CJK SC",
    "Consolas",
    "Menlo",
    "Monospace",
)

FS_MONO_TAG = 11
FS_XS = 12
FS_SM = 13
FS_MD = 14
FS_LG = 16
FS_CAPTION = 19

OCR_MIN_FONT_PX = 9
OCR_BASE_MIN_FONT_PX = 11
OCR_HEIGHT_FONT_RATIO = 0.72

RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14
RADIUS_PILL = 999

ColorSpec = str | tuple[int, int, int] | tuple[int, int, int, int]


def qcolor(spec: ColorSpec, *, opaque: bool = False, fallback: str = "#000000") -> QColor:
    """将十六进制色值或 RGB/RGBA 元组安全转为 QColor；opaque=True 时强制 alpha=255。"""
    if isinstance(spec, tuple):
        if len(spec) == 3:
            color = QColor(spec[0], spec[1], spec[2], 255)
        elif len(spec) == 4:
            color = QColor(spec[0], spec[1], spec[2], 255 if opaque else spec[3])
        else:
            color = QColor(fallback)
    else:
        color = QColor(spec)
        if not color.isValid():
            color = QColor(fallback)
    if opaque:
        color.setAlpha(255)
    return color


def make_font(
    *,
    pixel_size: int = FS_MD,
    mono: bool = False,
    weight: QFont.Weight = QFont.Weight.Normal,
) -> QFont:
    """构建跨平台像素字号字体，保证离屏渲染与多 DPI 下行高可预期。"""
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES_MONO if mono else FONT_FAMILIES_SANS))
    font.setPixelSize(max(1, int(pixel_size)))
    font.setWeight(weight)
    return font


def resolve_available_screen_rect(
    override: tuple[int, int, int, int] | None = None,
    anchor: QPoint | None = None,
) -> QRect:
    """解析目标显示器的可用屏幕矩形；优先使用显式注入值以便多屏与离屏单测。"""
    if override is not None:
        ax, ay, aw, ah = (int(v) for v in override)
        return QRect(ax, ay, aw, ah)
    screen = (
        QGuiApplication.screenAt(anchor) if anchor is not None else None
    ) or QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        if avail.width() > 0 and avail.height() > 0:
            return avail
    return QRect(0, 0, 1920, 1080)


def float_bar_stylesheet() -> str:
    """悬浮工具栏与确认卡片共用的精简 QSS（全部引用本模块令牌）。"""
    return (
        f"QFrame#oviFloatBar {{"
        f"  background-color: {SURFACE_HEX};"
        f"  border: 1px solid {BORDER_HEX};"
        f"  border-radius: {RADIUS_MD}px;"
        f"}}"
        f"QLabel#oviFloatStatus {{"
        f"  color: {ACCENT_HEX};"
        f"  font-size: {FS_XS}px;"
        f"  font-weight: 600;"
        f"  background: transparent;"
        f"  border: none;"
        f"}}"
        f"QLabel#oviFloatError {{"
        f"  color: {ERROR_HEX};"
        f"  font-size: {FS_XS}px;"
        f"  font-weight: 600;"
        f"  background: transparent;"
        f"  border: none;"
        f"}}"
        f"QPushButton {{"
        f"  background-color: {SURFACE_2_HEX};"
        f"  color: {TEXT_PRIMARY_HEX};"
        f"  border: 1px solid {BORDER_HEX};"
        f"  border-radius: {RADIUS_SM}px;"
        f"  padding: 3px 9px;"
        f"  font-size: {FS_XS}px;"
        f"}}"
        f"QPushButton:hover {{"
        f"  border-color: {ACCENT_HEX};"
        f"  color: {ACCENT_HEX};"
        f"}}"
        f"QPushButton:pressed {{"
        f"  background-color: {ACCENT_HEX};"
        f"  border-color: {ACCENT_HEX};"
        f"  color: {BG_DARK_HEX};"
        f"}}"
        f"QPushButton:disabled {{"
        f"  color: {TEXT_MUTED_HEX};"
        f"  border-color: {BORDER_HEX};"
        f"}}"
    )

"""方案 C（Studio 工作站）跨平台桌面 UI 视觉令牌与字体工具。

所有桌面浮窗、红十字框选器、原位遮挡译文层、语音一体底栏、系统托盘图标
均从本模块读取颜色、字号、圆角与动效常量，禁止在各组件内散落硬编码色值。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter

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
TEXT_SECONDARY_HEX = "#c9d1d9"
TEXT_MUTED_HEX = "#8b949e"

ACCENT_HEX = "#58a6ff"
ACCENT_HOVER_HEX = "#79c0ff"
ACCENT_SOFT_RGBA = (56, 139, 253, 41)
ACCENT_ACTIVE_BORDER_RGBA = (88, 166, 255, 115)
ACCENT_SWATCH_HOVER_BORDER_RGBA = (88, 166, 255, 166)
ACCENT_SWATCH_HOVER_BG_RGBA = (88, 166, 255, 20)

OK_HEX = "#3fb950"
OK_BRIGHT_HEX = "#56d364"
OK_SOFT_RGBA = (63, 185, 80, 41)
OK_HOVER_BG_RGBA = (63, 185, 80, 66)
OK_BORDER_RGBA = (63, 185, 80, 122)
WARN_HEX = "#d29922"
WARN_SOFT_RGBA = (210, 153, 34, 46)
ERROR_HEX = "#f85149"
ERROR_SOFT_RGBA = (248, 81, 73, 46)
ERROR_BORDER_RGBA = (248, 81, 73, 115)
ERROR_BG_RGBA = (248, 81, 73, 31)

# 原位识别 / 翻译浮窗（UI 验收标准 docs/design/v3.3/overlay-a.html）
DEFAULT_BLOCK_BG_HEX = "#ffffff"  # 截图含透明像素时垫底的不透明底色
OVERLAY_OUTLINE_HEX = ACCENT_HEX
OVERLAY_PATCH_ALPHA = 219  # 贴片 = 采样底色 × 86%，原文仍隐约可见
OVERLAY_PATCH_PAD_X_PX = 4
OVERLAY_PATCH_PAD_Y_PX = 3
OVERLAY_PATCH_RADIUS_PX = 3
OVERLAY_OUTLINE_WIDTH_PX = 1.5
OVERLAY_DASH_PATTERN_PX = (6.0, 4.0)
OVERLAY_ANTS_SPEED_PX_S = 25.0  # 加载中虚线外框流动速度
OVERLAY_RESULT_FADE_MS = 180
OVERLAY_MENU_GAP_PX = 8
OVERLAY_MENU_INSIDE_PAD_PX = 6
OVERLAY_FRAME_MS = 16
OVERLAY_SPINNER_PX = 11
OVERLAY_SPIN_PERIOD_MS = 900

# ---------------------------------------------------------------------------
# 3. 语音一体底栏（胶囊 / 字幕条 / 确认卡片三种形态）颜色令牌
# ---------------------------------------------------------------------------
CAPSULE_BG_RGBA = (13, 17, 23, 232)
CAPSULE_BORDER_HEX = "#30363d"
CAPSULE_TEXT_HEX = "#e6edf3"
CAPSULE_MUTED_HEX = "#8b949e"

STATE_IDLE_HEX = "#8b949e"
STATE_RECORDING_HEX = "#f85149"
STATE_POLISHING_HEX = "#58a6ff"
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

# 截屏标注浮窗（UI 验收标准 docs/design/v3.5/annotate-icons-b.html）
ANNOTATE_COLOR_AMBER_HEX = "#f59e0b"
ANNOTATE_COLOR_BLUE_HEX = "#2f81f7"
ANNOTATE_COLOR_BLACK_HEX = "#111827"

ANNOTATE_RING_LIGHT_RGBA = (230, 237, 243, 71)
ANNOTATE_RING_BLUE_RGBA = (230, 237, 243, 82)
ANNOTATE_RING_DARK_RGBA = (13, 17, 23, 140)
ANNOTATE_RING_BLACK_RGBA = (230, 237, 243, 158)


@dataclass(frozen=True)
class AnnotateColorSpec:
    """截屏标注单源色板规格（含深色工具条辨识描边与选中态反色中心点）。"""

    key: str
    label: str
    hex: str
    ring_rgba: tuple[int, int, int, int]
    dot_hex: str


@dataclass(frozen=True)
class AnnotateSizeSpec:
    """截屏标注单源 3 档粗细规格（线宽 / 文字字号 / 马赛克块 / 预览点直径）。"""

    key: str
    label: str
    stroke_px: int
    text_px: int
    block_px: int
    dot_px: int


ANNOTATE_COLORS: tuple[AnnotateColorSpec, ...] = (
    AnnotateColorSpec("red", "红", CROSSHAIR_HEX, ANNOTATE_RING_LIGHT_RGBA, STABLE_TEXT_HEX),
    AnnotateColorSpec("amber", "橙黄", ANNOTATE_COLOR_AMBER_HEX, ANNOTATE_RING_LIGHT_RGBA, ANNOTATE_COLOR_BLACK_HEX),
    AnnotateColorSpec("green", "绿", OK_HEX, ANNOTATE_RING_LIGHT_RGBA, ANNOTATE_COLOR_BLACK_HEX),
    AnnotateColorSpec("blue", "蓝", ANNOTATE_COLOR_BLUE_HEX, ANNOTATE_RING_BLUE_RGBA, STABLE_TEXT_HEX),
    AnnotateColorSpec("white", "白", STABLE_TEXT_HEX, ANNOTATE_RING_DARK_RGBA, ANNOTATE_COLOR_BLACK_HEX),
    AnnotateColorSpec("black", "黑", ANNOTATE_COLOR_BLACK_HEX, ANNOTATE_RING_BLACK_RGBA, STABLE_TEXT_HEX),
)

ANNOTATE_SIZES: tuple[AnnotateSizeSpec, ...] = (
    AnnotateSizeSpec("thin", "细", stroke_px=2, text_px=16, block_px=6, dot_px=4),
    AnnotateSizeSpec("medium", "中", stroke_px=3, text_px=20, block_px=10, dot_px=7),
    AnnotateSizeSpec("thick", "粗", stroke_px=6, text_px=28, block_px=16, dot_px=10),
)

ANNOTATE_DEFAULT_COLOR_HEX = ANNOTATE_COLORS[0].hex
ANNOTATE_DEFAULT_SIZE_LEVEL = ANNOTATE_SIZES[1].key
_ANNOTATE_COLOR_BY_HEX: dict[str, AnnotateColorSpec] = {c.hex.lower(): c for c in ANNOTATE_COLORS}
_ANNOTATE_SIZE_BY_KEY: dict[str, AnnotateSizeSpec] = {s.key: s for s in ANNOTATE_SIZES}

ANNOTATE_MIN_DRAG_PX = 6
ANNOTATE_BRUSH_MIN_STEP_PX = 2.0
ANNOTATE_BRUSH_MIN_PATH_PX = 2.0
ANNOTATE_DRAFT_DASH_PX = (4.0, 3.0)
ANNOTATE_COPY_FEEDBACK_MS = 1500
ANNOTATE_SAVED_FEEDBACK_MS = 2500
ANNOTATE_EDIT_MIN_WIDTH_PX = 48
ANNOTATE_EDIT_EXTRA_HEIGHT_PX = 8
ANNOTATE_EDIT_PADDING_X_PX = 18
ANNOTATE_SUB_BAR_GAP_PX = 4
ANNOTATE_SUB_BAR_RESERVE_H_PX = 34
ANNOTATE_ICON_BTN_PX = 28
ANNOTATE_PRIMARY_BTN_W_PX = 32
ANNOTATE_ICON_GLYPH_PX = 16
ANNOTATE_ICON_STROKE_PX = 1.6
ANNOTATE_SWATCH_BTN_PX = 24
ANNOTATE_SWATCH_CIRCLE_PX = 12
ANNOTATE_SWATCH_DOT_PX = 4
ANNOTATE_SIZE_BTN_PX = 24
ANNOTATE_PX_TAG_MIN_W_PX = 34
ANNOTATE_DISABLED_OPACITY = 0.38
ANNOTATE_MOSAIC_DIM_OPACITY = 0.28

# 一体底栏动效（UI 验收标准 docs/design/v3.3/voice-a.html「动效参数」表）
VOICE_FRAME_MS = 16
VOICE_FADE_IN_MS = 180
VOICE_RISE_PX = 8
VOICE_MORPH_MS = 200
VOICE_CONTENT_DELAY_MS = 40
VOICE_CONTENT_FADE_MS = 180
VOICE_ROLL_MS = 200
VOICE_FRESH_MS = 220
VOICE_COLOR_MS = 200
VOICE_SWAP_HALF_MS = 130
VOICE_CHECK_MS = 240
VOICE_FADE_OUT_MS = 260
VOICE_SINK_PX = 6
VOICE_PULSE_PERIOD_MS = 1200
VOICE_PULSE_MIN_OPACITY = 0.55
VOICE_SPIN_PERIOD_MS = 900
VOICE_BREATH_MIN_ALPHA = 115  # 润色描边呼吸 45%–80%
VOICE_BREATH_SPAN_ALPHA = 89
VOICE_FIRM_BORDER_ALPHA = 179  # 已输入 / 出错描边 70%
VOICE_SOFT_BORDER_ALPHA = 153  # 润色 / 待确认描边 60%

# 录音中警告的附加提示行（voice-a.html「附加提示行」规格）
VOICE_NOTICE_HEIGHT_PX = 24
VOICE_NOTICE_INSET_PX = 16
VOICE_NOTICE_PAD_X_PX = 10
VOICE_NOTICE_RADIUS_PX = 8
VOICE_NOTICE_RISE_PX = 4
VOICE_NOTICE_DOT_PX = 7
VOICE_NOTICE_IN_MS = 160
VOICE_NOTICE_SHOW_MS = 3000
VOICE_NOTICE_OUT_MS = 260
VOICE_NOTICE_BORDER_ALPHA = 153

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
OCR_REFERENCE_FONT_PX = 100  # 换算原字号与基线时测量行内文字紧框所用的参考字号
OCR_FIT_SPACING_MIN = -0.1  # 与原行文字相同的行按原行宽调字距的范围（× 字号）
OCR_FIT_SPACING_MAX = 0.3
OCR_SINGLE_LINE_PITCH_RATIO = 1.5  # 单行块需要下延时的行距（× 行高）

RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14
RADIUS_PILL = 999

ColorSpec = str | tuple[int, int, int] | tuple[int, int, int, int]


def validate_annotate_color(color: str) -> str:
    """校验标注预设色值并返回规范小写十六进制串，非法值立即报错。"""
    if not isinstance(color, str):
        raise ValueError(f"标注颜色必须是字符串，收到 {color!r}")
    spec = _ANNOTATE_COLOR_BY_HEX.get(color.strip().lower())
    if spec is None:
        allowed = [c.hex for c in ANNOTATE_COLORS]
        raise ValueError(f"不支持的标注颜色：{color!r}，允许 {allowed}")
    return spec.hex


def validate_annotate_size(size_level: str) -> AnnotateSizeSpec:
    """校验标注粗细档位并返回单源规格对象，非法值立即报错。"""
    if not isinstance(size_level, str):
        raise ValueError(f"标注粗细档位必须是字符串，收到 {size_level!r}")
    spec = _ANNOTATE_SIZE_BY_KEY.get(size_level)
    if spec is None:
        allowed = [s.key for s in ANNOTATE_SIZES]
        raise ValueError(f"不支持的标注粗细档位：{size_level!r}，允许 {allowed}")
    return spec


def resolve_annotate_style(
    color: str = ANNOTATE_DEFAULT_COLOR_HEX,
    size_level: str = ANNOTATE_DEFAULT_SIZE_LEVEL,
) -> tuple[str, AnnotateSizeSpec]:
    """统一校验并解析标注颜色与粗细档位。"""
    return validate_annotate_color(color), validate_annotate_size(size_level)


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


def decode_opaque_png(png_bytes: bytes, *, label: str = "png") -> QImage:
    """将图像字节解码为不透明 Format_ARGB32；无透明通道走快路径，含透明通道时用 DEFAULT_BLOCK_BG_HEX 垫底。"""
    if not isinstance(png_bytes, (bytes, bytearray, memoryview)):
        raise ValueError(f"{label} 必须是字节流，收到 {type(png_bytes).__name__}")
    if not png_bytes:
        raise ValueError(f"{label} 不能为空")
    raw = png_bytes if isinstance(png_bytes, bytes) else bytes(png_bytes)
    image = QImage.fromData(raw)
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise ValueError(f"{label} 不是有效的图像数据")
    if not image.hasAlphaChannel():
        return image.convertToFormat(QImage.Format.Format_ARGB32)
    converted = image.convertToFormat(QImage.Format.Format_ARGB32)
    opaque = QImage(converted.size(), QImage.Format.Format_ARGB32)
    opaque.fill(qcolor(DEFAULT_BLOCK_BG_HEX, opaque=True))
    painter = QPainter(opaque)
    painter.drawImage(0, 0, converted)
    painter.end()
    return opaque


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


def resolve_virtual_geometry(
    override: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """全屏透明窗口的虚拟桌面矩形 (x, y, w, h)：显式注入值优先，离屏环境取 1920×1080。"""
    if override is not None:
        vx, vy, vw, vh = (int(v) for v in override)
        if vw <= 0 or vh <= 0:
            raise ValueError(f"virtual_geometry 宽高必须大于 0，收到 {override!r}")
        return vx, vy, vw, vh
    if (QGuiApplication.platformName() or "").lower() not in {"offscreen", "minimal"}:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            vg = screen.virtualGeometry()
            if vg.width() > 0 and vg.height() > 0:
                return vg.x(), vg.y(), vg.width(), vg.height()
    return 0, 0, 1920, 1080


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


def _rgba_css(rgba: tuple[int, int, int, int]) -> str:
    return f"rgba({rgba[0]}, {rgba[1]}, {rgba[2]}, {rgba[3]})"


def float_bar_stylesheet() -> str:
    """悬浮工具条与二级样式条 QSS（overlay-a 与 annotate-icons-b 共用；全部引用本模块令牌）。"""
    err_border = _rgba_css(ERROR_BORDER_RGBA)
    err_bg = _rgba_css(ERROR_BG_RGBA)
    return (
        f"QFrame#oviFloatBar {{"
        f"  background-color: {SURFACE_HEX};"
        f"  border: 1px solid {BORDER_HEX};"
        f"  border-radius: {RADIUS_MD}px;"
        f"}}"
        f"QFrame#oviStyleBar {{"
        f"  background-color: {SURFACE_2_HEX};"
        f"  border: 1px solid {BORDER_HEX};"
        f"  border-radius: {RADIUS_MD}px;"
        f"}}"
        f"QLabel#oviFloatStatus, QLabel#oviFloatOk, QLabel#oviFloatError {{"
        f"  color: {ACCENT_HEX};"
        f"  font-size: {FS_XS}px;"
        f"  font-weight: 600;"
        f"  background: transparent;"
        f"  border: none;"
        f"}}"
        f"QLabel#oviFloatOk {{"
        f"  color: {OK_HEX};"
        f"}}"
        f"QLabel#oviFloatError {{"
        f"  color: {ERROR_HEX};"
        f"}}"
        f"QLabel#oviFloatHint, QLabel#oviStylePx {{"
        f"  color: {TEXT_MUTED_HEX};"
        f"  font-size: {FS_MONO_TAG}px;"
        f"  background: transparent;"
        f"  border: none;"
        f"}}"
        f"QFrame#oviFloatDivider {{"
        f"  background-color: {BORDER_HEX};"
        f"  border: none;"
        f"}}"
        f"QPushButton {{"
        f"  background-color: transparent;"
        f"  color: {TEXT_PRIMARY_HEX};"
        f"  border: 1px solid transparent;"
        f"  border-radius: {RADIUS_SM}px;"
        f"  padding: 3px 9px;"
        f"  font-size: {FS_XS}px;"
        f"  font-weight: 500;"
        f"}}"
        f"QPushButton:hover:enabled {{"
        f"  border-color: {ACCENT_HEX};"
        f"  color: {ACCENT_HEX};"
        f"}}"
        f"QPushButton:disabled {{"
        f"  color: {TEXT_MUTED_HEX};"
        f"  border-color: transparent;"
        f"}}"
        f'QPushButton[feedback="ok"] {{'
        f"  color: {OK_HEX};"
        f"}}"
        f'QPushButton[feedback="fail"] {{'
        f"  color: {ERROR_HEX};"
        f"  border-color: {err_border};"
        f"  background-color: {err_bg};"
        f"}}"
        f"QPushButton#oviIconBtn, QPushButton#oviSwatchBtn, QPushButton#oviSizeBtn {{"
        f"  padding: 0;"
        f"  border: none;"
        f"  background: transparent;"
        f"}}"
        f"QToolTip {{"
        f"  background-color: {BG_DARK_HEX};"
        f"  color: {TEXT_PRIMARY_HEX};"
        f"  border: 1px solid {BORDER_HEX};"
        f"  border-radius: {RADIUS_SM}px;"
        f"  padding: 3px 8px;"
        f"  font-size: {FS_MONO_TAG}px;"
        f"}}"
    )


def annotate_edit_stylesheet(*, color: str = ANNOTATE_DEFAULT_COLOR_HEX) -> str:
    """标注文字内联输入框 QSS（annotate-icons-b `.edit`；全部引用本模块令牌）。"""
    return (
        f"QLineEdit#oviAnnotateEdit {{"
        f"  background: transparent;"
        f"  color: {color};"
        f"  border: 1px dashed {ACCENT_HEX};"
        f"  padding: 0 2px;"
        f"}}"
    )

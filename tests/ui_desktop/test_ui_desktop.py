"""`_ui_desktop` 跨平台桌面纯 UI 层完整离屏测试套件。"""
from __future__ import annotations

import ast
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QEnterEvent, QGuiApplication, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QSystemTrayIcon, QWidget

from _contracts import OcrTextBlock, OcrTextLine
from _ui_desktop import (
    AnnotateOverlay,
    InplaceTranslateOverlay,
    RedCrosshairSelector,
    StudioTrayIcon,
    VoiceHudOverlay,
    compute_trailing_two_lines,
    render_status_icon,
    render_status_image,
    theme,
)
from _ui_desktop.annotate_icons import ANNOTATE_ICON_KEYS, draw_annotate_icon
from _ui_desktop.annotate_widgets import _IconButton
from _ui_desktop.caption_bar import CAPTION_AUTO_HIDE_MS, passive_overlay_flags
from _ui_desktop.inplace_layout import layout_ocr_block, layout_translated_block
from _ui_desktop.mini_menu import COPY_OCR, COPY_TRANSLATED, ESC_HINT, place_secondary_bar

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_DESKTOP_DIR = PROJECT_ROOT / "_ui_desktop"

ALLOWED_TOP_IMPORTS = {
    "__future__",
    "collections",
    "dataclasses",
    "inspect",
    "time",
    "typing",
    "PySide6",
    "_contracts",
    "_ui_desktop",
}
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _make_png(
    width: int,
    height: int,
    bg_hex: str = "#ffffff",
    patches: list[tuple[tuple[int, int, int, int], str]] | None = None,
) -> bytes:
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor(bg_hex))
    if patches:
        painter = QPainter(img)
        for (x, y, w, h), color_hex in patches:
            painter.fillRect(QRect(x, y, w, h), QColor(color_hex))
        painter.end()
    byte_array = QByteArray()
    buf = QBuffer(byte_array)
    assert buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "PNG")
    buf.close()
    return bytes(byte_array.data())


# ---------------------------------------------------------------------------
# 1. 架构纯度与方案 C 主题令牌校验
# ---------------------------------------------------------------------------
def test_theme_tokens_and_ui_layer_purity() -> None:
    assert theme.CROSSHAIR_HEX == "#ff2d2d"
    assert theme.STABLE_TEXT_HEX == "#f8fafc"
    assert theme.TAIL_TEXT_HEX == "#94a3b8"
    assert theme.CAPTION_STABLE_TEXT_HEX == "#f8fafc"
    assert theme.CAPTION_TAIL_TEXT_HEX == "#94a3b8"
    assert not hasattr(theme, "ANNOTATE_STROKE_PX")
    assert not hasattr(theme, "ANNOTATE_TEXT_PX")
    assert not hasattr(theme, "ANNOTATE_MOSAIC_BLOCK_PX")
    assert not hasattr(theme, "ANNOTATE_SIZE_BTN_W_PX")
    assert not hasattr(theme, "ANNOTATE_SIZE_BTN_H_PX")
    assert theme.OK_BRIGHT_HEX == "#56d364"
    assert theme.ANNOTATE_ICON_BTN_PX == 28
    assert theme.ANNOTATE_PRIMARY_BTN_W_PX == 32
    assert theme.ANNOTATE_ICON_GLYPH_PX == 16
    assert theme.ANNOTATE_ICON_STROKE_PX == 1.6
    assert theme.ANNOTATE_SIZE_BTN_PX == 24
    assert theme.ANNOTATE_DISABLED_OPACITY == 0.38
    assert theme.ANNOTATE_MOSAIC_DIM_OPACITY == 0.28
    qss = theme.float_bar_stylesheet()
    assert "QLabel#oviStyleHint" not in qss
    assert 'QPushButton[active="true"]' not in qss
    assert "QPushButton#oviIconBtn" in qss
    assert "QToolTip" in qss
    assert [c.hex for c in theme.ANNOTATE_COLORS] == [
        "#ff2d2d",
        "#f59e0b",
        "#3fb950",
        "#2f81f7",
        "#f8fafc",
        "#111827",
    ]
    assert [(s.key, s.stroke_px, s.text_px, s.block_px) for s in theme.ANNOTATE_SIZES] == [
        ("thin", 2, 16, 6),
        ("medium", 3, 20, 10),
        ("thick", 6, 28, 16),
    ]
    default_hex, default_size = theme.resolve_annotate_style()
    assert (default_hex, default_size.key, default_size.stroke_px, default_size.text_px, default_size.block_px) == (
        "#ff2d2d",
        "medium",
        3,
        20,
        10,
    )
    assert theme.validate_annotate_color("#FF2D2D") == "#ff2d2d"
    with pytest.raises(ValueError):
        theme.validate_annotate_color("#123456")
    with pytest.raises(ValueError):
        theme.validate_annotate_size("huge")
    assert theme.ANNOTATE_SAVED_FEEDBACK_MS == 2500
    assert ESC_HINT == "Esc"

    py_files = sorted(UI_DESKTOP_DIR.glob("*.py"))
    assert [f.name for f in py_files] == [
        "__init__.py",
        "annotate_icons.py",
        "annotate_overlay.py",
        "annotate_tools.py",
        "annotate_widgets.py",
        "caption_bar.py",
        "inplace_layout.py",
        "inplace_overlay.py",
        "mini_menu.py",
        "region_selector.py",
        "theme.py",
        "tray.py",
        "voice_bar_motion.py",
        "voice_bar_view.py",
        "voice_hud.py",
    ]

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        assert len(source.splitlines()) <= 580, (
            f"{py_file.name} 超过精简文件上限: {len(source.splitlines())} 行"
        )
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in ALLOWED_TOP_IMPORTS, (
                        f"{py_file.name} 引入了越界依赖: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                assert top in ALLOWED_TOP_IMPORTS, (
                    f"{py_file.name} 引入了越界依赖: {node.module}"
                )

        if py_file.name != "theme.py":
            # 收集模块/类/函数首语句 docstring 节点并排除，其余任何字符串字面量均不得硬编码 #RRGGBB
            doc_nodes: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if (
                        node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)
                    ):
                        doc_nodes.add(id(node.body[0].value))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in doc_nodes
                ):
                    assert not HEX_COLOR_RE.search(node.value), (
                        f"{py_file.name} 出现硬编码色值字面量: {node.value}"
                    )


# ---------------------------------------------------------------------------
# 2. RedCrosshairSelector：全屏透明红十字准星、边缘防裁切、负坐标反向拖拽与双模式复用
# ---------------------------------------------------------------------------
def test_selector_window_flags_mouse_tracking_and_red_crosshair_pixels(
    qapp: QApplication,
) -> None:
    selector = RedCrosshairSelector(
        virtual_geometry=(0, 0, 320, 220),
        mode_label="截屏翻译",
    )
    try:
        flags = selector.windowFlags()
        assert bool(flags & Qt.WindowType.FramelessWindowHint)
        assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
        assert bool(flags & Qt.WindowType.Tool)
        assert selector.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
        assert selector.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is False
        assert selector.hasMouseTracking() is True

        selector.show()
        QTest.qWait(10)

        # 移动鼠标到 (110, 80)，触发实时重绘
        QTest.mouseMove(selector, QPoint(110, 80))
        qapp.processEvents()

        grabbed = selector.grab().toImage()
        red = QColor(theme.CROSSHAIR_HEX)
        # 校验贯穿全屏宽高的 1px #ff2d2d 十字线（避开中心准星环与右下胶囊区取样两端）
        assert grabbed.pixelColor(20, 80).rgb() == red.rgb()
        assert grabbed.pixelColor(290, 80).rgb() == red.rgb()
        assert grabbed.pixelColor(110, 15).rgb() == red.rgb()
        assert grabbed.pixelColor(110, 200).rgb() == red.rgb()

        # 未框选时非十字线区域被暗色半透明遮罩覆盖
        dimmed_px = grabbed.pixelColor(20, 180)
        assert abs(dimmed_px.alpha() - theme.DIM_MASK_RGBA[3]) <= 2

        # 按下拖拽产生选区后，选区内部恢复完全透明直接透出桌面，选区外保持暗色遮罩
        QTest.mousePress(selector, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 40))
        QTest.mouseMove(selector, QPoint(160, 140))
        qapp.processEvents()
        dragging_img = selector.grab().toImage()
        assert dragging_img.pixelColor(90, 90).alpha() == 0
        assert abs(dragging_img.pixelColor(20, 180).alpha() - theme.DIM_MASK_RGBA[3]) <= 2
    finally:
        selector.close_silent()
        selector.deleteLater()


def test_selector_coord_tag_clamped_at_all_four_edges(qapp: QApplication) -> None:
    selector = RedCrosshairSelector(
        virtual_geometry=(0, 0, 360, 240),
    )
    try:
        selector.show()
        bounds = QRect(0, 0, 360, 240)
        for corner in (QPoint(1, 1), QPoint(358, 1), QPoint(1, 238), QPoint(358, 238)):
            QTest.mouseMove(selector, corner)
            qapp.processEvents()
            tag_rect = selector.coord_tag_rect()
            assert tag_rect is not None
            assert bounds.contains(tag_rect), (
                f"光标在边缘 {corner} 时胶囊越界: {tag_rect}"
            )
    finally:
        selector.close_silent()
        selector.deleteLater()


@pytest.mark.parametrize("mode_label", ["截屏翻译", "纯 OCR 识别"])
def test_selector_reverse_drag_negative_origin_and_dual_mode_reuse(
    qapp: QApplication,
    mode_label: str,
) -> None:
    # 逻辑虚拟桌面含副屏负坐标偏移 (-400, 50, 400, 300)
    captured: list[tuple[int, int, int, int]] = []
    cancelled: list[bool] = []

    selector = RedCrosshairSelector(
        virtual_geometry=(-400, 50, 400, 300),
        mode_label=mode_label,
        on_selected=lambda rect: captured.append(rect),
        on_cancelled=lambda: cancelled.append(True),
    )
    try:
        selector.show()
        # 反向拖拽（右下 -> 左上）同样应正确归一化并回传单参数全局逻辑矩形
        QTest.mousePress(
            selector,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(200, 150),
        )
        QTest.mouseMove(selector, QPoint(100, 50))
        QTest.mouseRelease(
            selector,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(100, 50),
        )
        qapp.processEvents()

        assert cancelled == []
        assert captured == [(-300, 100, 100, 100)]
    finally:
        selector.close_silent()
        selector.deleteLater()


def test_selector_under_8px_ignored_and_right_click_or_esc_cancels(
    qapp: QApplication,
) -> None:
    with pytest.raises(ValueError):
        RedCrosshairSelector(virtual_geometry=(0, 0, 0, 100))

    selected: list[tuple[int, int, int, int]] = []
    cancelled: list[str] = []

    sel1 = RedCrosshairSelector(
        virtual_geometry=(0, 0, 300, 200),
        on_selected=lambda r: selected.append(r),
        on_cancelled=lambda: cancelled.append("right"),
    )
    try:
        sel1.show()
        # 7x7 (< 8px) 微小误触不结束选区
        QTest.mousePress(sel1, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(50, 50))
        QTest.mouseMove(sel1, QPoint(57, 57))
        QTest.mouseRelease(sel1, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(57, 57))
        qapp.processEvents()
        assert selected == []
        assert cancelled == []
        assert sel1.isVisible()

        # 右键取消
        QTest.mousePress(sel1, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
        qapp.processEvents()
        assert cancelled == ["right"]
        assert not sel1.isVisible()
    finally:
        sel1.close_silent()
        sel1.deleteLater()

    sel2 = RedCrosshairSelector(
        virtual_geometry=(0, 0, 300, 200),
        on_cancelled=lambda: cancelled.append("esc"),
    )
    try:
        sel2.show()
        QTest.keyClick(sel2, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert cancelled == ["right", "esc"]
        assert not sel2.isVisible()
    finally:
        sel2.close_silent()
        sel2.deleteLater()


def test_selector_no_initial_crosshair_until_real_pointer_event_and_direct_press_drag(
    qapp: QApplication,
) -> None:
    """未收到真实指针事件前不画红十字线/准星/坐标胶囊，enterEvent/mouseMoveEvent 画在真实位置，直接左键按下拖拽起点准确。"""
    cursor = QCursor.pos()
    origin_x = max(4000, cursor.x() + 1000)
    origin_y = max(4000, cursor.y() + 1000)
    red = QColor(theme.CROSSHAIR_HEX)

    selector = RedCrosshairSelector(
        virtual_geometry=(origin_x, origin_y, 320, 220),
        mode_label="截屏翻译",
    )
    try:
        selector.show()
        qapp.processEvents()

        # 1. 刚打开且未收到任何真实指针事件：查询均为空，仅绘暗色遮罩与顶部提示条，无任何红十字/准星/坐标胶囊像素
        assert selector.mouse_position is None
        assert selector.coord_tag_rect() is None
        assert selector.coord_tag_text() == ""

        initial_img = selector.grab().toImage()
        assert abs(initial_img.pixelColor(20, 180).alpha() - theme.DIM_MASK_RGBA[3]) <= 2
        assert _pixels_of(initial_img, theme.TOP_HINT_TEXT_HEX)
        assert _pixels_of(initial_img, theme.CROSSHAIR_HEX) == []
        assert _pixels_of(initial_img, theme.COORD_TAG_TEXT_HEX) == []
        assert not any(
            initial_img.pixelColor(x, y).red() - initial_img.pixelColor(x, y).blue() > 20
            for y in range(initial_img.height())
            for x in range(initial_img.width())
        )

        # 2a. 收到首个真实 enterEvent 后立即在真实坐标绘制红十字线与坐标胶囊（不在屏幕中点 (160, 110)）
        enter_pos = QPoint(95, 70)
        QApplication.sendEvent(
            selector,
            QEnterEvent(
                QPointF(enter_pos),
                QPointF(enter_pos),
                QPointF(origin_x + enter_pos.x(), origin_y + enter_pos.y()),
            ),
        )
        qapp.processEvents()

        assert selector.mouse_position == enter_pos
        assert selector.coord_tag_rect() is not None
        assert selector.coord_tag_text() == f"X: {origin_x + 95}, Y: {origin_y + 70}"
        enter_img = selector.grab().toImage()
        assert enter_img.pixelColor(20, 70).rgb() == red.rgb()
        assert enter_img.pixelColor(290, 70).rgb() == red.rgb()
        assert enter_img.pixelColor(95, 5).rgb() == red.rgb()
        assert enter_img.pixelColor(95, 200).rgb() == red.rgb()
        assert enter_img.pixelColor(20, 110).rgb() != red.rgb()
        assert enter_img.pixelColor(160, 200).rgb() != red.rgb()
        assert _pixels_of(enter_img, theme.COORD_TAG_TEXT_HEX)

        # 2b. 收到 mouseMoveEvent 后红十字线与坐标胶囊随真实移动坐标更新
        move_pos = QPoint(210, 145)
        QTest.mouseMove(selector, move_pos)
        qapp.processEvents()

        assert selector.mouse_position == move_pos
        assert selector.coord_tag_rect() is not None
        assert selector.coord_tag_text() == f"X: {origin_x + 210}, Y: {origin_y + 145}"
        move_img = selector.grab().toImage()
        assert move_img.pixelColor(20, 145).rgb() == red.rgb()
        assert move_img.pixelColor(290, 145).rgb() == red.rgb()
        assert move_img.pixelColor(210, 5).rgb() == red.rgb()
        assert move_img.pixelColor(210, 200).rgb() == red.rgb()
        assert move_img.pixelColor(20, 70).rgb() != red.rgb()
        assert move_img.pixelColor(95, 200).rgb() != red.rgb()
    finally:
        selector.close_silent()
        selector.deleteLater()

    # 3. 无任何前置移动或 enterEvent，直接左键按下拖拽：选区起点精确等于 mousePressEvent 坐标
    captured: list[tuple[int, int, int, int]] = []
    direct_origin_x = origin_x + 1000
    direct_origin_y = origin_y + 1000
    direct = RedCrosshairSelector(
        virtual_geometry=(direct_origin_x, direct_origin_y, 320, 220),
        mode_label="纯 OCR 识别",
        on_selected=captured.append,
    )
    try:
        direct.show()
        qapp.processEvents()
        assert direct.mouse_position is None

        QTest.mousePress(
            direct,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(60, 50),
        )
        qapp.processEvents()
        assert direct.mouse_position == QPoint(60, 50)
        assert direct.selection_rect() == QRect(60, 50, 0, 0)
        press_img = direct.grab().toImage()
        assert press_img.pixelColor(20, 50).rgb() == red.rgb()
        assert press_img.pixelColor(60, 200).rgb() == red.rgb()

        QTest.mouseMove(direct, QPoint(180, 150))
        QTest.mouseRelease(
            direct,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(180, 150),
        )
        qapp.processEvents()
        assert captured == [(direct_origin_x + 60, direct_origin_y + 50, 120, 100)]
    finally:
        direct.close_silent()
        direct.deleteLater()


# ---------------------------------------------------------------------------
# 3. InplaceTranslateOverlay 原位浮窗（UI 验收标准 docs/design/v3.3/overlay-a.html）
# ---------------------------------------------------------------------------
def _ocr_line(x: int, y: int, w: int, h: int, text: str, soft_wrap: bool = False) -> OcrTextLine:
    return OcrTextLine(bbox=(x, y, w, h), text=text, soft_wrap=soft_wrap)


def _ocr_block(lines: list[OcrTextLine], translated_text: str = "", bg: str = "#ffffff", fg: str = "#0f172a") -> OcrTextBlock:
    x0 = min(line.bbox[0] for line in lines)
    y0 = min(line.bbox[1] for line in lines)
    x1 = max(line.bbox[0] + line.bbox[2] for line in lines)
    y1 = max(line.bbox[1] + line.bbox[3] for line in lines)
    return OcrTextBlock(
        bbox=(x0, y0, x1 - x0, y1 - y0),
        lines=lines,
        ocr_text="\n".join(line.text for line in lines),
        translated_text=translated_text,
        bg_color=bg,
        fg_color=fg,
    )


def _settled_pixel(widget: QWidget, x: int, y: int, frames: int = 3, timeout_ms: int = 2000) -> QColor:
    """等到该像素连续 frames 帧不再变化（淡入等过渡结束）后返回它的颜色。"""
    deadline = time.monotonic() + timeout_ms / 1000.0
    last, same = None, 0
    while same < frames:
        assert time.monotonic() < deadline, "像素在超时内没有稳定"
        QTest.qWait(theme.OVERLAY_FRAME_MS)
        color = widget.grab().toImage().pixelColor(x, y).rgba()
        same = same + 1 if color == last else 0
        last = color
    return QColor.fromRgba(last)


def _wait_until(predicate, timeout_ms: int = 2000) -> bool:
    """处理 Qt 事件直到条件成立（PySide6 的 QTest 没有 qWaitFor）。"""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not predicate():
        if time.monotonic() > deadline:
            return False
        QTest.qWait(10)
    return True


def _open_overlay(screen_rect: tuple[int, int, int, int], mode: str, **callbacks) -> InplaceTranslateOverlay:
    overlay = InplaceTranslateOverlay(
        _make_png(screen_rect[2], screen_rect[3], bg_hex="#808080"),
        screen_rect,
        mode=mode,
        virtual_geometry=(0, 0, 1280, 800),
        available_screen_rect=(0, 0, 1280, 800),
        **callbacks,
    )
    overlay.show()
    return overlay


def test_inplace_overlay_86_percent_patch_and_ocr_rows_on_line_boxes(qapp: QApplication) -> None:
    """贴片 ≈ 0.86·采样底色 + 0.14·原图；OCR 模式每行画回自己的行框，字铺满原行宽。"""
    lines = [_ocr_line(30, 20, 200, 18, "Select any region"), _ocr_line(30, 46, 160, 18, "on your machine", True)]
    block = _ocr_block(lines)
    overlay = InplaceTranslateOverlay(
        _make_png(320, 180, bg_hex="#808080", patches=[((30, 20, 200, 18), "#000000"), ((30, 46, 160, 18), "#000000")]),
        (100, 100, 320, 180),
        mode="ocr",
        virtual_geometry=(0, 0, 1280, 800),
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        overlay.set_result([block], "", block.ocr_text)
        canvas = overlay.canvas_rect()
        assert canvas == QRect(100, 100, 320, 180)
        expected = round(theme.OVERLAY_PATCH_ALPHA + (1 - theme.OVERLAY_PATCH_ALPHA / 255) * 0x80)

        patch = _settled_pixel(overlay, canvas.x() + 27, canvas.y() + 29)
        assert abs(patch.red() - expected) <= 3, patch.name()
        assert overlay.grab().toImage().pixelColor(canvas.x() + 5, canvas.y() + 5).name() == "#808080"

        layout = layout_ocr_block(block)
        assert len({row.font_px for row in layout.rows}) == 1  # 一块字号统一
        img = overlay.grab().toImage()
        for line, row in zip(lines, layout.rows):
            x, y, w, h = line.bbox
            assert (row.x, row.top, row.height, row.text) == (x, y, h, line.text)
            assert abs(row.width - w) <= 1
            dark = [
                px
                for py in range(y, y + h)
                for px in range(canvas.x() + x - 6, canvas.x() + x + w + 6)
                if max(img.pixelColor(px, canvas.y() + py).getRgb()[:3]) < 90
            ]
            assert dark, line.text
            assert abs(min(dark) - (canvas.x() + x)) <= 4
            assert abs(max(dark) - (canvas.x() + x + w)) <= 4
    finally:
        overlay.close_silent()
        overlay.deleteLater()


def test_inplace_layout_segments_start_at_head_slots_shrink_then_extend(qapp: QApplication) -> None:
    """第 k 段从第 k 个段首槽位起排、原字号行距；本段槽位放不下先缩到 ≥9px 再下延；行数不符抛错。"""
    lines = [
        _ocr_line(10, 10, 400, 18, "Select any region of the screen and the"),
        _ocr_line(10, 32, 380, 18, "text inside it is recognized.", True),
        _ocr_line(30, 54, 150, 18, "keep_code(line)"),
    ]
    bottom = 54 + 18

    fits = layout_translated_block(_ocr_block(lines, "框选屏幕上的任意区域，其中文字会被识别。\n    keep_code(line)"))
    first, code = fits.rows[0], fits.rows[-1]
    block_px = first.font_px
    assert (first.x, first.top) == (10, 10)
    assert (code.text, code.x, code.top, code.font_px) == ("keep_code(line)", 30, 54, block_px)
    assert abs(code.width - 150) <= 1
    assert fits.extent.bottom() <= bottom

    shrunk = layout_translated_block(_ocr_block(lines, "框选屏幕上的任意区域，其中的文字会在本机完成识别，" * 2 + "\nkeep_code(line)"))
    head_rows = [row for row in shrunk.rows if row.text != "keep_code(line)"]
    assert len(head_rows) <= 2
    assert theme.OCR_MIN_FONT_PX <= head_rows[0].font_px < block_px
    assert {row.top for row in head_rows} <= {10, 32}
    assert shrunk.extent.bottom() <= bottom  # 不压下一块

    extended = layout_translated_block(_ocr_block(lines, "很长的译文需要向下延伸，" * 30 + "\nkeep_code(line)"))
    long_rows = [row for row in extended.rows if row.text != "keep_code(line)"]
    assert {row.font_px for row in long_rows} == {theme.OCR_MIN_FONT_PX}
    assert len(long_rows) > 2
    assert extended.extent.bottom() > bottom
    assert extended.rows[-1].top > 54  # 其后的段随之下移

    with pytest.raises(ValueError, match="硬行段"):
        layout_translated_block(_ocr_block(lines, "只有一行译文"))


def test_inplace_layout_font_size_and_baseline_from_line_ink_on_real_tesseract(qapp: QApplication) -> None:
    """原字号按行内墨迹换算、块内取中位数，基线由紧框上沿换算：无下行字母行、带括号代码行、中文行、
    单行无下行字母块，对 ocr_samples 生成的截图跑真实 Tesseract，字号与基线都在真值 ±1px 内。"""
    pytesseract = pytest.importorskip("pytesseract")
    from PIL import ImageFont

    from _ocr_layout import extract_ocr_layout
    from tests.logic.ocr_samples import CJK, CJK_SC, MONO, SANS, all_samples, fonts_missing

    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        pytest.skip(f"无法查询 tesseract 语言包: {exc}")
    missing = [lang for lang in ("eng", "chi_sim") if lang not in langs]
    if missing:
        pytest.skip(f"本机未安装 tesseract 语言包: {missing}")
    reason = fonts_missing()
    if reason:
        pytest.skip(reason)

    cases = (  # (样本, 真值字体, 真值字号, 字面序号, 识别语言)
        ("menu", SANS, 14, 0, "eng"),
        ("code", MONO, 14, 0, "eng"),
        ("chinese_paragraph", CJK, 16, CJK_SC, "chi_sim+eng"),
        ("dark_paragraph", SANS, 14, 0, "eng"),  # 第二块是单行、无下行字母
    )
    checked = 0
    for name, path, size, index, lang in cases:
        sample = all_samples()[name]
        truth_font = ImageFont.truetype(path, size, index=index)
        _w, _h, _text, blocks = extract_ocr_layout(sample.png, lang=lang)
        for block in blocks:
            for line, row in zip(block.lines, layout_ocr_block(block).rows):
                center = line.bbox[1] + line.bbox[3] / 2
                truth = min(sample.lines, key=lambda t: abs(t.bbox[1] + t.bbox[3] / 2 - center))
                mask, (_dx, dy) = truth_font.getmask2(truth.text, anchor="ls")
                truth_baseline = truth.bbox[1] - (dy + mask.getbbox()[1])
                assert abs(row.font_px - size) <= 1, (name, line.text, row.font_px, size)
                assert abs(row.baseline - truth_baseline) <= 1, (name, line.text, row.baseline, truth_baseline)
                checked += 1
    assert checked >= 15


def test_inplace_overlay_mini_menu_modes_placement_close_keys_and_copy_feedback(qapp: QApplication) -> None:
    """菜单按模式与失败重试、下 → 上 → 内避让；点框外关、点框内不关、Esc 关、Ctrl+C 不关；复制按返回值反馈。"""
    copies: list[str] = []
    copy_ok = [True]
    retries: list[int] = []
    closed: list[int] = []

    def on_copy(text: str) -> bool:
        copies.append(text)
        return copy_ok[0]

    block = _ocr_block([_ocr_line(10, 10, 120, 16, "Settings")], translated_text="设置")
    overlay = _open_overlay(
        (200, 160, 320, 180),
        "translate",
        on_copy_text=on_copy,
        on_retry=lambda: retries.append(1),
        on_closed=lambda: closed.append(1),
        copy_feedback_ms=150,
    )
    try:
        menu = overlay.menu
        assert menu.status_text() == "识别并翻译中…"
        assert menu.visible_labels() == []

        overlay.set_error("本地翻译模型连接超时")
        assert menu.status_text() == "✕ 翻译失败：本地翻译模型连接超时"
        assert menu.visible_labels() == ["重试"]
        QTest.mouseClick(menu.button("retry"), Qt.MouseButton.LeftButton)
        assert retries == [1]

        overlay.set_result([block], "设置", "Settings")
        assert menu.visible_labels() == ["复制译文", "复制原文"]
        assert overlay.menu_placement == "below"
        assert menu.geometry().top() > overlay.canvas_rect().bottom()

        QTest.mouseClick(menu.button(COPY_TRANSLATED), Qt.MouseButton.LeftButton)
        assert copies == ["设置"]
        assert menu.button(COPY_TRANSLATED).text() == "✓ 已复制"
        copy_ok[0] = False
        QTest.mouseClick(menu.button(COPY_OCR), Qt.MouseButton.LeftButton)
        assert copies == ["设置", "Settings"]
        assert menu.button(COPY_OCR).text() == "复制失败"
        assert _wait_until(lambda: menu.button(COPY_OCR).text() == "复制原文", 2000)

        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 200))
        QTest.keyClick(overlay, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert closed == []
        assert overlay.isVisible() is True
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(5, 5))
        assert closed == [1]
        assert overlay.isVisible() is False
    finally:
        overlay.close_silent()
        overlay.deleteLater()

    esc_closed: list[int] = []
    ocr_overlay = _open_overlay((200, 700, 320, 90), "ocr", on_closed=lambda: esc_closed.append(1))
    try:
        ocr_overlay.set_result([block], "", "Settings")
        assert ocr_overlay.menu.visible_labels() == ["复制"]
        assert ocr_overlay.menu_placement == "above"
        assert ocr_overlay.menu.geometry().bottom() < ocr_overlay.canvas_rect().top()
        QTest.keyClick(ocr_overlay, Qt.Key.Key_Escape)
        assert esc_closed == [1]
    finally:
        ocr_overlay.close_silent()
        ocr_overlay.deleteLater()

    tall = _open_overlay((200, 10, 320, 780), "ocr")
    try:
        assert tall.menu_placement == "inside"
        assert tall.canvas_rect().contains(tall.menu.geometry())
    finally:
        tall.close_silent()
        tall.deleteLater()


# ---------------------------------------------------------------------------
# 4. VoiceHudOverlay 一体底栏（UI 验收标准 docs/design/v3.3/voice-a.html）
# ---------------------------------------------------------------------------
def _fake_clock(start_s: float = 100.0):
    now = [start_s]

    def advance(ms: float) -> None:
        now[0] += ms / 1000.0

    return (lambda: now[0]), advance


def _bar_image(hud: VoiceHudOverlay) -> QImage:
    return hud.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _pixels_of(img: QImage, hex_color: str, tol: int = 6) -> list[tuple[int, int]]:
    target = QColor(hex_color)
    found = []
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if (
                c.alpha() == 255
                and abs(c.red() - target.red()) <= tol
                and abs(c.green() - target.green()) <= tol
                and abs(c.blue() - target.blue()) <= tol
            ):
                found.append((x, y))
    return found


def _painted_box(img: QImage) -> QRect:
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 0:
                xs.append(x)
                ys.append(y)
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def _max_alpha(img: QImage) -> int:
    return max(img.pixelColor(x, y).alpha() for y in range(img.height()) for x in range(img.width()))


def _centroid(points: list[tuple[int, int]]) -> QPoint:
    return QPoint(sum(x for x, _ in points) // len(points), sum(y for _, y in points) // len(points))


def test_voice_bar_single_window_bottom_center_flow_and_fade_out(qapp: QApplication) -> None:
    """录音 → 字幕 → 润色 → 已输入 → 保持 1.5s 后淡出：全程只有一个顶层窗、底部居中。"""
    clock, advance = _fake_clock()
    before = {w for w in QApplication.topLevelWidgets() if w.isVisible()}
    hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1280, 800), clock=clock)
    try:
        assert hud.isVisible() is False
        hud.set_recording()
        advance(400)
        assert hud.isVisible() is True
        assert hud.mode == "recording"
        assert hud.capsule_text == "录音中 0:00"

        hud.update_caption(
            stable_text="第一句历史前缀应当在超过两行后平滑滚出顶部视野之外，" * 3 + "当前正在稳定输出的倒数第二行内容，",
            tail_text="以及尾部实时跳动的增量识别片段",
        )
        advance(400)
        lines = hud.visible_caption_lines()
        assert len(lines) == 2
        assert lines[-1][1].endswith("增量识别片段")
        img = _bar_image(hud)
        assert _pixels_of(img, theme.STABLE_TEXT_HEX)
        assert _pixels_of(img, theme.TAIL_TEXT_HEX)

        assert [w for w in QApplication.topLevelWidgets() if w.isVisible() and w not in before] == [hud]
        assert [w for w in hud.findChildren(QWidget) if w.isWindow()] == []
        drawn = _painted_box(img)
        assert abs(hud.x() + drawn.center().x() + 0.5 - 640) <= 1
        assert abs(hud.y() + drawn.bottom() + 1 - (800 - 28)) <= 1
        geometry = hud.geometry()

        hud.set_polishing()
        advance(400)
        assert hud.mode == "polishing"
        assert hud.visible_caption_lines()[-1][1] == ""

        hud.set_typed("最终润色后的句子。")
        advance(400)
        hud.set_idle()  # 事件流随后的 idle 不打断已输入的保持
        assert hud.mode == "toast"
        assert hud.capsule_text == "✓ 已输入"
        assert hud.visible_caption_lines() == [("最终润色后的句子。", "")]
        assert hud.geometry() == geometry

        advance(CAPTION_AUTO_HIDE_MS - 400 - 1)
        hud._on_frame()
        assert hud.isVisible() is True
        assert _max_alpha(_bar_image(hud)) == 255
        advance(2)
        hud._on_frame()
        advance(theme.VOICE_FADE_OUT_MS / 2)
        assert hud.isVisible() is True
        assert 0 < _max_alpha(_bar_image(hud)) < 128
        advance(theme.VOICE_FADE_OUT_MS)
        hud._on_frame()
        assert hud.isVisible() is False
        assert hud.mode == "idle"
    finally:
        hud.close()
        hud.deleteLater()


def test_voice_bar_warning_notice_line_while_recording(qapp: QApplication) -> None:
    """录音中的警告走附加提示行：不改状态、秒表、字幕与几何；替换重新计时、3s 后淡出、新录音清空。"""
    clock, advance = _fake_clock()
    hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1280, 800), clock=clock)
    try:
        hud.set_recording()
        advance(1000)
        hud.update_caption("今天下午三点开会讨论", "语音底栏")
        advance(400)
        hud._on_record_tick()
        snapshot = (hud.mode, hud.capsule_text, hud.visible_caption_lines(), hud.geometry())
        assert _pixels_of(_bar_image(hud), theme.STATE_WARN_HEX) == []

        hud.show_toast("历史记录写入失败", kind="warn")
        advance(300)
        hud._on_record_tick()
        assert (hud.mode, hud.capsule_text, hud.visible_caption_lines(), hud.geometry()) == snapshot
        img = _bar_image(hud)
        warn = _pixels_of(img, theme.STATE_WARN_HEX)
        caption = _pixels_of(img, theme.STABLE_TEXT_HEX)
        assert warn
        assert max(y for _, y in warn) < min(y for _, y in caption)

        advance(1700)
        hud.show_toast("第二条警告", kind="warn")
        advance(1500)  # 第一条若未被替换此时早已淡出
        assert _pixels_of(_bar_image(hud), theme.STATE_WARN_HEX)
        advance(theme.VOICE_NOTICE_SHOW_MS + theme.VOICE_NOTICE_OUT_MS - 1500)
        hud._on_frame()
        assert _pixels_of(_bar_image(hud), theme.STATE_WARN_HEX) == []
        assert hud.mode == "recording"

        hud.show_toast("第三条警告", kind="warn")
        advance(300)
        hud.set_recording()
        advance(300)
        assert _pixels_of(_bar_image(hud), theme.STATE_WARN_HEX) == []

        hud.set_error("未检测到麦克风输入")
        assert hud.mode == "error"
        hud.show_toast("历史记录写入失败", kind="warn")
        assert hud.mode == "toast"
        assert hud.capsule_text == "历史记录写入失败"
    finally:
        hud.close()
        hud.deleteLater()


def test_voice_bar_only_confirm_card_takes_clicks(qapp: QApplication) -> None:
    """只有确认卡片区域接收点击并回调 on_decision(job_id?, confirmed)；其余状态鼠标穿透。"""
    clock, advance = _fake_clock()
    hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1280, 800), clock=clock)
    try:
        for enter in (
            hud.set_recording,
            hud.set_polishing,
            lambda: hud.set_error("未检测到麦克风"),
            lambda: hud.show_toast("历史记录写入失败", kind="warn"),
        ):
            enter()
            assert hud.is_mouse_passthrough is True
            assert hud.mask().isEmpty()

        decisions: list[bool] = []
        hud.show_delivery_confirmation("job-unknown-1", "需要确认是否已经输入的语音句子", decisions.append)
        advance(400)
        hud._on_frame()
        assert hud.mode == "confirm"
        assert hud.is_mouse_passthrough is False
        assert bool(hud.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        img = _bar_image(hud)
        card = hud.mask().boundingRect()
        drawn = _painted_box(img)
        for edge in ("left", "top", "right", "bottom"):
            assert abs(getattr(card, edge)() - getattr(drawn, edge)()) <= 1, edge
        assert hud.mask().contains(QPoint(2, 2)) is False

        QTest.mouseClick(hud, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, _centroid(_pixels_of(img, theme.ACCENT_HEX)))
        assert decisions == [True]
        assert hud.is_mouse_passthrough is True
        assert hud.mask().isEmpty()

        pairs: list[tuple[str, bool]] = []
        hud.show_delivery_confirmation("job-unknown-2", "第二句待确认文本", lambda job_id, ok: pairs.append((job_id, ok)))
        advance(400)
        hud._on_frame()
        ignore_at = _centroid(_pixels_of(_bar_image(hud), theme.SURFACE_2_HEX))
        QTest.mouseClick(hud, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, ignore_at)
        assert pairs == [("job-unknown-2", False)]
        assert hud.is_mouse_passthrough is True
    finally:
        hud.close()
        hud.deleteLater()


def test_caption_utf16_surrogates_emoji_and_cjk_ext_exact_mapping(
    qapp: QApplication,
) -> None:
    font = theme.make_font(pixel_size=theme.FS_CAPTION)
    # 含多个补充平面代理对字符（ord > 0xFFFF：Emoji 🚀🎙️🔥✨ 与 CJK 扩展汉字 𠀀𠀁𪚥）
    stable_short = "🚀🎙️𠀀𠀁稳定前缀"
    tail_short = "🔥✨𪚥尾部增量"
    short_lines = compute_trailing_two_lines(
        stable_text=stable_short,
        tail_text=tail_short,
        max_line_width=680,
        font=font,
    )
    assert len(short_lines) == 1
    assert short_lines[0] == (stable_short, tail_short)

    # 多行折行场景：穿插大量代理对字符，验证不丢字、stable/tail 分界零偏移、无 ('', '') 空行
    stable_multi = (
        "🚀🔥✨🎙️𠀀𠀁第一段前缀内容，" * 4
        + "🎯𪚥稳定末端边界"
    )
    tail_multi = "💡🚀𠀀流式尾部精确衔接✨"
    multi_lines = compute_trailing_two_lines(
        stable_text=stable_multi,
        tail_text=tail_multi,
        max_line_width=320,
        font=font,
    )
    assert len(multi_lines) == 2
    for s_seg, t_seg in multi_lines:
        assert (s_seg, t_seg) != ("", "")
        assert (s_seg + t_seg).strip() != ""

    rendered_stable = "".join(s for s, _ in multi_lines)
    rendered_tail = "".join(t for _, t in multi_lines)
    assert stable_multi.endswith(rendered_stable)
    assert "🎯𪚥稳定末端边界" in rendered_stable
    assert rendered_tail == tail_multi


# ---------------------------------------------------------------------------
# 5. StudioTrayIcon：矢量状态图标、完整菜单回调与无系统托盘安全降级
# ---------------------------------------------------------------------------
def test_tray_vector_icons_callbacks_and_safe_degradation(qapp: QApplication) -> None:
    # 校验动态绘制状态图标在 idle 与 recording 下角标颜色不同
    idle_img = render_status_image("idle", size=64)
    rec_img = render_status_image("recording", size=64)
    badge_pt = QPoint(int(64 * 0.76), int(64 * 0.76))
    assert idle_img.pixelColor(badge_pt).rgb() == QColor(theme.STATE_OK_HEX).rgb()
    assert rec_img.pixelColor(badge_pt).rgb() == QColor(theme.STATE_RECORDING_HEX).rgb()

    # 校验多尺寸托盘 QIcon 包含 64/48/32/24/20/16 槽位且重复 size 自动去重（适配 Windows 125%/150% DPI）
    idle_icon = render_status_icon("idle")
    expected_sizes = {(px, px) for px in (64, 48, 32, 24, 20, 16)}
    assert {(s.width(), s.height()) for s in idle_icon.availableSizes()} == expected_sizes
    for px in (64, 48, 32, 24, 20, 16):
        pm = idle_icon.pixmap(QSize(px, px))
        assert not pm.isNull()
        assert (pm.width(), pm.height()) == (px, px)
    dedup_icon = render_status_icon("idle", size=48)
    dedup_sizes = [(s.width(), s.height()) for s in dedup_icon.availableSizes()]
    assert len(dedup_sizes) == len(set(dedup_sizes))
    assert set(dedup_sizes) == {(px, px) for px in (48, 32, 24, 20, 16)}

    events: list[object] = []
    # 1. 无系统托盘环境安全降级（offscreen 默认或显式返回 False）
    tray_fallback = StudioTrayIcon(
        on_open_home=lambda: events.append("home"),
        on_screenshot_translate=lambda: events.append("shot"),
        on_pure_ocr=lambda: events.append("ocr"),
        on_screenshot_annotate=lambda: events.append("annotate"),
        on_open_setup=lambda: events.append("setup"),
        on_toggle_hud=lambda enabled: events.append(("hud", enabled)),
        on_quit=lambda: events.append("quit"),
        tray_available_fn=lambda: False,
    )
    try:
        assert tray_fallback.available is False
        assert tray_fallback.show() is False
        tray_fallback.set_state("recording", tooltip="录音中")
        assert tray_fallback.state == "recording"
        assert tray_fallback.tooltip == "录音中"
        assert tray_fallback.screenshot_annotate_action.text() == "截屏标注"

        tray_fallback.open_home_action.trigger()
        tray_fallback.screenshot_translate_action.trigger()
        tray_fallback.pure_ocr_action.trigger()
        tray_fallback.screenshot_annotate_action.trigger()
        tray_fallback.open_setup_action.trigger()
        tray_fallback.toggle_hud_action.trigger()  # 默认 True -> 切换为 False
        tray_fallback.quit_action.trigger()

        assert events == ["home", "shot", "ocr", "annotate", "setup", ("hud", False), "quit"]

        # 外部同步勾选状态不应重复触发回调
        tray_fallback.set_hud_enabled(True)
        assert tray_fallback.toggle_hud_action.isChecked() is True
        assert len(events) == 7
    finally:
        tray_fallback.close()

    # 2. 模拟系统托盘可用分支（且不传 on_screenshot_annotate 保持默认 None 兼容），验证挂载与左键触发打开主页
    tray_active = StudioTrayIcon(
        on_open_home=lambda: events.append("tray_click_home"),
        tray_available_fn=lambda: True,
    )
    try:
        assert tray_active.available is True
        tray_active.screenshot_annotate_action.trigger()
        tray_active.set_state("polishing")
        assert tray_active.state == "polishing"
        tray_active._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert events[-1] == "tray_click_home"
    finally:
        tray_active.close()


def test_tray_retry_server_item_only_while_server_stopped(qapp: QApplication) -> None:
    """U15：「重试启动本地服务」只在应用告知本地服务已停止时可见（不从错误状态推断），点击回调一次。"""
    retries: list[int] = []
    tray = StudioTrayIcon(on_retry_server=lambda: retries.append(1), tray_available_fn=lambda: False)
    try:
        item = tray.retry_server_action
        assert item.text() == "重试启动本地服务"
        assert item.isVisible() is False

        tray.set_state("error", tooltip="语音识别服务已停止：连续失败 5 次")
        assert item.isVisible() is False

        tray.set_server_stopped(True)
        assert item.isVisible() is True
        assert tray.menu.actions()[0] is item
        item.trigger()
        assert retries == [1]

        tray.set_state("idle")
        assert item.isVisible() is True
        tray.set_server_stopped(False)
        assert item.isVisible() is False
    finally:
        tray.close()


def test_tray_icon_appears_when_host_registers_and_reregisters_after_host_restart(
    qapp: QApplication, capsys: pytest.CaptureFixture[str]
) -> None:
    """托盘宿主晚到（锁屏、开机早于宿主）：先写一行原因，宿主注册后图标出现；宿主重启后隐藏再显示以重新登记。"""
    available = [False]
    host_registered: list = []
    tray = StudioTrayIcon(
        tray_available_fn=lambda: available[0],
        watch_host=lambda on_registered, _parent: host_registered.append(on_registered),
    )
    try:
        assert tray.show() is False
        assert tray.available is False
        assert "将在宿主注册时自动显示" in capsys.readouterr().out

        available[0] = True
        host_registered[0]()
        assert tray.available is True
        assert tray._tray.isVisible() is True

        calls: list[str] = []
        tray._tray.hide = lambda: calls.append("hide")
        tray._tray.show = lambda: calls.append("show")
        host_registered[0]()
        assert calls == ["hide", "show"]
    finally:
        tray.close()


def test_selector_transparent_live_mode_and_no_flicker_hud_guards(qapp: QApplication) -> None:
    """根治回归：透明实时框选不需预抓全屏、选区内部完全透明透出桌面、HUD 关闭输入法且防闪烁。"""
    selected: list[tuple[int, int, int, int]] = []
    sel = RedCrosshairSelector(
        virtual_geometry=(0, 0, 320, 220),
        mode_label="纯 OCR 识别",
        on_selected=lambda rect: selected.append(rect),
    )
    try:
        assert sel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
        assert sel.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is False
        sel.show()
        QTest.mousePress(sel, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 30))
        QTest.mouseMove(sel, QPoint(180, 130))
        qapp.processEvents()
        QTest.mouseRelease(sel, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(180, 130))
        qapp.processEvents()
        assert selected == [(40, 30, 140, 100)]
    finally:
        sel.close_silent()
        sel.deleteLater()

    now_box = [100.0]
    hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1920, 1080), clock=lambda: now_box[0])
    try:
        assert hud.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is False
        hud.set_recording()
        first_geom = hud.geometry()
        # 同秒内 tick 不变更文本，不应抖动几何或重触发 show
        now_box[0] = 100.5
        hud._on_record_tick()
        assert hud.geometry() == first_geom
        assert hud.capsule_text == "录音中 0:00"
        # 满 1 秒更新文案但宽度一致，保持同几何不闪动
        now_box[0] = 101.2
        hud._on_record_tick()
        assert hud.capsule_text == "录音中 0:01"
    finally:
        hud.close()
        hud.deleteLater()


def test_passive_overlay_flags_xcb_vs_wayland_and_offscreen(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-4：验证 passive_overlay_flags() 仅在 xcb 下附加 BypassWindowManagerHint，wayland/offscreen 不附加。"""
    del qapp
    monkeypatch.setattr(QGuiApplication, "platformName", staticmethod(lambda: "xcb"))
    xcb_flags = passive_overlay_flags()
    assert bool(xcb_flags & Qt.WindowType.FramelessWindowHint) is True
    assert bool(xcb_flags & Qt.WindowType.WindowStaysOnTopHint) is True
    assert bool(xcb_flags & Qt.WindowType.Tool) is True
    assert bool(xcb_flags & Qt.WindowType.WindowDoesNotAcceptFocus) is True
    assert bool(xcb_flags & Qt.WindowType.WindowTransparentForInput) is True
    assert bool(xcb_flags & Qt.WindowType.BypassWindowManagerHint) is True

    for plat in ("wayland", "offscreen"):
        monkeypatch.setattr(QGuiApplication, "platformName", staticmethod(lambda _p=plat: _p))
        other_flags = passive_overlay_flags()
        assert bool(other_flags & Qt.WindowType.WindowTransparentForInput) is True
        assert bool(other_flags & Qt.WindowType.WindowDoesNotAcceptFocus) is True
        assert bool(other_flags & Qt.WindowType.BypassWindowManagerHint) is False


# ---------------------------------------------------------------------------
# 6. AnnotateOverlay 截屏标注浮窗与 Esc 焦点裁决（U12 · annotate-icons-b.html）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _FakeSaveResult:
    path: str | None = None
    error: str | None = None


def _open_annotator(
    screen_rect: tuple[int, int, int, int],
    *,
    png: bytes | None = None,
    **kwargs,
) -> AnnotateOverlay:
    overlay = AnnotateOverlay(
        png if png is not None else _make_png(screen_rect[2], screen_rect[3], bg_hex="#ffffff"),
        screen_rect,
        virtual_geometry=(0, 0, 1280, 800),
        available_screen_rect=(0, 0, 1280, 800),
        **kwargs,
    )
    overlay.show()
    return overlay


def test_annotate_overlay_toolbar_placement_no_shift_and_outside_click_stays_open(
    qapp: QApplication,
) -> None:
    """U12 + v3.5 模板 B：309×38 纯矢量图标主条、34px 纯圆点二级条（马赛克藏色）、切工具 0px 抖动、inside 防穿透无死区、反馈变宽联动。"""
    with pytest.raises(ValueError):
        AnnotateOverlay(b"", (0, 0, 100, 100))
    with pytest.raises(ValueError):
        AnnotateOverlay(b"not-a-png", (0, 0, 100, 100))
    with pytest.raises(ValueError):
        AnnotateOverlay(_make_png(100, 100), (0, 0, 0, 100))

    expected_tips = {
        "rect": "矩形",
        "arrow": "箭头",
        "brush": "画笔",
        "text": "文字",
        "mosaic": "马赛克",
        "undo": "撤销",
        "copy": "复制 (Enter)",
        "save": "保存 PNG 到图片目录",
        "cancel": "取消 (Esc)",
    }

    closed: list[int] = []
    mid = _open_annotator((200, 160, 480, 260), on_closed=lambda: closed.append(1))
    try:
        assert mid.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
        assert mid.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is False
        assert mid.toolbar.size() == QSize(309, 38)
        assert mid.toolbar.visible_button_keys() == list(expected_tips)
        assert not hasattr(mid.toolbar, "visible_labels")
        assert not hasattr(mid.toolbar, "key_hint_text")
        assert not hasattr(mid.style_bar, "is_mosaic_label_visible")
        assert [lbl for lbl in mid.toolbar.findChildren(QLabel) if not lbl.isHidden()] == []
        for key, tip in expected_tips.items():
            btn = mid.toolbar.button(key)
            assert btn.text() == ""
            assert btn.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert btn.toolTip() == tip
            assert btn.accessibleName() == tip
            expected_w = theme.ANNOTATE_PRIMARY_BTN_W_PX if key == "copy" else theme.ANNOTATE_ICON_BTN_PX
            assert btn.size() == QSize(expected_w, theme.ANNOTATE_ICON_BTN_PX)
        for sz in theme.ANNOTATE_SIZES:
            sz_btn = mid.style_bar.size_button(sz.key)
            assert sz_btn.text() == ""
            assert sz_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert sz_btn.size() == QSize(theme.ANNOTATE_SIZE_BTN_PX, theme.ANNOTATE_SIZE_BTN_PX)
        assert mid.toolbar.button("undo").isEnabled() is False
        assert mid.style_bar.isVisible() is False
        assert mid.menu_placement == "below"
        assert mid.toolbar.geometry().top() > mid.canvas_rect().bottom()

        # 连续切换 5 工具 + 再点 toggle 收起 3 轮：主条与全部主条按钮 0px 位移，二级条色板/马赛克等高且 3 档动态 ToolTip 对齐
        baseline_bar = mid.toolbar.geometry()
        all_keys = tuple(expected_tips)
        baseline_btns = {k: mid.toolbar.button(k).geometry() for k in all_keys}
        sub_heights: set[int] = set()
        for _ in range(3):
            for key in ("rect", "arrow", "brush", "text", "mosaic"):
                QTest.mouseClick(mid.toolbar.button(key), Qt.MouseButton.LeftButton)
                qapp.processEvents()
                assert mid.current_tool == key
                assert mid.toolbar.button(key).property("active") == "true"
                assert mid.toolbar.geometry() == baseline_bar
                for bk, rect in baseline_btns.items():
                    assert mid.toolbar.button(bk).geometry() == rect
                assert mid.style_bar.isVisible() is True
                assert mid.style_bar.geometry().top() == mid.toolbar.geometry().bottom() + 1 + theme.ANNOTATE_SUB_BAR_GAP_PX
                sub_heights.add(mid.style_bar.height())
                if key == "mosaic":
                    assert mid.style_bar.is_palette_visible() is False
                    assert [lbl.text() for lbl in mid.style_bar.findChildren(QLabel) if not lbl.isHidden()] == ["10px"]
                    assert [mid.style_bar.size_button(s.key).toolTip() for s in theme.ANNOTATE_SIZES] == [
                        "细 · 块尺寸 (6px)",
                        "中 · 块尺寸 (10px)",
                        "粗 · 块尺寸 (16px)",
                    ]
                elif key == "text":
                    assert mid.style_bar.is_palette_visible() is True
                    assert [mid.style_bar.size_button(s.key).toolTip() for s in theme.ANNOTATE_SIZES] == [
                        "细 · 字号 (16px)",
                        "中 · 字号 (20px)",
                        "粗 · 字号 (28px)",
                    ]
                else:
                    assert mid.style_bar.is_palette_visible() is True
                    assert [mid.style_bar.size_button(s.key).toolTip() for s in theme.ANNOTATE_SIZES] == [
                        "细 (2px)",
                        "中 (3px)",
                        "粗 (6px)",
                    ]
                for s in theme.ANNOTATE_SIZES:
                    assert mid.style_bar.size_button(s.key).accessibleName() == mid.style_bar.size_button(s.key).toolTip()
            # 再次点击当前工具（马赛克）收起二级条，主条依然 0px 位移
            QTest.mouseClick(mid.toolbar.button("mosaic"), Qt.MouseButton.LeftButton)
            qapp.processEvents()
            assert mid.current_tool is None
            assert mid.style_bar.isVisible() is False
            assert mid.toolbar.geometry() == baseline_bar
        assert sub_heights == {theme.ANNOTATE_SUB_BAR_RESERVE_H_PX}
        assert mid.style_bar.reserved_height() == theme.ANNOTATE_SUB_BAR_RESERVE_H_PX

        # 框外为 DIM_MASK_RGBA 暗罩；点框外消费点击但不关闭浮窗
        grabbed = mid.grab().toImage()
        assert abs(grabbed.pixelColor(20, 20).alpha() - theme.DIM_MASK_RGBA[3]) <= 2
        QTest.mouseClick(mid, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(20, 20))
        qapp.processEvents()
        assert mid.isVisible() is True
        assert closed == []

        # 点「取消」关闭并回调 on_closed 一次
        QTest.mouseClick(mid.toolbar.button("cancel"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert mid.isVisible() is False
        assert closed == [1]
    finally:
        mid.close_silent()
        mid.deleteLater()

    # 贴底翻到框上：主条紧贴选区上缘，二级条翻到主条正上方且切换工具主条 0px 抖动
    silent_closed: list[int] = []
    bottom = _open_annotator((200, 700, 480, 90), on_closed=lambda: silent_closed.append(1))
    try:
        assert bottom.menu_placement == "above"
        bottom_main_before = bottom.toolbar.geometry()
        assert bottom_main_before.bottom() < bottom.canvas_rect().top()
        QTest.mouseClick(bottom.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert bottom.toolbar.geometry() == bottom_main_before
        assert bottom.style_bar.isVisible() is True
        assert bottom.style_bar.geometry().bottom() < bottom.toolbar.geometry().top()
        assert bottom.style_bar.geometry().top() >= 0
    finally:
        bottom.close_silent()
        bottom.deleteLater()
    assert silent_closed == []

    # 全高放进框内底部：主条与二级条均在画布内；inside 点击条留白/可见二级条不穿透起笔，收起后原区域恢复可画（无死区）
    tall = _open_annotator((200, 10, 560, 780))
    try:
        assert tall.menu_placement == "inside"
        assert tall.canvas_rect().contains(tall.toolbar.geometry())
        QTest.mouseClick(tall.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert tall.style_bar.isVisible() is True
        assert tall.canvas_rect().contains(tall.style_bar.geometry())
        assert tall.style_bar.geometry().bottom() < tall.toolbar.geometry().top()

        sub_center = tall.style_bar.geometry().center()
        # 直接向父窗发压在可见二级条上的鼠标拖拽：_point_on_bars 拦截不产生标注
        QTest.mousePress(tall, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sub_center)
        QTest.mouseMove(tall, sub_center + QPoint(24, 0))
        QTest.mouseRelease(tall, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sub_center + QPoint(24, 0))
        qapp.processEvents()
        assert tall.operations == ()

        # 在二级条空白标签处直接操作子控件本身：_OverlayBarFrame 吞事件不冒泡
        QTest.mousePress(tall.style_bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(2, 2))
        QTest.mouseRelease(tall.style_bar, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(2, 2))
        qapp.processEvents()
        assert tall.operations == ()

        # 隐藏二级条后原覆盖区域恢复可绘制，不留隐藏死区
        tall.style_bar.hide()
        QTest.mousePress(tall, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sub_center)
        QTest.mouseMove(tall, sub_center + QPoint(24, 0))
        QTest.mouseRelease(tall, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sub_center + QPoint(24, 0))
        qapp.processEvents()
        assert len(tall.operations) == 1  # 收起二级条后原区域无隐藏死区，正常起笔提交
    finally:
        tall.close_silent()
        tall.deleteLater()

    # 靠右窄选区：复制失败导致右侧状态栏显示「复制失败」主条变宽左移，复制钮锁宽 32px 且可见二级条同步联动对齐不出右屏
    narrow = _open_annotator(
        (980, 160, 260, 220),
        on_copy_image=lambda _png: False,
        copy_feedback_ms=500,
    )
    try:
        QTest.mouseClick(narrow.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        before_main_x = narrow.toolbar.geometry().x()
        QTest.mouseClick(narrow.toolbar.button("copy"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert narrow.toolbar.button("copy").width() == theme.ANNOTATE_PRIMARY_BTN_W_PX
        assert narrow.toolbar.is_copy_failed() is True
        assert narrow.toolbar.status_text() == "复制失败"
        assert narrow.toolbar.geometry().x() < before_main_x
        assert narrow.toolbar.geometry().right() < 1280
        assert narrow.style_bar.geometry().x() == narrow.toolbar.geometry().x()
        assert narrow.style_bar.geometry().right() < 1280
    finally:
        narrow.close_silent()
        narrow.deleteLater()

    # 极端矮屏 inside 纵向钳制：place_secondary_bar 保证二级条不越出 avail 上下界
    avail_tiny = QRect(0, 20, 800, 60)
    clamped_sub = place_secondary_bar(QRect(100, 42, 400, 34), QRect(0, 0, 320, 34).size(), "inside", avail_tiny)
    assert avail_tiny.contains(QRect(clamped_sub, QRect(0, 0, 320, 34).size()))


def test_annotate_overlay_four_tools_undo_replay_mosaic_average_and_export_resolution(
    qapp: QApplication,
) -> None:
    """U12：2× HiDPI 冻结图下四种标注画出、马赛克块平均、撤销重放还原、导出 PNG 分辨率严格等于原图。"""
    # 原冻结图 640×360（2×），画布逻辑尺寸 320×180；在图像像素 (200, 200, 40, 20) 放半黑半彩双色块验证马赛克块平均
    hi_png = _make_png(
        640,
        360,
        bg_hex="#ffffff",
        patches=[
            ((200, 200, 10, 20), "#000000"),
            ((210, 200, 10, 20), "#c86432"),  # (200, 100, 50) -> 与黑平均 ≈ (100, 50, 25)
            ((220, 200, 20, 20), "#00c864"),  # 整块纯绿 (0, 200, 100)
        ],
    )
    overlay = _open_annotator((100, 80, 320, 180), png=hi_png)
    try:
        c = overlay.canvas_rect()

        # 1. 矩形（画布 (20, 20) -> (90, 70)）
        QTest.mouseClick(overlay.toolbar.button("rect"), Qt.MouseButton.LeftButton)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(20, 20))
        QTest.mouseMove(overlay, c.topLeft() + QPoint(90, 70))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(90, 70))
        qapp.processEvents()
        assert len(overlay.operations) == 1
        assert overlay.toolbar.button("undo").isEnabled() is True

        # 2. 箭头（画布 (120, 30) -> (220, 30)）
        QTest.mouseClick(overlay.toolbar.button("arrow"), Qt.MouseButton.LeftButton)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(120, 30))
        QTest.mouseMove(overlay, c.topLeft() + QPoint(220, 30))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(220, 30))
        qapp.processEvents()
        assert len(overlay.operations) == 2

        # 3. 文字（画布 (20, 120)，输入框开启输入法，Enter 提交）
        QTest.mouseClick(overlay.toolbar.button("text"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(20, 120))
        qapp.processEvents()
        assert overlay.is_editing_text is True
        edit = overlay._text_edit
        assert edit is not None
        assert edit.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is True
        edit.setText("标注测试")
        QTest.keyClick(edit, Qt.Key.Key_Return)
        qapp.processEvents()
        assert overlay.is_editing_text is False
        assert len(overlay.operations) == 3

        # 4. 马赛克（画布 (100, 100) -> (120, 110)，对应 2× 图像像素 (200, 200, 40, 20)，2 块×1 块，每块 20×20 像素）
        QTest.mouseClick(overlay.toolbar.button("mosaic"), Qt.MouseButton.LeftButton)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(100, 100))
        QTest.mouseMove(overlay, c.topLeft() + QPoint(120, 110))
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(120, 110))
        qapp.processEvents()
        assert len(overlay.operations) == 4

        # 校验导出 PNG 分辨率严格等于原冻结图 640×360，且四种标注像素均已合入
        exported = QImage.fromData(overlay.export_png())
        assert (exported.width(), exported.height()) == (640, 360)
        red = QColor(theme.CROSSHAIR_HEX)
        assert exported.pixelColor(40, 40).rgb() == red.rgb()  # 矩形左上角 (20*2, 20*2)
        assert exported.pixelColor(300, 60).rgb() == red.rgb()  # 箭头杆中段 (150*2, 30*2)
        assert exported.pixelColor(436, 60).rgb() == red.rgb()  # 箭头实心头部 (218*2, 30*2)
        text_region = exported.copy(QRect(36, 236, 220, 60))
        assert _pixels_of(text_region, theme.CROSSHAIR_HEX, tol=16)

        # 马赛克左侧块 (200..219, 200..219) 全部均化为约 (100, 50, 25)，右侧块保持 (0, 200, 100)
        m_left_a = exported.pixelColor(202, 205)
        m_left_b = exported.pixelColor(217, 215)
        assert m_left_a.rgb() == m_left_b.rgb()
        assert abs(m_left_a.red() - 100) <= 2
        assert abs(m_left_a.green() - 50) <= 2
        assert abs(m_left_a.blue() - 25) <= 2
        assert exported.pixelColor(230, 210).getRgb()[:3] == (0, 200, 100)

        # 撤销马赛克：重放后恢复原始黑/彩清晰分界，其它三种标注仍在
        QTest.mouseClick(overlay.toolbar.button("undo"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert len(overlay.operations) == 3
        after_undo_mosaic = overlay.export_image()
        assert after_undo_mosaic.pixelColor(202, 205).getRgb()[:3] == (0, 0, 0)
        assert after_undo_mosaic.pixelColor(215, 205).getRgb()[:3] == (200, 100, 50)
        assert after_undo_mosaic.pixelColor(40, 40).rgb() == red.rgb()

        # 全部撤销回空：撤销按钮重新置灰，合成图恢复为原冻结图
        for _ in range(3):
            QTest.mouseClick(overlay.toolbar.button("undo"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert overlay.operations == ()
        assert overlay.toolbar.button("undo").isEnabled() is False
        restored = overlay.export_image()
        assert restored.pixelColor(40, 40).name() == "#ffffff"
        assert restored.pixelColor(300, 60).name() == "#ffffff"
    finally:
        overlay.close_silent()
        overlay.deleteLater()


def test_annotate_overlay_copy_and_save_feedback_and_auto_close(qapp: QApplication) -> None:
    """U12：复制 False 提示 1.5s 不关 / True 立即关；保存 error 提示不关 / path 显示路径 2.5s 后关。"""
    # 验证默认保存自动关闭延迟取自 theme.ANNOTATE_SAVED_FEEDBACK_MS (2500)
    default_ov = _open_annotator((100, 100, 320, 180))
    try:
        assert default_ov._saved_feedback_ms == theme.ANNOTATE_SAVED_FEEDBACK_MS == 2500
    finally:
        default_ov.close_silent()
        default_ov.deleteLater()

    # 1. 复制 False 不关并限时恢复 -> 复制 True 立即关并回调 on_closed
    copy_payloads: list[bytes] = []
    copy_ok = [False]
    copy_closed: list[int] = []
    ov_copy = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda png: (copy_payloads.append(png), copy_ok[0])[1],
        on_closed=lambda: copy_closed.append(1),
        copy_feedback_ms=80,
    )
    try:
        QTest.mouseClick(ov_copy.toolbar.button("copy"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert len(copy_payloads) == 1
        assert ov_copy.isVisible() is True
        assert copy_closed == []
        assert ov_copy.toolbar.button("copy").text() == ""
        assert ov_copy.toolbar.button("copy").width() == theme.ANNOTATE_PRIMARY_BTN_W_PX
        assert ov_copy.toolbar.is_copy_failed() is True
        assert ov_copy.toolbar.button("copy").property("feedback") == "fail"
        assert ov_copy.toolbar.status_text() == "复制失败"
        assert _wait_until(
            lambda: not ov_copy.toolbar.is_copy_failed() and ov_copy.toolbar.status_text() == "",
            1000,
        )
        assert ov_copy.toolbar.size() == QSize(309, 38)

        copy_ok[0] = True
        QTest.mouseClick(ov_copy.toolbar.button("copy"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert len(copy_payloads) == 2
        assert ov_copy.isVisible() is False
        assert copy_closed == [1]
    finally:
        ov_copy.close_silent()
        ov_copy.deleteLater()

    # 2. 保存 error 不关 -> 保存 path 显示路径并在 saved_feedback_ms 后关闭
    save_results: list[_FakeSaveResult] = [
        _FakeSaveResult(error="目录无写权限"),
        _FakeSaveResult(path="/home/u/Pictures/Ollama Voice Input/截屏 2026-11-21 10-00-00.png"),
    ]
    save_closed: list[int] = []
    ov_save = _open_annotator(
        (100, 100, 320, 180),
        on_save_image=lambda _png: save_results.pop(0),
        on_closed=lambda: save_closed.append(1),
        saved_feedback_ms=90,
    )
    try:
        QTest.mouseClick(ov_save.toolbar.button("save"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_save.isVisible() is True
        assert save_closed == []
        assert ov_save.toolbar.status_text() == "保存失败：目录无写权限"

        QTest.mouseClick(ov_save.toolbar.button("save"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_save.isVisible() is True
        assert ov_save.toolbar.status_text() == "✓ 已保存 /home/u/Pictures/Ollama Voice Input/截屏 2026-11-21 10-00-00.png"
        assert save_closed == []
        assert _wait_until(lambda: not ov_save.isVisible(), 1500)
        assert save_closed == [1]
    finally:
        ov_save.close_silent()
        ov_save.deleteLater()

    # 3. 契约违例 SaveResult（全空）立即报错，不静默兜底
    ov_bad = _open_annotator((100, 100, 320, 180), on_save_image=lambda _png: _FakeSaveResult())
    try:
        with pytest.raises(ValueError, match="SaveResult"):
            ov_bad._save_image()
    finally:
        ov_bad.close_silent()
        ov_bad.deleteLater()

    # 4. 四态互斥：copy_failed → save_ok 与 copy_failed → save_failed 均停复制表、清红框，且 _clear_copy_failed 不误清保存文案
    mutex_closed: list[int] = []
    ov_mutex_ok = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda _png: False,
        on_save_image=lambda _png: _FakeSaveResult(path="/home/u/Pictures/ok.png"),
        on_closed=lambda: mutex_closed.append(1),
        copy_feedback_ms=80,
        saved_feedback_ms=120,
    )
    try:
        QTest.mouseClick(ov_mutex_ok.toolbar.button("copy"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_mutex_ok.toolbar.is_copy_failed() is True
        assert ov_mutex_ok.toolbar.status_text() == "复制失败"
        assert ov_mutex_ok._copy_reset_timer.isActive() is True

        QTest.mouseClick(ov_mutex_ok.toolbar.button("save"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_mutex_ok._copy_reset_timer.isActive() is False
        assert ov_mutex_ok.toolbar.is_copy_failed() is False
        assert ov_mutex_ok.toolbar.status_text() == "✓ 已保存 /home/u/Pictures/ok.png"
        assert ov_mutex_ok._save_close_timer.isActive() is True

        # 直接触发 _clear_copy_failed() 守卫：绝不误清已保存文案
        ov_mutex_ok._clear_copy_failed()
        assert ov_mutex_ok.toolbar.status_text() == "✓ 已保存 /home/u/Pictures/ok.png"
        assert _wait_until(lambda: not ov_mutex_ok.isVisible(), 1500)
        assert mutex_closed == [1]
    finally:
        ov_mutex_ok.close_silent()
        ov_mutex_ok.deleteLater()

    ov_mutex_err = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda _png: False,
        on_save_image=lambda _png: _FakeSaveResult(error="磁盘已满"),
        copy_feedback_ms=80,
    )
    try:
        QTest.mouseClick(ov_mutex_err.toolbar.button("copy"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_mutex_err._copy_reset_timer.isActive() is True

        QTest.mouseClick(ov_mutex_err.toolbar.button("save"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov_mutex_err._copy_reset_timer.isActive() is False
        assert ov_mutex_err.toolbar.is_copy_failed() is False
        assert ov_mutex_err.toolbar.status_text() == "保存失败：磁盘已满"

        ov_mutex_err._clear_copy_failed()
        assert ov_mutex_err.toolbar.status_text() == "保存失败：磁盘已满"
        assert ov_mutex_err.isVisible() is True
    finally:
        ov_mutex_err.close_silent()
        ov_mutex_err.deleteLater()


def test_overlays_escape_focus_arbitration_and_two_step_text_edit(qapp: QApplication) -> None:
    """U12（10.17 焦点裁决）：有焦点时 handle_escape 不做事仅按键事件生效；无焦点时 handle_escape 生效且连按两次均处理。"""
    other = QWidget()
    other.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    other.setGeometry(0, 0, 120, 80)
    other.show()
    try:
        # 1. AnnotateOverlay 持有键盘焦点：文字输入中 handle_escape 不做事；按键 Esc 仅结束输入；再次 handle_escape 仍不做事；第二次按键 Esc 关闭
        closed_focused: list[int] = []
        ov_focused = _open_annotator((100, 100, 320, 180), on_closed=lambda: closed_focused.append(1))
        try:
            ov_focused.activateWindow()
            ov_focused.setFocus()
            qapp.processEvents()
            assert ov_focused.isActiveWindow() is True

            QTest.mouseClick(ov_focused.toolbar.button("text"), Qt.MouseButton.LeftButton)
            QTest.mouseClick(ov_focused, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(140, 140))
            qapp.processEvents()
            assert ov_focused.is_editing_text is True
            assert ov_focused._text_edit is not None
            ov_focused._text_edit.setText("未提交")

            # 全局 Esc 先到：窗口有焦点 -> 不做事
            ov_focused.handle_escape()
            qapp.processEvents()
            assert ov_focused.is_editing_text is True
            assert ov_focused.isVisible() is True
            assert closed_focused == []

            # 窗内第一次 Esc：只结束文字输入，不提交、不关闭，焦点回到浮窗
            QTest.keyClick(ov_focused._text_edit, Qt.Key.Key_Escape)
            qapp.processEvents()
            assert ov_focused.is_editing_text is False
            assert ov_focused.operations == ()
            assert ov_focused.isVisible() is True
            assert closed_focused == []

            # 同一次物理按键若全局 Esc 晚到：浮窗仍持有焦点 -> 不做事（不会被一次按键关掉）
            ov_focused.handle_escape()
            qapp.processEvents()
            assert ov_focused.isVisible() is True
            assert closed_focused == []

            # 第二次窗内 Esc：关闭浮窗
            QTest.keyClick(ov_focused, Qt.Key.Key_Escape)
            qapp.processEvents()
            assert ov_focused.isVisible() is False
            assert closed_focused == [1]
        finally:
            ov_focused.close_silent()
            ov_focused.deleteLater()

        # 2. AnnotateOverlay 无键盘焦点：连续两次 handle_escape（0ms 间隔，无时间去重）分别结束文字输入、关闭浮窗
        closed_unfocused: list[int] = []
        ov_unfocused = _open_annotator((100, 100, 320, 180), on_closed=lambda: closed_unfocused.append(1))
        try:
            QTest.mouseClick(ov_unfocused.toolbar.button("text"), Qt.MouseButton.LeftButton)
            QTest.mouseClick(ov_unfocused, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(140, 140))
            qapp.processEvents()
            assert ov_unfocused.is_editing_text is True

            other.activateWindow()
            other.setFocus()
            qapp.processEvents()
            assert ov_unfocused.isActiveWindow() is False

            ov_unfocused.handle_escape()
            assert ov_unfocused.is_editing_text is False
            assert ov_unfocused.isVisible() is True
            assert closed_unfocused == []

            ov_unfocused.handle_escape()
            qapp.processEvents()
            assert ov_unfocused.isVisible() is False
            assert closed_unfocused == [1]
        finally:
            ov_unfocused.close_silent()
            ov_unfocused.deleteLater()

        # 3. InplaceTranslateOverlay 焦点裁决：有焦点时 handle_escape 不关（按键关）；无焦点时 handle_escape 直接关闭并回调
        inplace_focused_closed: list[int] = []
        ip_focused = _open_overlay((100, 100, 320, 180), "ocr", on_closed=lambda: inplace_focused_closed.append(1))
        try:
            ip_focused.activateWindow()
            ip_focused.setFocus()
            qapp.processEvents()
            assert ip_focused.isActiveWindow() is True

            ip_focused.handle_escape()
            qapp.processEvents()
            assert ip_focused.isVisible() is True
            assert inplace_focused_closed == []

            QTest.keyClick(ip_focused, Qt.Key.Key_Escape)
            qapp.processEvents()
            assert ip_focused.isVisible() is False
            assert inplace_focused_closed == [1]
        finally:
            ip_focused.close_silent()
            ip_focused.deleteLater()

        inplace_unfocused_closed: list[int] = []
        ip_unfocused = _open_overlay((100, 100, 320, 180), "ocr", on_closed=lambda: inplace_unfocused_closed.append(1))
        try:
            other.activateWindow()
            other.setFocus()
            qapp.processEvents()
            assert ip_unfocused.isActiveWindow() is False

            ip_unfocused.handle_escape()
            qapp.processEvents()
            assert ip_unfocused.isVisible() is False
            assert inplace_unfocused_closed == [1]
        finally:
            ip_unfocused.close_silent()
            ip_unfocused.deleteLater()
    finally:
        other.close()
        other.deleteLater()


def test_annotate_brush_closed_loop_step_dedup_styles_and_hidpi_export(qapp: QApplication) -> None:
    """v3.5 ST1-ST3：initial_style 强校验、画笔 2px 去重+松手补末点+闭合圆保留、6色3档、底缘大字重钳制与 2× HiDPI 导出。"""
    # 1. initial_style 非法值立即抛 ValueError；合法初值生效且不触发 on_style_changed
    with pytest.raises(ValueError):
        AnnotateOverlay(_make_png(100, 100), (0, 0, 100, 100), initial_style=("#123456", "medium"))
    with pytest.raises(ValueError):
        AnnotateOverlay(_make_png(100, 100), (0, 0, 100, 100), initial_style=("#ff2d2d", "xlarge"))
    with pytest.raises(ValueError):
        AnnotateOverlay(_make_png(100, 100), (0, 0, 100, 100), initial_style=("#ff2d2d",))  # type: ignore[arg-type]

    hi_png = _make_png(
        640,
        360,
        bg_hex="#ffffff",
        patches=[
            ((40, 260, 16, 32), "#000000"),
            ((56, 260, 16, 32), "#ffffff"),
        ],
    )
    style_events: list[tuple[str, str]] = []
    ov = _open_annotator(
        (100, 80, 320, 180),
        png=hi_png,
        initial_style=("#2f81f7", "thick"),
        on_style_changed=style_events.append,
    )
    try:
        assert ov.current_style == ("#2f81f7", "thick")
        assert ov.current_color == "#2f81f7"
        assert ov.current_size_level == "thick"
        assert style_events == []  # 初始化不触发 on_style_changed

        c = ov.canvas_rect()
        QTest.mouseClick(ov.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.style_bar.px_text() == "6px"

        # 2. 画笔 2px 步长去重 + 松手 <2px 补末点：起始 (30,30)，+1px 忽略，+10px 采纳，松手在 (41,30) 距末点 1px 强制补末点
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(30, 30))
        QTest.mouseMove(ov, c.topLeft() + QPoint(31, 30))  # 1px < 2px，去重
        assert ov._draft is not None and len(ov._draft.points) == 1
        QTest.mouseMove(ov, c.topLeft() + QPoint(40, 30))  # 10px >= 2px，采纳
        assert ov._draft is not None and len(ov._draft.points) == 2
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(41, 30))
        qapp.processEvents()
        assert len(ov.operations) == 1
        first_pts = ov.operations[0].points
        assert len(first_pts) == 3
        assert (first_pts[0].x(), first_pts[-1].x()) == (30.0, 41.0)
        assert ov.operations[0].color == "#2f81f7"
        assert ov.operations[0].stroke_px == 6

        # 3. 极短误触（<2px）不提交
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(10, 10))
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(11, 10))
        qapp.processEvents()
        assert len(ov.operations) == 1

        # 4. 闭合圆（起终点完全重合，但总路径长 >= 2px）有效提交并合入 2× HiDPI 导出图
        QTest.mouseClick(ov.style_bar.swatch_button("#f59e0b"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov.style_bar.size_button("medium"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.current_style == ("#f59e0b", "medium")
        assert style_events == [("#f59e0b", "thick"), ("#f59e0b", "medium")]
        assert ov.style_bar.px_text() == "3px"

        loop = [(120, 40), (150, 40), (150, 70), (120, 70), (120, 40)]
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(*loop[0]))
        for px, py in loop[1:-1]:
            QTest.mouseMove(ov, c.topLeft() + QPoint(px, py))
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(*loop[-1]))
        qapp.processEvents()
        assert len(ov.operations) == 2
        closed_op = ov.operations[1]
        assert closed_op.start == closed_op.end == QPointF(120.0, 40.0)
        assert closed_op.color == "#f59e0b"
        assert closed_op.stroke_px == 3

        # 5. 文字工具近底缘打开（小字 16px）后在输入中切颜色（黑）与大字号（28px）：实时刷新内联框并重钳制 y 不出选区底缘
        QTest.mouseClick(ov.toolbar.button("text"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov.style_bar.size_button("thin"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.style_bar.px_text() == "16px"
        QTest.mouseClick(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(180, 155))
        qapp.processEvents()
        assert ov.is_editing_text is True and ov._text_edit is not None
        ov._text_edit.setText("底缘大字")
        QTest.mouseClick(ov.style_bar.swatch_button("#111827"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov.style_bar.size_button("thick"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.style_bar.px_text() == "28px"
        assert ov._text_edit.font().pixelSize() == 28
        assert ov._text_edit.geometry().bottom() <= c.bottom()
        QTest.keyClick(ov._text_edit, Qt.Key.Key_Return)
        qapp.processEvents()
        assert len(ov.operations) == 3
        text_op = ov.operations[2]
        assert text_op.color == "#111827"
        assert text_op.text_px == 28
        assert text_op.start.y() + text_op.text_px <= c.height() + 2

        # 6. 马赛克档位（大块 16px）切回验证像素块均化与 2× HiDPI 分辨率
        QTest.mouseClick(ov.toolbar.button("mosaic"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.style_bar.px_text() == "16px"
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(20, 130))
        QTest.mouseMove(ov, c.topLeft() + QPoint(36, 146))
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(36, 146))
        qapp.processEvents()
        assert len(ov.operations) == 4
        assert ov.operations[3].block_px == 16

        exported = QImage.fromData(ov.export_png())
        assert (exported.width(), exported.height()) == (640, 360)
        # 蓝粗画笔中点 (35*2, 30*2) = (70, 60)
        assert exported.pixelColor(70, 60).rgb() == QColor("#2f81f7").rgb()
        # 橙黄闭合圆上边中点 (135*2, 40*2) = (270, 80)
        assert exported.pixelColor(270, 80).rgb() == QColor("#f59e0b").rgb()
        # 黑色 28px 文字区域含黑字像素
        assert _pixels_of(exported.copy(QRect(350, 270, 260, 88)), "#111827", tol=24)
        # 马赛克 16px 逻辑块在 2× 图为 32px 整块（40..71, 260..291）黑白各半 -> 约 128 灰
        m_px = exported.pixelColor(45, 270)
        assert 115 <= m_px.red() <= 140
        assert exported.pixelColor(65, 270).rgb() == m_px.rgb()

        # 撤销全部 4 笔后恢复原白底
        for _ in range(4):
            ov.undo()
        assert ov.operations == ()
        assert ov.export_image().pixelColor(70, 60).name() == "#ffffff"
        assert ov.export_image().pixelColor(270, 80).name() == "#ffffff"
    finally:
        ov.close_silent()
        ov.deleteLater()


def test_annotate_overlay_enter_copy_modifiers_draft_save_timer_and_inplace_unchanged(
    qapp: QApplication,
) -> None:
    """v3.5 ST2-ST3：无修饰主/小键盘 Enter 复制关闭、修饰键 Enter 屏蔽、文字两步 Enter、停保存表、草稿收敛与原位浮窗不变。"""
    # 1. 非输入态：修饰键 Enter 不触发；文字输入态：修饰键 Enter 吞掉不提交也不冒泡复制；无修饰 Enter 先提交文字，再按复制关闭
    copied_1: list[bytes] = []
    closed_1: list[int] = []
    ov1 = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda png: (copied_1.append(png), True)[1],
        on_closed=lambda: closed_1.append(1),
    )
    try:
        c = ov1.canvas_rect()
        for mod in (
            Qt.KeyboardModifier.ControlModifier,
            Qt.KeyboardModifier.AltModifier,
            Qt.KeyboardModifier.ShiftModifier,
            Qt.KeyboardModifier.MetaModifier,
        ):
            QTest.keyClick(ov1, Qt.Key.Key_Return, mod)
            qapp.processEvents()
        assert copied_1 == [] and closed_1 == [] and ov1.isVisible() is True

        # 进入文字输入态
        QTest.mouseClick(ov1.toolbar.button("text"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov1, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(30, 40))
        qapp.processEvents()
        assert ov1.is_editing_text is True and ov1._text_edit is not None
        ov1._text_edit.setText("回车测试")
        # 修饰键 Enter 在文字框内被吞掉：既不提交，也不冒泡触发复制关闭
        QTest.keyClick(ov1._text_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(ov1._text_edit, Qt.Key.Key_Enter, Qt.KeyboardModifier.ShiftModifier)
        qapp.processEvents()
        assert ov1.is_editing_text is True
        assert ov1.operations == ()
        assert copied_1 == [] and closed_1 == [] and ov1.isVisible() is True

        # 第一次无修饰 Enter：仅提交文字并退出输入态，不复制不关闭
        QTest.keyClick(ov1._text_edit, Qt.Key.Key_Return)
        qapp.processEvents()
        assert ov1.is_editing_text is False
        assert len(ov1.operations) == 1
        assert copied_1 == [] and closed_1 == [] and ov1.isVisible() is True

        # 第二次无修饰 Enter：复制含该段文字的合成图并关闭
        QTest.keyClick(ov1, Qt.Key.Key_Return)
        qapp.processEvents()
        assert len(copied_1) == 1
        assert closed_1 == [1]
        assert ov1.isVisible() is False
        exported = QImage.fromData(copied_1[0])
        assert _pixels_of(exported.copy(QRect(24, 34, 180, 50)), theme.CROSSHAIR_HEX, tol=16)
    finally:
        ov1.close_silent()
        ov1.deleteLater()

    # 2. 小键盘 Key_Enter + KeypadModifier 同样触发复制关闭，且按 Enter 前会收敛拖拽中的有效草稿
    copied_2: list[bytes] = []
    closed_2: list[int] = []
    ov2 = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda png: (copied_2.append(png), True)[1],
        on_closed=lambda: closed_2.append(1),
    )
    try:
        c = ov2.canvas_rect()
        QTest.mouseClick(ov2.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        QTest.mousePress(ov2, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(20, 20))
        QTest.mouseMove(ov2, c.topLeft() + QPoint(80, 20))
        assert ov2._draft is not None and ov2.operations == ()
        # 未松手直接小键盘 Enter：收敛有效画笔草稿并导出复制关闭
        QTest.keyClick(ov2, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
        qapp.processEvents()
        assert len(copied_2) == 1 and closed_2 == [1] and ov2.isVisible() is False
        assert len(ov2.operations) == 1
        assert QImage.fromData(copied_2[0]).pixelColor(50, 20).rgb() == QColor(theme.CROSSHAIR_HEX).rgb()
    finally:
        ov2.close_silent()
        ov2.deleteLater()

    # 3. 保存成功启动自动关闭计时器后，再按 Enter 复制但复制失败：停止 _save_close_timer 并清残留保存态，超期仍不关
    copied_3: list[bytes] = []
    closed_3: list[int] = []
    ov3 = _open_annotator(
        (100, 100, 320, 180),
        on_copy_image=lambda png: (copied_3.append(png), False)[1],
        on_save_image=lambda _png: _FakeSaveResult(path="/home/u/Pictures/fake.png"),
        on_closed=lambda: closed_3.append(1),
        copy_feedback_ms=100,
        saved_feedback_ms=40,
    )
    try:
        QTest.mouseClick(ov3.toolbar.button("save"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov3.toolbar.status_text().startswith("✓ 已保存 ")
        assert ov3._save_close_timer.isActive() is True

        # 立刻按 Enter 复制（返回 False）：停保存自动关闭表、清保存文案、亮红框且右侧显「复制失败」；
        # 等到 copy_feedback_ms(100ms) > saved_feedback_ms(40ms) 恢复常态时浮窗仍未关闭
        QTest.keyClick(ov3, Qt.Key.Key_Return)
        qapp.processEvents()
        assert len(copied_3) == 1
        assert ov3._save_close_timer.isActive() is False
        assert ov3.toolbar.is_copy_failed() is True
        assert ov3.toolbar.button("copy").text() == ""
        assert ov3.toolbar.button("copy").width() == theme.ANNOTATE_PRIMARY_BTN_W_PX
        assert ov3.toolbar.status_text() == "复制失败"
        assert _wait_until(
            lambda: not ov3.toolbar.is_copy_failed() and ov3.toolbar.status_text() == "",
            1000,
        )
        assert ov3.isVisible() is True
        assert closed_3 == []

        # 切工具时丢弃未松手草稿（防残留幽灵草稿）
        c = ov3.canvas_rect()
        QTest.mouseClick(ov3.toolbar.button("rect"), Qt.MouseButton.LeftButton)
        QTest.mousePress(ov3, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(20, 20))
        QTest.mouseMove(ov3, c.topLeft() + QPoint(90, 90))
        assert ov3._draft is not None
        ov3.pick_tool("arrow")
        assert ov3._draft is None
    finally:
        ov3.close_silent()
        ov3.deleteLater()

    # 4. InplaceTranslateOverlay（截屏翻译/纯 OCR）完全不受影响：按 Enter 不复制也不关闭
    inplace_copies: list[str] = []
    inplace_closed: list[int] = []
    block = _ocr_block([_ocr_line(10, 10, 120, 16, "Hello")], translated_text="你好")
    ip = _open_overlay(
        (200, 160, 320, 180),
        "translate",
        on_copy_text=lambda t: (inplace_copies.append(t), True)[1],
        on_closed=lambda: inplace_closed.append(1),
    )
    try:
        ip.set_result([block], "你好", "Hello")
        QTest.keyClick(ip, Qt.Key.Key_Return)
        QTest.keyClick(ip, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
        qapp.processEvents()
        assert inplace_copies == []
        assert inplace_closed == []
        assert ip.isVisible() is True
    finally:
        ip.close_silent()
        ip.deleteLater()


def test_annotate_real_chain_capture_flow_bridge_overlay_memory_and_enter_copy(
    qapp: QApplication,
) -> None:
    """v3.5 ST5 跨层真实链条：CaptureFlow + 真实 ui_bridge.open_annotator + 真实离屏 AnnotateOverlay。"""
    from _desktop.capture_flow import CaptureFlow
    from _desktop.status import PlatformStatus
    from _desktop.ui_bridge import (
        QtUiBridge,
        open_annotator as bridge_open_annotator,
        open_live_selector,
        query_virtual_screen_rect,
    )

    vrect = query_virtual_screen_rect(qapp)
    assert vrect is not None
    screen_rect = (40, 40, 240, 160)
    vx, vy, vw, vh = vrect
    assert QRect(vx, vy, vw, vh).contains(QRect(*screen_rect)), (
        f"选区 {screen_rect} 未落于虚拟桌面可视区 {vrect} 内"
    )

    bridge = QtUiBridge()
    posted_status: list[dict] = []
    status = PlatformStatus(
        posted_status.append,
        post_components=posted_status.append,
        start_thread=False,
    )
    created_selectors: list[RedCrosshairSelector] = []
    created_overlays: list[AnnotateOverlay] = []
    captured_requests: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int] | None]] = []
    copied_pngs: list[bytes] = []

    def _make_flow() -> CaptureFlow:
        def _open_selector(rect, label, on_selected, on_cancelled):
            sel = open_live_selector(qapp, RedCrosshairSelector, rect, label, on_selected, on_cancelled)
            created_selectors.append(sel)
            return sel

        def _open_annotator(png, rect, **kw):
            ann = bridge_open_annotator(qapp, AnnotateOverlay, png, rect, **kw)
            created_overlays.append(ann)
            return ann

        def _capture_region(rect, virtual_rect):
            captured_requests.append((rect, virtual_rect))
            # 2× HiDPI 冻结截图（480×320），验证原分辨率合成导出
            return _make_png(rect[2] * 2, rect[3] * 2, bg_hex="#ffffff")

        def _copy_image(png: bytes) -> str | None:
            copied_pngs.append(png)
            return None

        return CaptureFlow(
            capture_region=_capture_region,
            open_selector=_open_selector,
            open_inplace_overlay=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("标注模式不应打开原位浮窗")),
            copy_clipboard=lambda _text: True,
            api=object(),
            status=status,
            invoke_ui=bridge.post,
            get_virtual_rect=lambda: query_virtual_screen_rect(qapp),
            open_annotator=_open_annotator,
            copy_image=_copy_image,
            capture_delay_s=0.0,
            spawn=lambda fn: fn(),
        )

    def _trigger_and_open(flow: CaptureFlow) -> AnnotateOverlay:
        prev_sel = len(created_selectors)
        prev_ov = len(created_overlays)
        flow.trigger_annotate()
        assert _wait_until(lambda: len(created_selectors) == prev_sel + 1, 2000)
        sel = created_selectors[-1]
        assert sel.mode_label == CaptureFlow.ANNOTATE_MODE_LABEL
        sx, sy, sw, sh = screen_rect
        p0 = QPoint(sx - vx, sy - vy)
        p1 = QPoint(sx - vx + sw, sy - vy + sh)
        QTest.mousePress(sel, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        QTest.mouseMove(sel, p1)
        QTest.mouseRelease(sel, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        assert _wait_until(lambda: len(created_overlays) == prev_ov + 1, 2000)
        ov = created_overlays[-1]
        qapp.processEvents()
        return ov

    try:
        # 1. 首次触发标注打开真实 AnnotateOverlay：默认红 #ff2d2d + 中档 medium、未选工具仅显主条
        flow = _make_flow()
        assert flow.annotate_style is None
        ov1 = _trigger_and_open(flow)
        assert isinstance(ov1, AnnotateOverlay)
        assert ov1.isVisible() is True
        assert flow.overlay_active() is True
        assert captured_requests[-1] == (screen_rect, vrect)
        assert ov1.canvas_rect() == QRect(screen_rect[0] - vx, screen_rect[1] - vy, screen_rect[2], screen_rect[3])
        assert ov1.current_color == "#ff2d2d"
        assert ov1.current_size_level == "medium"
        assert ov1.current_style == ("#ff2d2d", "medium")
        assert ov1.current_tool is None
        assert ov1.toolbar.isVisible() is True
        assert ov1.style_bar.isVisible() is False
        assert flow.annotate_style is None

        # 2. 点选画笔展开二级条、切蓝 #2f81f7 + 粗 thick，拖拽画闭合圆圈并按无修饰 Enter 复制关闭
        QTest.mouseClick(ov1.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov1.current_tool == "brush"
        assert ov1.style_bar.isVisible() is True
        assert ov1.style_bar.px_text() == "3px"

        QTest.mouseClick(ov1.style_bar.swatch_button("#2f81f7"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov1.style_bar.size_button("thick"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov1.current_style == ("#2f81f7", "thick")
        assert ov1.style_bar.px_text() == "6px"
        assert flow.annotate_style == ("#2f81f7", "thick")

        c1 = ov1.canvas_rect()
        loop = [(60, 40), (140, 40), (140, 110), (60, 110), (60, 40)]
        QTest.mousePress(ov1, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c1.topLeft() + QPoint(*loop[0]))
        for px, py in loop[1:-1]:
            QTest.mouseMove(ov1, c1.topLeft() + QPoint(px, py))
        QTest.mouseRelease(ov1, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c1.topLeft() + QPoint(*loop[-1]))
        qapp.processEvents()
        assert len(ov1.operations) == 1
        assert ov1.operations[0].tool == "brush"
        assert ov1.operations[0].start == ov1.operations[0].end == QPointF(60.0, 40.0)
        assert ov1.operations[0].color == "#2f81f7"
        assert ov1.operations[0].stroke_px == 6

        QTest.keyClick(ov1, Qt.Key.Key_Return)
        qapp.processEvents()
        assert len(copied_pngs) == 1
        assert ov1.isVisible() is False
        assert flow.overlay_active() is False
        assert flow.annotate_style == ("#2f81f7", "thick")

        exported = QImage.fromData(copied_pngs[0])
        assert not exported.isNull()
        assert (exported.width(), exported.height()) == (480, 320)
        blue = QColor("#2f81f7")
        assert exported.pixelColor(120, 80).rgb() == blue.rgb()
        assert exported.pixelColor(280, 150).rgb() == blue.rgb()
        assert exported.pixelColor(200, 220).rgb() == blue.rgb()
        assert exported.pixelColor(120, 150).rgb() == blue.rgb()
        # 2× HiDPI 下 thick(6px) 笔划宽 12px，校验下边中点纵向 ±4px 均为蓝色（显著宽于 medium 6px）
        for dy in (-4, 0, 4):
            assert exported.pixelColor(200, 220 + dy).rgb() == blue.rgb()
        assert _pixels_of(exported.copy(QRect(110, 70, 180, 160)), "#ff2d2d") == []

        # 3. 同一 CaptureFlow 第二次打开真实 AnnotateOverlay：自动延续 ("#2f81f7", "thick")，
        #    切绿 #3fb950 + 细 thin 后按 Esc 取消关闭仍保留新记忆 ("#3fb950", "thin")
        ov2 = _trigger_and_open(flow)
        assert ov2 is not ov1
        assert ov2.isVisible() is True
        assert ov2.current_tool is None
        assert ov2.style_bar.isVisible() is False
        assert ov2.current_style == ("#2f81f7", "thick")

        QTest.mouseClick(ov2.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov2.style_bar.isVisible() is True
        assert ov2.style_bar.swatch_button("#2f81f7").property("selected") == "true"
        assert ov2.style_bar.size_button("thick").property("active") == "true"
        assert ov2.style_bar.px_text() == "6px"

        QTest.mouseClick(ov2.style_bar.swatch_button("#3fb950"), Qt.MouseButton.LeftButton)
        QTest.mouseClick(ov2.style_bar.size_button("thin"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov2.current_style == ("#3fb950", "thin")
        assert ov2.style_bar.px_text() == "2px"
        assert flow.annotate_style == ("#3fb950", "thin")

        QTest.keyClick(ov2, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert ov2.isVisible() is False
        assert flow.overlay_active() is False
        assert len(copied_pngs) == 1
        assert flow.annotate_style == ("#3fb950", "thin")

        # 4. 新建独立 CaptureFlow 实例打开真实 AnnotateOverlay：复位默认 ("#ff2d2d", "medium")（零跨实例污染）
        fresh_flow = _make_flow()
        assert fresh_flow.annotate_style is None
        ov3 = _trigger_and_open(fresh_flow)
        assert ov3.isVisible() is True
        assert ov3.current_tool is None
        assert ov3.style_bar.isVisible() is False
        assert ov3.current_style == ("#ff2d2d", "medium")
        assert fresh_flow.annotate_style is None
        assert flow.annotate_style == ("#3fb950", "thin")

        status.drain()
        assert posted_status == []
    finally:
        for ov in created_overlays:
            ov.close_silent()
            ov.deleteLater()
        for sel in created_selectors:
            sel.close_silent()
            sel.deleteLater()
        qapp.processEvents()


def test_annotate_toolbar_vector_icons_semantic_colors_and_hover_states(
    qapp: QApplication,
) -> None:
    """模板 B 纯矢量图标：强校验、9 钮像素、复制绿/失败压过悬停红、取消悬停红、激活蓝、撤销悬停禁用清蓝降透明、3 档圆点面积递增。"""
    assert ANNOTATE_ICON_KEYS == frozenset(
        {"rect", "arrow", "brush", "text", "mosaic", "undo", "copy", "save", "cancel"}
    )

    # 1. 非法图标键、非法尺寸与非法语义角色立即抛 ValueError
    probe_img = QImage(24, 24, QImage.Format.Format_ARGB32)
    probe_img.fill(Qt.GlobalColor.transparent)
    probe_painter = QPainter(probe_img)
    try:
        with pytest.raises(ValueError):
            draw_annotate_icon(probe_painter, "unknown", QColor(theme.TEXT_PRIMARY_HEX))
        with pytest.raises(ValueError):
            draw_annotate_icon(probe_painter, "rect", QColor(theme.TEXT_PRIMARY_HEX), size=0)
        with pytest.raises(ValueError):
            draw_annotate_icon(probe_painter, "rect", QColor(theme.TEXT_PRIMARY_HEX), size=-4)
        with pytest.raises(ValueError):
            draw_annotate_icon(probe_painter, "rect", QColor(theme.TEXT_PRIMARY_HEX), size=False)
    finally:
        probe_painter.end()

    with pytest.raises(ValueError):
        _IconButton("unknown", "非法图标")
    with pytest.raises(ValueError):
        _IconButton("rect", "矩形", role="invalid")  # type: ignore[arg-type]

    # 2. mosaic 淡块在 save/restore 内叠乘 painter.opacity() * 0.28（不覆盖既有不透明度）
    mosaic_half = QImage(16, 16, QImage.Format.Format_ARGB32)
    mosaic_half.fill(Qt.GlobalColor.transparent)
    mp = QPainter(mosaic_half)
    mp.setOpacity(0.5)
    draw_annotate_icon(mp, "mosaic", QColor(theme.TEXT_PRIMARY_HEX), size=16)
    mp.end()
    solid_alpha = mosaic_half.pixelColor(4, 4).alpha()
    dim_alpha = mosaic_half.pixelColor(8, 4).alpha()
    assert abs(solid_alpha - round(255 * 0.5)) <= 3
    assert abs(dim_alpha - round(255 * 0.5 * theme.ANNOTATE_MOSAIC_DIM_OPACITY)) <= 3

    def _hover(widget: QWidget, enter: bool) -> None:
        if enter:
            center = QPointF(widget.width() / 2.0, widget.height() / 2.0)
            gpos = QPointF(widget.mapToGlobal(center.toPoint()))
            QApplication.sendEvent(widget, QEnterEvent(center, center, gpos))
        else:
            QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
        qapp.processEvents()

    def _ink_count(img: QImage, bg_hex: str, rect: QRect | None = None, tol: int = 12) -> int:
        bg = QColor(bg_hex)
        box = rect if rect is not None else img.rect()
        count = 0
        for y in range(box.top(), box.bottom() + 1):
            for x in range(box.left(), box.right() + 1):
                c = img.pixelColor(x, y)
                if (
                    abs(c.red() - bg.red()) > tol
                    or abs(c.green() - bg.green()) > tol
                    or abs(c.blue() - bg.blue()) > tol
                ):
                    count += 1
        return count

    def _max_luma_delta(img: QImage, bg_hex: str) -> int:
        bg = QColor(bg_hex)
        return max(
            abs(img.pixelColor(x, y).red() - bg.red())
            + abs(img.pixelColor(x, y).green() - bg.green())
            + abs(img.pixelColor(x, y).blue() - bg.blue())
            for y in range(img.height())
            for x in range(img.width())
        )

    def _grab_child(host: QWidget, child: QWidget) -> QImage:
        top_left = child.mapTo(host, QPoint(0, 0))
        return host.grab().toImage().copy(QRect(top_left, child.size()))

    ov = _open_annotator(
        (160, 120, 420, 240),
        on_copy_image=lambda _png: False,
        copy_feedback_ms=500,
    )
    try:
        # 3. 9 枚按钮均有非空矢量像素绘制（从主条整帧裁切，确保真实合成在 SURFACE_HEX 上）
        rect_img = _grab_child(ov.toolbar, ov.toolbar.button("rect"))
        assert rect_img.pixelColor(0, 0).rgb() == QColor(theme.SURFACE_HEX).rgb()
        for key in ("rect", "arrow", "brush", "text", "mosaic", "undo", "copy", "save", "cancel"):
            btn_img = _grab_child(ov.toolbar, ov.toolbar.button(key))
            assert _ink_count(btn_img, theme.SURFACE_HEX) > 15, f"{key} 图标像素异常稀疏"

        # 4. 复制主钮：静息绿 → 悬停亮绿 → 悬停中触发复制失败必须显红（failed > primary + hovered）
        copy_btn = ov.toolbar.button("copy")
        copy_idle = _grab_child(ov.toolbar, copy_btn)
        assert _pixels_of(copy_idle, theme.OK_HEX, tol=16)
        assert _pixels_of(copy_idle, theme.ERROR_HEX, tol=16) == []

        _hover(copy_btn, True)
        copy_hover = _grab_child(ov.toolbar, copy_btn)
        assert _pixels_of(copy_hover, theme.OK_BRIGHT_HEX, tol=16)

        QTest.mouseClick(copy_btn, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert ov.toolbar.is_copy_failed() is True
        assert copy_btn.width() == theme.ANNOTATE_PRIMARY_BTN_W_PX
        copy_fail_hover = _grab_child(ov.toolbar, copy_btn)
        assert _pixels_of(copy_fail_hover, theme.ERROR_HEX, tol=16)
        assert _pixels_of(copy_fail_hover, theme.OK_HEX, tol=16) == []
        assert _pixels_of(copy_fail_hover, theme.OK_BRIGHT_HEX, tol=16) == []
        _hover(copy_btn, False)

        # 5. 取消钮：静息无红 → 悬停转微红 → 离开恢复无红
        cancel_btn = ov.toolbar.button("cancel")
        assert _pixels_of(_grab_child(ov.toolbar, cancel_btn), theme.ERROR_HEX, tol=16) == []
        _hover(cancel_btn, True)
        assert _pixels_of(_grab_child(ov.toolbar, cancel_btn), theme.ERROR_HEX, tol=16)
        _hover(cancel_btn, False)
        assert _pixels_of(_grab_child(ov.toolbar, cancel_btn), theme.ERROR_HEX, tol=16) == []

        # 6. 工具激活蓝态 + 撤销悬停中回空栈禁用立即清蓝框并降至 0.38 不透明度
        QTest.mouseClick(ov.toolbar.button("brush"), Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert _pixels_of(_grab_child(ov.toolbar, ov.toolbar.button("brush")), theme.ACCENT_HEX, tol=16)
        assert _pixels_of(_grab_child(ov.toolbar, ov.toolbar.button("rect")), theme.ACCENT_HEX, tol=16) == []

        c = ov.canvas_rect()
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(30, 30))
        QTest.mouseMove(ov, c.topLeft() + QPoint(90, 30))
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(90, 30))
        qapp.processEvents()
        undo_btn = ov.toolbar.button("undo")
        assert undo_btn.isEnabled() is True
        enabled_delta = _max_luma_delta(_grab_child(ov.toolbar, undo_btn), theme.SURFACE_HEX)

        _hover(undo_btn, True)
        assert _pixels_of(_grab_child(ov.toolbar, undo_btn), theme.ACCENT_HEX, tol=16)

        # 悬停中点击撤销回到空栈：按钮禁用并立即清蓝框且降不透明度，且后续重新启用不残留陈旧悬停蓝框
        QTest.mouseClick(undo_btn, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert undo_btn.isEnabled() is False
        undo_disabled_img = _grab_child(ov.toolbar, undo_btn)
        assert _pixels_of(undo_disabled_img, theme.ACCENT_HEX, tol=16) == []
        disabled_delta = _max_luma_delta(undo_disabled_img, theme.SURFACE_HEX)
        assert 0 < disabled_delta < enabled_delta * 0.35

        # 未发 Leave 即通过画布再次起笔重新启用撤销：因禁用已清 _hovered，不应出现幽灵悬停蓝框
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(30, 50))
        QTest.mouseMove(ov, c.topLeft() + QPoint(90, 50))
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.topLeft() + QPoint(90, 50))
        qapp.processEvents()
        assert undo_btn.isEnabled() is True
        assert _pixels_of(_grab_child(ov.toolbar, undo_btn), theme.ACCENT_HEX, tol=16) == []

        # 7. 二级样式条 3 档纯圆点：像素面积严格递增 thin < medium < thick，悬停转蓝离开恢复
        inner = QRect(5, 5, 14, 14)
        thin_btn = ov.style_bar.size_button("thin")
        med_btn = ov.style_bar.size_button("medium")
        thick_btn = ov.style_bar.size_button("thick")
        thin_img = _grab_child(ov.style_bar, thin_btn)
        assert thin_img.pixelColor(0, 0).rgb() == QColor(theme.SURFACE_2_HEX).rgb()
        thin_area = _ink_count(thin_img, theme.SURFACE_2_HEX, rect=inner, tol=24)
        med_area = len(_pixels_of(_grab_child(ov.style_bar, med_btn).copy(inner), theme.ACCENT_HEX, tol=40))
        thick_area = _ink_count(_grab_child(ov.style_bar, thick_btn), theme.SURFACE_2_HEX, rect=inner, tol=24)
        assert 0 < thin_area < med_area < thick_area, (thin_area, med_area, thick_area)

        assert _pixels_of(_grab_child(ov.style_bar, thin_btn), theme.ACCENT_HEX, tol=16) == []
        _hover(thin_btn, True)
        assert _pixels_of(_grab_child(ov.style_bar, thin_btn), theme.ACCENT_HEX, tol=16)
        _hover(thin_btn, False)
        assert _pixels_of(_grab_child(ov.style_bar, thin_btn), theme.ACCENT_HEX, tol=16) == []
    finally:
        ov.close_silent()
        ov.deleteLater()

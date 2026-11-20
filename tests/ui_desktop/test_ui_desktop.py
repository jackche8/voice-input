"""`_ui_desktop` 跨平台桌面纯 UI 层完整离屏测试套件。"""
from __future__ import annotations

import ast
import os
import re
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

from _contracts import OcrTextBlock, OcrTextLine
from _ui_desktop import (
    InplaceTranslateOverlay,
    RedCrosshairSelector,
    StudioTrayIcon,
    VoiceHudOverlay,
    compute_trailing_two_lines,
    render_status_image,
    theme,
)
from _ui_desktop.caption_bar import CAPTION_AUTO_HIDE_MS, passive_overlay_flags
from _ui_desktop.inplace_layout import layout_ocr_block, layout_translated_block
from _ui_desktop.mini_menu import COPY_OCR, COPY_TRANSLATED

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

    py_files = sorted(UI_DESKTOP_DIR.glob("*.py"))
    assert [f.name for f in py_files] == [
        "__init__.py",
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

    events: list[object] = []
    # 1. 无系统托盘环境安全降级（offscreen 默认或显式返回 False）
    tray_fallback = StudioTrayIcon(
        on_open_home=lambda: events.append("home"),
        on_screenshot_translate=lambda: events.append("shot"),
        on_pure_ocr=lambda: events.append("ocr"),
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

        tray_fallback.open_home_action.trigger()
        tray_fallback.screenshot_translate_action.trigger()
        tray_fallback.pure_ocr_action.trigger()
        tray_fallback.open_setup_action.trigger()
        tray_fallback.toggle_hud_action.trigger()  # 默认 True -> 切换为 False
        tray_fallback.quit_action.trigger()

        assert events == ["home", "shot", "ocr", "setup", ("hud", False), "quit"]

        # 外部同步勾选状态不应重复触发回调
        tray_fallback.set_hud_enabled(True)
        assert tray_fallback.toggle_hud_action.isChecked() is True
        assert len(events) == 6
    finally:
        tray_fallback.close()

    # 2. 模拟系统托盘可用分支，验证 QSystemTrayIcon 挂载与左键触发打开主页
    tray_active = StudioTrayIcon(
        on_open_home=lambda: events.append("tray_click_home"),
        tray_available_fn=lambda: True,
    )
    try:
        assert tray_active.available is True
        tray_active.set_state("polishing")
        assert tray_active.state == "polishing"
        tray_active._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert events[-1] == "tray_click_home"
    finally:
        tray_active.close()


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

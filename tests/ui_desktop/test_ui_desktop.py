"""`_ui_desktop` 跨平台桌面纯 UI 层完整离屏测试套件。"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from _contracts import OcrTextBlock
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
        "inplace_overlay.py",
        "region_selector.py",
        "theme.py",
        "tray.py",
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
# 3. InplaceTranslateOverlay：100% 不透明遮挡、按住看原图、9px 超高延伸与悬浮栏避让
# ---------------------------------------------------------------------------
def test_inplace_overlay_opaque_occlusion_and_peek_original(qapp: QApplication) -> None:
    # 构造含醒目红字像素 (#ff0000) 与深色块青字像素 (#38bdf8) 的底图
    cropped_png = _make_png(
        320,
        180,
        bg_hex="#ffffff",
        patches=[
            ((30, 24, 180, 28), "#ff0000"),
            ((20, 96, 260, 48), "#0f172a"),
            ((30, 106, 180, 24), "#38bdf8"),
        ],
    )
    blocks = [
        OcrTextBlock(
            bbox=(30, 24, 180, 28),
            ocr_text="Region Capture & Translation",
            translated_text="区域框选与原位翻译",
            bg_color="#ffffff",
            fg_color="#0f172a",
        ),
        OcrTextBlock(
            bbox=(30, 106, 180, 24),
            ocr_text="Verify GPU service",
            translated_text="检查本地显卡服务",
            bg_color="#0f172a",
            fg_color="#e6edf3",
        ),
    ]
    overlay = InplaceTranslateOverlay(
        cropped_png=cropped_png,
        screen_rect=(100, 100, 320, 180),
        blocks=blocks,
        full_translated_text="区域框选与原位翻译\n检查本地显卡服务",
        full_ocr_text="Region Capture & Translation\nVerify GPU service",
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        comp = overlay.render_composited_image()
        src_red = QColor("#ff0000").rgb()
        src_cyan = QColor("#38bdf8").rgb()
        bg_light = QColor("#ffffff").rgb()
        bg_dark = QColor("#0f172a").rgb()

        # 校验块 1（微扩 2px -> 28..212, 22..54）：原 #ff0000 100% 被遮盖，全部像素 alpha == 255，且绘制出深色译文字形
        light_fg_pixels = 0
        for y in range(22, 54):
            for x in range(28, 212):
                px = comp.pixelColor(x, y)
                assert px.alpha() == 255
                assert px.rgb() != src_red
                if px.rgb() != bg_light:
                    light_fg_pixels += 1
        assert light_fg_pixels > 20

        # 校验块 2（微扩 2px -> 28..212, 104..132）：原 #38bdf8 100% 被遮盖，全部像素 alpha == 255，且绘制出浅色译文字形
        dark_fg_pixels = 0
        for y in range(104, 132):
            for x in range(28, 212):
                px = comp.pixelColor(x, y)
                assert px.alpha() == 255
                assert px.rgb() != src_cyan
                if px.rgb() != bg_dark:
                    dark_fg_pixels += 1
        assert dark_fg_pixels > 20

        # 按住 [按住对比原图] 露出原图，松开恢复译文遮挡
        overlay.show()
        QTest.mousePress(overlay.peek_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert overlay.peek_original is True
        peek_img = overlay.render_composited_image()
        assert peek_img.pixelColor(50, 35).rgb() == src_red
        assert peek_img.pixelColor(50, 115).rgb() == src_cyan

        QTest.mouseRelease(overlay.peek_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert overlay.peek_original is False
        restored_img = overlay.render_composited_image()
        assert restored_img.pixelColor(50, 35).rgb() != src_red
    finally:
        overlay.close_silent()
        overlay.deleteLater()


def test_inplace_overlay_9px_overflow_extends_opaque_background_downward(
    qapp: QApplication,
) -> None:
    # 构造极矮且靠近底边的文本框 (bh=14)，配长句译文迫使 9px 仍多行换行超高
    cropped_png = _make_png(180, 90, bg_hex="#f8fafc", patches=[((12, 68, 90, 14), "#ff0000")])
    long_block = OcrTextBlock(
        bbox=(12, 68, 90, 14),
        ocr_text="Short tag",
        translated_text="这是一段在九像素最小字号下依然需要自动换行向下延伸不透明背景块防止截断的较长译文内容",
        bg_color="#ffffff",
        fg_color="#0f172a",
    )
    overlay = InplaceTranslateOverlay(
        cropped_png=cropped_png,
        screen_rect=(80, 80, 180, 90),
        blocks=[long_block],
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        comp = overlay.render_composited_image()
        assert len(overlay.last_rendered_patches) == 1
        info = overlay.last_rendered_patches[0]
        assert info.font_pixel_size == theme.OCR_MIN_FONT_PX == 9
        assert info.extended_downward is True
        assert info.patch_rect.height() > 14 + 4
        assert comp.height() >= info.patch_rect.y() + info.patch_rect.height()

        # 向下延伸出的区域同样必须是 alpha=255 的 100% 不透明像素
        ext_y = info.patch_rect.y() + info.patch_rect.height() - 3
        ext_px = comp.pixelColor(info.patch_rect.x() + 4, ext_y)
        assert ext_px.alpha() == 255
    finally:
        overlay.close_silent()
        overlay.deleteLater()


def test_inplace_overlay_toolbar_boundary_avoidance_copy_retry_and_close(
    qapp: QApplication,
) -> None:
    png = _make_png(220, 70, bg_hex="#ffffff")
    copied: list[str] = []
    retried: list[bool] = []
    closed: list[bool] = []

    # 1. 屏幕中部 -> 工具栏位于画布下方 (below)
    ov_mid = InplaceTranslateOverlay(
        cropped_png=png,
        screen_rect=(200, 160, 220, 70),
        full_translated_text="全部译文内容",
        full_ocr_text="Full OCR Source",
        on_copy_text=lambda txt: copied.append(txt),
        on_retry=lambda: retried.append(True),
        on_closed=lambda: closed.append(True),
        available_screen_rect=(0, 0, 1280, 800),
        copy_feedback_ms=100,
    )
    try:
        ov_mid.show()
        assert ov_mid.toolbar_placement == "below"
        assert ov_mid.toolbar_rect().top() > ov_mid.canvas_rect().bottom()
        # 画布屏幕全局起点严格对齐选区 (200, 160)
        assert ov_mid.x() + ov_mid.canvas_rect().x() == 200
        assert ov_mid.y() + ov_mid.canvas_rect().y() == 160

        # 点击 [复制译文]：触发回调、按钮文案变 "✓ 已复制译文"，到期恢复
        QTest.mouseClick(ov_mid.copy_translated_button, Qt.MouseButton.LeftButton)
        assert copied == ["全部译文内容"]
        assert ov_mid.copy_translated_button.text() == "✓ 已复制译文"
        QTest.qWait(150)
        assert ov_mid.copy_translated_button.text() == "复制译文"

        # 点击 [复制原文]
        QTest.mouseClick(ov_mid.copy_ocr_button, Qt.MouseButton.LeftButton)
        assert copied == ["全部译文内容", "Full OCR Source"]
        assert ov_mid.copy_ocr_button.text() == "✓ 已复制原文"

        # 加载态与错误态切换 + [重试] 回调
        ov_mid.set_loading()
        assert ov_mid.retry_button.isVisible() is False
        assert ov_mid.copy_translated_button.isEnabled() is False

        ov_mid.set_error("本地翻译服务连接超时")
        assert ov_mid.retry_button.isVisible() is True
        QTest.mouseClick(ov_mid.retry_button, Qt.MouseButton.LeftButton)
        assert retried == [True]

        # Esc 关闭并触发一次 on_closed
        QTest.keyClick(ov_mid, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert closed == [True]
    finally:
        ov_mid.close_silent()
        ov_mid.deleteLater()

    # 2. 紧贴屏幕下边缘 -> 工具栏自动避让翻转到画布上方 (above)，且画布全局坐标不变
    ov_bottom = InplaceTranslateOverlay(
        cropped_png=png,
        screen_rect=(200, 725, 220, 65),
        full_translated_text="底部测试",
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        assert ov_bottom.toolbar_placement == "above"
        assert ov_bottom.toolbar_rect().bottom() < ov_bottom.canvas_rect().top()
        assert ov_bottom.x() + ov_bottom.canvas_rect().x() == 200
        assert ov_bottom.y() + ov_bottom.canvas_rect().y() == 725
    finally:
        ov_bottom.close_silent()
        ov_bottom.deleteLater()

    # 3. 选区跨越可用屏幕右边缘 (1200 + 120 = 1320 > 1280) 且工具栏宽于画布 -> 画布与工具栏均完整落于窗口内不裁切
    narrow_png = _make_png(120, 60, bg_hex="#ffffff")
    ov_right_cross = InplaceTranslateOverlay(
        cropped_png=narrow_png,
        screen_rect=(1200, 100, 120, 60),
        full_translated_text="右边缘跨界防裁切验证",
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        win_bounds = QRect(0, 0, ov_right_cross.width(), ov_right_cross.height())
        assert win_bounds.contains(ov_right_cross.canvas_rect())
        assert win_bounds.contains(ov_right_cross.toolbar_rect())
        assert ov_right_cross.canvas_rect().right() < ov_right_cross.width()
        assert ov_right_cross.canvas_rect().x() + ov_right_cross.canvas_rect().width() <= ov_right_cross.width()
        assert ov_right_cross.x() + ov_right_cross.canvas_rect().x() == 1200
        assert ov_right_cross.y() + ov_right_cross.canvas_rect().y() == 100
    finally:
        ov_right_cross.close_silent()
        ov_right_cross.deleteLater()

    # 4. 选区跨越可用屏幕左边缘 (-30 < 0) -> 画布与工具栏同样不越界裁切
    ov_left_cross = InplaceTranslateOverlay(
        cropped_png=narrow_png,
        screen_rect=(-30, 100, 120, 60),
        full_translated_text="左边缘跨界防裁切验证",
        available_screen_rect=(0, 0, 1280, 800),
    )
    try:
        left_bounds = QRect(0, 0, ov_left_cross.width(), ov_left_cross.height())
        assert left_bounds.contains(ov_left_cross.canvas_rect())
        assert left_bounds.contains(ov_left_cross.toolbar_rect())
        assert ov_left_cross.x() + ov_left_cross.canvas_rect().x() == -30
    finally:
        ov_left_cross.close_silent()
        ov_left_cross.deleteLater()


# ---------------------------------------------------------------------------
# 4. VoiceHudOverlay：状态胶囊、可点击重发确认、双行大字幕防溢出与 1.5s 自动隐藏
# ---------------------------------------------------------------------------
def test_voice_hud_capsule_states_passthrough_and_delivery_confirmation(
    qapp: QApplication,
) -> None:
    now = [100.0]
    hud = VoiceHudOverlay(
        available_screen_rect=(0, 0, 1280, 800),
        clock=lambda: now[0],
    )
    try:
        # idle 默认隐藏，且处于鼠标穿透不抢焦点态
        assert hud.isVisible() is False
        assert hud.caption_bar.isVisible() is False
        assert hud.is_mouse_passthrough is True

        hud.set_recording()
        assert hud.isVisible() is True
        assert hud.is_mouse_passthrough is True
        assert "0:00" in hud.capsule_text
        now[0] = 104.2
        hud._on_record_tick()
        assert "0:04" in hud.capsule_text

        hud.set_polishing()
        assert hud.mode == "polishing"
        assert hud.is_mouse_passthrough is True

        hud.set_busy("截图准备中…")
        assert hud.mode == "busy"
        assert "截图准备中" in hud.capsule_text
        assert hud.is_mouse_passthrough is True

        hud.set_error("未检测到麦克风", duration_ms=0)
        assert hud.mode == "error"
        assert hud.is_mouse_passthrough is True

        hud.show_toast("已复制 OCR 文本（28 字）", kind="ok", duration_ms=70)
        assert hud.isVisible() is True
        assert hud.is_mouse_passthrough is True
        QTest.qWait(110)
        assert hud.isVisible() is False

        # delivery_unknown 确认卡片：唯一开启鼠标点击态（仍保持 WindowDoesNotAcceptFocus），点击后恢复穿透并隐藏
        decisions: list[bool] = []
        hud.show_delivery_confirmation(
            "job-unknown-1",
            "需要确认是否已经输入的语音句子",
            lambda confirmed: decisions.append(confirmed),
        )
        assert hud.isVisible() is True
        assert hud.is_mouse_passthrough is False
        assert bool(hud.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)

        QTest.mouseClick(hud.confirm_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert decisions == [True]
        assert hud.isVisible() is False
        assert hud.is_mouse_passthrough is True

        # 支持取消（忽略）与双参数 (job_id, confirmed) 回调签名
        pair_decisions: list[tuple[str, bool]] = []

        def on_pair(job_id: str, ok: bool) -> None:
            pair_decisions.append((job_id, ok))

        hud.show_delivery_confirmation("job-unknown-2", "第二句待确认文本", on_pair)
        QTest.mouseClick(hud.cancel_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert pair_decisions == [("job-unknown-2", False)]
        assert hud.is_mouse_passthrough is True
    finally:
        hud.close()
        hud.deleteLater()


def test_voice_hud_streaming_caption_strictly_two_lines_colors_and_autohide(
    qapp: QApplication,
) -> None:
    assert CAPTION_AUTO_HIDE_MS == 1500

    hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1280, 800))
    try:
        # 输入超长流式文本（远超 2 行宽度），验证严格只保留最新末尾两行
        early_prefix = "第一句历史前缀应当在超过两行后平滑滚出顶部视野之外不再挤压高度，" * 3
        recent_stable = "当前正在稳定输出的倒数第二行内容，"
        live_tail = "以及尾部实时跳动的增量识别片段"

        hud.update_caption(
            stable_text=early_prefix + recent_stable,
            tail_text=live_tail,
        )
        qapp.processEvents()

        assert hud.caption_bar.isVisible() is True
        assert bool(hud.caption_bar.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        assert bool(hud.caption_bar.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)

        lines = hud.visible_caption_lines()
        assert len(lines) == 2
        rendered_joined = "".join(s + t for s, t in lines)
        assert "第一句历史前缀" != rendered_joined[:7]
        assert live_tail in rendered_joined
        assert lines[-1][1].endswith("增量识别片段")

        # 抓取字幕条真实渲染像素，验证同屏存在 #f8fafc (stable_text) 与 #94a3b8 (tail_text) 色系字形像素
        cap_img = hud.caption_bar.grab().toImage()
        stable_target = QColor(theme.STABLE_TEXT_HEX)
        tail_target = QColor(theme.TAIL_TEXT_HEX)
        found_stable_px = False
        found_tail_px = False
        for y in range(cap_img.height()):
            for x in range(cap_img.width()):
                c = cap_img.pixelColor(x, y)
                if (
                    abs(c.red() - stable_target.red()) <= 8
                    and abs(c.green() - stable_target.green()) <= 8
                    and abs(c.blue() - stable_target.blue()) <= 8
                ):
                    found_stable_px = True
                if (
                    abs(c.red() - tail_target.red()) <= 10
                    and abs(c.green() - tail_target.green()) <= 10
                    and abs(c.blue() - tail_target.blue()) <= 10
                ):
                    found_tail_px = True
        assert found_stable_px is True
        assert found_tail_px is True

        # 验证完成态定格与自动隐藏（默认 1500ms；此处用短 hold 验证定时器闭环）
        hud.finish_caption(hold_ms=80)
        assert hud.caption_bar.isVisible() is True
        assert hud.caption_bar.is_auto_hide_active() is True
        QTest.qWait(120)
        assert hud.caption_bar.isVisible() is False
        assert hud.visible_caption_lines() == []
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
        assert hud.caption_bar.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled) is False
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

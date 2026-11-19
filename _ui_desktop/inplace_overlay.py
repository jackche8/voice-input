"""识别位置背景色 100% 不透明遮挡原位译文浮窗（方案 C）。

纯 UI 组件：原图打底，对每个有效 OcrTextBlock 用采样得到的 bg_color (alpha=255)
100% 不透明实心覆盖原文字，再以 fg_color 自适应字号原位排版译文；紧贴画布上下方
自适应避让屏幕边界提供悬浮操作栏。不含任何网络、OCR 或剪贴板系统调用。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from _contracts import OcrTextBlock
from _ui_desktop import theme

BBOX_EXPAND_PX = 2
TOOLBAR_GAP_PX = 8
COPY_FEEDBACK_DEFAULT_MS = 1500


@dataclass(frozen=True)
class RenderedPatchInfo:
    """记录单次原位遮挡渲染中每个文字块的实际遮罩区域与拟合字号，便于验证。"""

    patch_rect: QRect
    font_pixel_size: int
    extended_downward: bool


class InplaceTranslateOverlay(QWidget):
    """原位背景遮挡翻译浮窗 + 自动避让屏幕上下边界的悬浮工具栏。"""

    def __init__(
        self,
        cropped_png: bytes,
        screen_rect: tuple[int, int, int, int],
        blocks: list[OcrTextBlock] | None = None,
        full_translated_text: str = "",
        full_ocr_text: str = "",
        loading: bool = False,
        error_message: str | None = None,
        on_copy_text: Callable[[str], None] | None = None,
        on_retry: Callable[[], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        *,
        available_screen_rect: tuple[int, int, int, int] | None = None,
        copy_feedback_ms: int = COPY_FEEDBACK_DEFAULT_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not cropped_png:
            raise ValueError("cropped_png 不能为空")
        base_img = QImage.fromData(cropped_png)
        if base_img.isNull() or base_img.width() <= 0 or base_img.height() <= 0:
            raise ValueError("cropped_png 不是有效的图像数据")

        sx, sy, sw, sh = (int(v) for v in screen_rect)
        if sw <= 0 or sh <= 0:
            raise ValueError(f"screen_rect 宽高必须大于 0，收到 {screen_rect!r}")

        self._base_image: QImage = self._ensure_opaque_base(base_img)
        self._screen_rect_tuple: tuple[int, int, int, int] = (sx, sy, sw, sh)
        self._available_screen_override = available_screen_rect
        self._copy_feedback_ms = max(100, int(copy_feedback_ms))

        self._blocks: list[OcrTextBlock] = list(blocks or [])
        self._full_translated_text: str = full_translated_text
        self._full_ocr_text: str = full_ocr_text
        self._loading: bool = bool(loading)
        self._error_message: str | None = error_message
        self._peek_original: bool = False
        self._closed_notified: bool = False

        self._on_copy_text = on_copy_text
        self._on_retry = on_retry
        self._on_closed = on_closed

        self._last_patches: list[RenderedPatchInfo] = []
        self._composited_cache: QImage | None = None
        self._canvas_rect: QRect = QRect(0, 0, sw, sh)
        self._toolbar_placement: Literal["below", "above", "inside"] = "below"

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._build_toolbar()
        self._copy_trans_timer = QTimer(self)
        self._copy_trans_timer.setSingleShot(True)
        self._copy_trans_timer.timeout.connect(
            lambda: self.copy_translated_button.setText("复制译文")
        )
        self._copy_ocr_timer = QTimer(self)
        self._copy_ocr_timer.setSingleShot(True)
        self._copy_ocr_timer.timeout.connect(
            lambda: self.copy_ocr_button.setText("复制原文")
        )

        self._sync_text_fallbacks()
        self._refresh_state()

    @staticmethod
    def _ensure_opaque_base(image: QImage) -> QImage:
        converted = image.convertToFormat(QImage.Format.Format_ARGB32)
        opaque = QImage(converted.size(), QImage.Format.Format_ARGB32)
        opaque.fill(theme.qcolor(theme.DEFAULT_BLOCK_BG_HEX, opaque=True))
        painter = QPainter(opaque)
        painter.drawImage(0, 0, converted)
        painter.end()
        return opaque

    def _build_toolbar(self) -> None:
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("oviFloatBar")
        self.toolbar.setStyleSheet(theme.float_bar_stylesheet())

        layout = QHBoxLayout(self.toolbar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        self.status_label = QLabel(self.toolbar)
        self.status_label.setObjectName("oviFloatStatus")
        self.status_label.setFont(
            theme.make_font(pixel_size=theme.FS_XS, weight=QFont.Weight.DemiBold)
        )
        layout.addWidget(self.status_label)

        self.peek_button = QPushButton("按住对比原图", self.toolbar)
        self.peek_button.pressed.connect(lambda: self._set_peek_original(True))
        self.peek_button.released.connect(lambda: self._set_peek_original(False))
        layout.addWidget(self.peek_button)

        self.copy_translated_button = QPushButton("复制译文", self.toolbar)
        self.copy_translated_button.clicked.connect(self._handle_copy_translated)
        layout.addWidget(self.copy_translated_button)

        self.copy_ocr_button = QPushButton("复制原文", self.toolbar)
        self.copy_ocr_button.clicked.connect(self._handle_copy_ocr)
        layout.addWidget(self.copy_ocr_button)

        self.retry_button = QPushButton("重试", self.toolbar)
        self.retry_button.clicked.connect(self._handle_retry)
        layout.addWidget(self.retry_button)

        self.close_button = QPushButton("Esc 关闭", self.toolbar)
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)

    def _sync_text_fallbacks(self) -> None:
        if not self._full_translated_text and self._blocks:
            self._full_translated_text = "\n".join(
                b.translated_text.strip() for b in self._blocks if b.translated_text.strip()
            )
        if not self._full_ocr_text and self._blocks:
            self._full_ocr_text = "\n".join(
                b.ocr_text.strip() for b in self._blocks if b.ocr_text.strip()
            )

    @property
    def peek_original(self) -> bool:
        return self._peek_original

    @property
    def toolbar_placement(self) -> Literal["below", "above", "inside"]:
        return self._toolbar_placement

    @property
    def last_rendered_patches(self) -> list[RenderedPatchInfo]:
        return list(self._last_patches)

    def canvas_rect(self) -> QRect:
        """画布在当前浮窗内的局部逻辑矩形。"""
        return QRect(self._canvas_rect)

    def toolbar_rect(self) -> QRect:
        """悬浮工具栏在当前浮窗内的局部逻辑矩形。"""
        return self.toolbar.geometry()

    def set_loading(self) -> None:
        """切换为识别/翻译加载态。"""
        self._loading = True
        self._error_message = None
        self._peek_original = False
        self._refresh_state()

    def set_result(
        self,
        blocks: list[OcrTextBlock],
        full_translated_text: str = "",
        full_ocr_text: str = "",
    ) -> None:
        """更新原位识别翻译结果并重新合成遮挡图层。"""
        self._loading = False
        self._error_message = None
        self._blocks = list(blocks or [])
        self._full_translated_text = full_translated_text
        self._full_ocr_text = full_ocr_text
        self._sync_text_fallbacks()
        self._refresh_state()

    def set_error(self, message: str) -> None:
        """切换为可见错误态并展示重试按钮，绝不静默退回原文冒充译文。"""
        self._loading = False
        self._error_message = message or "原位翻译失败"
        self._peek_original = False
        self._refresh_state()

    def _set_peek_original(self, active: bool) -> None:
        if self._peek_original == active:
            return
        self._peek_original = active
        self._composited_cache = self.render_composited_image()
        self.update()

    def render_composited_image(self) -> QImage:
        """合成原位遮挡图像：Pass 1 全量 alpha=255 背景遮盖，Pass 2 前景自适应字号绘制译文。"""
        canvas = self._base_image.copy()
        self._last_patches = []
        if self._peek_original or not self._blocks:
            return canvas

        orig_w = canvas.width()
        orig_h = canvas.height()
        prepared: list[tuple[QRect, int, int, int, str, int, QRect, bool, OcrTextBlock]] = []
        max_bottom = orig_h

        for block in self._blocks:
            text = (block.translated_text or "").strip()
            if not text:
                continue
            bx, by, bw, bh = (int(v) for v in block.bbox)
            if bw <= 0 or bh <= 0:
                continue

            x0 = max(0, bx - BBOX_EXPAND_PX)
            y0 = max(0, by - BBOX_EXPAND_PX)
            x1 = min(orig_w, bx + bw + BBOX_EXPAND_PX)
            y1 = min(orig_h, by + bh + BBOX_EXPAND_PX)
            if x1 <= x0 or y1 <= y0:
                continue

            patch_w = x1 - x0
            base_patch_h = y1 - y0
            pad_x = 2 if patch_w >= 8 else 0
            pad_y = 1 if base_patch_h >= 6 else 0
            avail_w = max(1, patch_w - 2 * pad_x)
            avail_h = max(1, base_patch_h - 2 * pad_y)

            chosen_px, wrapped_rect, wrap_flag = self._fit_block_font(
                text, bh, avail_w, avail_h
            )
            patch_h = base_patch_h
            extended = False
            required_h = wrapped_rect.height() + 2 * pad_y
            if required_h > patch_h:
                patch_h = required_h
                extended = True

            patch_rect = QRect(x0, y0, patch_w, patch_h)
            max_bottom = max(max_bottom, y0 + patch_h)
            prepared.append(
                (
                    patch_rect,
                    pad_x,
                    pad_y,
                    chosen_px,
                    text,
                    wrap_flag,
                    wrapped_rect,
                    extended,
                    block,
                )
            )

        if not prepared:
            return canvas

        if max_bottom > canvas.height():
            expanded = QImage(
                canvas.width(),
                max_bottom,
                QImage.Format.Format_ARGB32,
            )
            expanded.fill(Qt.GlobalColor.transparent)
            grow_painter = QPainter(expanded)
            grow_painter.drawImage(0, 0, canvas)
            grow_painter.end()
            canvas = expanded

        painter = QPainter(canvas)
        # Pass 1: 100% 不透明 (alpha=255) 实心填充所有文字块采样背景，防止相邻块微扩擦除彼此字形
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        for (
            patch_rect,
            _pad_x,
            _pad_y,
            _chosen_px,
            _text,
            _wrap_flag,
            _wrapped_rect,
            extended,
            block,
        ) in prepared:
            bg_color = theme.qcolor(
                block.bg_color,
                opaque=True,
                fallback=theme.DEFAULT_BLOCK_BG_HEX,
            )
            painter.fillRect(patch_rect, bg_color)
            self._last_patches.append(
                RenderedPatchInfo(
                    patch_rect=patch_rect,
                    font_pixel_size=_chosen_px,
                    extended_downward=extended,
                )
            )

        # Pass 2: 在不透明背景上绘制高对比度译文
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        for (
            patch_rect,
            pad_x,
            pad_y,
            chosen_px,
            text,
            wrap_flag,
            wrapped_rect,
            _extended,
            block,
        ) in prepared:
            fg_color = theme.qcolor(
                block.fg_color,
                opaque=True,
                fallback=theme.DEFAULT_BLOCK_FG_HEX,
            )
            painter.setFont(theme.make_font(pixel_size=chosen_px))
            painter.setPen(fg_color)
            text_rect = patch_rect.adjusted(pad_x, pad_y, -pad_x, -pad_y)
            v_align = (
                Qt.AlignmentFlag.AlignVCenter
                if wrapped_rect.height() <= text_rect.height()
                else Qt.AlignmentFlag.AlignTop
            )
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | v_align) | wrap_flag,
                text,
            )

        painter.end()
        return canvas

    @staticmethod
    def _fit_block_font(
        text: str,
        bbox_h: int,
        avail_w: int,
        avail_h: int,
    ) -> tuple[int, QRect, int]:
        """从 max(11, int(bh*0.72)) 递减至 9px（Qt.TextWordWrap）寻找可容纳译文的最大字号。"""
        start_px = max(theme.OCR_BASE_MIN_FONT_PX, int(bbox_h * theme.OCR_HEIGHT_FONT_RATIO))
        min_px = theme.OCR_MIN_FONT_PX
        word_wrap = int(Qt.TextFlag.TextWordWrap)
        base_align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        probe_box = QRect(0, 0, max(1, avail_w), 100_000)

        last_rect = QRect(0, 0, avail_w, avail_h)
        for px in range(start_px, min_px - 1, -1):
            font = theme.make_font(pixel_size=px)
            fm = QFontMetrics(font)
            rect = fm.boundingRect(probe_box, base_align | word_wrap, text)
            last_rect = rect
            if rect.height() <= avail_h and rect.width() <= avail_w + 2:
                return px, rect, word_wrap

        if last_rect.width() > avail_w + 2:
            wrap_any = int(Qt.TextFlag.TextWrapAnywhere)
            fm_min = QFontMetrics(theme.make_font(pixel_size=min_px))
            last_rect = fm_min.boundingRect(probe_box, base_align | wrap_any, text)
            return min_px, last_rect, wrap_any
        return min_px, last_rect, word_wrap

    def _refresh_state(self) -> None:
        if self._loading:
            self.status_label.setObjectName("oviFloatStatus")
            self.status_label.setText("识别翻译中…")
            self.retry_button.setVisible(False)
            self.peek_button.setEnabled(False)
        elif self._error_message:
            self.status_label.setObjectName("oviFloatError")
            self.status_label.setText(f"✕ {self._error_message}")
            self.retry_button.setVisible(True)
            self.peek_button.setEnabled(bool(self._blocks))
        else:
            self.status_label.setObjectName("oviFloatStatus")
            self.status_label.setText("✓ 原位遮挡翻译完成")
            self.retry_button.setVisible(False)
            self.peek_button.setEnabled(True)

        self.toolbar.setStyleSheet(theme.float_bar_stylesheet())
        self.copy_translated_button.setEnabled(
            (not self._loading) and bool(self._full_translated_text)
        )
        self.copy_ocr_button.setEnabled(
            (not self._loading) and bool(self._full_ocr_text)
        )

        self._composited_cache = self.render_composited_image()
        self._update_window_geometry()
        self.update()

    def _update_window_geometry(self) -> None:
        sx, sy, sw, sh = self._screen_rect_tuple
        comp = self._composited_cache or self._base_image
        scale_y = sh / max(1, self._base_image.height())
        canvas_w = sw
        canvas_h = max(sh, int(round(comp.height() * scale_y)))

        self.toolbar.adjustSize()
        bar_hint = self.toolbar.sizeHint()
        bar_w = max(240, bar_hint.width())
        bar_h = max(34, bar_hint.height())

        avail = theme.resolve_available_screen_rect(
            self._available_screen_override, QPoint(sx, sy)
        )
        screen_left = avail.x()
        screen_top = avail.y()
        screen_right = avail.x() + avail.width()
        screen_bottom = avail.y() + avail.height()

        canvas_global_x = sx
        bar_global_x = sx
        if bar_global_x + bar_w > screen_right:
            bar_global_x = max(screen_left, screen_right - bar_w)
        if bar_global_x < screen_left:
            bar_global_x = screen_left

        win_x = min(canvas_global_x, bar_global_x)
        canvas_local_x = canvas_global_x - win_x
        bar_local_x = bar_global_x - win_x
        win_w = max(canvas_local_x + canvas_w, bar_local_x + bar_w)

        below_bottom = sy + canvas_h + TOOLBAR_GAP_PX + bar_h
        above_top = sy - TOOLBAR_GAP_PX - bar_h

        if below_bottom <= screen_bottom:
            self._toolbar_placement = "below"
            win_y = sy
            win_h = canvas_h + TOOLBAR_GAP_PX + bar_h
            canvas_local_y = 0
            bar_local_y = canvas_h + TOOLBAR_GAP_PX
        elif above_top >= screen_top:
            self._toolbar_placement = "above"
            win_y = above_top
            win_h = bar_h + TOOLBAR_GAP_PX + canvas_h
            bar_local_y = 0
            canvas_local_y = bar_h + TOOLBAR_GAP_PX
        else:
            self._toolbar_placement = "inside"
            win_y = sy
            win_h = max(canvas_h, bar_h + 12)
            canvas_local_y = 0
            bar_local_y = max(4, win_h - bar_h - 6)

        self._canvas_rect = QRect(canvas_local_x, canvas_local_y, canvas_w, canvas_h)
        target = QRect(win_x, win_y, win_w, win_h)
        if self.geometry() != target:
            self.setGeometry(target)
        self.toolbar.setGeometry(bar_local_x, bar_local_y, bar_w, bar_h)

    def paintEvent(self, _event: QPaintEvent) -> None:
        comp = self._composited_cache or self._base_image
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(self._canvas_rect, comp)

        # 画布高亮外框（与方案 C outline: 2px solid #58a6ff 对齐，画在外缘不污染块内像素）
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        border_color = (
            theme.qcolor(theme.ERROR_HEX, opaque=True)
            if self._error_message
            else theme.qcolor(theme.OVERLAY_OUTLINE_HEX, opaque=True)
        )
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(
            self._canvas_rect.adjusted(0, 0, -1, -1)
        )

        if self._loading or self._error_message:
            self._draw_canvas_banner(painter)
        painter.end()

    def _draw_canvas_banner(self, painter: QPainter) -> None:
        banner_text = "正在识别并原位翻译…" if self._loading else f"失败：{self._error_message}"
        font = theme.make_font(pixel_size=theme.FS_XS, weight=QFont.Weight.DemiBold)
        painter.setFont(font)
        fm = QFontMetrics(font)
        bw = min(max(60, self._canvas_rect.width() - 12), fm.horizontalAdvance(banner_text) + 20)
        bh = min(max(20, self._canvas_rect.height() - 4), fm.height() + 10)
        bx = self._canvas_rect.x() + max(0, (self._canvas_rect.width() - bw) // 2)
        by = self._canvas_rect.y() + max(0, (self._canvas_rect.height() - bh) // 2)
        rect = QRect(bx, by, bw, bh)

        painter.fillRect(rect, theme.qcolor(theme.OVERLAY_BANNER_BG_RGBA))
        fg = theme.ERROR_HEX if self._error_message else theme.ACCENT_HEX
        painter.setPen(theme.qcolor(fg, opaque=True))
        elided = fm.elidedText(banner_text, Qt.TextElideMode.ElideRight, max(20, bw - 12))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, elided)

    def _handle_copy_translated(self) -> None:
        if self._on_copy_text is not None:
            self._on_copy_text(self._full_translated_text)
        self.copy_translated_button.setText("✓ 已复制译文")
        self._copy_trans_timer.start(self._copy_feedback_ms)

    def _handle_copy_ocr(self) -> None:
        if self._on_copy_text is not None:
            self._on_copy_text(self._full_ocr_text)
        self.copy_ocr_button.setText("✓ 已复制原文")
        self._copy_ocr_timer.start(self._copy_feedback_ms)

    def _handle_retry(self) -> None:
        if self._on_retry is not None:
            self._on_retry()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def close_silent(self) -> None:
        """强制关闭浮窗而不触发 on_closed 回调。"""
        self._closed_notified = True
        self._copy_trans_timer.stop()
        self._copy_ocr_timer.stop()
        self.hide()
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._peek_original = False
        self._copy_trans_timer.stop()
        self._copy_ocr_timer.stop()
        if not self._closed_notified:
            self._closed_notified = True
            if self._on_closed is not None:
                self._on_closed()
        super().closeEvent(event)

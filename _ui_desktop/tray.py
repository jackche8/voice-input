"""跨平台系统托盘图标与菜单（方案 C Studio 工作站）。

纯 UI 组件：使用 `QPainter` 动态绘制多状态矢量麦克风托盘图标，所有菜单项通过
`Callable` 回调向外派发意图。托盘暂不可用时（Linux 托盘宿主 StatusNotifierWatcher 未注册：
锁屏中、扩展未加载或开机早于宿主）写一行日志，并在宿主注册时自动建立 / 重新登记图标，
不轮询、不阻塞主程序常驻。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, QSignalBlocker, QSysInfo, Qt
from PySide6.QtDBus import QDBusConnection, QDBusServiceWatcher
from PySide6.QtGui import QAction, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from _ui_desktop import theme

TRAY_HOST_SERVICE = "org.kde.StatusNotifierWatcher"

STATE_BADGE_HEX: dict[str, str] = {
    "idle": theme.STATE_OK_HEX,
    "ready": theme.STATE_OK_HEX,
    "recording": theme.STATE_RECORDING_HEX,
    "polishing": theme.STATE_POLISHING_HEX,
    "error": theme.STATE_ERROR_HEX,
    "disabled": theme.STATE_IDLE_HEX,
}


def state_badge_hex(state: str) -> str:
    """根据运行状态返回托盘角标色值令牌。"""
    return STATE_BADGE_HEX.get((state or "idle").lower(), theme.STATE_OK_HEX)


def render_status_image(state: str = "idle", size: int = 64) -> QImage:
    """动态绘制带状态角标的矢量麦克风托盘图像。"""
    px = max(16, int(size))
    image = QImage(px, px, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    s = float(px)
    pad = s * 0.06
    bg_rect = QRectF(pad, pad, s - 2 * pad, s - 2 * pad)
    bg_radius = s * 0.22
    bg_path = QPainterPath()
    bg_path.addRoundedRect(bg_rect, bg_radius, bg_radius)
    painter.fillPath(bg_path, theme.qcolor(theme.SURFACE_HEX, opaque=True))
    painter.setPen(QPen(theme.qcolor(theme.BORDER_HEX, opaque=True), max(1.0, s * 0.03)))
    painter.drawPath(bg_path)

    # 矢量麦克风主体
    fg_color = theme.qcolor(theme.TEXT_PRIMARY_HEX, opaque=True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fg_color)
    capsule_w = s * 0.22
    capsule_h = s * 0.34
    capsule_rect = QRectF((s - capsule_w) / 2.0, s * 0.18, capsule_w, capsule_h)
    painter.drawRoundedRect(capsule_rect, capsule_w / 2.0, capsule_w / 2.0)

    stroke_w = max(1.5, s * 0.055)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(fg_color, stroke_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    arc_rect = QRectF(s * 0.30, s * 0.28, s * 0.40, s * 0.34)
    painter.drawArc(arc_rect, 0, -180 * 16)
    painter.drawLine(QPointF(s * 0.50, s * 0.62), QPointF(s * 0.50, s * 0.76))
    painter.drawLine(QPointF(s * 0.36, s * 0.76), QPointF(s * 0.64, s * 0.76))

    # 右下角状态圆点角标
    badge_r = s * 0.14
    badge_cx = s * 0.76
    badge_cy = s * 0.76
    painter.setPen(QPen(theme.qcolor(theme.BG_DARK_HEX, opaque=True), max(1.2, s * 0.04)))
    painter.setBrush(theme.qcolor(state_badge_hex(state), opaque=True))
    painter.drawEllipse(QPointF(badge_cx, badge_cy), badge_r, badge_r)

    painter.end()
    return image


def watch_tray_host(on_registered: Callable[[], None], parent: QObject) -> QObject | None:
    """Linux：托盘宿主（StatusNotifierWatcher）每次注册时回调；其它系统托盘不依赖它，返回 None。"""
    if QSysInfo.kernelType() != "linux":
        return None
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        print(f"[tray] 会话总线不可用，无法监听托盘宿主：{bus.lastError().message()}", flush=True)
        return None
    watcher = QDBusServiceWatcher(
        TRAY_HOST_SERVICE, bus, QDBusServiceWatcher.WatchModeFlag.WatchForRegistration, parent
    )
    watcher.serviceRegistered.connect(lambda _name: on_registered())
    return watcher


def render_status_icon(state: str = "idle", size: int = 64) -> QIcon:
    """生成多尺寸自适应 QIcon。"""
    icon = QIcon()
    for target_px in (size, 32, 24, 16):
        img = render_status_image(state=state, size=target_px)
        icon.addPixmap(QPixmap.fromImage(img))
    return icon


class StudioTrayIcon(QObject):
    """系统托盘封装：支持状态图标热切换、完整工作站菜单回调与无托盘环境安全降级。"""

    def __init__(
        self,
        on_open_home: Callable[[], None] | None = None,
        on_screenshot_translate: Callable[[], None] | None = None,
        on_pure_ocr: Callable[[], None] | None = None,
        on_open_setup: Callable[[], None] | None = None,
        on_toggle_hud: Callable[[bool], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_retry_server: Callable[[], None] | None = None,
        *,
        hud_enabled: bool = True,
        tooltip: str = "Voice Studio · 本地语音与原位翻译",
        tray_available_fn: Callable[[], bool] | None = None,
        watch_host: Callable[[Callable[[], None], QObject], QObject | None] = watch_tray_host,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_open_home = on_open_home
        self._on_screenshot_translate = on_screenshot_translate
        self._on_pure_ocr = on_pure_ocr
        self._on_open_setup = on_open_setup
        self._on_toggle_hud = on_toggle_hud
        self._on_quit = on_quit
        self._on_retry_server = on_retry_server

        self._state: str = "idle"
        self._tooltip: str = tooltip
        self._current_icon: QIcon = render_status_icon(self._state)

        self.menu = QMenu()
        self._build_menu(hud_enabled=bool(hud_enabled))

        self._tray_available = tray_available_fn or QSystemTrayIcon.isSystemTrayAvailable
        self._tray: QSystemTrayIcon | None = None
        self._wants_visible = False
        self._ensure_tray()
        self._host_watch = watch_host(self._on_host_registered, self)

    def _build_menu(self, *, hud_enabled: bool) -> None:
        # 只在本地服务停止时显示（set_server_stopped）；隐藏时其后的分隔线被 QMenu 折叠
        self.retry_server_action = QAction("重试启动本地服务", self.menu)
        self.retry_server_action.setVisible(False)
        self.retry_server_action.triggered.connect(self._trigger_retry_server)
        self.menu.addAction(self.retry_server_action)
        self.menu.addSeparator()

        self.open_home_action = QAction("打开主界面", self.menu)
        self.open_home_action.triggered.connect(self._trigger_open_home)
        self.menu.addAction(self.open_home_action)

        self.screenshot_translate_action = QAction("截屏原位翻译", self.menu)
        self.screenshot_translate_action.triggered.connect(self._trigger_screenshot_translate)
        self.menu.addAction(self.screenshot_translate_action)

        self.pure_ocr_action = QAction("纯 OCR 识别", self.menu)
        self.pure_ocr_action.triggered.connect(self._trigger_pure_ocr)
        self.menu.addAction(self.pure_ocr_action)

        self.menu.addSeparator()

        self.toggle_hud_action = QAction("显示状态与字幕提示", self.menu)
        self.toggle_hud_action.setCheckable(True)
        self.toggle_hud_action.setChecked(hud_enabled)
        self.toggle_hud_action.toggled.connect(self._trigger_toggle_hud)
        self.menu.addAction(self.toggle_hud_action)

        self.open_setup_action = QAction("模型与热键设置", self.menu)
        self.open_setup_action.triggered.connect(self._trigger_open_setup)
        self.menu.addAction(self.open_setup_action)

        self.menu.addSeparator()

        self.quit_action = QAction("退出", self.menu)
        self.quit_action.triggered.connect(self._trigger_quit)
        self.menu.addAction(self.quit_action)

    @property
    def available(self) -> bool:
        """托盘图标是否已建立（宿主晚到时在其注册后变为 True）。"""
        return self._tray is not None

    def _ensure_tray(self) -> bool:
        """托盘可用且尚未建立时建立图标（沿用当前状态图标与提示）。"""
        if self._tray is None and self._tray_available():
            self._tray = QSystemTrayIcon(self._current_icon, self)
            self._tray.setToolTip(self._tooltip)
            self._tray.setContextMenu(self.menu)
            self._tray.activated.connect(self._on_activated)
        return self._tray is not None

    def _on_host_registered(self) -> None:
        """托盘宿主注册（解锁、扩展重载、宿主重启）：建立图标，或隐藏再显示以向新宿主重新登记。"""
        if not self._wants_visible:
            return
        existed = self._tray is not None
        if not self._ensure_tray():
            print("[tray] 托盘宿主已注册，但系统仍报告托盘不可用；托盘图标未显示", flush=True)
            return
        if existed:
            self._tray.hide()
        self._tray.show()
        print("[tray] 托盘宿主已就绪，已显示托盘图标", flush=True)

    @property
    def state(self) -> str:
        return self._state

    @property
    def tooltip(self) -> str:
        return self._tooltip

    def icon(self) -> QIcon:
        return self._current_icon

    def set_state(self, state: str, tooltip: str | None = None) -> None:
        """动态更新托盘图标状态角标与悬浮提示文案。"""
        self._state = (state or "idle").lower()
        self._current_icon = render_status_icon(self._state)
        if tooltip is not None:
            self._tooltip = tooltip
        if self._tray is not None:
            self._tray.setIcon(self._current_icon)
            if tooltip is not None:
                self._tray.setToolTip(self._tooltip)

    def set_server_stopped(self, stopped: bool) -> None:
        """本地服务停止时显示「重试启动本地服务」，恢复后隐藏（由应用显式调用，不从错误状态推断）。"""
        self.retry_server_action.setVisible(bool(stopped))

    def set_hud_enabled(self, enabled: bool) -> None:
        """同步菜单勾选状态而不重复触发外部回调。"""
        with QSignalBlocker(self.toggle_hud_action):
            self.toggle_hud_action.setChecked(bool(enabled))

    def show(self) -> bool:
        """显示托盘图标；暂不可用时写一行原因并返回 False，宿主注册后自动显示。"""
        self._wants_visible = True
        if not self._ensure_tray():
            print(
                f"[tray] 系统托盘暂不可用（{TRAY_HOST_SERVICE} 未注册：锁屏中、扩展未加载或早于宿主启动），"
                "将在宿主注册时自动显示",
                flush=True,
            )
            return False
        self._tray.show()
        return True

    def hide(self) -> None:
        self._wants_visible = False
        if self._tray is not None:
            self._tray.hide()

    def close(self) -> None:
        self.hide()
        self.menu.close()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._trigger_open_home()

    def _trigger_open_home(self) -> None:
        if self._on_open_home is not None:
            self._on_open_home()

    def _trigger_screenshot_translate(self) -> None:
        if self._on_screenshot_translate is not None:
            self._on_screenshot_translate()

    def _trigger_pure_ocr(self) -> None:
        if self._on_pure_ocr is not None:
            self._on_pure_ocr()

    def _trigger_open_setup(self) -> None:
        if self._on_open_setup is not None:
            self._on_open_setup()

    def _trigger_toggle_hud(self, checked: bool) -> None:
        if self._on_toggle_hud is not None:
            self._on_toggle_hud(bool(checked))

    def _trigger_quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()

    def _trigger_retry_server(self) -> None:
        if self._on_retry_server is not None:
            self._on_retry_server()

"""被控端托盘（阶段 6.10，v4 gui/tray.py 迁移，pystray → QSystemTrayIcon）。

v4 用 pystray（本机实测未安装）+ PIL 画图标；v5 用 Qt 原生托盘 + QPainter
绘制 16px 圆形状态图标（绿=运行/橙=暂停/灰=停止），零新依赖。

菜单（v4 全保留）：状态 / 复制 IPv4 / 复制 IPv6 / 暂停·恢复 / 重启服务 / 退出。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu

from rdc.host.netinfo import get_local_ip_v4, get_local_ip_v6
from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme.tokens import TOKENS

if TYPE_CHECKING:
    pass

__all__ = ["TrayController", "TrayState"]


class TrayState:
    """托盘状态常量（与 HostSnapshot.running/paused 组合对应）。"""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"

    _COLORS = {
        RUNNING: QColor(TOKENS.ok),
        PAUSED: QColor(TOKENS.warn),
        STOPPED: QColor(TOKENS.text_dim),
    }


def build_tray_icon(state: str = TrayState.RUNNING, size: int = 16) -> QIcon:
    """QPainter 绘制托盘图标：圆底 + 状态点。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QColor(TOKENS.bg_selected)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(base)
    m = size / 2
    p.drawEllipse(int(m * 0.25), int(m * 0.25), int(size - m * 0.5), int(size - m * 0.5))
    dot = TrayState._COLORS.get(state, TrayState._COLORS[TrayState.STOPPED])
    p.setBrush(dot)
    d = size * 0.45
    p.drawEllipse(int((size - d) / 2), int((size - d) / 2), int(d), int(d))
    p.end()
    return QIcon(pix)


class TrayController(QObject):
    """托盘生命周期与菜单动作。

    Signals:
        pauseToggled(): 用户点击"暂停/恢复"。
        restartRequested(): 用户点击"重启服务"。
        quitRequested(): 用户点击"退出"。

    Args:
        parent: 所有者（一般 = QApplication）。
        on_status_show: 点击"状态"项（弹出被控端状态窗/主窗）的回调。
    """

    pauseToggled = Signal()
    restartRequested = Signal()
    quitRequested = Signal()

    def __init__(
        self,
        *,
        on_status_show: Callable[[], None] | None = None,
        allow_restart: bool = True,
        parent: QObject | None = None,
    ) -> None:
        ensure_gui_thread("TrayController")
        super().__init__(parent)
        from PySide6.QtWidgets import QSystemTrayIcon  # 延迟：构造需要 QApplication

        self._state = TrayState.RUNNING
        self._client_count = 0
        self._on_status_show = on_status_show
        self._allow_restart = allow_restart
        self._icon = QSystemTrayIcon(build_tray_icon(self._state), parent or QApplication.instance())
        self._icon.activated.connect(self._on_activated)
        self._menu = self._build_menu()
        self._icon.setContextMenu(self._menu)
        self._uptime_fn: Callable[[], float] | None = None
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._refresh_tooltip)

    # -- 公开 API ---------------------------------------------------------------

    def show(self) -> None:
        """显示托盘图标（setVisualNotification 由调用方处理）。"""
        ensure_gui_thread("TrayController.show")
        self._icon.show()
        self._ticker.start()

    def hide(self) -> None:
        """隐藏并释放（close 级联）。"""
        ensure_gui_thread("TrayController.hide")
        self._ticker.stop()
        self._icon.hide()

    def set_state(self, state: str, *, uptime_s: float = 0.0) -> None:
        """同步状态：图标颜色 + tooltip + 暂停菜单文案。"""
        ensure_gui_thread("TrayController.set_state")
        if state != self._state:
            self._state = state
            self._icon.setIcon(build_tray_icon(state))
            self._rebuild_menu()
        self._uptime_s = uptime_s
        self._refresh_tooltip()

    def update_client_count(self, count: int) -> None:
        """连接数变化（tooltip + 状态项文案）。"""
        ensure_gui_thread("TrayController.update_client_count")
        self._client_count = count
        self._refresh_tooltip()

    # -- 内部 ---------------------------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        self._status_action = QAction(f"📡 状态: {self._state_label()}", menu)
        self._status_action.triggered.connect(self._show_status)
        menu.addAction(self._status_action)
        ipv4 = get_local_ip_v4()
        act4 = QAction(f"📋 复制IPv4: {ipv4}", menu)
        act4.triggered.connect(lambda: self._copy(ipv4))
        menu.addAction(act4)
        v6 = get_local_ip_v6()
        if v6:
            act6 = QAction(f"📋 复制IPv6: {v6}", menu)
            act6.triggered.connect(lambda: self._copy(v6))
            menu.addAction(act6)
        menu.addSeparator()
        pause_label = "⏸ 暂停服务" if self._state == TrayState.RUNNING else "▶ 恢复服务"
        act_pause = QAction(pause_label, menu)
        act_pause.triggered.connect(self.pauseToggled.emit)
        menu.addAction(act_pause)
        if self._allow_restart:
            act_restart = QAction("🔄 重启服务", menu)
            act_restart.triggered.connect(self.restartRequested.emit)
            menu.addAction(act_restart)
        menu.addSeparator()
        act_quit = QAction("🚪 退出", menu)
        act_quit.triggered.connect(self.quitRequested.emit)
        menu.addAction(act_quit)
        return menu

    def _rebuild_menu(self) -> None:
        old = self._menu
        self._menu = self._build_menu()
        self._icon.setContextMenu(self._menu)
        old.deleteLater()

    def _state_label(self) -> str:
        return {
            TrayState.RUNNING: "运行中",
            TrayState.PAUSED: "已暂停",
            TrayState.STOPPED: "已停止",
        }.get(self._state, self._state)

    def _show_status(self) -> None:
        if self._on_status_show is not None:
            self._on_status_show()

    @staticmethod
    def _copy(text: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication) and text:
            app.clipboard().setText(text)

    def _on_activated(self, reason: object) -> None:  # noqa: ANN401 - Qt 枚举跨版本
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_status()

    def _refresh_tooltip(self) -> None:
        up = getattr(self, "_uptime_s", 0.0)
        h, m, s = int(up // 3600), int(up % 3600 // 60), int(up % 60)
        self._icon.setToolTip(
            f"远程桌面控制 v5 · {self._state_label()} · 连接 {self._client_count}"
            + (f" · 运行 {h:02d}:{m:02d}:{s:02d}" if up else "")
        )

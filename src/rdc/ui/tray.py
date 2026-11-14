"""被控端托盘（阶段 6.10，v4 gui/tray.py 迁移，pystray → QSystemTrayIcon）。

v4 用 pystray（本机实测未安装）+ PIL 画图标；v5 用 Qt 原生托盘 + QPainter
绘制状态图标，零新依赖。阶段 10 起底图换为 WaltonDesk 品牌图标（包内
``theme/icon.ico``），右下角叠状态角标（绿=运行/橙=暂停/灰=停止）。

菜单（v4 全保留）：状态 / 复制 IPv4 / 复制 IPv6 / 暂停·恢复 / 重启服务 / 退出。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu

from rdc.brand import BRAND_NAME
from rdc.host.netinfo import get_display_ips, get_local_ip_v6, get_preferred_ipv4
from rdc.logging import get_logger
from rdc.ui.app import ensure_gui_thread
from rdc.ui.icons import build_app_icon
from rdc.ui.theme.tokens import TOKENS

if TYPE_CHECKING:
    pass

__all__ = ["TrayController", "TrayState"]

_LOG = get_logger("ui.tray")


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


def _brand_pixmap(size: int) -> QPixmap | None:
    """取品牌图标的指定尺寸位图；资源缺失/解不出返回 ``None``（调用方回落）。"""
    icon = build_app_icon()
    if icon is None:
        return None
    pix = icon.pixmap(size, size)
    return None if pix.isNull() else pix


def _legacy_status_icon(state: str, size: int) -> QIcon:
    """无品牌资源时的兜底画法：圆底 + 居中状态点（阶段 6.10 原实现）。"""
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


def build_tray_icon(state: str = TrayState.RUNNING, size: int = 16) -> QIcon:
    """托盘图标：WaltonDesk 品牌底图 + 右下角状态角标。

    阶段 10 接入品牌图标：底图来自包内 ``icon.ico``（与窗口图标同一资源），
    右下角叠状态点（绿=运行/橙=暂停/灰=停止）并描白色环——在 Windows 深浅
    两种任务栏底色下都看得清。

    资源缺失时回落 :func:`_legacy_status_icon`（纯色圆点），托盘**始终**可用：
    图标是外观问题，不能成为被控端起不来的原因。
    """
    pix = _brand_pixmap(size)
    if pix is None:
        return _legacy_status_icon(state, size)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    dot = TrayState._COLORS.get(state, TrayState._COLORS[TrayState.STOPPED])
    d = max(4, int(size * 0.42))
    p.setPen(QPen(QColor("#FFFFFF"), max(1, size // 12)))
    p.setBrush(dot)
    p.drawEllipse(size - d - 1, size - d - 1, d, d)
    p.end()
    return QIcon(pix)


class TrayController(QObject):
    """托盘生命周期与菜单动作。

    Signals:
        pauseToggled(): 用户点击"暂停/恢复"。
        restartRequested(): 用户点击"重启服务"。
        quitRequested(): 用户点击"退出"。
        autostartToggled(bool, bool): 开机自启注册结果 (目标态, 是否成功)；
            失败时菜单勾选已回滚（Feature 17）。
        changePasswordRequested(): 用户点击"修改密码"（Feature 18）。
        noticeSuppressed(): 用户在初始口令气泡勾选"不再提示"（Feature 18）。

    Args:
        parent: 所有者（一般 = QApplication）。
        on_status_show: 点击"状态"项（弹出被控端状态窗/主窗）的回调。
        autostart_get: 读取开机自启状态（默认
            :func:`rdc.host.autostart.is_autostart_enabled`；测试注入替身）。
        autostart_set: 开关开机自启（默认 :func:`rdc.host.autostart.set_autostart`）。
    """

    pauseToggled = Signal()
    restartRequested = Signal()
    quitRequested = Signal()
    #: 开机自启注册结果：``(enabled, ok)``（ok=False 表示注册/注销失败，
    #: 勾选态已回滚；Feature 17）。
    autostartToggled = Signal(bool, bool)
    changePasswordRequested = Signal()
    noticeSuppressed = Signal()

    def __init__(
        self,
        *,
        on_status_show: Callable[[], None] | None = None,
        allow_restart: bool = True,
        autostart_get: Callable[[], bool] | None = None,
        autostart_set: Callable[[bool], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        ensure_gui_thread("TrayController")
        super().__init__(parent)
        from PySide6.QtWidgets import QSystemTrayIcon  # 延迟：构造需要 QApplication

        if autostart_get is None:
            from rdc.host.autostart import is_autostart_enabled

            autostart_get = is_autostart_enabled
        if autostart_set is None:
            from rdc.host.autostart import set_autostart

            autostart_set = set_autostart
        self._autostart_get = autostart_get
        self._autostart_set = autostart_set
        self._state = TrayState.RUNNING
        self._client_count = 0
        self._on_status_show = on_status_show
        self._allow_restart = allow_restart
        self._icon = QSystemTrayIcon(build_tray_icon(self._state), parent or QApplication.instance())
        self._icon.activated.connect(self._on_activated)
        # 走查第十三批 Bug 2：messageClicked 一次性 connect 到守卫方法（替代
        # 旧 notify 里 disconnect()+connect()——无连接时 disconnect 必发
        # RuntimeWarning）。守卫确保仅在初始口令气泡展示期间触发抑制。
        self._notice_pending = False
        self._icon.messageClicked.connect(self._on_message_clicked)
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

    def notify_initial_password(self, text: str, *, timeout_ms: int = 60_000) -> bool:
        """托盘气泡展示初始口令提示（Feature 18）。

        Returns:
            气泡实际发出为 ``True``；平台不支持气泡（无系统托盘）返回
            ``False``，调用方须另寻途径提示（Launcher/日志），不得静默。
        """
        ensure_gui_thread("TrayController.notify_initial_password")
        from PySide6.QtWidgets import QSystemTrayIcon

        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        # 气泡本体放不进"不再提示"勾选框（QSystemTrayIcon 限制）：气泡负责
        # 显眼展示明文，点击气泡后由 _on_message_clicked 路径处理抑制。
        # 走查第十三批 Bug 2 两处修正：
        # ① 投递延迟到下一轮事件循环——showMessage 紧跟 show() 时，Windows
        #    通知区尚未完成图标注册，气泡被系统直接吞掉（真机表现"托盘没有
        #    弹出气泡"）；QTimer.singleShot(0) 跨过注册时序窗。
        # ② 点击回调构建期一次性 connect（见 __init__），此处**不再**
        #    disconnect()/connect()——PySide6 对无连接信号调用 disconnect
        #    必发 RuntimeWarning（真机终端警告源头）。
        QTimer.singleShot(0, lambda: self._icon.showMessage(
            f"{BRAND_NAME} · 初始密码", text, build_tray_icon(TrayState.RUNNING), timeout_ms
        ))
        self._notice_pending = True
        return True

    def _on_message_clicked(self) -> None:
        """气泡点击 → 仅在初始口令气泡展示期间触发"不再提示"（第十三批）。"""
        if self._notice_pending:
            self._notice_pending = False
            self.noticeSuppressed.emit()

    # -- 内部 ---------------------------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        self._status_action = QAction(f"📡 状态: {self._state_label()}", menu)
        self._status_action.triggered.connect(self._show_status)
        menu.addAction(self._status_action)
        # 走查第十四批 Bug 1：菜单文案与复制值都在**触发时刻**实时取——
        # 旧实现把启动时取到的 IP 闭包进 lambda，Windows IPv6 隐私临时地址
        # 周期性轮换 / 切网后用户复制的必是旧地址（连不上）。menu.aboutToShow
        # 每次弹出重写文案；复制动作再取一次当前值（弹出→点击间也可能变）。
        self._ip_action_v4 = QAction("📋 复制IPv4: …", menu)
        self._ip_action_v4.triggered.connect(
            lambda: self._copy(get_preferred_ipv4(), "IPv4")
        )
        menu.addAction(self._ip_action_v4)
        self._ip_action_v6 = QAction("📋 复制IPv6: …", menu)
        self._ip_action_v6.triggered.connect(
            lambda: self._copy(get_local_ip_v6() or "", "IPv6")
        )
        menu.addAction(self._ip_action_v6)
        menu.aboutToShow.connect(self._refresh_ip_actions)
        menu.addSeparator()
        pause_label = "⏸ 暂停服务" if self._state == TrayState.RUNNING else "▶ 恢复服务"
        act_pause = QAction(pause_label, menu)
        act_pause.triggered.connect(self.pauseToggled.emit)
        menu.addAction(act_pause)
        if self._allow_restart:
            act_restart = QAction("🔄 重启服务", menu)
            act_restart.triggered.connect(self.restartRequested.emit)
            menu.addAction(act_restart)
        # Feature 17：开机自启勾选（checkable，反映注册表现状）
        self._autostart_action = QAction("🚀 开机自启（静默被控端）", menu)
        self._autostart_action.setCheckable(True)
        try:
            self._autostart_action.setChecked(bool(self._autostart_get()))
        except Exception:  # noqa: BLE001 - 读取失败按未勾选呈现，不阻断菜单
            self._autostart_action.setChecked(False)
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        menu.addAction(self._autostart_action)
        # Feature 18：修改密码入口
        act_pwd = QAction("🔑 修改密码…", menu)
        act_pwd.triggered.connect(self.changePasswordRequested.emit)
        menu.addAction(act_pwd)
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

    def _refresh_ip_actions(self) -> None:
        """菜单弹出前刷新 IP 项：实时取数、标明协议；无 v6 时隐藏该行。

        走查第十四批 Bug 1 展示面：文案绝不缓存——每次 aboutToShow 都重新
        调用底层 API（隐私临时地址轮换/切网后，上一行字就是旧地址）。
        """
        try:
            v4 = get_preferred_ipv4()
        except Exception as exc:  # noqa: BLE001 - 取数失败降级占位，菜单仍可用
            _LOG.warning("tray_ip_probe_failed", proto="IPv4", error=repr(exc))
            v4 = "?"
        self._ip_action_v4.setText(f"📋 复制IPv4: {v4}")
        try:
            v6 = get_local_ip_v6()
        except Exception as exc:  # noqa: BLE001 - 同上
            _LOG.warning("tray_ip_probe_failed", proto="IPv6", error=repr(exc))
            v6 = None
        self._ip_action_v6.setVisible(bool(v6))
        if v6:
            self._ip_action_v6.setText(f"📋 复制IPv6: {v6}")

    def _show_status(self) -> None:
        if self._on_status_show is not None:
            self._on_status_show()

    @staticmethod
    def _copy(text: str, proto: str = "") -> None:
        """复制当前时刻的 IP 值（第十四批 Bug 1：值由调用点实时取参传入）。"""
        app = QApplication.instance()
        if isinstance(app, QApplication) and text and text != "?":
            app.clipboard().setText(text)
        elif proto:
            _LOG.info("tray_copy_unavailable", proto=proto)

    def _on_activated(self, reason: object) -> None:  # noqa: ANN401 - Qt 枚举跨版本
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_status()

    def _on_autostart_toggled(self, enable: bool) -> None:
        """开机自启勾选即时注册/注销；失败回滚勾选并上报（不静默，Feature 17）。"""
        try:
            ok = bool(self._autostart_set(enable))
        except Exception as exc:  # noqa: BLE001 - 注册表写失败：回滚后上报由调用方提示
            _LOG.error("tray_autostart_toggle_failed", enable=enable, error=repr(exc))
            ok = False
        if not ok:
            self._autostart_action.blockSignals(True)
            self._autostart_action.setChecked(not enable)
            self._autostart_action.blockSignals(False)
        self.autostartToggled.emit(enable, ok)

    def sync_autostart(self) -> bool:
        """重新读取注册表刷新勾选态（Launcher 改过自启后回托盘可调用）。"""
        try:
            enabled = bool(self._autostart_get())
        except Exception:  # noqa: BLE001 - 读取失败保持现状，不阻断
            return self._autostart_action.isChecked()
        self._autostart_action.blockSignals(True)
        self._autostart_action.setChecked(enabled)
        self._autostart_action.blockSignals(False)
        return enabled

    def _refresh_tooltip(self) -> None:
        up = getattr(self, "_uptime_s", 0.0)
        h, m, s = int(up // 3600), int(up % 3600 // 60), int(up % 60)
        # 走查第十四批 Bug 1：tooltip 的 IP 同样实时取（随秒级 ticker 刷新，
        # 绝不缓存启动值），并按 "IPv4: x / IPv6: y" 标明协议；取数失败
        # 降级省略该行，不影响状态/连接/运行时长显示。
        try:
            ips = " · ".join(get_display_ips())
        except Exception as exc:  # noqa: BLE001 - 展示降级，不阻断托盘
            _LOG.debug("tray_tooltip_ip_failed", error=repr(exc))
            ips = ""
        self._icon.setToolTip(
            f"{BRAND_NAME} · {self._state_label()} · 连接 {self._client_count}"
            + (f" · 运行 {h:02d}:{m:02d}:{s:02d}" if up else "")
            + (f"\n{ips}" if ips else "")
        )

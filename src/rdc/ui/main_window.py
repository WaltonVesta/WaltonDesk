"""主控端主窗口（阶段 6.7，v4 gui/main_window.py 778 行迁移重写）。

架构（v4 病灶全拆除）：

- **状态机单入口** :meth:`MainWindow._set_state`（v4 六态全保留：
  IDLE/CONNECTING/AUTHENTICATING/CONNECTED/RECONNECTING/DISCONNECTED），
  同步 toolbar/status/indicator 一处完成（v4 散在 5 个方法里）；
- **依赖倒置**：不 import 任何 controller 实现——:class:`ClientController`
  Protocol 注入（fake 可测；真实实现阶段 9 接线，且 client 层无 Widgets 导入）；
- **对话框编排协程化**：密码/指纹确认以 ``Callable[[str], Awaitable[...]]``
  传给 controller（B2 根治：同线程 await，无线程阻塞缝合）；
- v4 的 after_idle ×11、root.after 轮询、Event.wait 模式全部消失。

输入路由：ScreenView 信号 → controller.send_*（qasync 同线程直调）。
指标路由：controller.set_events(self) → frame_arrived/metrics_updated →
ScreenView/StatusBar/PerformancePanel/QualityIndicator。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from rdc.config.settings import (
    Settings,
    apply_validators,
    current_settings,
    save_settings,
    set_current,
)
from rdc.logging import get_logger
from rdc.ui.app import ensure_gui_thread
from rdc.ui.clipboard_sync import ClipboardSync
from rdc.ui.fingerprint_dialog import confirm_fingerprint
from rdc.ui.password_dialog import request_password
from rdc.ui.performance_panel import PerformancePanel
from rdc.ui.screen_view import ScreenView
from rdc.ui.settings_dialog import SettingsDialog
from rdc.ui.status_bar import StatusBar
from rdc.ui.toolbar import Toolbar

__all__ = [
    "ClientController",
    "ClientEvents",
    "ConnectionState",
    "MainWindow",
]

_LOG = get_logger("ui.main_window")


class ConnectionState(StrEnum):
    """连接状态（v4 ConnectionState 六态原样保留）。"""

    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


class ClientController(Protocol):
    """主控端会话协作者协议（阶段 9 真实实现；UI 只依赖本接口）。

    约定：所有回调（events）由实现方保证在 **GUI 线程**调用（qasync 同线程
    结构天然满足；跨线程实现必须自行投递）。
    """

    def set_events(self, events: ClientEvents) -> None:
        """注册事件回调（connect 之前调用）。"""
        ...

    async def connect(
        self,
        host: str,
        port: int,
        *,
        get_password: Callable[[str], Awaitable[str | None]],
        confirm_fingerprint: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        """建连+认证。失败抛 ConnectionError；密码/指纹经回调向 UI 索取。"""
        ...

    async def disconnect(self) -> None:
        """主动断开（幂等）。"""
        ...

    def set_control_enabled(self, enabled: bool) -> None:
        """远程控制开关（False 时 send_* 为 no-op）。"""
        ...

    def send_mouse(self, action: str, x: int, y: int) -> None:
        """转发鼠标（action 同 ScreenView.mouseMoved 载荷格式）。"""
        ...

    def send_key(self, qt_key: int, press: bool) -> None:
        """转发按键（Qt key code → 阶段 7 映射）。"""
        ...

    def send_text(self, text: str) -> None:
        """转发已上屏文本（IME 提交串 → InputBackend.type_text，阶段 7 批 E）。"""
        ...


class ClientEvents(Protocol):
    """controller → UI 事件回调协议（GUI 线程调用）。"""

    def state_changed(self, state: str) -> None: ...

    def frame_arrived(self, image: QImage, *, left: int = 0, top: int = 0) -> None: ...

    def metrics_updated(
        self,
        *,
        fps: float | None = None,
        latency_ms: float | None = None,
        bandwidth_kbps: float | None = None,
        quality: int | None = None,
        resolution: tuple[int, int] | None = None,
        total_frames: int | None = None,
        packet_loss: float = 0.0,
    ) -> None: ...

    def clipboard_from_remote(self, text: str) -> None: ...

    def clipboard_image_from_remote(self, image: QImage) -> None:
        """远端图片写入本地剪贴板（批 E 扩展；阶段 9 实现）。"""
        ...

    def clipboard_files_from_remote(self, paths: list[str]) -> None:
        """远端文件清单写入本地剪贴板（内容经阶段 8 通道；批 E）。"""
        ...


class MainWindow(QMainWindow):
    """主控端窗口：工具栏 + 画面视图 + 性能面板 + 状态栏。"""

    def __init__(
        self,
        controller: ClientController | None = None,
        *,
        settings: Settings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        ensure_gui_thread("MainWindow")
        super().__init__(parent)
        self._controller = controller
        self._settings = settings if settings is not None else current_settings()
        self._state = ConnectionState.IDLE
        self._control_on = False
        self._tasks: set[asyncio.Task[object]] = set()
        self.setWindowTitle("远程桌面控制 v5 — 主控端")
        self.resize(1080, 720)

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.toolbar = Toolbar(
            default_port=self._settings.control_port,
            recent_hosts=tuple(self._settings.recent_hosts),
        )
        lay.addWidget(self.toolbar)
        self.screen_view = ScreenView()
        lay.addWidget(self.screen_view, 1)
        self.performance_panel = PerformancePanel(
            bandwidth_limit_kbps=float(self._settings.bandwidth_limit)
        )
        self.performance_panel.setVisible(False)
        lay.addWidget(self.performance_panel)
        self.status_bar = StatusBar()
        lay.addWidget(self.status_bar)

        # 工具栏信号 → 状态机（协程槽必须经 ensure_future 投递，见 _spawn）
        self.toolbar.connectRequested.connect(
            lambda h, p: self._spawn(self._start_connect(h, p))
        )
        self.toolbar.disconnectRequested.connect(lambda: self._spawn(self._user_disconnect()))
        self.toolbar.controlToggled.connect(self._on_control_toggled)
        self.toolbar.settingsRequested.connect(self._open_settings)
        self.toolbar.fullscreenRequested.connect(self._toggle_fullscreen)

        # 画面输入 → controller
        self.screen_view.mouseMoved.connect(self._on_mouse)
        self.screen_view.keyEvent.connect(self._on_key)
        self.screen_view.textCommitted.connect(self._on_text)

        # 快捷键（v4 保留：F11 全屏、Ctrl+Alt+P 性能面板、Esc 退全屏）
        sc_full = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_perf = QShortcut(QKeySequence("Ctrl+Alt+P"), self)
        sc_perf.activated.connect(self._toggle_perf)
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.activated.connect(self._on_escape)

        # 剪贴板同步（enable_clipboard 开关联动；真实收发接线阶段 9）
        self.clipboard = ClipboardSync()
        self.clipboard.set_enabled(self._settings.enable_clipboard)
        self.clipboard.localChanged.connect(self._on_local_clipboard)
        self.clipboard.localImageChanged.connect(self._on_local_clipboard_image)
        self.clipboard.localFilesChanged.connect(self._on_local_clipboard_files)

        if self._controller is not None:
            self._controller.set_events(self)

    # -- 状态机 ---------------------------------------------------------------

    def _spawn(self, coro: Awaitable[object]) -> None:
        """把协程投递到 qasync 事件循环（Qt 信号槽不能直连协程）。

        任务引用挂 self._tasks 防 GC（asyncio 官方口径）；完成后自动移除。
        """
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def state(self) -> str:
        """当前连接状态。"""
        return self._state

    def state_changed(self, state: str) -> None:
        """ClientEvents 实现：controller 状态变化入口（幂等设态）。"""
        ensure_gui_thread("MainWindow.state_changed")
        self._set_state(state)

    def _set_state(self, new: ConnectionState | str) -> None:
        """状态流转唯一入口（同步全部 UI 件；v4 _set_state 的 hasattr 防御拆除）。"""
        try:
            st = ConnectionState(new)
        except ValueError:
            st = ConnectionState.DISCONNECTED  # 未知状态串：保守按未连接渲染
        old = self._state
        if old == st:
            return
        self._state = st
        _LOG.info("window_state", old=old, new=st)
        connected = st == ConnectionState.CONNECTED
        self.screen_view.set_input_enabled(connected and self._control_on)
        self.toolbar.set_connection_state(str(st))
        if not connected:
            self.toolbar.indicator.set_offline()
        label = {
            ConnectionState.CONNECTING: ("连接中...", "connecting"),
            ConnectionState.AUTHENTICATING: ("认证中...", "connecting"),
            ConnectionState.CONNECTED: ("已连接", "connected"),
            ConnectionState.RECONNECTING: ("重连中...", "reconnecting"),
            ConnectionState.DISCONNECTED: ("未连接", "disconnected"),
            ConnectionState.IDLE: ("未连接", "info"),
        }[st]
        self.status_bar.set_status(label[0], label[1])

    # -- ClientEvents 其余实现 ----------------------------------------------------

    def frame_arrived(self, image: QImage, *, left: int = 0, top: int = 0) -> None:
        """ClientEvents：新解码帧上屏 + FPS 计数。"""
        ensure_gui_thread("MainWindow.frame_arrived")
        self.screen_view.present_image(image, left=left, top=top)
        self.status_bar.notify_frame()

    def metrics_updated(
        self,
        *,
        fps: float | None = None,
        latency_ms: float | None = None,
        bandwidth_kbps: float | None = None,
        quality: int | None = None,
        resolution: tuple[int, int] | None = None,
        total_frames: int | None = None,
        packet_loss: float = 0.0,
    ) -> None:
        """ClientEvents：指标快照分发到状态栏/性能面板/指示灯。"""
        ensure_gui_thread("MainWindow.metrics_updated")
        self.status_bar.set_latency(latency_ms)
        self.status_bar.set_quality(quality)
        self.performance_panel.update_metrics(
            fps=fps,
            latency_ms=latency_ms,
            bandwidth_kbps=bandwidth_kbps,
            quality=quality,
            resolution=resolution,
            total_frames=total_frames,
        )
        if fps is not None and latency_ms is not None:
            self.toolbar.indicator.update_quality(
                latency_ms, fps, packet_loss, is_connected=self.state == ConnectionState.CONNECTED
            )

    def clipboard_from_remote(self, text: str) -> None:
        """ClientEvents：远端剪贴板写入本地（防回环在 ClipboardSync 内）。"""
        ensure_gui_thread("MainWindow.clipboard_from_remote")
        if self._settings.enable_clipboard:
            self.clipboard.apply_remote(text)

    def clipboard_image_from_remote(self, image: QImage) -> None:
        """ClientEvents：远端剪贴板图片写入本地（批 E）。"""
        ensure_gui_thread("MainWindow.clipboard_image_from_remote")
        if self._settings.enable_clipboard:
            self.clipboard.apply_remote_image(image)

    def clipboard_files_from_remote(self, paths: list[str]) -> None:
        """ClientEvents：远端剪贴板文件清单写入本地（批 E）。"""
        ensure_gui_thread("MainWindow.clipboard_files_from_remote")
        if self._settings.enable_clipboard:
            self.clipboard.apply_remote_files(paths)

    # -- 动作路由 ---------------------------------------------------------------

    async def _start_connect(self, host: str, port: int) -> None:
        """连接动作（toolbar 信号槽）：状态机驱动 + controller 协程。"""
        if self._controller is None:
            self._error_box("会话引擎未接线（阶段 9 装配），当前仅 UI 演示。")
            return
        if self._state not in {ConnectionState.IDLE, ConnectionState.DISCONNECTED}:
            return
        self._set_state(ConnectionState.CONNECTING)
        self.toolbar.set_host_editable(False)
        try:
            await self._controller.connect(
                host,
                port or self._settings.control_port,
                get_password=lambda h: request_password(self, h),
                confirm_fingerprint=lambda h, f: confirm_fingerprint(self, h, f),
            )
        except Exception as exc:  # noqa: BLE001 - 边界：网络失败展示并走重连策略
            _LOG.warning("connect_failed", host=host, error=repr(exc))
            self._handle_connect_failure(host)
            return
        self._on_connect_success(host)

    def _handle_connect_failure(self, host: str) -> None:
        """失败后决策：auto_reconnect 开启 → RECONNECTING（controller 内部重试），
        否则 DISCONNECTED。"""
        if self._settings.auto_reconnect:
            self._set_state(ConnectionState.RECONNECTING)
        else:
            self._set_state(ConnectionState.DISCONNECTED)
            self.status_bar.set_status(f"连接 {host} 失败", "error")
            self.toolbar.set_host_editable(True)

    def _on_connect_success(self, host: str) -> None:
        self._set_state(ConnectionState.CONNECTED)
        self.toolbar.add_recent_host(host)
        self.status_bar.set_info(host)

    async def _user_disconnect(self) -> None:
        """主动断开：不走重连。"""
        if self._controller is not None:
            try:
                await self._controller.disconnect()
            except Exception as exc:  # noqa: BLE001 - 断开失败仍要落 UI 态
                _LOG.warning("disconnect_error", error=repr(exc))
        self._set_state(ConnectionState.DISCONNECTED)
        self.screen_view.clear_frame()
        self.toolbar.set_host_editable(True)

    def _on_control_toggled(self, enabled: bool) -> None:
        self._control_on = enabled
        self.screen_view.set_input_enabled(enabled and self.state == ConnectionState.CONNECTED)
        if self._controller is not None:
            self._controller.set_control_enabled(enabled)
        self.status_bar.set_status("控制已启用" if enabled else "控制已禁用", "control" if enabled else "info")

    def _on_mouse(self, action: str, x: int, y: int) -> None:
        if self._controller is not None:
            self._controller.send_mouse(action, x, y)

    def _on_key(self, qt_key: int, press: bool) -> None:
        if self._controller is not None:
            self._controller.send_key(qt_key, press)

    def _on_text(self, text: str) -> None:
        """IME/可打印文本提交 → controller.send_text（无 controller 时 no-op）。"""
        if self._controller is not None:
            self._controller.send_text(text)

    def _on_local_clipboard(self, text: str) -> None:
        # 阶段 9 接 controller.send_clipboard；此处仅计数观测
        _LOG.debug("clipboard_local_change", length=len(text))

    def _on_local_clipboard_image(self, image: QImage) -> None:
        """本地图片变更观测（批 E；阶段 9 接 controller 出站编码）。"""
        _LOG.debug("clipboard_local_image", width=image.width(), height=image.height())

    def _on_local_clipboard_files(self, paths: list[str]) -> None:
        """本地文件清单变更观测（批 E；内容传输归阶段 8 通道）。"""
        _LOG.debug("clipboard_local_files", count=len(paths))

    # -- 对话框/视图编排 -----------------------------------------------------------

    def _open_settings(self) -> None:
        ensure_gui_thread("MainWindow._open_settings")
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            new = dlg.apply()
            warnings = apply_validators(new)
            save_settings(new)
            set_current(new)
            self._settings = new
            self.performance_panel.set_bandwidth_limit(float(new.bandwidth_limit))
            self.clipboard.set_enabled(new.enable_clipboard)
            self.toolbar.set_connection_state(self.state)  # 端口提示等重同步
            if warnings:
                self.status_bar.set_status("；".join(warnings), "error")
            else:
                self.status_bar.set_status("设置已保存", "info")

    def _toggle_fullscreen(self) -> None:
        ensure_gui_thread("MainWindow._toggle_fullscreen")
        if self.isFullScreen():
            self.showNormal()
            self.screen_view.set_fit_mode(False)
        else:
            self.showFullScreen()
            self.screen_view.set_fit_mode(True)

    def _toggle_perf(self) -> None:
        ensure_gui_thread("MainWindow._toggle_perf")
        self.performance_panel.setVisible(not self.performance_panel.isVisible())

    def _on_escape(self) -> None:
        if self.isFullScreen():
            self._toggle_fullscreen()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 覆写
        if self.state == ConnectionState.CONNECTED and self._control_on:
            # 控制态：窗口级按键也转发（输入框等例外由 Qt focus 先消费）
            self._controller_send_key(event)
            return
        super().keyPressEvent(event)

    def _controller_send_key(self, event: QKeyEvent) -> None:
        if self._controller is not None:
            self._controller.send_key(event.key(), True)

    # -- 关闭 -------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写
        ensure_gui_thread("MainWindow.closeEvent")
        self.clipboard.stop()
        self.status_bar.stop()
        if self._controller is not None:
            # 尽力断开；事件循环若已在关停则跳过（阶段 9 统一异步关停编排）
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not None and not running.is_closed():
                asyncio.ensure_future(self._controller.disconnect())
        self._esc.setEnabled(False)
        super().closeEvent(event)

    # -- 小工具 -------------------------------------------------------------------

    def _error_box(self, msg: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("提示")
        box.setText(msg)
        box.open()  # 非模态（红线：不阻塞事件循环）

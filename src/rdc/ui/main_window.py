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

import shiboken6
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QVBoxLayout, QWidget

from rdc.config.settings import (
    Settings,
    apply_validators,
    current_settings,
    save_settings,
    set_current,
)
from rdc.exceptions import AuthBannedError, AuthCancelledError, AuthError, TlsError
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

    def set_events(self, events: ClientEvents | None) -> None:
        """注册事件回调（connect 之前调用）；``None`` = 解除绑定（关窗防回调
        触达已销毁 UI，走查第六批 Bug5）。"""
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

    def host_status(
        self,
        *,
        stalled: bool,
        secure_desktop: bool,
        paused: bool = False,
        active: bool = False,
    ) -> None:
        """服务端状态下行（走查第六批 P0）：画面停滞/恢复，含安全桌面归因；
        走查第十二批 Bug 12 扩展 paused=被控端托盘暂停通知；走查第十四批
        Bug 2 扩展 active=服务已从暂停恢复（会话保活、无 EOF 覆盖，UI 据此
        在"被控端已暂停"与"已连接"间切换，严禁进入断连态）。"""
        ...

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
        #: 走查第十四批 Bug 2：软暂停呈现态。_paused_hint=当前显示"已暂停"
        #: 提示中；_control_before_pause=暂停前的控制开关（恢复时还原）。
        self._paused_hint = False
        self._control_before_pause = False
        self._tasks: set[asyncio.Task[object]] = set()
        #: 封禁倒计时（走查第六批 Bug6）：墙钟 QTimer 每秒刷新状态栏，
        #: 归零自动恢复连接按钮。None = 无倒计时。
        self._ban_timer: QTimer | None = None
        self._ban_remaining_s = 0
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

        # 走查第十批：Tab/Backtab 被 Qt 焦点链在 keyPressEvent **之前**消费
        # （QWidget::event() 直接转 focusNextPrevChild，谁有焦点谁吃掉——
        # 按钮/视口都算，控件级 accept() 够不着）。应用级过滤器是拦它的唯一
        # 入口；仅"已连接+控制开启+事件属本窗控件树"三条件齐才拦截，
        # 其余场景（含本窗非控制态、其它顶层窗/对话框）导航如常。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

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

    def _ui_alive(self) -> bool:
        """C++ 侧窗口（及其子控件树）是否仍存活。

        走查第六批 Bug5：``session.py`` 给主窗挂 ``WA_DeleteOnClose``——关窗即
        销毁 ScreenView/StatusBar 等子对象，而 controller 仍持有本窗的 Python
        引用并在稍后（disconnect 协程收尾、画面流 EOF 事件）回调事件入口，
        触达已删对象即抛 ``RuntimeError: Internal C++ object already deleted``
        （真机日志 event_hook_failed hook=state_changed）。``shiboken6.isValid``
        对**已删自身**返回 False，子对象随父树同删，故本判定覆盖全部下游触达。
        """
        return shiboken6.isValid(self)

    def state_changed(self, state: str) -> None:
        """ClientEvents 实现：controller 状态变化入口（幂等设态）。"""
        ensure_gui_thread("MainWindow.state_changed")
        if not self._ui_alive():
            return
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
        if not self._ui_alive():
            return
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
        if not self._ui_alive():
            return
        self.status_bar.set_latency(latency_ms)
        self.status_bar.set_quality(quality)
        self.status_bar.set_resolution(resolution)
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

    def host_status(
        self,
        *,
        stalled: bool,
        secure_desktop: bool,
        paused: bool = False,
        active: bool = False,
    ) -> None:
        """ClientEvents：服务端状态下行（第六批 P0 停滞 + 第十二批 Bug 12 暂停）。

        走查第十四批 Bug 2（映射重做）：被控端暂停改**保活软暂停**——会话
        不拆、无 EOF，本端绝不进入 DISCONNECTED/RECONNECTING/AUTHENTICATING
        （旧 bug：拆会话的 EOF 把暂停提示覆盖成"认证中/已断开"）。

        - paused → 状态栏红字"被控端已暂停"、禁用控制（记住暂停前开关态）；
        - active → 还原控制开关、状态栏回"已连接"；
        - 停滞且安全桌面 → "远端处于安全桌面（UAC 确认中），画面暂停"；
        - 普通停滞 → 采集异常等待自愈；恢复 → 状态栏回常态。
        """
        ensure_gui_thread("MainWindow.host_status")
        if not self._ui_alive():
            return
        if paused:
            self._paused_hint = True
            self._control_before_pause = self._control_on
            # 先经统一入口关控制（维护 _control_on 与控制器开关一致），再覆
            # 写状态栏——_on_control_toggled 自带的"控制已禁用"不能盖掉暂停提示。
            self._on_control_toggled(False)
            self.toolbar.set_control_state(False)
            self.status_bar.set_status("被控端已暂停（连接保持，等待对方恢复）", "error")
            return
        if active:
            if not self._paused_hint:
                return  # 非暂停期收到 ACTIVE（乱序/重复帧）：不动 UI
            self._paused_hint = False
            restore = self._control_before_pause
            self._on_control_toggled(restore)
            self.toolbar.set_control_state(restore)
            self.status_bar.set_status("已连接", "connected")
            return
        if stalled:
            if secure_desktop:
                self.status_bar.set_status(
                    "远端处于安全桌面（UAC 确认中），画面暂停——操作完成后自动恢复",
                    "warn",
                )
            else:
                self.status_bar.set_status("远端画面采集异常，等待自动恢复…", "warn")
        else:
            self.status_bar.set_status("画面已恢复", "connected")

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
            self._handle_connect_failure(host, exc)
            return
        self._on_connect_success(host)

    def _handle_connect_failure(self, host: str, exc: Exception | None = None) -> None:
        """失败分派（第四批缺陷 1/3 口径 + 第十二批 Bug 19 收口）：

        - 用户取消口令（AuthCancelledError）→ DISCONNECTED，中性"已取消连接"
          （主动放弃不报"认证失败"，避免误导）；
        - 认证/TLS 被拒（AuthError/TlsError）→ controller 已不重连（缺陷 3），
          UI 直接落定 DISCONNECTED 并用红字展示具体原因（如"口令校验失败"）；
        - 封禁（AuthBannedError，AuthError 子类）→ 落定 DISCONNECTED 后**额外
          禁用连接按钮 + 启动墙钟倒计时**（走查第六批 Bug6），归零自动恢复；
        - **其余（网络类：ConnectionRefusedError / 其它 OSError）→ 落定
          DISCONNECTED + 弹窗明确提示**（第十二批 Bug 19）。旧版此处按
          auto_reconnect 进 RECONNECTING，而 controller 首连已不再起环——
          UI 会永远停在"重连中..."且无任何提示（界面像死机）。现在弹窗
          给出人话原因（被拒=对方没开被控端），状态机回到可连接态。
        """
        if isinstance(exc, AuthCancelledError):
            self._set_state(ConnectionState.DISCONNECTED)
            self.status_bar.set_status("已取消连接", "disconnected")
            self.toolbar.set_host_editable(True)
            return
        if isinstance(exc, AuthBannedError):
            # 先于 (AuthError, TlsError) 判定：AuthBannedError 是其子类
            self._set_state(ConnectionState.DISCONNECTED)  # 会重新启用连接按钮
            self._start_ban_countdown(exc.remaining_seconds)  # 随即禁用 + 倒计时
            self.toolbar.set_host_editable(True)
            return
        if isinstance(exc, (AuthError, TlsError)):
            self._set_state(ConnectionState.DISCONNECTED)
            self.status_bar.set_status(str(exc) or "认证失败", "error")
            self.toolbar.set_host_editable(True)
            return
        # 网络类：首连失败不再假装"重连中"（controller 侧亦已不起环）
        self._set_state(ConnectionState.DISCONNECTED)
        reason = self._network_failure_reason(exc)
        self.status_bar.set_status(f"连接 {host} 失败：{reason}", "error")
        self.toolbar.set_host_editable(True)
        self._error_box(f"无法连接 {host}\n\n{reason}\n\n请确认对方已开启被控端后重试。")

    @staticmethod
    def _network_failure_reason(exc: Exception | None) -> str:
        """网络失败 → 用户可读原因（Bug 19：把异常分型翻译成人话）。"""
        if isinstance(exc, ConnectionRefusedError):
            return "对方未开启被控端（连接被拒绝）"
        if isinstance(exc, TimeoutError):
            return "连接超时（地址不可达或被防火墙拦截）"
        if isinstance(exc, OSError):
            return str(exc) or "网络不可达"
        return str(exc) or "连接失败"

    def _on_connect_success(self, host: str) -> None:
        self._stop_ban_countdown(restore_button=False)  # 成功即清残留倒计时（按钮归状态机管）
        self._set_state(ConnectionState.CONNECTED)
        self.toolbar.add_recent_host(host)
        self.status_bar.set_info(host)

    # -- 封禁倒计时（走查第六批 Bug6） -----------------------------------------

    def _start_ban_countdown(self, remaining_seconds: int) -> None:
        """启动封禁倒计时：禁用连接按钮 + 状态栏每秒刷新，归零自动恢复。

        ``remaining_seconds`` 来自 controller 依服务端回传缓存折算（Bug3 的
        ``AuthBannedError.remaining_seconds``）。UI 用墙钟 QTimer 独立倒数，
        与 controller 注入时钟解耦（此处只需真实秒级观感）。重复触发（封禁期
        再点连接又被拒）以最新剩余秒重置。
        """
        ensure_gui_thread("MainWindow._start_ban_countdown")
        self._ban_remaining_s = max(0, int(remaining_seconds))
        self.toolbar.set_connect_enabled(False)
        self._render_ban_countdown()
        if self._ban_timer is None:
            self._ban_timer = QTimer(self)
            self._ban_timer.setInterval(1000)
            self._ban_timer.timeout.connect(self._tick_ban_countdown)
        self._ban_timer.start()

    def _tick_ban_countdown(self) -> None:
        """每秒递减；归零停表并恢复连接按钮。"""
        self._ban_remaining_s -= 1
        if self._ban_remaining_s <= 0:
            self._stop_ban_countdown()
            return
        self._render_ban_countdown()

    def _render_ban_countdown(self) -> None:
        """刷新状态栏倒计时文案（"X 分 Y 秒后可重试" / "Y 秒后可重试"）。"""
        secs = max(0, self._ban_remaining_s)
        mins, rest = divmod(secs, 60)
        tail = f"{mins} 分 {rest} 秒后可重试" if mins else f"{rest} 秒后可重试"
        self.status_bar.set_status(f"已被服务端封禁，{tail}", "error")

    def _stop_ban_countdown(self, *, restore_button: bool = True) -> None:
        """停倒计时（幂等）。``restore_button`` 决定是否恢复连接按钮可用性
        （归零恢复=True；连接成功/关窗时按钮由状态机管理，传 False）。"""
        if self._ban_timer is not None:
            self._ban_timer.stop()
        self._ban_remaining_s = 0
        if restore_button:
            self.toolbar.set_connect_enabled(True)

    async def _user_disconnect(self) -> None:
        """主动断开：不走重连。"""
        if self._controller is not None:
            try:
                await self._controller.disconnect()
            except Exception as exc:  # noqa: BLE001 - 断开失败仍要落 UI 态
                _LOG.warning("disconnect_error", error=repr(exc))
        self._set_state(ConnectionState.DISCONNECTED)
        self.screen_view.clear_frame()
        self.status_bar.set_resolution(None)
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
        # 第五批走查：fit（等比居中黑边）已是默认常驻策略，全屏切换只改窗口
        # 模式——视口尺寸变化由 ScreenView.resizeEvent 自动重算缩放，
        # 不再在此开关 fit（旧版退出全屏强切 1:1，窗口态又回到溢出老 bug）
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _toggle_perf(self) -> None:
        ensure_gui_thread("MainWindow._toggle_perf")
        self.performance_panel.setVisible(not self.performance_panel.isVisible())

    def _on_escape(self) -> None:
        if self.isFullScreen():
            self._toggle_fullscreen()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 覆写
        if self.state == ConnectionState.CONNECTED and self._control_on:
            # 控制态：窗口级按键也转发（输入框等例外由 Qt focus 先消费）
            self._controller_send_key(event, True)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 覆写
        # 走查第九批配套：窗口级此前只转 press 不转 release（无本覆写，
        # 释放走 QWidget 默认实现被丢）——焦点恰不在 ScreenView（如停在
        # 工具栏）时每个键都"按下已转发、释放丢失"→ 被控端卡键。与
        # keyPressEvent 对称转发；粘滞热键兜底另有 controller 层对账。
        if self.state == ConnectionState.CONNECTED and self._control_on:
            self._controller_send_key(event, False)
            return
        super().keyReleaseEvent(event)

    def _controller_send_key(self, event: QKeyEvent, press: bool) -> None:
        if self._controller is not None:
            self._controller.send_key(event.key(), press)

    def eventFilter(self, obj: object, event: object) -> bool:  # noqa: N802
        """走查第十批：应用级拦截 Tab/Backtab，阻止本地焦点跳转并转发远端。

        Qt 在 ``QWidget::event()`` 把无修饰 Tab/Backtab 转成焦点导航，发生
        在 ``keyPressEvent`` **之前**——消费主体是"当前有焦点的控件"：焦点
        在画面时由 :meth:`ScreenView.focusNextPrevChild` 拒掉（画面自身覆写），
        焦点在工具栏/状态栏时则被那个控件直接吃掉，画面层覆写够不着。
        本过滤器覆盖全窗控件树，三条件齐才拦：已连接 + 控制开启 + 事件对象
        属本窗控件树。口令/设置/指纹对话框是**独立顶层窗**（不在本窗树内），
        其 Tab 焦点导航不受影响；控制关闭时本窗导航亦如常。
        """
        if (
            self._control_on
            and self.state == ConnectionState.CONNECTED
            and isinstance(event, QKeyEvent)
            and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
            and isinstance(obj, QWidget)
            and (obj is self or self.isAncestorOf(obj))
        ):
            et = event.type()
            if et == QEvent.Type.KeyPress:
                self._controller_send_key(event, True)
            elif et == QEvent.Type.KeyRelease:
                self._controller_send_key(event, False)
            return True
        return super().eventFilter(obj, event)  # type: ignore[arg-type]

    # -- 关闭 -------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写
        ensure_gui_thread("MainWindow.closeEvent")
        # 走查第十批：先卸载应用级 Tab 守卫过滤器（与第六批 Bug5"先解绑
        # 回调源"同纵深——窗口销毁后 QApplication 不得再回调本窗 Python 壳）。
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._stop_ban_countdown(restore_button=False)
        self.clipboard.stop()
        self.status_bar.stop()
        if self._controller is not None:
            # 走查第六批 Bug5（纵深防御第一层）：**先解绑回调源**再调度断开。
            # disconnect() 是后台协程，其收尾的 state_changed("disconnected")
            # 可能落在本窗（WA_DeleteOnClose）销毁之后；解绑后 controller 不再
            # 持有本窗引用，事件自然停发。_ui_alive() 守卫兜住其它拆窗路径。
            self._controller.set_events(None)
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

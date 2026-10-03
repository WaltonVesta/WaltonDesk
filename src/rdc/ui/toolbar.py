"""主窗口工具栏（阶段 6.5/6.7，v4 gui/toolbar.py 迁移）。

v5 差异：不再在 widget 内回调 controller（v4 直接持 controller 引用）——
本件**只发 Signal**，动作由 main_window 状态机路由；连接状态由外部 setter
驱动（单一事实源在主窗口）。最近主机下拉从 QSettings 读（v4 用 config 单例）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from rdc.ui.app import ensure_gui_thread
from rdc.ui.quality_indicator import QualityIndicator
from rdc.ui.theme.tokens import TOKENS

__all__ = ["Toolbar"]

_MAX_RECENT = 8


class Toolbar(QWidget):
    """顶部工具条：IP/端口输入 | 连接/断开 | 控制开关 | 画质 | 指示灯 | 全屏/设置。"""

    #: 用户在 IP 输入框回车/点连接 → (host, port)
    connectRequested = Signal(str, int)
    disconnectRequested = Signal()
    #: 切换远程控制（目标态）
    controlToggled = Signal(bool)
    #: 请求打开设置对话框
    settingsRequested = Signal()
    #: 请求切换全屏
    fullscreenRequested = Signal()
    #: 画质预设变更（0-100）
    qualityChanged = Signal(int)

    def __init__(
        self,
        *,
        default_host: str = "",
        default_port: int = 0,
        recent_hosts: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        ensure_gui_thread("Toolbar")
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(TOKENS.spacing, TOKENS.spacing, TOKENS.spacing, TOKENS.spacing)
        lay.setSpacing(TOKENS.spacing)

        self._host = QComboBox()
        self._host.setEditable(True)
        self._host.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._host.setMinimumWidth(160)
        self._host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._host.addItems(list(recent_hosts)[:_MAX_RECENT])
        if default_host:
            self._host.setCurrentText(default_host)
        edit = self._host.lineEdit()
        assert edit is not None  # nosec B101 -- setEditable(True) 后 lineEdit() 恒非空（Qt API 不变量）
        edit.setPlaceholderText("IP 地址")
        edit.returnPressed.connect(self._emit_connect)

        self._port = QLabel(str(default_port) if default_port else "")
        self._port.setStyleSheet(f"color: {TOKENS.text_dim};")

        self._connect_btn = QPushButton("连接")
        self._connect_btn.setDefault(True)
        self._connect_btn.clicked.connect(self._emit_connect)

        self._disconnect_btn = QPushButton("断开")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self.disconnectRequested.emit)

        self._control_btn = QPushButton("启用控制")
        self._control_btn.setCheckable(True)
        self._control_btn.setEnabled(False)
        self._control_btn.toggled.connect(self._on_control_toggled)

        self._quality_label = QLabel("画质: --")
        self._quality_label.setStyleSheet(f"color: {TOKENS.text_dim};")

        self.indicator = QualityIndicator()

        self._fullscreen_btn = QPushButton("⛶ 全屏")
        self._fullscreen_btn.clicked.connect(self.fullscreenRequested.emit)
        self._settings_btn = QPushButton("⚙")
        self._settings_btn.clicked.connect(self.settingsRequested.emit)

        lay.addWidget(QLabel("IP:"))
        lay.addWidget(self._host, 1)
        if default_port:
            lay.addWidget(self._port)
        lay.addWidget(self._connect_btn)
        lay.addWidget(self._disconnect_btn)
        lay.addWidget(self._control_btn)
        lay.addWidget(self._quality_label)
        lay.addWidget(self.indicator)
        lay.addStretch(1)
        lay.addWidget(self._fullscreen_btn)
        lay.addWidget(self._settings_btn)

        self._connected = False

    # -- 公开写入口（main_window 状态机调用） ------------------------------------

    def set_connection_state(self, state: str) -> None:
        """按主状态机同步按钮态。

        Args:
            state: ``disconnected`` / ``connecting`` / ``connected`` /
                ``reconnecting``。其他值按 disconnected 处理（展示层宽容）。
        """
        ensure_gui_thread("Toolbar.set_connection_state")
        self._connected = state == "connected"
        if state == "connected":
            self._connect_btn.setText("已连")
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
            self._control_btn.setEnabled(True)
        elif state in {"connecting", "reconnecting"}:
            self._connect_btn.setText("连接中" if state == "connecting" else "重连中")
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(False)
            self._control_btn.setEnabled(False)
        else:  # disconnected / 未知
            self._connect_btn.setText("连接")
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)
            self._control_btn.setEnabled(False)
            self._control_btn.setChecked(False)
            self._host.setEnabled(True)

    def set_host_editable(self, editable: bool) -> None:
        """锁/解锁 IP 输入（连接进行中锁定）。"""
        ensure_gui_thread("Toolbar.set_host_editable")
        self._host.setEnabled(editable)

    def set_control_state(self, enabled: bool) -> None:
        """同步"启用/禁用控制"按钮文案（幂等；不触发信号）。"""
        ensure_gui_thread("Toolbar.set_control_state")
        self._control_btn.blockSignals(True)
        self._control_btn.setChecked(enabled)
        self._control_btn.setText("禁用控制" if enabled else "启用控制")
        self._control_btn.blockSignals(False)

    def set_quality(self, quality: int | None) -> None:
        """画质标签刷新（百分比或 CRF 由上层换算好再传入）。"""
        ensure_gui_thread("Toolbar.set_quality")
        self._quality_label.setText(f"画质: {quality}" if quality is not None else "画质: --")

    def add_recent_host(self, host: str) -> None:
        """连接成功后写入"最近主机"（去重置首）。"""
        ensure_gui_thread("Toolbar.add_recent_host")
        current = [self._host.itemText(i) for i in range(self._host.count())]
        if host in current:
            current.remove(host)
        current.insert(0, host)
        del current[_MAX_RECENT:]
        self._host.blockSignals(True)
        self._host.clear()
        self._host.addItems(current)
        self._host.setCurrentText(host)
        self._host.blockSignals(False)

    def current_host(self) -> str:
        """输入框当前 host（strip）。"""
        return self._host.currentText().strip()

    def current_port(self) -> int:
        """工具栏端口文本（无端口上下文时返回 0，由主窗口回落 Settings）。"""
        try:
            return int(self._port.text() or 0)
        except ValueError:
            return 0

    # -- 信号桥 -------------------------------------------------------------------

    def _emit_connect(self) -> None:
        host = self.current_host()
        if not host:
            self._host.setFocus()
            return
        self.connectRequested.emit(host, self.current_port())

    def _on_control_toggled(self, checked: bool) -> None:
        self._control_btn.setText("禁用控制" if checked else "启用控制")
        self.controlToggled.emit(checked)

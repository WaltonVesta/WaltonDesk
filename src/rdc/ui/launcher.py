"""启动菜单对话框（阶段 6.12，v4 main.py:show_gui() 迁移）。

v4：tkinter 双按钮启动窗（被控端服务 / 主控端）。v5：QDialog 同构两选项 +
"退出"；选择结果由协程化 :func:`launch_choice` 返回（qasync 下主入口 await）。

走查第十二批 Feature 17：勾选"开机自启（静默被控端）"——勾选即注册
HKCU Run 自启（``serve --tray --silent``），**不改变本次启动流程**：本次
仍按用户点的按钮正常进被控端/主控端，自启只作用于下次开机。注册失败回滚
勾选并留状态可观测（不静默）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rdc.brand import brand_title
from rdc.logging import get_logger
from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme.tokens import TOKENS

__all__ = ["LaunchOption", "LauncherDialog", "launch_choice"]

_LOG = get_logger("ui.launcher")


class LaunchOption(StrEnum):
    """启动菜单选项。"""

    HOST = "host"       # 启动被控端服务
    CLIENT = "client"   # 启动主控端
    QUIT = "quit"       # 退出


class LauncherDialog(QDialog):
    """启动选择框（三键；Enter 默认被控端——v4 主按钮序）。

    Args:
        parent: 宿主窗口。
        autostart_get: 读取当前开机自启状态（默认
            :func:`rdc.host.autostart.is_autostart_enabled`；测试注入替身）。
        autostart_set: 开启/关闭开机自启，返回是否成功（默认
            :func:`rdc.host.autostart.set_autostart`；测试注入替身）。
    """

    #: 自启注册结果（``ok=False`` 时供装配层/状态提示；Feature 17）。
    autostartChanged = Signal(bool, bool)  # (enabled, ok)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        autostart_get: Callable[[], bool] | None = None,
        autostart_set: Callable[[bool], bool] | None = None,
    ) -> None:
        ensure_gui_thread("LauncherDialog")
        super().__init__(parent)
        if autostart_get is None:
            from rdc.host.autostart import is_autostart_enabled

            autostart_get = is_autostart_enabled
        if autostart_set is None:
            from rdc.host.autostart import set_autostart

            autostart_set = set_autostart
        self._autostart_set = autostart_set
        self.setWindowTitle(brand_title())
        self.setMinimumWidth(340)
        lay = QVBoxLayout(self)
        title = QLabel("选择启动模式")
        title.setStyleSheet(f"font-size: {TOKENS.font_size + 2}px; font-weight: 600;")
        hint = QLabel("💡 被控端启动后在后台运行，可使用托盘管理")
        hint.setStyleSheet(f"color: {TOKENS.text_dim};")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addSpacing(TOKENS.spacing)

        buttons = QDialogButtonBox()
        self._host_btn = buttons.addButton("🚀 启动被控端服务", QDialogButtonBox.ButtonRole.AcceptRole)
        self._client_btn = buttons.addButton(
            "🖥️ 启动主控端", QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self._host_btn.setDefault(True)
        self._host_btn.clicked.connect(lambda: self._pick(LaunchOption.HOST))
        self._client_btn.clicked.connect(lambda: self._pick(LaunchOption.CLIENT))
        buttons.rejected.connect(lambda: self._pick(LaunchOption.QUIT))
        lay.addWidget(buttons)

        # Feature 17：开机自启勾选（静默被控端形态；注册失败回滚，不静默）
        self._autostart_check = QCheckBox("开机自动启动被控端（静默，不弹此窗口）")
        try:
            self._autostart_check.setChecked(bool(autostart_get()))
        except Exception as exc:  # noqa: BLE001 - 读取失败按未开启呈现，不阻断启动
            _LOG.warning("autostart_state_read_failed", error=repr(exc))
            self._autostart_check.setChecked(False)
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        lay.addWidget(self._autostart_check)
        lay.addWidget(hint)
        self._choice: LaunchOption | None = None

    def _on_autostart_toggled(self, enable: bool) -> None:
        """勾选即时注册/注销；失败回滚复选框并上报（约束 6：不静默吞错）。"""
        try:
            ok = bool(self._autostart_set(enable))
        except Exception as exc:  # noqa: BLE001 - 注册表写失败边界，回滚后上报
            _LOG.error("autostart_toggle_failed", enable=enable, error=repr(exc))
            ok = False
        if not ok:
            # 回滚勾选态（blockSignals 防递归触发）
            self._autostart_check.blockSignals(True)
            self._autostart_check.setChecked(not enable)
            self._autostart_check.blockSignals(False)
        self.autostartChanged.emit(enable, ok)

    def _pick(self, option: LaunchOption) -> None:
        self._choice = option
        self.accept()

    def choice(self) -> LaunchOption:
        """用户选择（done 回调未触达时兜底 quit）。"""
        return self._choice or LaunchOption.QUIT


async def launch_choice(parent: QWidget | None = None) -> LaunchOption:
    """协程化启动菜单：返回用户选项（对话框关闭即 resolve）。"""
    ensure_gui_thread("launch_choice")
    dlg = LauncherDialog(parent)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[LaunchOption] = loop.create_future()

    def _finished(_code: int) -> None:
        if not fut.done():
            fut.set_result(dlg.choice())

    dlg.finished.connect(_finished)
    dlg.show()
    return await fut

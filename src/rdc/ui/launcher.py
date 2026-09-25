"""启动菜单对话框（阶段 6.12，v4 main.py:show_gui() 迁移）。

v4：tkinter 双按钮启动窗（被控端服务 / 主控端）。v5：QDialog 同构两选项 +
"退出"；选择结果由协程化 :func:`launch_choice` 返回（qasync 下主入口 await）。
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme.tokens import TOKENS

__all__ = ["LaunchOption", "LauncherDialog", "launch_choice"]


class LaunchOption(StrEnum):
    """启动菜单选项。"""

    HOST = "host"       # 启动被控端服务
    CLIENT = "client"   # 启动主控端
    QUIT = "quit"       # 退出


class LauncherDialog(QDialog):
    """启动选择框（三键；Enter 默认被控端——v4 主按钮序）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        ensure_gui_thread("LauncherDialog")
        super().__init__(parent)
        self.setWindowTitle("远程桌面控制 v5")
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
        lay.addWidget(hint)
        self._choice: LaunchOption | None = None

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

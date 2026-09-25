"""密码输入对话框（阶段 6.9，修复 B2）。

v4 病灶（B2）：网络线程里 ``simpledialog`` 直建 Tk 子线程崩溃，v4.0.1 用
``root.after + threading.Event.wait(30)`` 缝合（网络线程阻塞 30s）。
v5：qasync 同线程下对话框 **非模态 + asyncio Future**——协程 await 用户输入，
主循环（含其他任务）继续泵送，超时用 ``asyncio.wait_for`` 原生实现。
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from rdc.ui.app import ensure_gui_thread

__all__ = ["PasswordDialog", "request_password"]


class PasswordDialog(QDialog):
    """密码输入框（回车提交；确定/取消；超时自动 reject）。"""

    def __init__(
        self,
        host: str,
        parent: QWidget | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        ensure_gui_thread("PasswordDialog")
        super().__init__(parent)
        self.setWindowTitle("认证")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"请输入被控端 {host} 的连接密码："))
        self._edit = QLineEdit()
        self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit.setPlaceholderText("密码")
        lay.addWidget(self._edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._edit.returnPressed.connect(self.accept)
        lay.addWidget(buttons)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(timeout_s * 1000))
        self._timer.timeout.connect(self.reject)
        self._timer.start()
        self._edit.setFocus()

    def password(self) -> str:
        """当前输入文本。"""
        return self._edit.text()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写
        self._timer.stop()
        super().closeEvent(event)


async def request_password(
    parent: QWidget | None,
    host: str,
    *,
    timeout_s: float = 30.0,
) -> str | None:
    """协程化密码索取（main_window 在 auth_required 事件里 await 本函数）。

    Args:
        parent: 宿主窗口。
        host: 被控端地址（提示文案）。
        timeout_s: 等待上限；超时视为取消。

    Returns:
        输入文本（可为空串=用户点了确定但没输入）；取消/超时 ``None``。
    """
    ensure_gui_thread("request_password")
    dlg = PasswordDialog(host, parent, timeout_s=timeout_s)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()

    def _finished(code: int) -> None:
        if not fut.done():
            fut.set_result(code == int(QDialog.DialogCode.Accepted))

    dlg.finished.connect(_finished)
    dlg.show()
    try:
        accepted = await asyncio.wait_for(fut, timeout=timeout_s + 1.0)  # 对话框内 timer 先动
    except TimeoutError:  # pragma: no cover - 双保险，timer 兜底
        accepted = False
    dlg.deleteLater()
    return dlg.password() if accepted else None

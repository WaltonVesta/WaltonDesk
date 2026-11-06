"""修改被控端连接口令对话框（走查第十二批 Feature 18）。

非模态 + asyncio Future（同 :mod:`rdc.ui.password_dialog` 的 B2 拆除模式），
协程 await 用户输入；强度校验走 :mod:`rdc.security.password_policy`（与
CLI ``set-password`` 单一来源）。两次输入一致且合规才返回新口令。

口令明文只在内存态短暂存在（交调用方 bcrypt 哈希后即清），绝不落日志。
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

from rdc.security.password_policy import check_password, policy_error_text
from rdc.ui.app import ensure_gui_thread

__all__ = ["ChangePasswordDialog", "request_new_password"]


class ChangePasswordDialog(QDialog):
    """新口令输入 + 二次确认 + 实时强度提示。"""

    def __init__(
        self, parent: QWidget | None = None, *, timeout_s: float = 60.0
    ) -> None:
        ensure_gui_thread("ChangePasswordDialog")
        super().__init__(parent)
        self.setWindowTitle("修改连接口令")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("设置新的被控端连接口令（≥8 位，含字母+数字）："))
        self._new = QLineEdit()
        self._new.setEchoMode(QLineEdit.EchoMode.Password)
        self._new.setPlaceholderText("新口令")
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm.setPlaceholderText("再次输入确认")
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        lay.addWidget(self._new)
        lay.addWidget(self._confirm)
        lay.addWidget(self._hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._ok = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(timeout_s * 1000))
        self._timer.timeout.connect(self.reject)
        self._timer.start()
        self._new.setFocus()

    def _on_accept(self) -> None:
        """点确定：当场校验，不合规就地提示、不关窗（避免用户白填一次）。"""
        pw = self._new.text()
        issues = check_password(pw)
        if issues:
            self._hint.setText(policy_error_text(issues))
            return
        if pw != self._confirm.text():
            self._hint.setText("两次输入不一致，请重新确认。")
            return
        self._ok = True
        self.accept()

    def password(self) -> str:
        return self._new.text() if self._ok else ""

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写
        self._timer.stop()
        super().closeEvent(event)


async def request_new_password(
    parent: QWidget | None, *, timeout_s: float = 60.0
) -> str | None:
    """协程化索取新口令。

    Returns:
        合规且两次一致的明文口令；取消/超时 ``None``。
    """
    ensure_gui_thread("request_new_password")
    dlg = ChangePasswordDialog(parent, timeout_s=timeout_s)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()

    def _finished(code: int) -> None:
        if not fut.done():
            fut.set_result(code == int(QDialog.DialogCode.Accepted))

    dlg.finished.connect(_finished)
    dlg.show()
    try:
        accepted = await asyncio.wait_for(fut, timeout=timeout_s + 1.0)
    except TimeoutError:  # pragma: no cover - 双保险，timer 兜底
        accepted = False
    text = dlg.password()
    dlg.deleteLater()
    return text if accepted and text else None

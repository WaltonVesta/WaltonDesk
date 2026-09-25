"""证书指纹 TOFU 确认对话框（阶段 6.9，B2 同款修复）。

v4：messagebox.askyesno + after 调度 + Event 阻塞（网络线程）。
v5：非模态 QDialog + asyncio Future，超时 60s 默认**拒绝**（与 v4 一致，
S1-S4 安全收紧：TOFU 无确认不信任）。
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rdc.ui.app import ensure_gui_thread

__all__ = ["FingerprintDialog", "confirm_fingerprint"]

_MSG = (
    "检测到自签名证书（首次连接或证书变更）。\n\n"
    "主机: {host}\n"
    "SHA-256 指纹:\n{fingerprint}\n\n"
    "请确认指纹与被控端显示的一致。\n"
    "确认后将加入信任列表，后续自动信任。"
)


class FingerprintDialog(QDialog):
    """指纹确认框（信任/拒绝；超时自动拒绝）。"""

    def __init__(
        self,
        host: str,
        fingerprint: str,
        parent: QWidget | None = None,
        *,
        timeout_s: float = 60.0,
    ) -> None:
        ensure_gui_thread("FingerprintDialog")
        super().__init__(parent)
        self.setWindowTitle("证书指纹确认")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(_MSG.format(host=host, fingerprint=fingerprint)))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No
        )
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("信任")
        buttons.button(QDialogButtonBox.StandardButton.No).setText("拒绝")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(timeout_s * 1000))
        self._timer.timeout.connect(self.reject)
        self._timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 覆写
        self._timer.stop()
        super().closeEvent(event)


async def confirm_fingerprint(
    parent: QWidget | None,
    host: str,
    fingerprint: str,
    *,
    timeout_s: float = 60.0,
) -> bool:
    """协程化 TOFU 确认。返回用户是否信任；超时/关闭默认 False。"""
    ensure_gui_thread("confirm_fingerprint")
    dlg = FingerprintDialog(host, fingerprint, parent, timeout_s=timeout_s)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()

    def _finished(code: int) -> None:
        if not fut.done():
            fut.set_result(code == int(QDialog.DialogCode.Accepted))

    dlg.finished.connect(_finished)
    dlg.show()
    try:
        return await asyncio.wait_for(fut, timeout=timeout_s + 1.0)
    except TimeoutError:  # pragma: no cover - 双保险
        return False
    finally:
        dlg.deleteLater()

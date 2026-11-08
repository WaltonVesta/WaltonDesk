"""首启初始密码弹窗（走查第十四批 Bug 3：可见性保底通道）。

病灶：首启明文此前只有控制台 print 与托盘气泡两条通道——``--silent``
（开机自启）跳过横幅打印，``--windowed`` 下 ``console_stream`` 无控制台
丢弃 print，气泡又常被通知中心折叠吞掉；GUI 模式用户三处都看不到密码，
只剩日志里一条 ``initial_password_generated`` info，无法登录。

本模块补第三条**保底通道：非模态显眼弹窗**（大字号明文可选中复制 +
"不再提示"勾选）。非阻塞设计（同 :mod:`rdc.ui.change_password_dialog`
的 B2 拆除模式）：窗自行持有引用直到关闭，``host_session`` 投后继续跑
事件循环——被控端必须保持服务中，绝不能用模态窗卡死协程。

零 Qt 红线不受影响（本模块在 ui 层，controller/server 不导入）。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rdc.ui.app import ensure_gui_thread

__all__ = ["InitialPasswordDialog", "present_initial_password_dialog"]


class InitialPasswordDialog(QDialog):
    """初始密码展示窗：明文大字可选 + 复制按钮 + 不再提示 + 记下并关闭。"""

    def __init__(self, password: str, parent: QWidget | None = None) -> None:
        ensure_gui_thread("InitialPasswordDialog")
        super().__init__(parent)
        self._password = password
        self.suppress_requested = False
        self.setWindowTitle("远程桌面控制 v5 — 初始密码")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)

        title = QLabel("首次启动，已为你生成随机初始密码：")
        lay.addWidget(title)

        self.value_label = QLabel(password)
        f = QFont()
        f.setPointSize(20)
        f.setBold(True)
        self.value_label.setFont(f)
        self.value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lay.addWidget(self.value_label)

        hint = QLabel(
            "请记下此密码，主控端连接时需要输入。\n"
            "这是系统自动生成的初始密码，建议在托盘菜单中修改为您自己的密码。\n"
            "勾选\u201c不再提示\u201d可关闭此通知。"
        )
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("复制密码")
        self.copy_btn.clicked.connect(self._copy)
        row.addWidget(self.copy_btn)
        self.suppress_check = QCheckBox("不再提示")
        row.addWidget(self.suppress_check)
        row.addStretch(1)
        self.ok_btn = QPushButton("记下并关闭")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._accept)
        row.addWidget(self.ok_btn)
        lay.addLayout(row)

    def _copy(self) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.clipboard().setText(self._password)

    def _accept(self) -> None:
        self.suppress_requested = self.suppress_check.isChecked()
        self.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 覆写
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._accept()
            return
        super().keyPressEvent(event)


def present_initial_password_dialog(
    password: str, on_suppress: Callable[[], None] | None = None
) -> InitialPasswordDialog:
    """非阻塞弹出首启密码窗（host_session 保底通道；立即返回窗引用）。

    窗引用挂 QApplication 上保活（防 GC 提前销毁 C++ 对象），关闭时解除；
    ``on_suppress`` 在用户勾选"不再提示"并关闭时被调用（持久化归调用方）。
    """
    ensure_gui_thread("present_initial_password_dialog")
    app = QApplication.instance()
    dlg = InitialPasswordDialog(password)
    holder: list[InitialPasswordDialog] = [dlg]
    if app is not None:
        app.setProperty("_rdc_initial_password_dlg", holder)

    def _closed() -> None:
        holder.remove(dlg)
        if dlg.suppress_requested and on_suppress is not None:
            on_suppress()

    dlg.finished.connect(_closed)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg

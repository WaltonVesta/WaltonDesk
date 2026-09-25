"""剪贴板同步（阶段 6.11，v4 gui/clipboard.py 迁移）。

QClipboard + ``QTimer`` 轮询（v4 是 ``root.after(100)``，语义等价）+ 变更去抖：
- 本地内容哈希与"上次已知值"比对，仅真实变更才外发；
- 远端写入路径（``apply_remote``）先置"抑制标记"再 setText——防止回环
  （远端写本地 → 本地检测到"变更" → 再发回远端 → …）。

v4 病灶不迁移：其 ``_handle_paste_result`` 用 future 回调 + root.after 双层
调度；v5 同线程结构下全部退化为同步代码。
"""

from __future__ import annotations

import hashlib

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from rdc.ui.app import ensure_gui_thread

__all__ = ["ClipboardSync"]


class ClipboardSync(QObject):
    """双向剪贴板同步器（仅文本；图片/文件归阶段 8 文件传输）。

    Signals:
        localChanged(str): 本地剪贴板出现新文本（去抖后）→ controller 发远端。

    Args:
        interval_ms: 轮询周期（v4 同值 100ms）。
        parent: QObject 父件。
    """

    localChanged = Signal(str)

    def __init__(self, *, interval_ms: int = 100, parent: QObject | None = None) -> None:
        ensure_gui_thread("ClipboardSync")
        super().__init__(parent)
        app = QApplication.instance()
        if not isinstance(app, QApplication):  # pragma: no cover - 构造前置条件
            raise RuntimeError("ClipboardSync 需要已创建 QApplication")
        self._clip = app.clipboard()
        self._enabled = True
        self._suppress = False  # apply_remote 回写抑制标记
        self._last_hash = self._hash(self._clip.text())
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll_once)

    # -- 生命周期 -----------------------------------------------------------------

    def start(self) -> None:
        """开始轮询（幂等）。"""
        ensure_gui_thread("ClipboardSync.start")
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """停止轮询（幂等；禁用同步亦经此）。"""
        ensure_gui_thread("ClipboardSync.stop")
        self._timer.stop()

    def set_enabled(self, enabled: bool) -> None:
        """开关同步（``enable_clipboard`` 设置项映射；关闭即停轮询）。"""
        ensure_gui_thread("ClipboardSync.set_enabled")
        self._enabled = enabled
        if enabled:
            self.start()
        else:
            self.stop()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- 远端 → 本地 ----------------------------------------------------------------

    def apply_remote(self, text: str) -> None:
        """写入远端传来的文本并更新基线（防回环：本帧及后续同文不触发外发）。"""
        ensure_gui_thread("ClipboardSync.apply_remote")
        self._suppress = True
        try:
            self._clip.setText(text)
        finally:
            self._suppress = False
        self._last_hash = self._hash(text)

    # -- 轮询 ---------------------------------------------------------------------

    def _poll_once(self) -> None:
        ensure_gui_thread("ClipboardSync._poll_once")
        if self._suppress or not self._enabled:
            return
        current = self._clip.text()
        h = self._hash(current)
        if h != self._last_hash:
            self._last_hash = h
            self.localChanged.emit(current)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()

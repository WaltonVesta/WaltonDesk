"""客户端状态栏（阶段 6.4，v4 gui/status_bar.py 迁移）。

v5 差异：不轮询、不持 controller——**纯 setter 注入**（main_window 把
Signal 接到这里）；FPS 统计内聚（收到帧计数 + QTimer 每秒刷新，全在 GUI
线程，合法）。状态色走 tokens 语义色。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme.tokens import TOKENS

__all__ = ["StatusBar"]

#: 状态类型 → 前景色（v4 七种状态全保留）
_STATUS_COLORS: dict[str, str] = {
    "info": TOKENS.text_dim,
    "connected": TOKENS.ok,
    "disconnected": TOKENS.text_dim,
    "connecting": TOKENS.warn,
    "error": TOKENS.error,
    "control": TOKENS.ok,
    "reconnecting": TOKENS.warn,
}


class StatusBar(QFrame):
    """底部状态条：状态 | 信息提示 |（右起）分辨率 画质 延迟 FPS。"""

    def __init__(self, parent: QFrame | None = None) -> None:
        ensure_gui_thread("StatusBar")
        super().__init__(parent)
        self.setObjectName("StatusBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(TOKENS.spacing, 2, TOKENS.spacing, 2)
        lay.setSpacing(TOKENS.spacing)

        self._status = QLabel("状态: 未连接")
        self._status.setStyleSheet(f"color: {_STATUS_COLORS['info']};")
        self._info = QLabel("请输入IP地址")
        self._info.setStyleSheet(f"color: {TOKENS.text_dim};")
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: {TOKENS.border};")
        self._resolution = QLabel("")
        self._quality = QLabel("")
        self._latency = QLabel("")
        self._fps = QLabel("")
        for lb in (self._resolution, self._quality, self._latency, self._fps):
            lb.setStyleSheet(f"color: {TOKENS.text_dim};")

        lay.addWidget(self._status)
        lay.addWidget(sep)
        lay.addWidget(self._info, 1)
        lay.addWidget(self._resolution)
        lay.addWidget(self._quality)
        lay.addWidget(self._latency)
        lay.addWidget(self._fps)

        # FPS 内聚统计（v4 语义）：每秒聚合一次
        self._frame_count = 0
        self._fps_start = time.monotonic()
        self._fps_value = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_fps)
        self._timer.start()

    # -- 公开写入口（全部守卫） -------------------------------------------------

    def set_status(self, text: str, status_type: str = "info") -> None:
        """状态文本 + 语义色（未知类型回落 info，不抛错——展示层宽容）。"""
        ensure_gui_thread("StatusBar.set_status")
        color = _STATUS_COLORS.get(status_type, _STATUS_COLORS["info"])
        self._status.setText(f"状态: {text}")
        self._status.setStyleSheet(f"color: {color};")

    def set_info(self, text: str) -> None:
        """右侧提示区文本。"""
        ensure_gui_thread("StatusBar.set_info")
        self._info.setText(text)

    def notify_frame(self) -> None:
        """收到一帧（client controller 的 frameReceived Signal 槽）。"""
        ensure_gui_thread("StatusBar.notify_frame")
        self._frame_count += 1

    def set_latency(self, latency_ms: float | None) -> None:
        """网络延迟；``None``/0 清除显示。"""
        ensure_gui_thread("StatusBar.set_latency")
        if latency_ms and latency_ms > 0:
            self._latency.setText(f"延迟: {latency_ms:.0f}ms")
            color = (
                TOKENS.ok if latency_ms < 100 else TOKENS.warn if latency_ms < 300 else TOKENS.error
            )
            self._latency.setStyleSheet(f"color: {color};")
        else:
            self._latency.setText("")

    def set_quality(self, quality: int | None) -> None:
        """画质百分比；``None`` 清除。"""
        ensure_gui_thread("StatusBar.set_quality")
        self._quality.setText(f"画质: {quality}%" if quality is not None else "")

    def set_resolution(self, resolution: tuple[int, int] | None) -> None:
        """远端分辨率（第五批走查：状态栏可见）；``None`` 清除。"""
        ensure_gui_thread("StatusBar.set_resolution")
        if resolution is None:
            self._resolution.setText("")
        else:
            self._resolution.setText(f"分辨率: {resolution[0]}x{resolution[1]}")

    def stop(self) -> None:
        """停 FPS 定时器（close 级联调用；幂等）。"""
        ensure_gui_thread("StatusBar.stop")
        self._timer.stop()

    # -- 内部 -------------------------------------------------------------------

    def _tick_fps(self) -> None:
        ensure_gui_thread("StatusBar._tick_fps")
        now = time.monotonic()
        span = now - self._fps_start
        if span < 1.0:
            return
        if self._frame_count > 0:
            self._fps_value = self._frame_count / span
            self._fps.setText(f"{self._fps_value:.1f} FPS")
        else:
            # 第五批走查：画面静止（idle 档降载）时 0.0 FPS 观感差，改显 idle
            self._fps_value = 0.0
            self._fps.setText("idle")
        self._frame_count = 0
        self._fps_start = now

    @property
    def measured_fps(self) -> float:
        """最近实测 FPS（诊断/测试）。"""
        return self._fps_value

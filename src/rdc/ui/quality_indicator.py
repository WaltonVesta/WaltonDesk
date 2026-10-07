"""连接质量指示灯（阶段 6.5，v4 gui/quality_indicator.py 迁移）。

v5 差异：tk.Canvas 画点 → **QPainter 自绘 + QPropertyAnimation 脉冲**
（计划 6.5 原要求）；色值全部走 tokens；等级判定阈值与 v4 对齐（用户可见
行为不突变）。无 controller 依赖：main_window 把 statsUpdated 信号接入。
"""

from __future__ import annotations

from PySide6.QtCore import Property, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme.tokens import TOKENS

__all__ = ["QualityIndicator"]

#: 等级 → (标签, 颜色)。阈值见 :meth:`QualityIndicator.update_quality`。
_GOOD = ("优秀", TOKENS.ok)
_FINE = ("良好", TOKENS.ok)
_MEDIUM = ("一般", TOKENS.warn)
_POOR = ("较差", TOKENS.error)
_BAD = ("极差", TOKENS.error)
_OFFLINE = ("已断开", TOKENS.text_dim)
_WAIT = ("等待连接...", TOKENS.text_dim)


class QualityIndicator(QWidget):
    """圆点指示灯（连接时脉冲）+ 等级标签 + 详情（延迟|帧率）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        ensure_gui_thread("QualityIndicator")
        super().__init__(parent)
        self.setFixedSize(160, 22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)

        self._dot = _DotPulse(self)  # 自绘圆点（含脉冲动画属性）
        self._label = QLabel(_WAIT[0])
        self._label.setStyleSheet(f"color: {_WAIT[1]};")
        self._detail = QLabel("")
        self._detail.setStyleSheet(f"color: {TOKENS.text_dim};")
        lay.addWidget(self._dot)
        lay.addWidget(self._label)
        lay.addWidget(self._detail)
        self._quality = "unknown"

    def update_quality(
        self,
        latency: float,
        fps: float,
        packet_loss: float = 0.0,
        is_connected: bool = True,
    ) -> None:
        """按指标刷新等级与灯色（阈值与 v4 完全一致）。"""
        ensure_gui_thread("QualityIndicator.update_quality")
        if not is_connected:
            self._apply(_OFFLINE, "offline", pulse=False)
            self._detail.setText("")
            return
        if fps <= 0:
            # 第五批走查：画面静止时 fps=0 是 idle 降载（framerate 控制器
            # 语义），不代表链路差——等级只看延迟/丢包，详情显示 idle
            if latency < 100 and packet_loss < 0.03:
                grade, txt = _FINE, "good"
            elif latency < 200 and packet_loss < 0.08:
                grade, txt = _MEDIUM, "medium"
            elif latency < 400 and packet_loss < 0.15:
                grade, txt = _POOR, "poor"
            else:
                grade, txt = _BAD, "poor"
            self._apply(grade, txt, pulse=True)
            self._detail.setText(f"{latency:.0f}ms | idle")
            return
        if latency < 50 and packet_loss < 0.01 and fps > 20:
            grade, txt = _GOOD, "good"
        elif latency < 100 and packet_loss < 0.03 and fps > 15:
            grade, txt = _FINE, "good"
        elif latency < 200 and packet_loss < 0.08 and fps > 10:
            grade, txt = _MEDIUM, "medium"
        elif latency < 400 and packet_loss < 0.15:
            grade, txt = _POOR, "poor"
        else:
            grade, txt = _BAD, "poor"
        self._apply(grade, txt, pulse=True)
        self._detail.setText(f"{latency:.0f}ms | {fps:.1f}fps")

    def set_offline(self) -> None:
        """显式置离线（断开信号直达，不经指标推算）。"""
        ensure_gui_thread("QualityIndicator.set_offline")
        self._apply(_OFFLINE, "offline", pulse=False)
        self._detail.setText("")

    def get_quality(self) -> str:
        """当前等级（unknown/good/medium/poor/offline）。"""
        return self._quality

    # -- 内部 ---------------------------------------------------------------------

    def _apply(self, grade: tuple[str, str], quality: str, *, pulse: bool) -> None:
        text, color = grade
        self._quality = quality
        self._label.setText(text)
        self._label.setStyleSheet(f"color: {color};")
        self._dot.set_color(color, pulse=pulse)


class _DotPulse(QWidget):
    """14px 圆点：QPainter 自绘；pulse 开启时外环 300ms 扩散淡出循环。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor(TOKENS.text_dim)
        self._pulse_phase = 0.0  # 0→1 动画属性
        self._pulse_on = False
        self._anim = QPropertyAnimation(self, b"pulsePhase", self)
        self._anim.setDuration(300)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._timer = QTimer(self)
        self._timer.setInterval(600)  # 脉冲周期：300ms 动 + 300ms 静（v4 节奏）
        self._timer.timeout.connect(self._kick)

    def get_pulse_phase(self) -> float:
        """动画属性读端（Qt property system 要求命名成对）。"""
        return self._pulse_phase

    def set_pulse_phase(self, v: float) -> None:
        """动画属性写端：值变化即重绘。"""
        self._pulse_phase = v
        self.update()

    pulsePhase = Property(float, get_pulse_phase, set_pulse_phase)  # noqa: N815 - Qt 属性名须与动画字节名一致

    def set_color(self, css_color: str, *, pulse: bool) -> None:
        self._color = QColor(css_color)
        self._pulse_on = pulse
        if pulse:
            if not self._timer.isActive():
                self._timer.start()
                self._kick()
        else:
            self._timer.stop()
            self._anim.stop()
            self._pulse_phase = 0.0
        self.update()

    def _kick(self) -> None:
        if self._pulse_on:
            self._anim.stop()
            self._anim.setStartValue(0.0)
            self._anim.start()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802, ARG002 - Qt 覆写签名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QRectF(3, 3, 8, 8))
        if self._pulse_on and self._pulse_phase > 0.0:
            grow = 8.0 + 5.0 * self._pulse_phase
            alpha = int(150 * (1.0 - self._pulse_phase))
            ring = QColor(self._color)
            ring.setAlpha(max(0, alpha))
            p.setBrush(ring)
            off = (14 - grow) / 2
            p.drawEllipse(QRectF(off, off, grow, grow))
        p.end()

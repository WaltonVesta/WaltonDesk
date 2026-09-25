"""客户端性能监控面板（阶段 6.4，v4 gui/performance_panel.py 迁移）。

v5 差异：不轮询 controller——main_window 把 frameReceived/statsUpdated 信号
接到这里（纯 setter）；带宽使用率用 QProgressBar；色值走 tokens。
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar

from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme import TOKENS

__all__ = ["PerformancePanel"]


class PerformancePanel(QFrame):
    """两列网格：FPS/延迟/带宽/画质/分辨率/总帧数 + 带宽使用率进度条。"""

    def __init__(self, parent: QFrame | None = None, *, bandwidth_limit_kbps: float = 0.0) -> None:
        """
        Args:
            parent: 父控件。
            bandwidth_limit_kbps: 带宽上限（KB/s），用于使用率进度条；
                ``0`` 表示未设置（进度条隐藏百分比语义，显示 "--"）。
        """
        ensure_gui_thread("PerformancePanel")
        super().__init__(parent)
        self._limit = bandwidth_limit_kbps
        grid = QGridLayout(self)
        grid.setContentsMargins(TOKENS.spacing, TOKENS.spacing, TOKENS.spacing, TOKENS.spacing)

        def _cell(row: int, col: int, text: str) -> QLabel:
            lb = QLabel(text)
            lb.setStyleSheet(f"color: {TOKENS.text};")
            grid.addWidget(lb, row, col)
            return lb

        self._fps = _cell(0, 0, "FPS: --")
        self._latency = _cell(0, 1, "延迟: --ms")
        self._bandwidth = _cell(1, 0, "带宽: --KB/s")
        self._quality = _cell(1, 1, "画质: --")
        self._resolution = _cell(2, 0, "分辨率: --x--")
        self._frames = _cell(2, 1, "总帧数: --")
        self._usage = QProgressBar()
        self._usage.setRange(0, 100)
        self._usage.setValue(0)
        self._usage.setFormat("带宽使用率: --%")
        grid.addWidget(self._usage, 3, 0, 1, 2)

    # -- 公开写入口 ---------------------------------------------------------------

    def update_metrics(
        self,
        *,
        fps: float | None = None,
        latency_ms: float | None = None,
        bandwidth_kbps: float | None = None,
        quality: int | None = None,
        resolution: tuple[int, int] | None = None,
        total_frames: int | None = None,
    ) -> None:
        """批量更新指标（``None`` 项保持原显示；与 v4 "--" 占位语义一致）。"""
        ensure_gui_thread("PerformancePanel.update_metrics")
        if fps is not None:
            self._fps.setText(f"FPS: {fps:.1f}" if fps else "FPS: --")
        if latency_ms is not None:
            self._latency.setText(f"延迟: {latency_ms:.0f}ms" if latency_ms else "延迟: --ms")
        if bandwidth_kbps is not None:
            self._bandwidth.setText(
                f"带宽: {bandwidth_kbps:.1f}KB/s" if bandwidth_kbps else "带宽: --KB/s"
            )
            self._update_usage(bandwidth_kbps)
        if quality is not None:
            self._quality.setText(f"画质: {quality}" if quality else "画质: --")
        if resolution is not None:
            self._resolution.setText(f"分辨率: {resolution[0]}x{resolution[1]}")
        if total_frames is not None:
            self._frames.setText(f"总帧数: {total_frames}" if total_frames else "总帧数: --")

    def set_bandwidth_limit(self, kbps: float) -> None:
        """设置带宽上限（0=清除使用率）。"""
        ensure_gui_thread("PerformancePanel.set_bandwidth_limit")
        self._limit = kbps

    # -- 内部 ---------------------------------------------------------------------

    def _update_usage(self, kbps: float) -> None:
        if self._limit <= 0:
            self._usage.setFormat("带宽使用率: --%")
            return
        pct = min(100, int(100 * kbps / self._limit))
        self._usage.setValue(pct)
        self._usage.setFormat(f"带宽使用率: {pct}%")

"""被控端状态面板（阶段 6.4，v4 gui/status_panel.py 迁移）。

v5 差异：不 import v4 service、不 ``after()`` 自轮询——数据源抽象为
:class:`HostStatsProvider` Protocol（QTimer 每秒拉一次快照，全在 GUI 线程）。
快照字段对齐 v5 真实统计件：``BroadcasterStats`` / ``PipelineStats``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from rdc.ui.app import ensure_gui_thread
from rdc.ui.theme import TOKENS

__all__ = ["HostSnapshot", "HostStatsProvider", "StatusPanel"]


@dataclass(frozen=True)
class HostSnapshot:
    """被控端一次状态快照（provider 返回；不可变值对象）。

    Attributes:
        running: 服务运行中。
        paused: 服务暂停。
        active_connections: 活动连接数。
        subscriber_count: 画面广播订阅者数。
        bytes_sent_total: 累计发送字节。
        uptime_s: 运行秒数。
        encoder_quality: 当前 CRF（None=编码器未就绪）。
        encoder_avg_kb: 平均帧大小（KB）。
        frames_encoded: 累计编码帧数。
    """

    running: bool = False
    paused: bool = False
    active_connections: int = 0
    subscriber_count: int = 0
    bytes_sent_total: int = 0
    uptime_s: float = 0.0
    encoder_quality: int | None = None
    encoder_avg_kb: float = 0.0
    frames_encoded: int = 0


@runtime_checkable
class HostStatsProvider(Protocol):
    """被控端统计源（service/pipeline 适配实现；测试用替身）。"""

    def snapshot(self) -> HostSnapshot:
        """返回当前快照（轻量纯读取，GUI 线程每秒调用一次）。"""
        ...


class StatusPanel(QFrame):
    """被控端监控：状态/连接数/带宽/运行时长/编码统计/订阅者。"""

    def __init__(self, provider: HostStatsProvider, parent: QFrame | None = None) -> None:
        ensure_gui_thread("StatusPanel")
        super().__init__(parent)
        self._provider = provider
        lay = QVBoxLayout(self)
        lay.setContentsMargins(TOKENS.spacing_lg, TOKENS.spacing, TOKENS.spacing_lg, TOKENS.spacing)

        def _row(text: str) -> QLabel:
            lb = QLabel(text)
            lb.setStyleSheet(f"color: {TOKENS.text};")
            lay.addWidget(lb)
            return lb

        self._status = _row("状态: --")
        self._connections = _row("连接数: 0")
        self._bandwidth = _row("带宽: 0 KB/s")
        self._uptime = _row("运行时间: --")
        self._quality = _row("编码质量: --")
        self._subscribers = _row("订阅者: 0")

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        """拉一次快照并渲染（公开：测试与外部即时刷新可直调）。"""
        ensure_gui_thread("StatusPanel.refresh")
        s = self._provider.snapshot()
        if s.running and not s.paused:
            self._status.setText("状态: 运行中")
            self._status.setStyleSheet(f"color: {TOKENS.ok};")
        elif s.paused:
            self._status.setText("状态: 已暂停")
            self._status.setStyleSheet(f"color: {TOKENS.warn};")
        else:
            self._status.setText("状态: 已停止")
            self._status.setStyleSheet(f"color: {TOKENS.error};")
        self._connections.setText(f"连接数: {s.active_connections}")
        if s.uptime_s > 0:
            kbps = (s.bytes_sent_total / s.uptime_s) / 1024
            self._bandwidth.setText(f"带宽: {kbps:.1f} KB/s")
            h, m, sec = int(s.uptime_s // 3600), int(s.uptime_s % 3600 // 60), int(s.uptime_s % 60)
            self._uptime.setText(f"运行时间: {h:02d}:{m:02d}:{sec:02d}")
        if s.encoder_quality is not None:
            self._quality.setText(
                f"编码质量: CRF {s.encoder_quality} | 帧均 {s.encoder_avg_kb:.0f}KB | "
                f"总帧 {s.frames_encoded}"
            )
        self._subscribers.setText(f"订阅者: {s.subscriber_count}")

    def stop(self) -> None:
        """停轮询定时器（幂等级联自 close）。"""
        ensure_gui_thread("StatusPanel.stop")
        self._timer.stop()

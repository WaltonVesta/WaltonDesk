"""被控端统计值对象（阶段 9 批 3：自 ``ui.status_panel`` 下沉）。

服务核心（``rdc serve`` 无 GUI 场景）零 Qt 依赖红线：快照值对象归 server 层，
``rdc.ui.status_panel`` 继续 re-export 同名符号（阶段 6 的 UI 调用面零改动）。
"""

from __future__ import annotations

from dataclasses import dataclass


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

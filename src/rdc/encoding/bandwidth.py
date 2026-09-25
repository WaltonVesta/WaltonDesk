"""带宽探测器（从 :mod:`rdc.encoding.jpeg` 迁出为独立模块）。

阶段 5 任务 9。v4 的 ``BandwidthProbe`` 内嵌在 ``encoding/jpeg.py``，与 JPEG
编码器无概念耦合（任何"发送侧计数"都能喂进来），迁出后：

1. H.264 主路径与 v4 兼容路径共用同一探测器（``server/service.py`` 的
   ``from rdc.encoding.jpeg import BandwidthProbe`` 兼容面由 jpeg.py 的
   re-export 保证，阶段 9 一并删除）；
2. **计时源改为** ``time.monotonic()``：v4 用 ``time.time()``，系统时钟回拨
   （NTP 校正/休眠唤醒）会算出负的窗口时长或虚假峰值带宽。
3. 补全类型注解与文档（移出 mypy 宽松清单的前提）。

语义：以"发送侧实际写入字节"为样本，每 :data:`WINDOW_SEC` 秒折算一次瞬时带宽
（字节/秒），保留最近 :data:`SAMPLE_CAP` 个窗口取算术平均作为估算值。这是
**估算**而非测量：空闲期无样本则估算保持上次值，长空闲后建议上层
:meth:`BandwidthProbe.reset`。
"""

from __future__ import annotations

import time
from collections import deque

__all__ = [
    "BandwidthProbe",
    "SAMPLE_CAP",
    "WINDOW_SEC",
]

#: 带宽估算窗口长度（秒）。1s 与 v4 行为一致，也对 30fps 有足够样本数。
WINDOW_SEC = 1.0

#: 参与平均的历史窗口数上限（10 窗口 = 最长回看 10s）。
SAMPLE_CAP = 10


class BandwidthProbe:
    """发送侧带宽估算器（单调时钟、滑动窗口平均）。

    示例:
        >>> probe = BandwidthProbe()
        >>> probe.record_send(1024)           # doctest: +ELLIPSIS
        >>> probe.get_bandwidth() >= 0
        True
    """

    def __init__(self, *, window_sec: float = WINDOW_SEC, sample_cap: int = SAMPLE_CAP) -> None:
        """构造探测器。

        Args:
            window_sec: 折算窗口长度（秒），须 >0。
            sample_cap: 平均所用的历史窗口数，须 ≥1。

        Raises:
            ValueError: 参数越界。
        """
        if window_sec <= 0:
            raise ValueError(f"window_sec 必须 >0，实得 {window_sec}")
        if sample_cap < 1:
            raise ValueError(f"sample_cap 必须 ≥1，实得 {sample_cap}")
        self._window_sec = window_sec
        self._samples: deque[float] = deque(maxlen=sample_cap)
        self._window_start: float | None = None
        self._window_bytes = 0
        self._estimated: float = 0.0

    def record_send(self, bytes_sent: int) -> None:
        """记录一次成功发送的字节数（传输层写完成后调用）。

        Args:
            bytes_sent: 本次写入的字节数，须 ≥0；0 视为无效样本直接忽略。
        """
        if bytes_sent <= 0:
            return
        now = time.monotonic()
        if self._window_start is None:
            self._window_start = now
        self._window_bytes += bytes_sent

        elapsed = now - self._window_start
        if elapsed >= self._window_sec:
            # 窗口折算：用实际经过时长而非名义窗口，避免调度抖动引入系统性偏差
            self._samples.append(self._window_bytes / elapsed)
            self._estimated = sum(self._samples) / len(self._samples)
            self._window_bytes = 0
            self._window_start = now

    def get_bandwidth(self) -> float:
        """估算带宽（**字节**/秒）。尚无完整窗口时返回 0。"""
        return self._estimated

    def reset(self) -> None:
        """清空全部窗口与样本（长空闲/重连后调用，防止陈旧估算误导 rater）。"""
        self._samples.clear()
        self._window_start = None
        self._window_bytes = 0
        self._estimated = 0.0

    @property
    def window_count(self) -> int:
        """已累积的完整窗口样本数（诊断用）。"""
        return len(self._samples)

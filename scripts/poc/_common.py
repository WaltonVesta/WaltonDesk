"""阶段 6 桥接 PoC 公共度量工具（方案甲 poc_qasync / 方案乙 poc_threadloop 共用）。

判据口径（用户 2026-09-25 确认）：
- QTimer 1ms 精度：Windows 系统时钟粒度实测约 15.6ms，故判据为"中位 ≤16ms 且
  竞争负载下抖动增幅 ≤50%"，而非物理上不可能的 1ms 稳定。
- 异常传播：直接 await 可见 / fire-and-forget 未被吞（stderr 有痕迹）/
  跨线程 future 异常在 Qt 侧 re-raise。
- 混合任务：Qt 定时器 tick → 协程 → Qt 回调闭环，往返计数与时延。
- Ctrl+C：进程收到信号后 ≤3s 干净退出（CLEAN_EXIT 标记 + 退出码 0 + 无线程残留）。
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer

INTERVAL_MS = 1
BENCH_SECONDS = 2.0
BURN_TASKS = 4


def emit(tag: str, payload: dict[str, Any]) -> None:
    """输出一行机器可读度量：``TAG {json}``。"""
    print(f"{tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)


class TimerProbe(QObject):
    """QTimer(PreciseTimer) 采样器：记录相邻 tick 的实测间隔（毫秒）。"""

    def __init__(self, interval_ms: int = INTERVAL_MS) -> None:
        super().__init__()
        self.samples: list[float] = []
        self._last: float | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._last = None
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        now = time.perf_counter()
        if self._last is not None:
            self.samples.append((now - self._last) * 1000.0)
        self._last = now

    def metrics(self) -> dict[str, Any]:
        # 丢弃前 5 个样本：启动抖动（事件循环冷启动）不代表稳态
        deltas = self.samples[5:]
        if not deltas:
            return {"count": 0}
        ordered = sorted(deltas)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return {
            "count": len(deltas),
            "median_ms": round(statistics.median(deltas), 3),
            "p95_ms": round(p95, 3),
            "max_ms": round(max(deltas), 3),
            "lost_ms": round(sum(deltas) - len(deltas) * INTERVAL_MS, 1),
        }


async def burn(seconds: float) -> None:
    """GIL 竞争源：纯 Python 忙等，模拟 asyncio 侧 CPU 任务对 Qt 线程的挤占。"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        sum(range(1000))
        await asyncio.sleep(0)  # 让出循环但不释放 GIL 长时间片


async def contention_bench(stop_after: float, tasks: int = BURN_TASKS) -> None:
    """在 asyncio 侧制造持续 CPU 负载（与 Qt 定时器并行）。"""
    burns = [asyncio.ensure_future(burn(stop_after)) for _ in range(tasks)]
    await asyncio.gather(*burns)


def report_threads_left() -> int:
    """退出前统计非 daemon 线程残留（Qt 主线程自身除外）。"""
    live = [t for t in threading_live_non_main()]
    return len(live)


def threading_live_non_main() -> list[str]:
    import threading

    me = threading.current_thread()
    return [t.name for t in threading.enumerate() if t is not me and t.is_alive()]


def excepthook_note() -> None:
    """确保未捕获协程异常会打到 stderr（默认 handler 即如此；此处显式声明口径）。"""
    pass  # noqa: B016 - 占位：两方案都依赖默认 asyncio exception handler


__all__ = [
    "BENCH_SECONDS",
    "INTERVAL_MS",
    "TimerProbe",
    "asyncio",
    "contention_bench",
    "emit",
    "report_threads_left",
    "sys",
    "time",
]

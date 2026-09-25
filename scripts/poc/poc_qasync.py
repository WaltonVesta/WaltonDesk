"""方案甲 PoC：qasync —— Qt 事件循环原生实现 asyncio 事件循环（同线程）。

模式（由 run_poc.py 以子进程驱动）：
- timer   裸 Qt 定时器精度（两方案的公共基线）
- mixed   Qt 定时器 tick → asyncio 协程 → Qt 槽回调闭环 + 协程 CPU 负载下的定时器抖动
- exc     asyncio 异常传播三口径
- live    Ctrl+C 干净退出（外部信号，见 run_poc）
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    BENCH_SECONDS,
    INTERVAL_MS,
    TimerProbe,
    contention_bench,
    emit,
    report_threads_left,
    time,
)


def mode_timer() -> int:
    """基线：无 asyncio 负载时 QTimer(1ms) 精度。"""
    app = QApplication([])
    loop = QEventLoop(app)
    probe = TimerProbe(INTERVAL_MS)
    probe.start()

    async def _run() -> None:
        await asyncio.sleep(BENCH_SECONDS)

    asyncio.run(_run(), loop_factory=lambda: loop)
    probe.stop()
    emit("TIMER", probe.metrics())
    return 0


class _MixedHarness:
    """QTimer → 协程 → Qt 槽 闭环计数器。"""

    def __init__(self) -> None:
        self.rtts: list[float] = []
        self.rounds = 0

    def on_tick(self) -> None:
        # Qt → asyncio：tick 直接创建协程任务（同线程，无跨线程投递）
        asyncio.ensure_future(self._coro(time.perf_counter()))

    async def _coro(self, t0: float) -> None:
        await asyncio.sleep(0.001)
        # asyncio → Qt：回到"Qt 侧"记录（同线程，直接调用即合法）
        self.rtts.append((time.perf_counter() - t0) * 1000.0)
        self.rounds += 1


def mode_mixed() -> int:
    app = QApplication([])
    loop = QEventLoop(app)
    h = _MixedHarness()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(50)
    timer.timeout.connect(h.on_tick)
    timer.start()

    async def _run() -> None:
        await contention_bench(BENCH_SECONDS)  # 协程侧 CPU 负载，测定时器被挤占程度
        await asyncio.sleep(0.3)

    probe = TimerProbe(INTERVAL_MS)
    probe.start()
    asyncio.run(_run(), loop_factory=lambda: loop)
    timer.stop()
    probe.stop()
    rtts = sorted(h.rtts)
    emit(
        "MIXED",
        {
            "rounds": h.rounds,
            "rtt_median_ms": round(rtts[len(rtts) // 2], 2) if rtts else None,
            "timer_under_contention": probe.metrics(),
        },
    )
    return 0


def mode_exc() -> int:
    app = QApplication([])
    loop = QEventLoop(app)
    result: dict[str, object] = {}

    async def _boom_direct() -> None:
        raise ValueError("EXC-DIRECT-SENTINEL")

    async def _boom_unhandled() -> None:
        raise RuntimeError("EXC-UNHANDLED-SENTINEL")

    async def _main() -> None:
        loop2 = asyncio.get_running_loop()
        seen: list[str] = []
        orig = loop2.get_exception_handler()

        def _handler(_loop: object, context: dict) -> None:
            exc = context.get("exception")
            seen.append(f"{context.get('message', '?')}|{type(exc).__name__ if exc else '-'}:{exc}")
            if orig:
                orig(_loop, context)  # 保留默认 stderr 输出

        loop2.set_exception_handler(_handler)
        # ① 直接 await：异常可见
        try:
            await _boom_direct()
        except ValueError as e:
            result["direct"] = str(e)
        # ② fire-and-forget：不调 exception()，看 handler 是否收到（未被吞）
        asyncio.ensure_future(_boom_unhandled())
        await asyncio.sleep(0.3)
        import gc

        gc.collect()
        result["unhandled_to_handler"] = [m for m in seen if "EXC-UNHANDLED" in m]
        # ③ 同线程 future 完成
        fut = asyncio.ensure_future(_boom_direct())
        try:
            await fut
        except ValueError as e:
            result["future"] = str(e)

    asyncio.run(_main(), loop_factory=lambda: loop)
    emit("EXC", result)
    return 0


def mode_live() -> int:
    import signal

    app = QApplication([])
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    tasks = [asyncio.ensure_future(contention_bench(60))]

    pulse = QTimer()
    pulse.setInterval(50)
    pulse.timeout.connect(lambda: None)  # 保证信号点能落进 Python 帧
    pulse.start()

    def _on_signal(signum, frame):  # noqa: ANN001, ARG001 - 信号回调固定签名
        # 注意：quit() 只结束 exec() 嵌套会话；run_forever 必须用 stop()
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGBREAK, _on_signal)

    print("READY", flush=True)
    loop.run_forever()
    pulse.stop()
    for t in tasks:
        t.cancel()
    loop.call_soon_threadsafe(loop.stop)
    emit("CLEAN_EXIT", {"threads_left": report_threads_left()})
    return 0


MODES = {"timer": mode_timer, "mixed": mode_mixed, "exc": mode_exc, "live": mode_live}

if __name__ == "__main__":
    raise SystemExit(MODES[sys.argv[1]]())

"""方案乙 PoC：自建 loop-in-thread + queued Signal 投递（零新依赖）。

拓扑：Qt 主线程跑 QEventLoop；asyncio loop 跑在独立 daemon 线程。
两侧通信 100% 走 Signal（跨线程 = queued 投递），禁止任何直接跨线程调用。

模式：timer / mixed / exc / live（语义与 poc_qasync 对齐，便于横向比较）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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


class AsyncBridge(QObject):
    """把 asyncio 世界的完成事件，经 queued Signal 投递回 Qt 主线程。"""

    task_finished = Signal(object)  # 载荷：(label, result_or_exception)

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop

    def submit(self, label: str, coro: object) -> None:
        """Qt 线程 → loop 线程：唯一合法入口是 run_coroutine_threadsafe。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]

        def _done(f: object) -> None:
            try:
                payload = (label, f.result())  # type: ignore[attr-defined]
            except BaseException as e:  # noqa: BLE001 - 有意透传给主线程 re-raise
                payload = (label, e)
            # emit 跨线程 → AutoConnection 归为 QueuedConnection（非直调）
            self.task_finished.emit(payload)

        fut.add_done_callback(_done)


def _start_loop_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    th = threading.Thread(target=_run, name="asyncio-loop", daemon=True)
    th.start()
    ready.wait(2.0)
    return loop, th


def _shutdown(loop: asyncio.AbstractEventLoop, th: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    th.join(timeout=2.0)


def mode_timer() -> int:
    """基线：loop 线程空转时，Qt 主线程定时器精度（应与裸 Qt 一致）。"""
    app = QApplication([])
    loop, th = _start_loop_thread()
    probe = TimerProbe(INTERVAL_MS)
    probe.start()
    QTimer.singleShot(int(BENCH_SECONDS * 1000), app.quit)
    app.exec()
    probe.stop()
    _shutdown(loop, th)
    emit("TIMER", probe.metrics())
    return 0


class _MixedHarness(QObject):
    def __init__(self, bridge: AsyncBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.rtts: list[float] = []
        self.rounds = 0
        self._t0 = 0.0
        self.bridge.task_finished.connect(self._on_done)

    def on_tick(self) -> None:
        self._t0 = time.perf_counter()
        self.bridge.submit("mixed", _noop_coro())

    async def _wait(self) -> None:  # pragma: no cover - 占位保持接口对称
        await _noop_coro()

    def _on_done(self, payload: tuple) -> None:
        label, result = payload
        if label != "mixed":
            return
        if isinstance(result, BaseException):
            raise result
        self.rtts.append((time.perf_counter() - self._t0) * 1000.0)
        self.rounds += 1


async def _noop_coro() -> float:
    await asyncio.sleep(0.001)
    return time.perf_counter()


def mode_mixed() -> int:
    app = QApplication([])
    loop, th = _start_loop_thread()
    bridge = AsyncBridge(loop)
    h = _MixedHarness(bridge)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(50)
    timer.timeout.connect(h.on_tick)
    timer.start()

    probe = TimerProbe(INTERVAL_MS)
    probe.start()

    # loop 线程侧制造同等 CPU 负载（GIL 争用），观察 Qt 定时器是否被拖慢
    asyncio.run_coroutine_threadsafe(contention_bench(BENCH_SECONDS), loop)
    QTimer.singleShot(int((BENCH_SECONDS + 0.3) * 1000), app.quit)
    app.exec()
    timer.stop()
    probe.stop()
    rtts = sorted(h.rtts)
    _shutdown(loop, th)
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
    loop, th = _start_loop_thread()
    bridge = AsyncBridge(loop)
    result: dict[str, object] = {}
    raise_once = {"arm": True}

    async def _boom() -> None:
        raise ValueError("EXC-XTREAD-SENTINEL")

    async def _boom_loop() -> None:
        raise RuntimeError("EXC-LOOP-SENTINEL")

    def _on_done(payload: tuple) -> None:
        label, value = payload
        if label == "xthread" and isinstance(value, BaseException):
            # ④ 跨线程异常必须在 Qt 主线程 re-raise（此处捕获以记录"已到达"）
            result["xthread_reraised_in_main"] = f"{type(value).__name__}:{value}"
            if raise_once["arm"]:
                raise_once["arm"] = False
                raise value  # 真实使用姿势：让 Qt 槽看到异常
        if label == "unhandled":
            result["unhandled_seen"] = True

    def _guard() -> None:  # 接住上面故意抛出的异常，避免炸掉测试进程
        result["slot_caught"] = True

    bridge.task_finished.connect(lambda p: None)  # 先占位，保证连接类型为 queued
    bridge.task_finished.connect(_on_done)
    # 用 try/except 包一层：Qt 槽异常默认打印到 stderr，这里改由探测器判定
    try:
        bridge.submit("xthread", _boom())
    except Exception as e:  # noqa: BLE001
        result["xthread_reraised_in_main"] = f"{type(e).__name__}:{e}"

    async def _fire_unhandled() -> None:
        asyncio.ensure_future(_boom_loop())

    def _kick() -> None:
        asyncio.run_coroutine_threadsafe(_fire_unhandled(), loop)

    QTimer.singleShot(50, _kick)
    QTimer.singleShot(int(BENCH_SECONDS * 1000), app.quit)
    app.exec()
    _guard()
    _shutdown(loop, th)
    emit("EXC", result)
    return 0


def mode_live() -> int:
    import signal

    app = QApplication([])
    loop, th = _start_loop_thread()
    asyncio.run_coroutine_threadsafe(contention_bench(60), loop)

    pulse = QTimer()
    pulse.setInterval(50)
    pulse.timeout.connect(lambda: None)
    pulse.start()

    def _on_signal(signum, frame):  # noqa: ANN001, ARG001
        app.quit()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGBREAK, _on_signal)

    print("READY", flush=True)
    app.exec()
    pulse.stop()
    _shutdown(loop, th)
    emit("CLEAN_EXIT", {"threads_left": report_threads_left()})
    return 0


MODES = {"timer": mode_timer, "mixed": mode_mixed, "exc": mode_exc, "live": mode_live}

if __name__ == "__main__":
    raise SystemExit(MODES[sys.argv[1]]())

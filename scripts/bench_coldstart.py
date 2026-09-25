"""冷启动计时（阶段 6 验收：<1.5s）。

口径：每次在**独立子进程**里从"解释器就绪"测到"create_app + MainWindow 构造
完成 + 首帧 processEvents"（offscreen 平台）。子进程隔离保证每次都含 PySide6
全量导入（Qt 绑定是冷启动主成本，不能在同一进程内重复计）。真实桌面首绘另加
合成器开销，但 Python 导入 + Qt 对象构造才是 v4（实测 >3s，13 线程 + tkinter）
慢的根因，本口径与之可比。

用法：uv run python scripts/bench_coldstart.py [--repeat 5]
判定：median < 1500ms。lazy-import 优化留给实测超标后（预授权对策）。
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()


def one_coldstart(*, show: bool) -> float:
    """一次冷启动的毫秒数（在干净子进程内执行，含全部 import 成本）。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    t0 = time.perf_counter()

    from rdc.ui.app import create_app  # 主链路：app → theme → tokens → PySide6
    from rdc.ui.main_window import MainWindow

    app = create_app([])
    win = MainWindow(None)
    if show:
        win.show()
        app.processEvents()  # 首帧布局/绘制泵送
    return (time.perf_counter() - t0) * 1000.0


def _child() -> None:
    print(f"{one_coldstart(show=True):.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true", help="子进程模式：单次计时输出")
    ap.add_argument("--repeat", type=int, default=5)
    ns = ap.parse_args()

    samples: list[float] = []
    for _ in range(ns.repeat):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE), "--child"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        samples.append(float(r.stdout.strip().splitlines()[-1]))
    med = statistics.median(samples)
    print(f"coldstart samples(ms): {[round(s, 1) for s in samples]}")
    print(f"median={med:.1f}ms  min={min(samples):.1f}ms  max={max(samples):.1f}ms")
    ok = med < 1500.0
    print(f"verdict: {'PASS' if ok else 'FAIL'} (<1500ms)")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--child" in sys.argv:
        _child()
        raise SystemExit(0)
    raise SystemExit(main())

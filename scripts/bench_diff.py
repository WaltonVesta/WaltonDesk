"""差分基准（阶段 5 任务 10）：ScreenDiffer 端到端耗时 vs 验收线 <5ms。

内置 §2.6 三大防坑：
1. **预生成帧池**——内容合成绝不混进计时循环（否则 4K 测出 71.67ms 的假数据）；
2. 通道序无关（差分全程 uint32 打包，无 RGB/BGR 比较）；
3. 默认**不触网不抓屏**（用合成内容），``--real`` 才用 ``mss.MSS()``
   （硬约束①：绝不用已废弃的 ``mss.mss()``，pytest filterwarnings=error 会炸）。

用法::

    uv run python scripts/bench_diff.py                # 合成内容（可 CI 复现）
    uv run python scripts/bench_diff.py --real         # 叠加真实屏幕抓帧
    uv run python scripts/bench_diff.py --iters 100    # 自定义迭代数
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from rdc.capture.diff import ScreenDiffer  # noqa: E402

#: (名称, 宽, 高, 每帧变化的块数比例) —— 覆盖验收分辨率与两类内容形态
SCENARIOS: list[tuple[str, int, int, str]] = [
    ("4K high-change", 3840, 2160, "moving"),
    ("4K desktop", 3840, 2160, "desktop"),
    ("1080p high-change", 1920, 1080, "moving"),
    ("1080p desktop", 1920, 1080, "desktop"),
    ("1440p", 2560, 1440, "moving"),
    ("720p", 1280, 720, "desktop"),
]


def synth_frame(width: int, height: int, seq: int, kind: str) -> np.ndarray:
    """确定性合成帧（同一 (kind, seq) 永远同内容——可复现）。"""
    yy, xx = np.mgrid[0:height, 0:width]
    base = (xx // 3 + yy // 2 + seq * 5) % 256
    frame = np.empty((height, width, 4), dtype=np.uint8)
    frame[:, :, 0] = base
    frame[:, :, 1] = (base + 60) % 256
    frame[:, :, 2] = (base * 3) % 256
    frame[:, :, 3] = 255
    rng = np.random.default_rng(seq * 7919 + width)
    if kind == "moving":  # 高变化：散布 8x8 色块（约 230 块/帧变化）
        for _ in range(60):
            x0 = int(rng.integers(0, width - 64))
            y0 = int(rng.integers(0, height - 64))
            frame[y0 : y0 + 64, x0 : x0 + 64, :3] = rng.integers(0, 256, 3, dtype=np.uint8)
    else:  # desktop：两块小窗（时钟 + 通知，~24 块）
        for cx, cy, w, h in ((10, 10, 96, 64), (width - 140, height - 110, 128, 96)):
            frame[cy : cy + h, cx : cx + w, :3] = rng.integers(0, 256, 3, dtype=np.uint8)
    return frame


def bench(
    width: int,
    height: int,
    kind: str,
    iters: int,
    frame_factory: Callable[[int, int, int], np.ndarray] | None = None,
) -> dict[str, float]:
    """生产路径计时：双缓冲交替 + adopt=True（与 MssScreenSource 语义一致）。"""
    factory = frame_factory or (lambda w, h, s: synth_frame(w, h, s, kind))
    # 帧池只保留 2 块交替缓冲——正是被测语义，也顺带验证双缓冲不产生假变化
    buffers = [factory(width, height, 0), factory(width, height, 1)]
    differ = ScreenDiffer()
    differ.compare(buffers[0])  # 首帧（不计时）
    differ.compare(buffers[1])
    times: list[float] = []
    for i in range(iters):
        slot = i % 2
        buffers[slot] = factory(width, height, i + 2)  # 写"刚释放"的缓冲
        result = differ.compare(buffers[slot], adopt=True)
        times.append(result.elapsed_ms)
    differ.close()
    arr = np.asarray(times)
    return {
        "median": float(np.median(arr)),
        "min": float(arr.min()),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=50)
    parser.add_argument("--real", action="store_true", help="叠加真实屏幕抓帧（mss.MSS）")
    args = parser.parse_args()

    print(f"ScreenDiffer 基准（iters={args.iters}，块=32，生产路径 双缓冲+adopt=True）")
    print("-" * 78)
    print(f"{'场景':<22}{'中位数':>10}{'最小':>10}{'p95':>10}{'最大':>10}  判定(<5ms)")
    failures = 0
    for name, w, h, kind in SCENARIOS:
        stats = bench(w, h, kind, args.iters)
        ok = stats["median"] < 5.0
        failures += not ok
        print(
            f"{name:<22}{stats['median']:>9.2f}ms{stats['min']:>9.2f}ms"
            f"{stats['p95']:>9.2f}ms{stats['max']:>9.2f}ms  {'PASS' if ok else 'FAIL'}"
        )

    if args.real:
        import mss  # 仅 --real 路径导入

        with mss.MSS() as sct:  # 硬约束①：MSS()，不是 mss.mss()
            mon = sct.monitors[1]
            w, h = int(mon["width"]), int(mon["height"])

            def real_factory(width: int, height: int, _seq: int) -> np.ndarray:
                shot = sct.grab(mon)
                return np.copyto(
                    np.empty((height, width, 4), np.uint8),
                    np.frombuffer(shot.bgra, np.uint8).reshape(height, width, 4),
                )

            stats = bench(w, h, "real", args.iters, real_factory)  # type: ignore[arg-type]
            ok = stats["median"] < 5.0
            failures += not ok
            print(
                f"{'real screen ' + str(w) + 'x' + str(h):<22}{stats['median']:>9.2f}ms"
                f"{stats['min']:>9.2f}ms{stats['p95']:>9.2f}ms{stats['max']:>9.2f}ms  "
                f"{'PASS' if ok else 'FAIL'}"
            )

    print("-" * 78)
    verdict = "全部达标" if failures == 0 else f"{failures} 个场景超线"
    print(f"验收线 4K 差分 <5ms：{verdict}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

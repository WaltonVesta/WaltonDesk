"""事件循环滞后实测（阶段 6 A3，executor vs 进程池判定依据）。

方法：worker 线程连续跑生产单元重段（PyAV 编码 / numpy 差分）时，主线程
1ms asyncio 定时器的迟到分布。口径等价"qasync 下 Qt 主线程事件响应延迟"。

⚠️ 测量陷阱（实测踩中）：Windows 系统时钟默认粒度 15.6ms 会把所有延迟钳在
15ms 地板（假象"无差异"）。必须 ``timeBeginPeriod(1)``；且**每个用例独立进程
运行**（同进程顺序跑时，前序 PyAV open 阶段原生库可能把粒度复位，后续用例
p50 又回到 15ms 地板——那是时钟粒度而非 GIL 饥饿）。

实测结论（本机 2026-09-25 / Python 3.14.3 / av 18.1.0，逐用例独立进程）：
- encode 1080p ×120：p50=1.0ms p95=2.0ms
- encode 4K ×40：   p50=1.0ms p95=3.8ms
- diff 1080p ×120： p50=1.0ms p95=2.0ms
全部 ≪15ms → PyAV/cv2/numpy 重段基本释放 GIL；辅证：PoC qasync 轨 4×纯
Python 忙等满载下 QTimer p95 仍 1.15ms。
→ **executor 线程模型成立，pipeline 无需进程池**（数据入 STAGE6_REPORT §4）。
（注：纯忙等计数口径测"GIL 保持率"仅 44–57%——那是调度公平性口径；对 UI
真正有意义的是本脚本的循环滞后口径。）
"""

import asyncio
import contextlib
import threading
import time

import numpy as np


def make_frame(h, w, seed):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 4), dtype=np.uint8)


async def probe(stop: asyncio.Event, delays: list, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        t0 = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        delays.append((time.perf_counter() - t0 - interval) * 1000)


def stats(name, delays):
    d = sorted(delays[10:])
    if len(d) < 20:
        print(f"{name}: 样本不足 n={len(d)}")
        return
    p50, p95 = d[len(d) // 2], d[int(len(d) * 0.95)]
    print(f"{name}: n={len(d)} p50={p50:.1f}ms p95={p95:.1f}ms max={d[-1]:.1f}ms")


async def run_case(name, work):
    import ctypes

    ctypes.windll.winmm.timeBeginPeriod(1)  # 其他进程可能中途复位粒度，逐用例重申
    delays = []
    stop_async = asyncio.Event()
    task = asyncio.create_task(probe(stop_async, delays, 0.001))
    t0 = time.perf_counter()
    stop_work = threading.Event()
    th = threading.Thread(target=work, args=(stop_work,), daemon=True)
    th.start()
    while time.perf_counter() - t0 < 3.0 and not stop_work.is_set():
        await asyncio.sleep(0.05)
    stop_work.set()
    th.join(timeout=2)
    stop_async.set()
    await task
    stats(name, delays)


async def main():
    import ctypes
    import sys

    ctypes.windll.winmm.timeBeginPeriod(1)  # 突破默认 15.6ms 定时器粒度
    only = sys.argv[1] if len(sys.argv) > 1 else None  # 单用例复测：进程内粒度可能被外部复位

    from rdc.capture.diff import ScreenDiffer
    from rdc.encoding.h264 import H264Encoder

    for label, h, w, iters in (("1080p", 1080, 1920, 120), ("4K", 2160, 3840, 40)):
        if only and only != label.lower():
            continue
        enc = H264Encoder(w, h)
        frames = [make_frame(h, w, s) for s in range(3)]
        enc.encode(frames[0])

        def w_enc(stop_evt, enc=enc, frames=frames, iters=iters):
            for i in range(iters):
                if stop_evt.is_set():
                    return
                enc.encode(frames[i % len(frames)])

        await run_case(f"encode {label} 连续 {iters} 帧（worker 线程）", w_enc)
        enc.close()

    if only and only != "diff":
        return

    diff = ScreenDiffer(workers=8)
    a, b = make_frame(1080, 1920, 1), make_frame(1080, 1920, 2)
    diff.compare(a, adopt=True)

    def w_diff(stop_evt):
        i = 0
        while not stop_evt.is_set() and i < 120:
            diff.compare(b if i % 2 else a, adopt=False)
            i += 1

    await run_case("diff 1080p（worker 线程）", w_diff)
    diff.close()

    if only:
        return

    def w_none(stop_evt):
        stop_evt.wait(3.0)

    await run_case("baseline（无负载）", w_none)


asyncio.run(main())

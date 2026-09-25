"""编码基准（阶段 5 任务 10）：H.264 延迟 vs 验收线 <40ms + 多场景带宽降幅 vs ≥40%。

内置 §2.6 三大防坑：
1. 预生成帧池，内容合成不进计时循环；
2. 往返 PSNR 用 **BGRA 对齐**的通道序比较（拿 BGRA 当 RGB 比会得到 ~15dB 假值）；
3. 全程不触 ``mss.mss()``；``--real`` 分支只用 ``mss.MSS()``。

场景口径对齐 STAGE5_INPROGRESS §2.7：JPEG q60（optimize=True, subsampling=0）
为 v4 基线，H.264 crf28 为 v5 主路径；30 帧/场景取平均包大小。

用法::

    uv run python scripts/bench_encode.py               # 合成内容 + 真实屏幕各一遍
    uv run python scripts/bench_encode.py --iters 30
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from rdc.capture.source import bgra_view  # noqa: E402
from rdc.encoding.h264 import H264Encoder  # noqa: E402
from rdc.encoding.jpeg import JPEGEncoder  # noqa: E402

ITERS_DEFAULT = 24

#: (名称, 宽, 高, 内容类型)
SCENARIOS: list[tuple[str, int, int, str]] = [
    ("4K static", 3840, 2160, "static"),
    ("4K moving", 3840, 2160, "moving"),
    ("1080p static", 1920, 1080, "static"),
    ("1080p moving", 1920, 1080, "moving"),
    ("1080p scroll", 1920, 1080, "scroll"),
    ("1080p noise", 1920, 1080, "noise"),
]


def synth_frame(width: int, height: int, seq: int, kind: str) -> np.ndarray:
    """确定性合成帧（与 bench_diff 同风格；kind 决定场景内容形态）。"""
    if kind == "static":
        yy, xx = np.mgrid[0:height, 0:width]
        frame = np.empty((height, width, 4), np.uint8)
        frame[:, :, 0] = (xx // 2) % 256
        frame[:, :, 1] = (yy // 2) % 256
        frame[:, :, 2] = ((xx + yy) // 3) % 256
        frame[:, :, 3] = 255
        # 唯一的"时钟"角标在变（桌面静止语义）
        frame[8:40, 8:120] = np.uint8((seq * 13) % 256)
        return frame
    rng = np.random.default_rng(seq * 104729 + width)
    if kind == "noise":  # 高熵最坏场景
        frame = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        return frame
    base_yy, base_xx = np.mgrid[0:height, 0:width]
    shift = seq * 24 if kind == "scroll" else 0  # 文档滚动：整屏纵向平移
    yy = (base_yy + shift) % height
    frame = np.empty((height, width, 4), np.uint8)
    frame[:, :, 0] = (base_xx // 4 + yy // 2) % 256
    frame[:, :, 1] = (yy * 3) % 256
    frame[:, :, 2] = ((base_xx + yy) // 5) % 256
    frame[:, :, 3] = 255
    if kind == "moving":
        for _ in range(20):
            x0 = int(rng.integers(0, width - 48))
            y0 = int(rng.integers(0, height - 48))
            frame[y0 : y0 + 48, x0 : x0 + 48, :3] = rng.integers(0, 256, 3, dtype=np.uint8)
    return frame


def encode_pool_h264(pool: list[np.ndarray], crf: int) -> tuple[list[bytes], list[float], int]:
    """编码帧池，返回（包序列, 每帧耗时, 关键帧数）。池已预生成——计时纯净。"""
    h, w = pool[0].shape[:2]
    enc = H264Encoder(w, h, crf=crf)
    datas: list[bytes] = []
    times: list[float] = []
    keys = 0
    for f in pool:
        out = enc.encode(f)
        datas.append(out.data)
        times.append(out.elapsed_ms)
        keys += out.is_keyframe
    enc.close()
    return datas, times, keys


def encode_pool_jpeg(pool: list[np.ndarray], quality: int) -> list[bytes]:
    """v4 基线：JPEG q60（optimize=True subsampling=0）。"""
    enc = JPEGEncoder(quality=quality)
    datas = [enc.encode(f).data for f in pool]
    enc.close()
    return datas


def roundtrip_psnr(pool: list[np.ndarray], packets: list[bytes]) -> tuple[float, float]:
    """H.264 往返 PSNR（min/avg dB）。防坑②：解码帧用 to_ndarray('bgra')
    回到与源同通道序再比，杜绝 BGRA/RGB 错位假值。"""
    import av

    decoder = av.CodecContext.create("h264", "r")
    decoded: list[np.ndarray] = []
    for pkt in packets:
        for frame in decoder.decode(av.Packet(pkt)):
            decoded.append(frame.to_ndarray(format="bgra"))
    try:
        for frame in decoder.decode():
            decoded.append(frame.to_ndarray(format="bgra"))
    except Exception:  # pragma: no cover - 冲刷异常容忍
        pass
    if not decoded:
        return (0.0, 0.0)
    psnrs: list[float] = []
    for src, got in zip(pool, decoded, strict=False):
        diff = src[:, :, :3].astype(np.float64) - got[:, :, :3].astype(np.float64)
        mse = float((diff * diff).mean())
        psnrs.append(99.0 if mse == 0 else float(10 * np.log10(255 * 255 / mse)))
    return (min(psnrs), float(np.mean(psnrs)))


def bench_latency(width: int, height: int, kind: str, iters: int, frame_factory: Callable[..., np.ndarray] | None = None) -> dict[str, float]:
    factory = frame_factory or (lambda w, h, s: synth_frame(w, h, s, kind))
    pool = [factory(width, height, i) for i in range(iters)]  # 防坑①：池外合成
    _, times, _ = encode_pool_h264(pool, crf=28)
    arr = np.asarray(times)
    return {"median": float(np.median(arr)), "min": float(arr.min()), "max": float(arr.max())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=ITERS_DEFAULT)
    parser.add_argument("--skip-real", action="store_true", help="跳过真实屏幕场景")
    args = parser.parse_args()

    print(f"H.264 编码基准（iters={args.iters}，ultrafast+zerolatency，帧池预生成）")
    print("=" * 78)

    print("① 编码延迟（验收线 <40ms）")
    failures = 0
    for name, w, h, kind in SCENARIOS:
        pool_iters = 12 if w >= 3840 and args.iters > 12 else args.iters  # 4K 帧池省内存
        stats = bench_latency(w, h, kind, pool_iters)
        ok = stats["median"] < 40.0
        failures += not ok
        print(
            f"  {name:<22} 中位 {stats['median']:>7.2f}ms  最小 {stats['min']:>7.2f}ms"
            f"  最大 {stats['max']:>7.2f}ms  {'PASS' if ok else 'FAIL'}"
        )
    # 最坏口径（仅记录不断言）：4K 随机噪声——编码器熵上限场景
    worst = bench_latency(3840, 2160, "noise", 6)
    print(
        f"  {'4K noise(最坏参考)':<22} 中位 {worst['median']:>7.2f}ms  最小 {worst['min']:>7.2f}ms"
        f"  最大 {worst['max']:>7.2f}ms  （信息项：桌面内容不会到达此熵）"
    )

    print()
    print("② 带宽降幅（v4 JPEG q60 → v5 H.264 crf28，验收线 ≥40%）")
    for name, w, h, kind in SCENARIOS:
        it = 12 if w >= 3840 else min(args.iters, 30)
        pool = [synth_frame(w, h, i, kind) for i in range(it)]
        j_pkts = encode_pool_jpeg(pool, 60)
        h_pkts, _, _ = encode_pool_h264(pool, 28)
        j_avg = float(np.mean([len(p) for p in j_pkts]))
        h_avg = float(np.mean([len(p) for p in h_pkts]))
        drop = (1 - h_avg / j_avg) * 100 if j_avg else 0.0
        ok = drop >= 40.0
        failures += not ok
        print(
            f"  {name:<22} JPEG {j_avg / 1024:>9.2f}KB  H.264 {h_avg / 1024:>9.2f}KB"
            f"  降幅 {drop:>5.1f}%  {'PASS' if ok else 'FAIL'}"
        )

    print()
    print("③ 往返 PSNR（crf28，防坑②通道序修正后的真实画质）")
    pool = [synth_frame(640, 360, i, "moving") for i in range(10)]
    pkts, _, _ = encode_pool_h264(pool, 28)
    lo, avg = roundtrip_psnr(pool, pkts)
    print(f"  640x360 moving: min={lo:.2f}dB avg={avg:.2f}dB（>30dB 为肉眼几乎无损）")
    ok = lo > 25.0
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}")

    if not args.skip_real:
        print()
        print("④ 真实屏幕抽样（mss.MSS，桌面静 + 拖窗动）")
        import mss

        with mss.MSS() as sct:  # 硬约束①
            mon = sct.monitors[1]
            w, h = int(mon["width"]), int(mon["height"])

            def grab(_w: int = w, _h: int = h) -> np.ndarray:
                shot = sct.grab(mon)
                return np.array(bgra_view(shot.bgra, _w, _h), dtype=np.uint8)  # 转持有副本

            pool = [grab() for _ in range(6)]
            for i in range(1, 6):  # 模拟小窗移动
                pool[i][40 * i : 40 * i + 64, 40 : 160, :3] = 128 - i * 20
            j_avg = float(np.mean([len(p) for p in encode_pool_jpeg(pool, 60)]))
            h_pkts, times, _ = encode_pool_h264(pool, 28)
            h_avg = float(np.mean([len(p) for p in h_pkts]))
            drop = (1 - h_avg / j_avg) * 100 if j_avg else 0.0
            med = float(np.median(times))
            ok = med < 40.0 and drop >= 40.0
            failures += not ok
            print(f"  {w}x{h}: 延迟中位 {med:.2f}ms  JPEG {j_avg/1024:.1f}KB → H.264 {h_avg/1024:.1f}KB  降幅 {drop:.1f}%  {'PASS' if ok else 'FAIL'}")

    print()
    print("=" * 78)
    verdict = "全部达标" if failures == 0 else f"{failures} 项超线/未达标"
    print(f"验收：编码 <40ms & 带宽降幅 ≥40% —— {verdict}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

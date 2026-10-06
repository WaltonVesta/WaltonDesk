"""H.264 编码器（rdc.encoding.h264）单元测试。

覆盖三条实测硬约束的可断言部分：
- 硬约束 1（P2 色彩路径）：1080p 编码延迟上界（4K 口径交给 bench_encode.py）；
- 硬约束 2（thread_count≥4）：私有属性 + 极小值注入钳制；
- 硬约束 3（不调 ctx.close）：close 幂等且 PyAV 对象无 close 调用路径。
"""

from __future__ import annotations

import time

import av
import numpy as np
import pytest

from rdc.encoding.h264 import CRF_RANGE, DEFAULT_CRF, H264Encoder
from rdc.protocol.messages import Codec

W, H = 64, 48


def make_frame(seed: int = 0, width: int = W, height: int = H) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[:, :, :3] = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    frame[:, :, 3] = 255
    return frame


def decode_all(chunks: list[bytes]) -> int:
    """把 Annex-B 分片序列喂裸解码器，返回解出的帧数（往返正确性 oracle）。"""
    decoder = av.CodecContext.create("h264", "r")
    n = 0
    for chunk in chunks:
        for _decoded in decoder.decode(av.Packet(chunk)):
            n += 1
    # 冲刷（zerolatency 下通常为空，但必须调，否则末帧可能滞留）
    try:
        for _decoded in decoder.decode():
            n += 1
    except av.error.InvalidDataError:  # pragma: no cover - 冲刷异常容忍
        pass
    return n


class TestConstruction:
    """构造与参数钳制。"""

    def test_bad_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="尺寸非法"):
            H264Encoder(0, 10)
        with pytest.raises(ValueError, match="尺寸非法"):
            H264Encoder(10, -1)

    def test_bad_fps_rejected(self) -> None:
        with pytest.raises(ValueError, match="fps"):
            H264Encoder(64, 64, fps=0)

    def test_codec_identity(self) -> None:
        enc = H264Encoder(W, H)
        assert enc.codec is Codec.H264
        enc.close()

    def test_default_crf(self) -> None:
        enc = H264Encoder(W, H)
        assert enc.stats().quality == DEFAULT_CRF == 28
        enc.close()

    def test_crf_clamped(self) -> None:
        low = H264Encoder(W, H, crf=1)
        assert low.stats().quality == CRF_RANGE[0]
        low.close()
        high = H264Encoder(W, H, crf=999)
        assert high.stats().quality == CRF_RANGE[1]
        high.close()

    def test_thread_count_min_four(self) -> None:
        # 硬约束 2：thread_count=1 会让 4K 编码飙到 65.8ms，必须钳到 ≥4
        enc = H264Encoder(W, H, thread_count=1)
        assert enc._threads >= 4  # noqa: SLF001
        enc.close()
        auto = H264Encoder(W, H, thread_count=0)
        assert auto._threads >= 4  # noqa: SLF001
        auto.close()
        big = H264Encoder(W, H, thread_count=16)
        assert big._threads == 16  # noqa: SLF001
        big.close()

    def test_gop_from_interval(self) -> None:
        enc = H264Encoder(W, H, fps=30, keyframe_interval_sec=2.0)
        assert enc._gop == 60  # noqa: SLF001
        enc.close()


class TestEncode:
    """encode 主路径。"""

    def test_produces_data_and_keyframe(self) -> None:
        enc = H264Encoder(W, H)
        out = enc.encode(make_frame(1))
        assert out.data and out.is_keyframe
        assert (out.width, out.height) == (W, H)
        assert out.pts == 0 and out.elapsed_ms > 0
        enc.close()

    def test_rejects_bad_frame(self) -> None:
        enc = H264Encoder(W, H)
        with pytest.raises(ValueError, match="BGRA"):
            enc.encode(np.zeros((H, W, 3), np.uint8))
        with pytest.raises(ValueError, match="BGRA"):
            enc.encode(np.zeros((H, W, 4), np.float32))
        enc.close()

    def test_negative_pts_rejected(self) -> None:
        enc = H264Encoder(W, H)
        with pytest.raises(ValueError, match="pts"):
            enc.encode(make_frame(), pts=-1)
        enc.close()

    def test_stream_decodes_roundtrip(self) -> None:
        """编码流必须能被裸 H.264 解码器完整解回（Annex-B、无 AVCC 依赖）。"""
        enc = H264Encoder(W, H)
        chunks = [enc.encode(make_frame(i)).data for i in range(10)]
        assert decode_all(chunks) == 10
        enc.close()

    def test_gop_periodic_keyframes(self) -> None:
        enc = H264Encoder(W, H, fps=30, keyframe_interval_sec=0.1)  # gop=3
        flags = [enc.encode(make_frame(i)).is_keyframe for i in range(9)]
        enc.close()
        assert flags[0] is True
        assert sum(flags) >= 3  # 0,3,6 至少三个关键帧

    def test_force_keyframe_mid_stream(self) -> None:
        enc = H264Encoder(W, H, fps=30, keyframe_interval_sec=10.0)  # gop=300 罕到
        assert enc.encode(make_frame(1)).is_keyframe
        assert not enc.encode(make_frame(2)).is_keyframe
        assert enc.encode(make_frame(3), force_keyframe=True).is_keyframe
        assert not enc.encode(make_frame(4)).is_keyframe
        enc.close()

    def test_request_keyframe_latches_once(self) -> None:
        enc = H264Encoder(W, H, fps=30, keyframe_interval_sec=10.0)
        enc.encode(make_frame(1))
        enc.request_keyframe()
        assert enc.encode(make_frame(2)).is_keyframe
        assert not enc.encode(make_frame(3)).is_keyframe  # 标志只吃一次
        enc.close()

    def test_forced_keyframe_makes_stream_independent(self) -> None:
        """从关键帧开头的切片可独立解码（丢包恢复的依据）。"""
        enc = H264Encoder(W, H, fps=30, keyframe_interval_sec=10.0)  # gop=300 罕到
        outs = []
        for i in range(12):
            outs.append(enc.encode(make_frame(i), force_keyframe=(i == 6)))
        enc.close()
        assert not outs[5].is_keyframe and outs[6].is_keyframe
        assert decode_all([o.data for o in outs[6:10]]) >= 4


class TestQualityAndResolution:
    """CRF 切换与分辨率重建。"""

    def test_set_quality_rebuild_lazily_and_keyframes(self) -> None:
        enc = H264Encoder(W, H, crf=28, fps=30, keyframe_interval_sec=10.0)
        enc.encode(make_frame(1))
        enc.set_quality(20)
        assert enc._pending_rebuild  # noqa: SLF001 - 惰性：不在调用线程重建
        out = enc.encode(make_frame(2))
        assert not enc._pending_rebuild  # noqa: SLF001
        assert out.is_keyframe  # 重建后首帧必关键帧
        assert out.quality == 20
        enc.close()

    def test_set_quality_same_value_noop(self) -> None:
        enc = H264Encoder(W, H, crf=28)
        enc.encode(make_frame(1))
        enc.set_quality(28)
        assert not enc._pending_rebuild  # noqa: SLF001
        enc.close()

    def test_quality_affects_size(self) -> None:
        """CRF 越高包越小（§2.4 单调性的最小验证）。"""
        frames = [make_frame(100 + i) for i in range(6)]
        sizes = []
        for crf in (18, 45):
            enc = H264Encoder(160, 120, crf=crf)
            sizes.append(sum(len(enc.encode(f).data) for f in frames))
            enc.close()
        assert sizes[0] > sizes[1] * 1.5, f"CRF18={sizes[0]} vs CRF45={sizes[1]}"

    def test_set_resolution_rebuilds(self) -> None:
        enc = H264Encoder(W, H)
        enc.encode(make_frame(1))
        enc.set_resolution(128, 96)
        out = enc.encode(make_frame(2, 128, 96))
        assert (out.width, out.height) == (128, 96) and out.is_keyframe
        enc.close()

    def test_set_resolution_same_size_noop(self) -> None:
        enc = H264Encoder(W, H)
        ctx_before = enc._ctx  # noqa: SLF001
        enc.set_resolution(W, H)
        assert enc._ctx is ctx_before  # noqa: SLF001
        enc.close()

    def test_set_resolution_bad(self) -> None:
        enc = H264Encoder(W, H)
        with pytest.raises(ValueError, match="尺寸非法"):
            enc.set_resolution(-5, 10)
        enc.close()

    def test_resolution_change_auto_follows(self) -> None:
        """采集端偷偷变了尺寸而未调 set_resolution：编码器自动跟随不崩。"""
        enc = H264Encoder(W, H)
        enc.encode(make_frame(1))
        out = enc.encode(make_frame(2, 80, 60))
        assert (out.width, out.height) == (80, 60)
        assert out.is_keyframe
        st = enc.stats()
        assert st.frames == 2
        enc.close()


class TestOddSizeAlignment:
    """走查 bug 3：奇数分辨率（mss 窗口模式常态）被 libx264 拒 → 编码层向下取偶自兜底。"""

    @pytest.mark.parametrize(
        ("w", "h", "aw", "ah"),
        [(955, 910, 954, 910), (1921, 1081, 1920, 1080)],
    )
    def test_odd_construct_opens_and_encodes(
        self, w: int, h: int, aw: int, ah: int
    ) -> None:
        enc = H264Encoder(w, h)
        assert (enc._width, enc._height) == (aw, ah)  # noqa: SLF001
        ctx = enc._ctx  # noqa: SLF001
        assert (ctx.width, ctx.height) == (aw, ah)
        out = enc.encode(make_frame(1, w, h))
        assert (out.width, out.height) == (aw, ah)
        assert out.is_keyframe and out.data
        enc.close()

    def test_odd_stream_decodes_roundtrip(self) -> None:
        """对齐后的奇数源流必须能被裸解码器完整解回。"""
        enc = H264Encoder(65, 49)
        chunks = [enc.encode(make_frame(i, 65, 49)).data for i in range(5)]
        assert decode_all(chunks) == 5
        enc.close()

    def test_odd_source_rebuilds_once_not_every_frame(self) -> None:
        """奇数源持续进来：跟随重建只发生一次，后续对齐同尺寸不得反复重建。"""
        enc = H264Encoder(W, H)  # (64, 48)
        enc.encode(make_frame(1, 80, 60))  # 尺寸变 → 跟随重建到 (80, 60)
        ctx_after = enc._ctx  # noqa: SLF001
        enc.encode(make_frame(2, 81, 61))  # 对齐 (80, 60) == 现尺寸 → 不重建
        assert enc._ctx is ctx_after  # noqa: SLF001
        enc.encode(make_frame(3, 81, 61))
        assert enc._ctx is ctx_after  # noqa: SLF001
        assert enc.stats().frames == 3
        enc.close()

    def test_set_resolution_odd_aligned_and_idempotent(self) -> None:
        enc = H264Encoder(W, H)
        enc.set_resolution(955, 911)
        assert (enc._width, enc._height) == (954, 910)  # noqa: SLF001
        ctx_before = enc._ctx  # noqa: SLF001
        enc.set_resolution(954, 910)  # 与对齐值相同 → 幂等不重建
        assert enc._ctx is ctx_before  # noqa: SLF001
        enc.close()

    def test_one_dimension_odd(self) -> None:
        enc = H264Encoder(64, 49)
        assert (enc._width, enc._height) == (64, 48)  # noqa: SLF001
        out = enc.encode(make_frame(1, 64, 49))
        assert (out.width, out.height) == (64, 48)
        enc.close()

    def test_tiny_sizes(self) -> None:
        """2x2 可建可编；1x1 对齐后为 0，按尺寸过小拒绝。"""
        enc = H264Encoder(2, 2)
        out = enc.encode(make_frame(1, 2, 2))
        assert (out.width, out.height) == (2, 2) and out.data
        enc.close()
        with pytest.raises(ValueError, match="过小"):
            H264Encoder(1, 1)
        enc2 = H264Encoder(W, H)
        try:
            with pytest.raises(ValueError, match="过小"):
                enc2.set_resolution(1, 4096)
        finally:
            enc2.close()


class TestStats:
    """统计快照。"""

    def test_accumulates(self) -> None:
        enc = H264Encoder(W, H)
        sizes = []
        for i in range(4):
            sizes.append(len(enc.encode(make_frame(i)).data))
        st = enc.stats()
        assert st.frames == 4
        assert st.keyframes >= 1
        assert st.total_bytes == sum(sizes) > 0
        assert st.max_ms >= st.avg_ms > 0 and st.last_ms > 0
        assert st.bitrate_bps(30.0) > 0
        enc.close()


class TestClose:
    """关闭语义（硬约束 3：不写 ctx.close()）。"""

    def test_close_idempotent(self) -> None:
        enc = H264Encoder(W, H)
        enc.encode(make_frame(1))
        enc.close()
        enc.close()

    def test_encode_after_close_raises(self) -> None:
        enc = H264Encoder(W, H)
        enc.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            enc.encode(make_frame(1))

    def test_stats_after_close_readable(self) -> None:
        enc = H264Encoder(W, H)
        enc.encode(make_frame(1))
        enc.close()
        assert enc.stats().frames == 1

    def test_source_has_no_ctx_close_call(self) -> None:
        """硬约束 3 的静态锁：源码里不得出现 ctx.close()。"""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(H264Encoder))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("close", "release")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx"
            ):  # pragma: no cover - 期望永不命中
                pytest.fail("PyAV CodecContext 没有 close()，源码不得出现 ctx.close()")


@pytest.mark.load
class TestPerformance:
    """性能护栏（正式口径见 scripts/bench_encode.py，这里只做防回归粗筛）。

    ⚠️ 用**平滑渐变 + 少量色块**的真实屏幕式内容——随机噪声是高熵最坏情况
    （x264 在 4K 噪声上可到 ~40ms），不代表验收口径。
    """

    def smooth_pool(self, width: int, height: int, n: int) -> list[np.ndarray]:
        yy, xx = np.mgrid[0:height, 0:width]
        base = np.empty((height, width, 4), np.uint8)
        base[:, :, 0] = (xx // 2) % 256
        base[:, :, 1] = (yy // 2) % 256
        base[:, :, 2] = ((xx + yy) // 4) % 256
        base[:, :, 3] = 255
        pool = []
        for i in range(n):
            f = base.copy()
            f[i * 32 : i * 32 + 96, 40:280, :3] = np.uint8(20 + i * 7)  # 小窗移动
            pool.append(f)
        return pool

    def test_1080p_under_40ms(self) -> None:
        pool = self.smooth_pool(1920, 1080, 10)  # 帧池预生成（§2.6 坑 1）
        enc = H264Encoder(1920, 1080)
        enc.encode(pool[0])  # 预热
        t0 = time.perf_counter()
        for f in pool:
            enc.encode(f)
        avg_ms = (time.perf_counter() - t0) / len(pool) * 1000
        enc.close()
        assert avg_ms < 40.0, f"1080p 编码 {avg_ms:.1f}ms 破 40ms 线"

    def test_4k_under_40ms(self) -> None:
        """验收项：4K 编码 <40ms（真实内容基线 ~20ms，留 2× 余量）。"""
        pool = self.smooth_pool(3840, 2160, 5)
        enc = H264Encoder(3840, 2160)
        enc.encode(pool[0])
        t0 = time.perf_counter()
        for f in pool:
            enc.encode(f)
        avg_ms = (time.perf_counter() - t0) / len(pool) * 1000
        enc.close()
        assert avg_ms < 40.0, f"4K 编码 {avg_ms:.1f}ms 破 40ms 线"

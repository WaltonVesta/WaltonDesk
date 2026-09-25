"""块级差分器（rdc.capture.diff）单元测试——批 A smoke 的 26 项断言固化。

正确性 oracle：与 v4 朴素双重循环 ``np.array_equal`` 逐块对比（§2.1 的
"向量化结果与朴素实现逐块全等"结论），覆盖非整除块、极端小图等配置。
性能验收（4K <5ms）标 ``load``，由 ``scripts/bench_diff.py`` 正式取样。
"""

from __future__ import annotations

import numpy as np
import pytest

from rdc.capture.diff import ScreenDiffer

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def naive_mask(frame_a: np.ndarray, frame_b: np.ndarray, bs: int) -> np.ndarray:
    """v4 语义的朴素差分（双重循环 + 逐块 array_equal），oracle 用。"""
    h, w = frame_a.shape[:2]
    rows, cols = (h + bs - 1) // bs, (w + bs - 1) // bs
    mask = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            r_end, c_end = min((r + 1) * bs, h), min((c + 1) * bs, w)
            mask[r, c] = not np.array_equal(frame_a[r * bs : r_end, c * bs : c_end], frame_b[r * bs : r_end, c * bs : c_end])
    return mask


def content_frame(h: int, w: int, seed: int) -> np.ndarray:
    """平滑渐变 + 细网格 + 色块的"桌面式"内容（高频边避开块边界）。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.empty((h, w, 4), np.uint8)
    frame[:, :, 0] = (xx * 3 + yy) % 256
    frame[:, :, 1] = (yy * 5) % 256
    frame[:, :, 2] = (xx + yy) % 128
    frame[:, :, 3] = 255
    vstep, hstep = max(w // 8, 1), max(h // 8, 1)
    frame[:, ::vstep, :3] = rng.integers(0, 256, 3, dtype=np.uint8)  # 竖细线
    frame[::hstep, :, :3] = rng.integers(0, 256, 3, dtype=np.uint8)  # 横细线
    for _ in range(5):
        x0 = int(rng.integers(0, max(w - 40, 1)))
        y0 = int(rng.integers(0, max(h - 40, 1)))
        frame[y0 : y0 + 40, x0 : x0 + 40, :3] = rng.integers(0, 256, 3, dtype=np.uint8)
    return frame


# (高, 宽, 块大小)：§1.5 六配置 + 极端小尺寸
ORACLE_CONFIGS = [
    pytest.param(2160, 3840, 32, id="4K@32", marks=pytest.mark.load),
    pytest.param(1080, 1920, 32, id="1080p@32"),
    pytest.param(1080, 1928, 32, id="1928x1080-nondivisible"),
    pytest.param(63, 65, 32, id="65x63-tiny"),
    pytest.param(100, 130, 16, id="130x100@16"),
    pytest.param(1080, 1920, 8, id="1080p@8"),
    pytest.param(1, 1, 32, id="1x1"),
    pytest.param(1, 4000, 32, id="1x4000", marks=pytest.mark.load),
    pytest.param(4000, 1, 32, id="4000x1", marks=pytest.mark.load),
]


class TestOracleEquivalence:
    """向量化结果必须与朴素双重循环逐块全等（§2.1 oracle 固化）。"""

    @pytest.mark.parametrize(("h", "w", "bs"), ORACLE_CONFIGS)
    def test_matches_naive_blockwise(self, h: int, w: int, bs: int) -> None:
        a = content_frame(h, w, seed=1)
        b = content_frame(h, w, seed=2)
        differ = ScreenDiffer(block_size=bs)
        differ.compare(a)
        result = differ.compare(b)
        assert result.mask.shape == ((h + bs - 1) // bs, (w + bs - 1) // bs)
        assert np.array_equal(result.mask, naive_mask(a, b, bs)), "逐块掩码与朴素 oracle 不一致"
        differ.close()

    @pytest.mark.parametrize(("h", "w", "bs"), ORACLE_CONFIGS[:6])
    def test_single_pixel_change_location(self, h: int, w: int, bs: int) -> None:
        """任意单像素变化必须落到正确的块。"""
        rng = np.random.default_rng(7)
        for _ in range(5):
            a = content_frame(h, w, seed=3)
            y, x = int(rng.integers(0, h)), int(rng.integers(0, w))
            b = a.copy()
            b[y, x, 1] ^= 0xFF
            differ = ScreenDiffer(block_size=bs)
            differ.compare(a)
            res = differ.compare(b)
            differ.close()
            expected = naive_mask(a, b, bs)
            assert np.array_equal(res.mask, expected)
            assert res.changed_blocks == 1

    def test_identical_frames_static(self) -> None:
        a = content_frame(200, 300, seed=5)
        differ = ScreenDiffer()
        differ.compare(a)
        res = differ.compare(a.copy())
        assert not res.any_change and res.is_static
        assert res.changed_blocks == 0 and res.dirty_rects == ()
        differ.close()


class TestFrameSemantics:
    """首帧 / 分辨率变化 / alpha / adopt 语义（smoke 断言）。"""

    def test_first_frame_full(self) -> None:
        differ = ScreenDiffer()
        res = differ.compare(np.zeros((64, 64, 4), np.uint8))
        assert res.is_first and res.full_frame
        assert res.changed_blocks == res.total_blocks
        differ.close()

    def test_v4_compat_get_changed_blocks_none_on_first(self) -> None:
        from PIL import Image

        differ = ScreenDiffer()
        assert differ.get_changed_blocks(Image.new("RGB", (64, 64))) is None  # 首帧→发全帧
        differ.close()

    def test_resolution_change_resets(self) -> None:
        differ = ScreenDiffer()
        differ.compare(np.zeros((64, 64, 4), np.uint8))
        res = differ.compare(np.zeros((64, 96, 4), np.uint8))  # 宽度变化
        assert res.is_first and res.full_frame
        assert res.frame_size == (96, 64)
        differ.close()

    def test_alpha_change_detected_by_default(self) -> None:
        a = np.zeros((64, 64, 4), np.uint8)
        b = a.copy()
        b[0, 0, 3] = 128
        differ = ScreenDiffer(ignore_alpha=False)
        differ.compare(a)
        assert differ.compare(b).changed_blocks == 1  # 默认四字节全比 → 有变化
        differ.close()

    def test_alpha_ignored_when_enabled(self) -> None:
        a = np.zeros((64, 64, 4), np.uint8)
        b = a.copy()
        b[0, 0, 3] = 128
        differ = ScreenDiffer(ignore_alpha=True)
        differ.compare(a)
        assert not differ.compare(b).any_change
        differ.close()

    def test_adopt_true_violation_blindness(self) -> None:
        """adopt=True 违约示范：调用方原地改写同一缓冲 → 前帧==当前帧，
        差分**永远看不到变化**。这正是要用双缓冲交替（MssScreenSource）的原因。"""
        buf = content_frame(64, 64, seed=9)
        differ = ScreenDiffer()
        differ.compare(buf, adopt=True)  # 差分器按引用持有 buf
        buf[0, 0, 0] ^= 0xFF  # 调用方违约：复用了同一缓冲
        res = differ.compare(buf, adopt=True)
        assert res.changed_blocks == 0, "同一对象自比必然无变化（违约被掩盖）"
        # 正确姿势：交替到另一块缓冲
        buf2 = buf.copy()
        buf2[32, 32, 1] ^= 0xFF
        good = differ.compare(buf2, adopt=True)
        assert good.changed_blocks == 1
        differ.close()

    def test_adopt_false_copies(self) -> None:
        buf = content_frame(64, 64, seed=10)
        differ = ScreenDiffer()
        differ.compare(buf, adopt=False)
        buf[0, 0, 0] ^= 0xFF
        res = differ.compare(buf, adopt=False)
        assert res.changed_blocks == 1  # 拷贝语义：与改写后的对比同样检出
        differ.close()

    def test_bgr_input_supported(self) -> None:
        a = np.zeros((64, 64, 3), np.uint8)
        b = a.copy()
        b[32, 32, 2] = 200
        differ = ScreenDiffer()
        differ.compare(a)
        assert differ.compare(b).changed_blocks == 1
        differ.close()

    @pytest.mark.parametrize(
        "bad",
        [
            np.zeros((8, 8, 2), np.uint8),  # 通道数
            np.zeros((8, 8), np.uint8),  # 2D
            np.zeros((8, 8, 4), np.float32),  # dtype
            np.zeros((0, 8, 4), np.uint8),  # 空高
        ],
    )
    def test_invalid_frame_rejected(self, bad: np.ndarray) -> None:
        differ = ScreenDiffer()
        with pytest.raises(ValueError):
            differ.compare(bad)
        differ.close()


class TestDirtyRects:
    """B5 修复：多矩形脏区不退化为单外接矩形（smoke 的 453× 膨胀场景）。"""

    def test_two_clusters_two_rects(self) -> None:
        bs = 32
        rows, cols = 68, 120
        a = np.zeros((rows * bs, cols * bs, 4), np.uint8)
        b = a.copy()
        # 左上 6x3 块 + 右下 4x3 块（§2.8 场景）
        b[0 : 3 * bs, 0 : 6 * bs] = 255
        b[(rows - 3) * bs : rows * bs, (cols - 4) * bs : cols * bs] = 255
        differ = ScreenDiffer()
        differ.compare(a)
        res = differ.compare(b)
        differ.close()
        assert len(res.dirty_rects) == 2, f"应 2 个连通域，实得 {len(res.dirty_rects)}"
        total_blocks = sum(r.block_count for r in res.dirty_rects)
        assert total_blocks == 18 + 12  # 6*3 + 4*3
        # 多矩形面积 ≪ 外接矩形（453× 膨胀不再发生）
        assert res.dirty_area < (res.bounding_box[2] * res.bounding_box[3]) / 10

    def test_rect_pixels_cover_changed_blocks(self) -> None:
        bs = 32
        a = np.zeros((bs * 10, bs * 10, 4), np.uint8)
        b = a.copy()
        b[bs * 2 : bs * 4, bs * 5 : bs * 8] = 9  # 一块 3x2 矩形
        differ = ScreenDiffer()
        differ.compare(a)
        res = differ.compare(b)
        differ.close()
        assert len(res.dirty_rects) == 1
        rect = res.dirty_rects[0]
        assert rect.box == (5 * bs, 2 * bs, 8 * bs, 4 * bs)

    def test_overflow_merges_tail(self) -> None:
        """超过 max_dirty_rects 的连通域合并为一个尾部矩形。"""
        bs = 16
        a = np.zeros((bs * 20, bs * 20, 4), np.uint8)
        b = a.copy()
        spots = [(r, c) for r in range(1, 19, 5) for c in range(1, 19, 5)]  # 9 点 < 15% 阈值
        assert len(spots) * 1 < a.shape[0] // bs * (a.shape[1] // bs) * 0.15
        for r, c in spots:
            b[r * bs : (r + 1) * bs, c * bs : (c + 1) * bs] = 200
        differ = ScreenDiffer(block_size=bs, max_dirty_rects=4)  # 块与绘制步长对齐
        differ.compare(a)
        res = differ.compare(b)
        differ.close()
        assert not res.full_frame
        assert len(res.dirty_rects) == 4  # 3 头部 + 1 合并尾
        assert sum(r.block_count for r in res.dirty_rects) == len(spots)

    def test_full_frame_flag_suppresses_rects(self) -> None:
        a = np.zeros((640, 640, 4), np.uint8)
        b = np.full_like(a, 1)
        differ = ScreenDiffer(change_threshold=0.15)
        differ.compare(a)
        res = differ.compare(b)
        assert res.full_frame and res.dirty_rects == ()
        differ.close()


class TestConstructorValidation:
    """构造期参数校验。"""

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"block_size": 0}, "block_size"),
            ({"change_threshold": 0.0}, "change_threshold"),
            ({"change_threshold": 1.5}, "change_threshold"),
            ({"max_dirty_rects": 0}, "max_dirty_rects"),
            ({"connectivity": 5}, "connectivity"),
            ({"workers": -1}, "workers"),
        ],
    )
    def test_bad_args(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            ScreenDiffer(**kwargs)


class TestLifecycleAndStats:
    """close/stats/v4 兼容面（smoke 最后两项）。"""

    def test_close_then_raise(self) -> None:
        differ = ScreenDiffer()
        differ.close()
        differ.close()  # 幂等
        with pytest.raises(RuntimeError, match="已关闭"):
            differ.compare(np.zeros((64, 64, 4), np.uint8))

    def test_stats_counts_42_frames(self) -> None:
        src = np.zeros((96, 96, 4), np.uint8)
        differ = ScreenDiffer(workers=8)
        differ.compare(src)
        for i in range(41):
            frame = src.copy()
            frame[i % 96, (i * 7) % 96, 0] ^= 0xFF
            differ.compare(frame)
        st = differ.get_stats()
        assert st["frames"] == 42
        # 带数 = min(workers=8, 块行数=3)：按行切带不可能多于行数
        assert st["workers"] == 3
        assert st["total_blocks"] == 9  # (96/32)^2
        assert st["block_size"] == 32
        assert st["adaptive_enabled"] is False  # B4：不再自适应
        assert st["max_ms"] >= st["avg_ms"] >= 0
        differ.close()

    def test_reset_makes_next_first(self) -> None:
        differ = ScreenDiffer()
        differ.compare(np.zeros((64, 64, 4), np.uint8))
        differ.reset()
        assert differ.compare(np.zeros((64, 64, 4), np.uint8)).is_first

    def test_v4_get_changed_region_crops(self) -> None:
        from PIL import Image

        img = Image.new("RGB", (128, 128), (0, 0, 0))
        differ = ScreenDiffer()
        cropped, offset = differ.get_changed_region(img, [(1, 1), (2, 3)])
        assert offset == (32, 32)
        # bx∈{1,2} by∈{1,3} → 像素 x∈[32,96) y∈[32,128)
        assert cropped.size == (64, 96)
        same, no_offset = differ.get_changed_region(img, [])
        assert same is img and no_offset == (0, 0)
        differ.close()

    def test_set_adaptive_is_noop(self) -> None:
        differ = ScreenDiffer(block_size=24)
        differ.set_adaptive(True)
        assert differ.block_size == 24  # B4：不可变
        differ.close()

    @pytest.mark.load
    def test_4k_diff_under_5ms(self) -> None:
        """验收项：4K 生产路径（双缓冲+adopt=True）<5ms（bench 脚本正式复测）。"""
        a = content_frame(2160, 3840, seed=21)
        b = content_frame(2160, 3840, seed=22)
        differ = ScreenDiffer()
        differ.compare(a)
        times = [differ.compare(b, adopt=True).elapsed_ms for _ in range(20)]
        differ.close()
        med = float(np.median(times))
        assert med < 5.0, f"4K 差分中位 {med:.2f}ms 超线"

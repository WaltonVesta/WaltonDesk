"""JPEG 编码器（rdc.encoding.jpeg）适配测试（决策 A：构造注入 + 双签名兼容）。"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from rdc.encoding.bandwidth import BandwidthProbe
from rdc.encoding.jpeg import DEFAULT_JPEG_QUALITY, JPEGEncoder


def make_pil(w: int = 64, h: int = 48, color: tuple[int, int, int] = (10, 120, 200)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def make_bgra(w: int = 64, h: int = 48, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[:, :, :3] = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    frame[:, :, 3] = 255
    return frame


class TestConstructionInjection:
    """决策 A：参数注入 + 默认值（默认值 = v4 生产配置）。"""

    def test_defaults_match_v4_config(self) -> None:
        enc = JPEGEncoder()  # service.py 的裸构造必须照常工作
        assert enc.current_quality == DEFAULT_JPEG_QUALITY == 60
        assert (enc.min_quality, enc.max_quality) == (30, 95)

    def test_injected_values(self) -> None:
        enc = JPEGEncoder(quality=80, min_quality=40, max_quality=88, bandwidth_limit=500)
        assert enc.current_quality == 80
        assert enc.min_quality == 40 and enc.max_quality == 88
        assert enc.get_stats()["bandwidth_limit"] == 500

    def test_quality_clamped_at_init(self) -> None:
        assert JPEGEncoder(quality=5).current_quality == 30  # 钳到 min
        assert JPEGEncoder(quality=99).current_quality == 95  # 钳到 max

    def test_bad_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="质量区间"):
            JPEGEncoder(min_quality=90, max_quality=30)
        with pytest.raises(ValueError, match="质量区间"):
            JPEGEncoder(max_quality=101)

    def test_no_v4_config_import(self) -> None:
        """AST 级锁定：模块不得再 import rdc.config._v4_config。"""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(__import__("rdc.encoding.jpeg", fromlist=["x"])))
        for node in ast.walk(tree):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            assert not any("_v4_config" in (m or "") for m in [*(mod or ""), *names])


class TestV4Surface:
    """v4 调用面（service.py 依赖）：encode tuple 与调参方法。"""

    def test_encode_tuple(self) -> None:
        enc = JPEGEncoder()
        data, quality, fmt = enc.encode(make_pil())
        assert isinstance(data, bytes) and data[:2] == b"\xff\xd8"  # JPEG SOI 魔数
        assert quality == 60 and fmt == "jpeg"

    def test_encode_explicit_quality(self) -> None:
        enc = JPEGEncoder()
        data, quality, _ = enc.encode(make_pil(), 90)
        assert quality == 90
        assert len(data) > len(enc.encode(make_pil(), 30)[0])

    def test_adjust_quality_fps_drop_lowers(self) -> None:
        enc = JPEGEncoder(quality=60)
        q = enc.adjust_quality(target_size=50_000, current_fps=10, target_fps=30)
        assert q < 60

    def test_adjust_quality_bandwidth_cap(self) -> None:
        enc = JPEGEncoder(quality=80)
        for _ in range(5):
            enc.encode(make_pil())  # 建立 avg_size 样本
        # 带宽 1KB/s、fps 30 → 远超预算 → 必须降
        q = enc.adjust_quality(target_size=1000, current_fps=30, target_fps=30, bandwidth=1024)
        assert q <= enc.current_quality

    def test_adjust_quality_by_bandwidth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        enc = JPEGEncoder(quality=80)
        for _ in range(3):
            enc.encode(make_pil())
        probe = BandwidthProbe()
        # 无样本（带宽 0）→ 不动
        assert enc.adjust_quality_by_bandwidth(probe, 30) == 80
        # target_fps<=0 防御分支
        monkeypatch.setattr(probe, "get_bandwidth", lambda: 1_000_000)
        assert enc.adjust_quality_by_bandwidth(probe, 0) == 80
        # 充裕分支（avg 未建立 → 不进比例计算）
        fresh = JPEGEncoder(quality=60)
        monkeypatch.setattr(fresh, "_stats", {**fresh._stats, "avg_size": 0})  # noqa: SLF001
        assert fresh.adjust_quality_by_bandwidth(probe, 30) == 60
        # 预算充裕 +2 分支：avg 远小于 bandwidth/fps
        tight = JPEGEncoder(quality=60)
        monkeypatch.setattr(tight, "_stats", {**tight._stats, "avg_size": 100.0})  # noqa: SLF001
        monkeypatch.setattr(probe, "get_bandwidth", lambda: 3_000_000)  # 100KB/帧预算
        assert tight.adjust_quality_by_bandwidth(probe, 30) == 62

    def test_adjust_quality_by_bandwidth_squeeze(self) -> None:
        """预算远低于当前帧均值 → 必须降质量（注入均值，免依赖 JPEG 实际尺寸）。"""
        enc = JPEGEncoder(quality=80)
        enc._stats["avg_size"] = 100_000  # noqa: SLF001 - 白盒：分支逻辑测试
        q = enc.adjust_quality(
            target_size=10_000, current_fps=30, target_fps=30, bandwidth=30_000
        )  # 30KB/s ÷ 30fps = 1KB/帧预算 vs 100KB 均值 → 大幅超支
        assert q < 80

    def test_adjust_quality_fps_headroom_raises(self) -> None:
        """帧率超额达标 + 包远小于目标 → 回升分支。"""
        enc = JPEGEncoder(quality=50)
        enc._stats["avg_size"] = 100  # noqa: SLF001
        q = enc.adjust_quality(target_size=500_000, current_fps=40, target_fps=30)
        assert q > 50

    def test_adjust_quality_smooths_history(self) -> None:
        """连调多次后移动平均生效（历史 ≥3 取均值分支），结果恒在区间内。"""
        enc = JPEGEncoder(quality=60)
        enc._stats["avg_size"] = 1_000  # noqa: SLF001
        for fps in (10, 10, 10, 45):
            enc.adjust_quality(target_size=10, current_fps=fps, target_fps=30)
        assert enc.min_quality <= enc.current_quality <= enc.max_quality

    def test_set_and_get_stats(self) -> None:
        enc = JPEGEncoder()
        enc.set_quality(70)
        assert enc.current_quality == 70
        enc.encode(make_pil())
        st = enc.get_stats()
        assert st["current_quality"] == 70
        assert st["total_frames"] == 1 and st["total_bytes"] > 0
        assert st["avg_size"] > 0 and st["sample_count"] == 1
        assert st["quality_history_len"] >= 1

    def test_set_bandwidth_limit(self) -> None:
        enc = JPEGEncoder()
        enc.set_bandwidth_limit(200)
        assert enc.get_stats()["bandwidth_limit"] == 200
        enc.set_bandwidth_limit(-1)
        assert enc.get_stats()["bandwidth_limit"] == 0


class TestProtocolSurface:
    """v5 VideoEncoder 协议面（契约之外的 JPEG 特有断言）。"""

    def test_ndarray_encode_returns_frame(self) -> None:
        enc = JPEGEncoder()
        out = enc.encode(make_bgra())
        assert out.is_keyframe and (out.width, out.height) == (64, 48)
        assert out.data[:2] == b"\xff\xd8"
        assert out.quality == 60
        enc.close()

    def test_bgr_three_channel_accepted(self) -> None:
        enc = JPEGEncoder()
        out = enc.encode(np.zeros((16, 16, 3), np.uint8))
        assert out.nbytes > 0
        enc.close()

    def test_bgra_channel_order_preserved(self) -> None:
        """BGRA(蓝=255) 编出的 JPEG 解码回 RGB 仍应是纯蓝（通道序坑 §2.6 坑 2）。"""
        import io

        enc = JPEGEncoder(quality=95)
        frame = np.zeros((32, 32, 4), np.uint8)
        frame[:, :, 0] = 255  # BGRA 的 B 通道拉满 = 纯蓝
        frame[:, :, 3] = 255
        data = enc.encode(frame).data
        img = Image.open(io.BytesIO(data)).convert("RGB")
        arr = np.asarray(img)
        # RGB 布局：channel0=R 应≈0，channel2=B 应≈255
        assert arr[:, :, 2].mean() > 200, "蓝色被错道：疑似 BGR 当 RGB 处理"
        assert arr[:, :, 0].mean() < 60
        enc.close()

    def test_rejects_bad_ndarray(self) -> None:
        enc = JPEGEncoder()
        with pytest.raises(ValueError):
            enc.encode(np.zeros((8, 8, 2), np.uint8))
        with pytest.raises(ValueError):
            enc.encode(np.zeros((8, 8, 4), np.int16))
        with pytest.raises(ValueError):  # 非 ndarray 非 PIL
            enc.encode("not-an-image")  # type: ignore[arg-type]
        enc.close()

    def test_negative_pts_rejected(self) -> None:
        enc = JPEGEncoder()
        with pytest.raises(ValueError, match="pts"):
            enc.encode(make_bgra(), pts=-5)
        enc.close()

    def test_set_resolution_records(self) -> None:
        enc = JPEGEncoder()
        enc.set_resolution(128, 72)
        assert enc._declared_size == (128, 72)  # noqa: SLF001
        with pytest.raises(ValueError, match="分辨率非法"):
            enc.set_resolution(0, 5)
        enc.close()

    def test_bandwidth_limit_branch(self) -> None:
        """内置带宽限制生效路径（不传 bandwidth 形参）。"""
        enc = JPEGEncoder(quality=80, bandwidth_limit=1)  # 1KB/s 极紧
        enc._stats["avg_size"] = 50_000  # noqa: SLF001
        q = enc.adjust_quality(target_size=10_000, current_fps=30, target_fps=30)
        assert q < 80

    def test_close_blocks_encode(self) -> None:
        enc = JPEGEncoder()
        enc.close()
        with pytest.raises(RuntimeError, match="已关闭"):
            enc.encode(make_bgra())
        with pytest.raises(RuntimeError, match="已关闭"):
            enc.encode(make_pil())

    def test_bandwidth_probe_reexport(self) -> None:
        """v4 import 面：from rdc.encoding.jpeg import BandwidthProbe 仍有效。"""
        from rdc.encoding import jpeg

        assert jpeg.BandwidthProbe is BandwidthProbe

"""VideoEncoder 契约测试（阶段 5 叮嘱 e）。

同一套契约断言跑所有 :class:`~rdc.encoding.base.VideoEncoder` 实现
（H264Encoder / JPEGEncoder），保证广播层可以无差别替换后端。

基类命名 ``EncoderContractBase``（不带 Test 前缀）避免被 pytest 单独收集；
真正的用例子类是文件末尾的 ``TestH264Contract`` 与 ``TestJPEGContract``。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from rdc.encoding.base import EncodedFrame, EncoderStats, VideoEncoder
from rdc.protocol.messages import Codec


def make_bgra(width: int = 64, height: int = 48, seed: int = 0) -> np.ndarray:
    """确定性的合成 BGRA 帧（内容随 seed 变化，模拟画面更新）。"""
    rng = np.random.default_rng(seed)
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[:, :, :3] = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    frame[:, :, 3] = 255
    return frame


class EncoderContractBase:
    """所有 VideoEncoder 实现必须通过的契约。

    子类提供 ``make_encoder()`` 工厂；如某断言对特定 codec 不适用，
    覆写对应测试并在 docstring 说明理由（禁止无脑 skip）。
    """

    def make_encoder(self) -> Any:
        raise NotImplementedError

    # -- 结构契约 --------------------------------------------------------------

    def test_satisfies_protocol(self) -> None:
        enc = self.make_encoder()
        assert isinstance(enc, VideoEncoder)

    def test_codec_identity(self) -> None:
        enc = self.make_encoder()
        assert isinstance(enc.codec, Codec)
        enc.close()

    # -- encode 契约 -------------------------------------------------------------

    def test_encode_returns_encoded_frame(self) -> None:
        enc = self.make_encoder()
        out = enc.encode(make_bgra())
        assert isinstance(out, EncodedFrame)
        assert out.data  # 单帧编码器（JPEG）与 zerolatency H.264 都必须出字节
        assert out.width == 64 and out.height == 48
        enc.close()

    def test_encode_pts_monotonic(self) -> None:
        enc = self.make_encoder()
        pts = [enc.encode(make_bgra(seed=i)).pts for i in range(5)]
        assert pts == sorted(pts)
        assert len(set(pts)) == 5
        enc.close()

    def test_encode_explicit_pts(self) -> None:
        enc = self.make_encoder()
        out = enc.encode(make_bgra(), pts=1000)
        assert out.pts == 1000
        enc.close()

    def test_encode_rejects_bad_shape(self) -> None:
        enc = self.make_encoder()
        with pytest.raises(ValueError):
            enc.encode(np.zeros((48, 64, 3), dtype=np.float32))
        enc.close()

    def test_first_frame_is_keyframe(self) -> None:
        enc = self.make_encoder()
        assert enc.encode(make_bgra()).is_keyframe
        enc.close()

    def test_force_keyframe(self) -> None:
        enc = self.make_encoder()
        enc.encode(make_bgra(seed=1))
        enc.encode(make_bgra(seed=2))
        out = enc.encode(make_bgra(seed=3), force_keyframe=True)
        assert out.is_keyframe
        enc.close()

    def test_request_keyframe(self) -> None:
        enc = self.make_encoder()
        enc.encode(make_bgra(seed=1))
        enc.request_keyframe()
        assert enc.encode(make_bgra(seed=2)).is_keyframe
        enc.close()

    # -- 质量契约 ----------------------------------------------------------------

    def test_set_quality_clamps_not_raises(self) -> None:
        # rater 高频调用：越界必须钳制而不是抛错
        enc = self.make_encoder()
        enc.set_quality(-100)
        enc.set_quality(10_000)
        out = enc.encode(make_bgra())
        assert isinstance(out, EncodedFrame)
        enc.close()

    def test_stats_quality_tracks_set(self) -> None:
        # 40 同时落在 H264 [18,45] 与 JPEG [30,95]，两实现都无钳制损耗
        enc = self.make_encoder()
        enc.set_quality(40)
        assert enc.stats().quality == 40
        enc.close()

    # -- 统计契约 ----------------------------------------------------------------

    def test_stats_accumulate(self) -> None:
        enc = self.make_encoder()
        for i in range(3):
            enc.encode(make_bgra(seed=i))
        st = enc.stats()
        assert isinstance(st, EncoderStats)
        assert st.frames == 3
        assert st.total_bytes > 0
        assert st.avg_bytes > 0
        assert st.bitrate_bps(30.0) > 0
        enc.close()

    # -- 生命周期契约 --------------------------------------------------------------

    def test_close_idempotent(self) -> None:
        enc = self.make_encoder()
        enc.encode(make_bgra())
        enc.close()
        enc.close()

    def test_encode_after_close_raises(self) -> None:
        enc = self.make_encoder()
        enc.close()
        with pytest.raises(RuntimeError):
            enc.encode(make_bgra())

    def test_set_resolution_rebuild(self) -> None:
        enc = self.make_encoder()
        enc.encode(make_bgra())
        enc.set_resolution(32, 24)
        out = enc.encode(make_bgra(32, 24))
        assert (out.width, out.height) == (32, 24)
        enc.close()

    def test_set_resolution_rejects_bad(self) -> None:
        enc = self.make_encoder()
        with pytest.raises(ValueError):
            enc.set_resolution(0, 10)
        enc.close()


class TestH264Contract(EncoderContractBase):
    """libx264 实现过全部契约。"""

    def make_encoder(self) -> Any:
        from rdc.encoding.h264 import H264Encoder

        return H264Encoder(64, 48)

    def test_codec_is_h264(self) -> None:
        enc = self.make_encoder()
        assert enc.codec is Codec.H264
        enc.close()


class TestJPEGContract(EncoderContractBase):
    """JPEG 回退路径实现过全部契约。"""

    def make_encoder(self) -> Any:
        from rdc.encoding.jpeg import JPEGEncoder

        return JPEGEncoder()

    def test_codec_is_jpeg(self) -> None:
        enc = self.make_encoder()
        assert enc.codec is Codec.JPEG
        enc.close()

    def test_every_frame_keyframe(self) -> None:
        # JPEG 无帧间依赖：逐帧皆关键帧（H.264 不适用此断言，故不放基类）
        enc = self.make_encoder()
        assert all(enc.encode(make_bgra(seed=i)).is_keyframe for i in range(4))
        enc.close()

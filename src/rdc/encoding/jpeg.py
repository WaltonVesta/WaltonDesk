"""JPEG 编码器（v4 兼容/回退路径；协议化适配见阶段 5）。

本类同时承载两个调用面（Q3 决策：v4 兼容路径继续走 JPEG）：

1. **v4 面**（``server/service.py`` 广播循环，阶段 9 随 v4 运行时删除）：
   ``encode(pil_img) -> (bytes, quality, 'jpeg')`` 及 ``adjust_quality`` /
   ``adjust_quality_by_bandwidth`` / ``set_quality`` / ``get_stats``；
2. **v5 :class:`~rdc.encoding.base.VideoEncoder` 协议面**（回退路径/契约测试）：
   ``encode(bgra_ndarray, force_keyframe=…, pts=…) -> EncodedFrame``。

:meth:`encode` 用重载按入参类型分派——PIL 输入走 v4 语义，ndarray 输入走协议
语义。JPEG 逐帧独立：``is_keyframe`` 恒真、``request_keyframe`` 空操作。

阶段 5 决策 A（配置注入）：v4 在本类构造时读 ``Config()`` 单例（
``rdc.config._v4_config``，未类型化），导致不可测且 mypy 无法 strict。
v5 改为**构造参数注入 + 默认值**（``quality=60, min_quality=30,
max_quality=95, bandwidth_limit=0``，默认值即 v4 生产配置的实测值），
彻底移除对 v4 配置单例的依赖——``service.py`` 现有的裸 ``JPEGEncoder()``
构造无需任何改动。

带宽探测已迁至 :mod:`rdc.encoding.bandwidth`（任务 9），本模块 re-export
``BandwidthProbe`` 保持 ``from rdc.encoding.jpeg import BandwidthProbe``
的 v4 兼容 import 面（service.py 仍在使用，阶段 9 删除）。
"""

from __future__ import annotations

import io
import time
from collections import deque
from typing import Any, Final, overload

import numpy as np
import numpy.typing as npt
from PIL import Image

from rdc.encoding.bandwidth import BandwidthProbe
from rdc.encoding.base import EncodedFrame, EncoderStats, VideoEncoder
from rdc.logging import get_logger
from rdc.protocol.messages import Codec

# 说明：v4 此处原有 _check_h264_available() / HAS_H264 / encode_h264()，
# 均为无效代码——cv2.imencode('.h264') 不是合法用法，且 HAS_H264 仅凭创建
# fourcc 整数就返回 True，毫无检测意义（见 ASSESSMENT.md §5.2 B1）。
# 已按 REFACTOR_PLAN §2.1 删除，原件保留于 legacy_v4/screen/encoder.py。
# v5 的 H.264 路径在 encoding/h264.py（PyAV libx264）。

__all__ = [
    "BandwidthProbe",  # re-export：v4 兼容 import 面（阶段 9 删除）
    "DEFAULT_JPEG_QUALITY",
    "JPEGEncoder",
]

DEFAULT_JPEG_QUALITY: Final = 60


class JPEGEncoder(VideoEncoder):
    """JPEG 编码器（动态质量 + 带宽限制 + 质量平滑）。

    Args:
        quality: 初始质量（``[min_quality, max_quality]`` 内）。
        min_quality: 动态调整下限。
        max_quality: 动态调整上限。
        bandwidth_limit: 带宽上限（KB/s），``0`` = 不限制。
    """

    def __init__(
        self,
        *,
        quality: int = DEFAULT_JPEG_QUALITY,
        min_quality: int = 30,
        max_quality: int = 95,
        bandwidth_limit: int = 0,
    ) -> None:
        if not 1 <= min_quality <= max_quality <= 100:
            raise ValueError(
                f"质量区间非法: [{min_quality}, {max_quality}]（须 1≤min≤max≤100）"
            )
        self.min_quality = min_quality
        self.max_quality = max_quality
        self.current_quality = self._clamp(quality)

        # 带宽限制（KB/s → B/s）
        self._bandwidth_limit = max(0, bandwidth_limit)
        self._bandwidth_limit_bytes = self._bandwidth_limit * 1024 if self._bandwidth_limit > 0 else 0

        # 质量平滑历史
        self._quality_history: deque[int] = deque(maxlen=30)
        self._size_history: deque[int] = deque(maxlen=30)

        self.logger = get_logger("encoding.jpeg")
        # avg_size 为浮点均值，其余为整型计数；统一放宽为 float 以容纳两者。
        self._stats: dict[str, float] = {
            "avg_size": 0.0,
            "sample_count": 0,
            "last_size": 0,
            "total_bytes": 0,
            "total_frames": 0,
        }
        # v5 协议面状态
        self._pts = 0
        self._declared_size: tuple[int, int] | None = None
        self._closed = False

    # -- 协议：codec 标识 -------------------------------------------------------

    @property
    def codec(self) -> Codec:
        """:attr:`Codec.JPEG`。"""
        return Codec.JPEG

    # -- 双签名 encode（v4 面 / v5 协议面按入参类型分派）------------------------

    @overload
    def encode(
        self, image: Image.Image, quality: int | None = ..., /
    ) -> tuple[bytes, int, str]: ...

    @overload
    def encode(
        self, image: npt.NDArray[np.uint8], /, *, force_keyframe: bool = ..., pts: int | None = ...
    ) -> EncodedFrame: ...

    def encode(self, image: Any, quality: int | None = None, /, **kw: Any) -> Any:
        """编码一帧（分派语义见模块 docstring）。

        v4 面：``encode(pil_img, quality=None) -> (data, quality, 'jpeg')``。

        v5 协议面：``encode(bgra_ndarray, *, force_keyframe=False, pts=None)
        -> EncodedFrame``；``force_keyframe`` 被忽略（JPEG 逐帧独立），
        返回值 ``is_keyframe`` 恒真。

        Raises:
            RuntimeError: 编码器已 :meth:`close`。
            ValueError: ndarray 形状/dtype 非法。
        """
        if self._closed:
            raise RuntimeError("JPEGEncoder 已关闭")
        if isinstance(image, Image.Image):
            q = self.current_quality if quality is None else quality
            data, used_q = self._encode_jpeg(image, q)
            return data, used_q, "jpeg"
        return self._encode_protocol(image, **kw)

    def _encode_protocol(
        self,
        frame: npt.NDArray[np.uint8],
        *,
        force_keyframe: bool = False,
        pts: int | None = None,
    ) -> EncodedFrame:
        """协议面实现：BGRA ndarray → PIL → JPEG → EncodedFrame。"""
        if not isinstance(frame, np.ndarray):
            raise ValueError(f"协议面需要 (H, W, 4) uint8 ndarray，实得 {type(frame).__name__}")
        if frame.ndim != 3 or frame.shape[2] not in (3, 4) or frame.dtype != np.uint8:
            raise ValueError(f"协议面需要 (H, W, 3|4) uint8，实得 {frame.shape} {frame.dtype}")
        del force_keyframe  # JPEG 无帧间依赖，天然全关键帧
        t0 = time.perf_counter()
        height, width = int(frame.shape[0]), int(frame.shape[1])
        # BGRA→RGB：直接通道切片重排，不整幅 copy 两次
        rgb = np.ascontiguousarray(frame[:, :, :3][:, :, ::-1])
        img = Image.frombytes("RGB", (width, height), rgb.tobytes())
        data, used_q = self._encode_jpeg(img, self.current_quality)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        out_pts = self._pts if pts is None else pts
        if out_pts < 0:
            raise ValueError(f"pts 不能为负: {pts}")
        self._pts = out_pts + 1
        return EncodedFrame(
            data=data,
            is_keyframe=True,
            pts=out_pts,
            width=width,
            height=height,
            quality=used_q,
            elapsed_ms=elapsed_ms,
        )

    # -- 编码核心 ---------------------------------------------------------------

    def _encode_jpeg(self, img: Image.Image, quality: int) -> tuple[bytes, int]:
        """编码图像为 JPEG（v4 原逻辑，含统计与历史维护）。"""
        quality = self._clamp(quality)
        buffer = io.BytesIO()
        try:
            # 优化参数与 v4 完全一致（bench 的 JPEG 基线口径）
            img.save(
                buffer, format="JPEG", quality=quality, optimize=True, subsampling=0, progressive=False
            )
        finally:
            data = buffer.getvalue()
            buffer.close()

        self._stats["last_size"] = len(data)
        self._stats["sample_count"] += 1
        self._stats["total_frames"] += 1
        self._stats["total_bytes"] += len(data)
        self._stats["avg_size"] = (
            self._stats["avg_size"] * (self._stats["sample_count"] - 1) + len(data)
        ) / self._stats["sample_count"]
        self._quality_history.append(quality)
        self._size_history.append(len(data))
        return data, quality

    # -- v4 动态调参面（行为与 v4 一致）-----------------------------------------

    def adjust_quality(
        self, target_size: int, current_fps: float, target_fps: float, bandwidth: float = 0
    ) -> int:
        """根据目标包大小与帧率调整质量（v4 语义）。

        Args:
            target_size: 目标包大小（字节）。
            current_fps: 当前帧率。
            target_fps: 目标帧率。
            bandwidth: 可用带宽（字节/秒），0 表示用内置限制。

        Returns:
            调整后的质量。
        """
        # 1. 帧率调整
        if current_fps < target_fps * 0.7:
            self.current_quality = max(self.min_quality, self.current_quality - 5)
        elif current_fps > target_fps * 1.1 and self._stats["avg_size"] < target_size * 0.8:
            self.current_quality = min(self.max_quality, self.current_quality + 3)

        # 2. 带宽限制
        effective_bandwidth = bandwidth if bandwidth > 0 else self._bandwidth_limit_bytes
        if effective_bandwidth > 0 and self._stats["avg_size"] > 0:
            estimated_bps = self._stats["avg_size"] * current_fps
            if estimated_bps > effective_bandwidth:
                reduction_ratio = estimated_bps / effective_bandwidth
                new_quality = int(self.current_quality / reduction_ratio)
                self.current_quality = max(self.min_quality, new_quality)
            elif estimated_bps < effective_bandwidth * 0.5 and current_fps >= target_fps:
                if self.current_quality < self.max_quality:
                    self.current_quality = min(self.max_quality, self.current_quality + 2)

        # 3. 包大小微调
        if self._stats["avg_size"] > target_size * 1.2:
            self.current_quality = max(self.min_quality, self.current_quality - 2)
        elif self._stats["avg_size"] < target_size * 0.5 and current_fps >= target_fps:
            self.current_quality = min(self.max_quality, self.current_quality + 2)

        # 4. 移动平均平滑
        self._quality_history.append(self.current_quality)
        if len(self._quality_history) >= 3:
            avg_quality = sum(self._quality_history) / len(self._quality_history)
            self.current_quality = int(avg_quality)

        self.current_quality = self._clamp(self.current_quality)
        return self.current_quality

    def adjust_quality_by_bandwidth(self, probe: BandwidthProbe, target_fps: float) -> int:
        """根据带宽探测结果调整质量（v4 语义）。

        Args:
            probe: 带宽探测器。
            target_fps: 目标帧率。

        Returns:
            调整后的质量。
        """
        bandwidth = probe.get_bandwidth()
        if bandwidth <= 0 or target_fps <= 0:
            return self.current_quality
        target_frame_size = bandwidth / target_fps
        if self._stats["avg_size"] > 0:
            ratio = target_frame_size / self._stats["avg_size"]
            if ratio < 0.8:
                self.current_quality = max(self.min_quality, int(self.current_quality * ratio))
            elif ratio > 1.2 and self.current_quality < self.max_quality:
                self.current_quality = min(self.max_quality, self.current_quality + 2)
        return self.current_quality

    # -- 协议 / v4 共存的质量与统计面 ---------------------------------------------

    def set_quality(self, quality: int) -> None:
        """设置当前质量（钳制到区间）。

        v4 在此还会 ``config.set('jpeg_quality', …)`` 持久化；决策 A 移除
        单例依赖后不再写配置——会话期调整即内存态，跨会话偏好由阶段 6 的
        Settings 层负责。
        """
        self.current_quality = self._clamp(quality)
        self.logger.info("JPEG质量设置为", quality=self.current_quality)

    def request_keyframe(self) -> None:
        """空操作（JPEG 逐帧独立，每帧都是完整帧）。"""
        return None

    def set_resolution(self, width: int, height: int) -> None:
        """记录声明分辨率（JPEG 本身不缩放，缩放责任在采集/广播侧）。"""
        if width <= 0 or height <= 0:
            raise ValueError(f"分辨率非法: {width}x{height}")
        self._declared_size = (width, height)
        self.logger.debug("JPEG 声明分辨率更新", width=width, height=height)

    def set_bandwidth_limit(self, limit_kbps: int) -> None:
        """设置带宽限制（KB/s，v4 语义）。"""
        self._bandwidth_limit = max(0, limit_kbps)
        self._bandwidth_limit_bytes = self._bandwidth_limit * 1024 if self._bandwidth_limit > 0 else 0
        self.logger.info("带宽限制设置为", limit_kbps=self._bandwidth_limit)

    def get_stats(self) -> dict[str, float | int]:
        """v4 统计面（dict），语义与 v4 一致。"""
        avg_quality = (
            sum(self._quality_history) / len(self._quality_history)
            if self._quality_history
            else self.current_quality
        )
        return {
            "current_quality": self.current_quality,
            "avg_quality": avg_quality,
            "avg_size": self._stats["avg_size"],
            "last_size": self._stats["last_size"],
            "sample_count": self._stats["sample_count"],
            "total_frames": self._stats["total_frames"],
            "total_bytes": self._stats["total_bytes"],
            "bandwidth_limit": self._bandwidth_limit,
            "quality_history_len": len(self._quality_history),
        }

    def stats(self) -> EncoderStats:
        """v5 协议统计快照（与 :meth:`get_stats` 同源）。"""
        return EncoderStats(
            frames=int(self._stats["total_frames"]),
            keyframes=int(self._stats["total_frames"]),  # JPEG 全帧独立
            total_bytes=int(self._stats["total_bytes"]),
            last_ms=0.0,  # v4 路径不计时，协议消费方按均值口径读
            avg_ms=0.0,
            max_ms=0.0,
            quality=self.current_quality,
            extra={"avg_size": self._stats["avg_size"]},
        )

    def close(self) -> None:
        """标记关闭（幂等）；关闭后 :meth:`encode` 抛 ``RuntimeError``。"""
        self._closed = True

    # -- 内部 ---------------------------------------------------------------------

    def _clamp(self, quality: int) -> int:
        """把质量钳制到 ``[min_quality, max_quality]``。"""
        return max(self.min_quality, min(self.max_quality, int(quality)))

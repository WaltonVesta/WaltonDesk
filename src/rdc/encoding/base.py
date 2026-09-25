"""编码器协议与公共数据类型（v5 编码层唯一抽象）。

与 :class:`rdc.capture.source.ScreenSource` 同构的依赖倒置注入点：广播循环
（阶段 6）只依赖本协议，H.264（主路径）与 JPEG（v4 兼容/回退路径）是两个实现，
可注入、可 mock、可契约测试（契约测试基类见 ``tests/contract/``）。

协议方法的设计取舍
------------------
- :meth:`VideoEncoder.encode` 入参为 **BGRA ``(H, W, 4)`` uint8 数组**（与采集层
  :mod:`rdc.capture.source` 的像素格式约定一致），而非 PIL——省一次通道转换，
  且差分主路径本来全程 ndarray；
- ``quality`` 语义随编解码器而异（H.264 = CRF，JPEG = 质量分），协议只保证
  **"越大画质越好/码率越高"** 的单调方向，具体区间由实现定义并文档化；
- :meth:`VideoEncoder.request_keyframe` 与 ``encode(force_keyframe=True)`` 并存：
  前者供异步路径（客户端加入/解码错误上报后调用，下一帧生效），后者供同步
  路径（本次编码强制 I 帧）。H.264 两种都要，JPEG 天然逐帧独立、两者皆空操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from rdc.protocol.messages import Codec

__all__ = [
    "EncodedFrame",
    "EncoderStats",
    "VideoEncoder",
]


@runtime_checkable
class VideoEncoder(Protocol):
    """视频编码器协议。

    实现方约定：

    - :meth:`encode` 的 ``frame`` 为 ``(H, W, 4)`` uint8 BGRA、C 连续
      （采集层 :meth:`CaptureFrame.data <rdc.capture.source.CaptureFrame.data>`
      即满足此形状）；
    - :meth:`set_quality` 的入参按 codec 各自的**单调**语义解释，实现须自行
      钳制到合法区间，不得抛错（rater 高频调用）；
    - :meth:`set_resolution` 允许实现重建内部状态；重建后首帧**必须**是
      关键帧（否则解码端花屏），此责任归实现；
    - :meth:`close` 幂等；关闭后 :meth:`encode` 抛 ``RuntimeError``；
    - 所有方法**非线程安全**：一个编码实例对应一条推流管线。
    """

    @property
    def codec(self) -> Codec:
        """本编码器产出的 :class:`~rdc.protocol.messages.Codec` 标识。"""
        ...

    def encode(
        self,
        frame: npt.NDArray[np.uint8],
        *,
        force_keyframe: bool = False,
        pts: int | None = None,
    ) -> EncodedFrame:
        """编码一帧。

        Args:
            frame: ``(H, W, 4)`` uint8 BGRA。
            force_keyframe: 本帧强制编码为关键帧（客户端新加入/丢包恢复）。
            pts: 显示时间戳；``None`` 时由实现自增维护。

        Returns:
            :class:`EncodedFrame`。⚠️ 有状态编解码器（H.264）可能因前瞻缓冲
            返回**空 data**——实现应尽量避免（zerolatency），但调用方须容忍。

        Raises:
            RuntimeError: 编码器已关闭。
            ScreenCaptureError 之外的编码错误由实现定义（一般为内部异常包装）。
        """
        ...

    def request_keyframe(self) -> None:
        """请求下一帧编码为关键帧（异步路径：当前帧之后再无从属参考需求）。"""
        ...

    def set_quality(self, quality: int) -> None:
        """设置质量（H.264 = CRF 越小越好；JPEG = 值越大越好）。

        实现须钳制到自身合法区间。对**有状态**编码器，质量变化若需要重建
        编码器（libx264 的 crf 经 options 传入），重建后下一帧自动转关键帧。
        """
        ...

    def set_resolution(self, width: int, height: int) -> None:
        """切换输出分辨率（切显示器/缩放）。重建后首帧须为关键帧。"""
        ...

    def stats(self) -> EncoderStats:
        """编码性能统计快照。"""
        ...

    def close(self) -> None:
        """释放编码器资源（幂等；不做二次释放）。"""
        ...


@dataclass(frozen=True)
class EncodedFrame:
    """一次编码的产物（不可变值对象）。

    Attributes:
        data: 编码字节（H.264 为 Annex-B 裸流分片；JPEG 为完整图像）。
        is_keyframe: 是否关键帧（H.264: I 帧；JPEG 恒真）。
        pts: 显示时间戳（帧序号语义，单调不减）。
        width: 编码时实际分辨率宽（尺寸变化后与请求值可差一帧，供上层核对）。
        height: 高。
        quality: 实际生效的质量参数（CRF 或 JPEG 质量分）。
        elapsed_ms: 本次编码耗时（毫秒），供带宽/性能面板。
    """

    data: bytes
    is_keyframe: bool
    pts: int
    width: int
    height: int
    quality: int
    elapsed_ms: float

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"编码帧尺寸非法: {self.width}x{self.height}")
        if self.pts < 0:
            raise ValueError(f"pts 不能为负: {self.pts}")

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)``。"""
        return (self.width, self.height)

    @property
    def nbytes(self) -> int:
        """编码字节数。"""
        return len(self.data)


@dataclass(frozen=True)
class EncoderStats:
    """编码器统计快照（协议统一口径，便于 UI 与基准脚本通用消费）。

    Attributes:
        frames: 成功编码帧数。
        keyframes: 关键帧数（H.264 用于核对 GOP 生效）。
        total_bytes: 累计输出字节。
        last_ms / avg_ms / max_ms: 编码耗时（毫秒）。
        quality: 当前生效质量参数。
        extra: 编码器特有指标（如 JPEG 的历史窗口），协议不约束内容。
    """

    frames: int = 0
    keyframes: int = 0
    total_bytes: int = 0
    last_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    quality: int = 0
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def avg_bytes(self) -> float:
        """平均每帧输出字节（``frames`` 为 0 时取 0）。"""
        return self.total_bytes / self.frames if self.frames else 0.0

    def bitrate_bps(self, fps: float) -> float:
        """按给定帧率折算当前平均输出的码率（bps）。"""
        return self.avg_bytes * fps * 8.0

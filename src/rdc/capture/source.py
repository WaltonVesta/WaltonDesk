"""屏幕采集源抽象（依赖倒置注入点）。

v4 的 ``screen/capturer.py`` 把 mss 调用、PIL 转换、显示器选择、异常兜底
全部焊死在 ``ScreenCapturer`` 里（ASSESSMENT §5.7 判定为"🔴 高难测"：
写任何单测都要真实屏幕）。本模块定义与后端无关的 :class:`ScreenSource`
协议，使：

- ``capture/diff.py``（差分）与 ``encoding/h264.py``（编码）只依赖本协议，
  可用 :class:`ArrayScreenSource` 喂合成帧，**完全脱离真实屏幕**跑单测；
- 真实后端只有一个实现（:mod:`rdc.capture.mss_source`），换后端不改业务代码；
- 覆盖率目标 ≥85% 从"做不到"变成"自然达成"。

像素格式约定
------------
**全链路统一 BGRA（``uint8``, shape ``(H, W, 4)``, C 连续）**，理由（阶段 5 实测）：

1. mss 原生输出就是 BGRA，``np.frombuffer`` 零拷贝即可拿到数组；
2. 差分把 4 通道打包成 ``uint32`` 一次比较，通道轴折进比较里，
   省掉一次 ``(H, W, 3)`` 的通道归约；
3. 编码走 ``cv2.cvtColor(BGRA -> YUV_I420)``（4K 实测 1.8ms），比
   ``av.VideoFrame.from_ndarray(bgra)`` 的内部 swscale（4K 实测 23.6ms）快一个数量级。

v4 的 ``Image.frombytes('RGB', size, screenshot.rgb)`` 路径每帧多做一次
BGRA→RGB 重排 + PIL 对象构造，是纯浪费，v5 不再经过 PIL。

alpha 通道
----------
mss 在 Windows 上返回的 alpha 恒为 255（阶段 5 实测 ``unique=[255]``），
但**不保证**所有平台如此。差分默认屏蔽 alpha 字节，避免"仅 alpha 抖动"
被误判为画面变化而白白推流；见 :class:`~rdc.capture.diff.ScreenDiffer` 的
``ignore_alpha``。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from rdc.capture.monitor import MonitorInfo, MonitorSelector

__all__ = [
    "CaptureFrame",
    "ScreenSource",
    "ArrayScreenSource",
    "FrameStats",
    "BGRA_CHANNELS",
    "bgra_view",
]

#: BGRA 每像素通道数（全链路像素格式约定）。
BGRA_CHANNELS = 4

#: 全链路统一像素格式的 numpy dtype。
BGRA_DTYPE = np.dtype(np.uint8)


def bgra_view(buffer: bytes | bytearray | memoryview, width: int, height: int) -> npt.NDArray[np.uint8]:
    """把原始 BGRA 字节缓冲包装成 ``(H, W, 4)`` uint8 视图（**零拷贝**）。

    Args:
        buffer: 原始字节（如 ``mss.ScreenShot.bgra``）。
        width: 帧宽（像素）。
        height: 帧高（像素）。

    Returns:
        与 ``buffer`` 共享内存的只读视图。

    Raises:
        ValueError: 字节数与 ``width * height * 4`` 不符。

    Note:
        返回视图而非副本：mss 每次 ``grab`` 都新建缓冲，视图生命周期安全。
        若调用方会复用同一缓冲（如 mss 的内部复用），需自行 ``.copy()``。
    """
    expected = width * height * BGRA_CHANNELS
    actual = len(buffer)
    if actual != expected:
        raise ValueError(
            f"BGRA 缓冲长度不符: 期望 {expected} 字节 ({width}x{height}x4)，实得 {actual}"
        )
    return np.frombuffer(buffer, dtype=BGRA_DTYPE).reshape(height, width, BGRA_CHANNELS)


@dataclass(frozen=True)
class CaptureFrame:
    """一次采集得到的帧（不可变值对象）。

    Attributes:
        data: ``(H, W, 4)`` uint8 BGRA 数组，C 连续。**可能是后端缓冲的视图**，
            消费方若要长期持有必须自行拷贝（差分内部用双缓冲 swap 规避拷贝）。
        timestamp: epoch 秒（采集完成时刻）。
        monitor_index: 该帧来自哪台显示器（0 = 虚拟全屏）。
        seq: 采集序号，从 0 递增；用于诊断丢帧与对齐 :class:`~rdc.protocol.messages.ScreenFrame`。
        left: 帧在虚拟桌面坐标系中的原点 x（供输入坐标反算）。
        top: 帧在虚拟桌面坐标系中的原点 y。
    """

    data: npt.NDArray[np.uint8]
    timestamp: float
    monitor_index: int
    seq: int
    left: int = 0
    top: int = 0

    @property
    def width(self) -> int:
        """帧宽（像素）。"""
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        """帧高（像素）。"""
        return int(self.data.shape[0])

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)``。"""
        return (self.width, self.height)

    @property
    def nbytes(self) -> int:
        """原始帧字节数（未编码），用于带宽/内存诊断。"""
        return int(self.data.nbytes)


@dataclass(frozen=True)
class FrameStats:
    """采集性能统计快照（供 UI 性能面板与基准脚本消费）。

    Attributes:
        captured: 成功采集的帧数。
        failed: 采集失败次数（已抛错，此处仅计数用于诊断）。
        last_capture_ms: 最近一次 ``grab`` 耗时（毫秒）。
        avg_capture_ms: 滑动窗口平均采集耗时（毫秒）。
        max_capture_ms: 观测到的最大采集耗时（毫秒）。
    """

    captured: int = 0
    failed: int = 0
    last_capture_ms: float = 0.0
    avg_capture_ms: float = 0.0
    max_capture_ms: float = 0.0


@runtime_checkable
class ScreenSource(Protocol):
    """屏幕采集源协议（v5 采集层唯一抽象）。

    实现方约定：

    - :meth:`capture` 必须返回 **BGRA uint8 ``(H, W, 4)`` C 连续**数组；
    - :meth:`monitors` 返回物理显示器列表（``index`` 1 起），顺序即索引顺序；
    - 采集失败必须抛 :class:`~rdc.exceptions.ScreenCaptureError`，
      **不得**像 v4 那样返回占位尺寸或静默降级（v4 的
      ``get_screen_size`` 异常时返回硬编码 ``1920x1080``，会让上层拿着
      错误分辨率编码，见硬性约束 6）；
    - 实现必须可重入 :meth:`close`（幂等），且 ``close`` 后再 ``capture``
      抛错而非返回陈旧数据。

    Note:
        本协议是**同步**的。mss 的 ``grab`` 是阻塞 C 调用且释放 GIL，
        asyncio 侧用 ``await loop.run_in_executor(None, source.capture)``
        即可，无需把协议本身做成 async——同步协议才能被差分/编码的
        纯 CPU 路径直接复用。
    """

    def monitors(self) -> Sequence[MonitorInfo]:
        """枚举全部物理显示器（不含虚拟全屏）。

        Returns:
            ``index`` 从 1 起的显示器信息序列。无显示器时返回空序列
            （不抛错——headless 环境下上层需要能感知"没有屏"并给出提示）。
        """
        ...

    def capture(self) -> CaptureFrame:
        """采集当前选中显示器的一帧。

        Returns:
            :class:`CaptureFrame`，``data`` 为 BGRA uint8 ``(H, W, 4)``。

        Raises:
            rdc.exceptions.ScreenCaptureError: 后端不可用、grab 失败或返回尺寸异常。
        """
        ...

    def capture_into(self, destination: npt.NDArray[np.uint8]) -> CaptureFrame:
        """采集一帧并写入 ``destination``（**避免每帧分配 33MB**）。

        Args:
            destination: 预分配的 ``(H, W, 4)`` uint8 数组，尺寸须与当前
                显示器一致。

        Returns:
            ``data`` 即 ``destination`` 本身的 :class:`CaptureFrame`。

        Raises:
            ValueError: ``destination`` 尺寸/dtype 不匹配。
            rdc.exceptions.ScreenCaptureError: 同 :meth:`capture`。

        Note:
            4K 帧 33MB，30fps 下每帧新分配会产生 ~1GB/s 的分配压力。
            本方法让调用方复用缓冲；实现方可选择"grab 到新缓冲再 copy 进
            destination"（仍是 1 次拷贝，但避免了 GC 压力与内存峰值翻倍）。
        """
        ...

    def set_monitor(self, index: int) -> MonitorInfo:
        """切换采集目标显示器。

        Args:
            index: 目标索引（0 = 虚拟全屏，1 起 = 物理屏）。

        Returns:
            切换后的显示器信息。

        Raises:
            rdc.capture.monitor.MonitorNotFoundError: 索引不存在。
        """
        ...

    @property
    def current_monitor(self) -> MonitorInfo:
        """当前采集目标显示器。"""
        ...

    @property
    def frame_size(self) -> tuple[int, int]:
        """当前显示器分辨率 ``(width, height)``。

        与 :meth:`capture` 返回的帧尺寸一致。**不得**像 v4 那样在异常时
        返回硬编码兜底值——分辨率错误会导致编码器与客户端显示错位。
        """
        ...

    def stats(self) -> FrameStats:
        """返回采集性能统计快照（只读，调用方可安全持有）。"""
        ...

    def close(self) -> None:
        """释放后端资源（幂等）。"""
        ...


class ArrayScreenSource:
    """用内存中的数组序列充当采集源（测试/基准专用）。

    让差分、编码、raters 的单测与基准脚本**完全不依赖真实屏幕**：
    可以精确构造"第 3 帧改了左上角 64x64"这类场景，也能重放同一段帧序列
    做可复现的性能测量。

    Args:
        frames: 帧序列（每个 ``(H, W, 4)`` uint8 BGRA）。为空时自动生成一帧黑图。
        monitors: 显示器列表；``None`` 时按首帧尺寸合成一台 1920x1080 主屏。
        loop: 帧序列用尽后是否回到开头（``False`` 则重复最后一帧）。
        timestamp_step: 相邻帧时间戳增量（秒），默认 1/30。

    示例:
        >>> src = ArrayScreenSource([np.zeros((64, 128, 4), np.uint8)])
        >>> frame = src.capture()
        >>> frame.size
        (128, 64)
        >>> src.close()
    """

    def __init__(
        self,
        frames: Sequence[npt.NDArray[np.uint8]] | None = None,
        monitors: Sequence[MonitorInfo] | None = None,
        *,
        loop: bool = True,
        timestamp_step: float = 1.0 / 30.0,
    ) -> None:
        if frames:
            self._frames: tuple[npt.NDArray[np.uint8], ...] = tuple(
                np.ascontiguousarray(f, dtype=BGRA_DTYPE) for f in frames
            )
        else:
            self._frames = (np.zeros((1080, 1920, BGRA_CHANNELS), dtype=np.uint8),)
        self._shape = self._frames[0].shape
        if not all(f.shape == self._shape for f in self._frames):
            raise ValueError(
                "ArrayScreenSource 要求所有帧尺寸一致，实得 "
                f"{sorted({f.shape[:2] for f in self._frames})}"
            )
        h, w = self._shape[:2]
        if monitors is not None:
            self._selector = MonitorSelector(monitors)
        else:
            self._selector = MonitorSelector(
                [
                    MonitorInfo(
                        index=1, left=0, top=0, width=w, height=h, is_primary=True,
                        name="array-source",
                    )
                ]
            )
        self._loop = loop
        self._timestamp_step = timestamp_step
        self._pos = 0
        self._seq = 0
        self._base_time = time.time()
        self._closed = False
        self._captured = 0
        self._failed = 0
        self._last_ms = 0.0
        self._max_ms = 0.0
        self._sum_ms = 0.0

    # -- ScreenSource 协议实现 ------------------------------------------------

    def monitors(self) -> Sequence[MonitorInfo]:
        """返回构造时给定（或合成）的物理显示器列表。"""
        return self._selector.physical_monitors

    def capture(self) -> CaptureFrame:
        """返回帧序列中的下一帧（按 ``loop`` 决定回绕或停在末帧）。"""
        self._check_open()
        t0 = time.perf_counter()
        data = self._frames[self._pos]
        info = self._selector.current
        frame = CaptureFrame(
            data=data,
            timestamp=self._base_time + self._seq * self._timestamp_step,
            monitor_index=info.index,
            seq=self._seq,
            left=info.left,
            top=info.top,
        )
        self._advance()
        self._record(t0)
        return frame

    def capture_into(self, destination: npt.NDArray[np.uint8]) -> CaptureFrame:
        """把下一帧拷进 ``destination`` 并返回指向它的 :class:`CaptureFrame`。"""
        self._check_open()
        t0 = time.perf_counter()
        if destination.dtype != BGRA_DTYPE:
            raise ValueError(f"目标缓冲 dtype 必须是 uint8，实得 {destination.dtype}")
        if destination.shape != self._shape:
            raise ValueError(
                f"目标缓冲尺寸 {destination.shape} 与帧尺寸 {self._shape} 不符"
            )
        np.copyto(destination, self._frames[self._pos])
        info = self._selector.current
        frame = CaptureFrame(
            data=destination,
            timestamp=self._base_time + self._seq * self._timestamp_step,
            monitor_index=info.index,
            seq=self._seq,
            left=info.left,
            top=info.top,
        )
        self._advance()
        self._record(t0)
        return frame

    def set_monitor(self, index: int) -> MonitorInfo:
        """切换采集目标（帧序列不变，仅改 ``monitor_index`` 元信息）。"""
        self._check_open()
        return self._selector.select(index)

    @property
    def current_monitor(self) -> MonitorInfo:
        """当前选中的显示器。"""
        return self._selector.current

    @property
    def frame_size(self) -> tuple[int, int]:
        """帧序列的尺寸 ``(width, height)``。"""
        h, w = self._shape[:2]
        return (int(w), int(h))

    def stats(self) -> FrameStats:
        """返回采集统计（``failed`` 恒为 0——内存源不会失败）。"""
        avg = self._sum_ms / self._captured if self._captured else 0.0
        return FrameStats(
            captured=self._captured,
            failed=self._failed,
            last_capture_ms=self._last_ms,
            avg_capture_ms=avg,
            max_capture_ms=self._max_ms,
        )

    def close(self) -> None:
        """标记关闭（幂等）；关闭后 :meth:`capture` 抛 ``RuntimeError``。"""
        self._closed = True

    # -- 测试专用附加面（不属于协议）------------------------------------------

    @property
    def position(self) -> int:
        """当前帧游标（测试断言用）。"""
        return self._pos

    @property
    def is_closed(self) -> bool:
        """是否已关闭（测试断言用）。"""
        return self._closed

    def seek(self, index: int) -> None:
        """把帧游标定位到 ``index``（测试构造特定场景用）。

        Raises:
            IndexError: ``index`` 越界。
        """
        if not 0 <= index < len(self._frames):
            raise IndexError(f"帧索引 {index} 越界（共 {len(self._frames)} 帧）")
        self._pos = index

    def push_frame(self, frame: npt.NDArray[np.uint8]) -> None:
        """向序列尾部追加一帧（测试动态构造变化序列用）。

        Raises:
            ValueError: 尺寸与既有帧不一致。
        """
        arr = np.ascontiguousarray(frame, dtype=BGRA_DTYPE)
        if arr.shape != self._shape:
            raise ValueError(f"帧尺寸 {arr.shape} 与序列 {self._shape} 不符")
        self._frames = (*self._frames, arr)

    # -- 内部 -----------------------------------------------------------------

    def _check_open(self) -> None:
        """关闭后拒绝采集（避免返回陈旧帧被误当实时画面）。"""
        if self._closed:
            raise RuntimeError("ArrayScreenSource 已关闭")

    def _advance(self) -> None:
        """推进帧游标与序号。"""
        self._seq += 1
        if self._pos + 1 < len(self._frames):
            self._pos += 1
        elif self._loop:
            self._pos = 0
        # 不 loop 时停在最后一帧（重复推送静止画面，符合"屏幕没变"的真实语义）

    def _record(self, t0: float) -> None:
        """记录一次采集耗时统计。"""
        ms = (time.perf_counter() - t0) * 1000.0
        self._captured += 1
        self._last_ms = ms
        self._sum_ms += ms
        if ms > self._max_ms:
            self._max_ms = ms

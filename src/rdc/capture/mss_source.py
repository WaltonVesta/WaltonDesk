"""mss 屏幕采集源（v5 唯一真实后端）。

替换 v4 的 ``ScreenCapturer`` 主体（阶段 5 重写，配方见 STAGE5_INPROGRESS §3）。
v4 实现的问题与本模块的对应决策：

1. **PIL 中转是纯浪费**：v4 ``Image.frombytes('RGB', size, screenshot.rgb)`` 每帧多做
   一次 BGRA→RGB 通道重排 + PIL 对象构造。v5 全链路 BGRA：``grab`` 的原始字节经
   :func:`rdc.capture.source.bgra_view` 零拷贝成 ``(H, W, 4)`` 数组。
2. **静默降级掩盖故障**：v4 的 ``HAS_MSS`` try/except ImportError 与
   ``get_screen_size`` 异常时 ``return 1920, 1080`` 会让上层拿着错误分辨率编码。
   mss 是硬依赖（pyproject 已声明），本模块**任何失败一律抛
   :class:`~rdc.exceptions.ScreenCaptureError`**（硬性约束 3）。
3. **每帧 33MB 分配**：``capture()`` 内部维护**双缓冲**，帧写入"未被持有"的持久缓冲后
   交替返回，供 :meth:`rdc.capture.diff.ScreenDiffer.compare` 以 ``adopt=True``
   按引用持有前帧而**零拷贝**（实测不这样做每帧多付 6.19ms，见 STAGE5_INPROGRESS §1.5）。
   ``capture_into()`` 则把帧写入调用方自备缓冲（调用方自行交替）。

⚠️ 三条硬约束（阶段 5 任务书锁定，改动前先读）：

① 必须用 ``mss.MSS()``。``mss.mss()`` 在 mss 10.2 已废弃、调用即抛
   ``DeprecationWarning``，而本项目 pytest 配置 ``filterwarnings = ["error", ...]``
   只豁免 PySide6/protobuf → 任何测试触达废弃 API 都会直接失败。
② 双缓冲交替 + 差分 ``adopt=True``（见上）。
③ 失败一律 ``raise ScreenCaptureError(...) from exc``，禁止兜底返回值。

文件末尾的 v4 兼容外壳 ``ScreenCapturer`` 已随阶段 9 批 4 遗留面清理删除
（其消费方 v4 ``service.py`` 已于批 3 拆除）。v5 一律使用
:class:`MssScreenSource`。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import mss
import numpy as np
import numpy.typing as npt

from rdc.capture.monitor import (
    VIRTUAL_SCREEN_INDEX,
    MonitorInfo,
    MonitorSelector,
)
from rdc.capture.source import BGRA_CHANNELS, CaptureFrame, FrameStats, bgra_view
from rdc.exceptions import ScreenCaptureError
from rdc.logging import get_logger

__all__ = [
    "MssScreenSource",
]

_LOG = get_logger("capture.mss_source")


class MssScreenSource:
    """基于 mss 的 :class:`~rdc.capture.source.ScreenSource` 实现。

    Args:
        monitor_index: 初始采集目标（0 = 虚拟全屏，1 起 = 物理屏）。
            默认虚拟全屏，与 v4 的 ``monitor=1`` 不同（v5 默认拼屏）。
        buffer_count: 内部持久缓冲数，默认 2（双缓冲交替）。

    Raises:
        ScreenCaptureError: mss 初始化失败，或当前无任何显示器可枚举。

    示例:
        >>> src = MssScreenSource()          # doctest: +SKIP
        >>> frame = src.capture()            # doctest: +SKIP
        >>> differ.compare(frame.data, adopt=True)  # 前帧即上一缓冲，未被覆盖
    """

    def __init__(
        self,
        *,
        monitor_index: int = VIRTUAL_SCREEN_INDEX,
        buffer_count: int = 2,
    ) -> None:
        if buffer_count < 1:
            raise ValueError(f"buffer_count 必须 ≥1，实得 {buffer_count}")
        self._buffer_count = buffer_count
        self._buffers: list[npt.NDArray[np.uint8]] = []
        self._write_idx = 0
        self._seq = 0
        self._closed = True
        self._captured = 0
        self._failed = 0
        self._last_ms = 0.0
        self._sum_ms = 0.0
        self._max_ms = 0.0

        try:
            # 硬约束 ①：mss.MSS()，不是已废弃的 mss.mss()
            self._mss: Any | None = mss.MSS()
        except Exception as exc:
            _LOG.error("mss 初始化失败", error=str(exc))
            raise ScreenCaptureError(f"mss 初始化失败: {exc}") from exc

        try:
            self._selector = MonitorSelector(
                self._read_monitors(), initial_index=monitor_index
            )
        except ValueError as exc:
            # MonitorSelector 对空列表抛 ValueError——对上层是"没有屏幕可用"
            self._dispose_backend()
            raise ScreenCaptureError("没有可用显示器") from exc
        self._closed = False

    # -- ScreenSource 协议实现 -------------------------------------------------

    def monitors(self) -> Sequence[MonitorInfo]:
        """重新枚举物理显示器（反映热插拔；不改变当前选中项）。"""
        self._check_open()
        return self._read_monitors()

    def capture(self) -> CaptureFrame:
        """采集一帧并写入内部双缓冲中"刚释放"的那块，返回其视图。

        连续两帧返回的 ``data`` 必属**不同**缓冲：把返回值交给
        :meth:`rdc.capture.diff.ScreenDiffer.compare`（``adopt=True``）后，
        下一次采集只写差分器**不再持有**的缓冲，前帧不被覆盖。

        Raises:
            ScreenCaptureError: 已关闭、grab 失败或回传尺寸异常。
        """
        self._check_open()
        info = self._selector.current
        t0 = time.perf_counter()
        view = self._grab_bgra(info)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._record_success(elapsed_ms)

        # 显示器分辨率就地变化（改了缩放/切屏没走 set_monitor）：重建缓冲池
        if not self._buffers or self._buffers[0].shape != view.shape:
            self._allocate(view.shape)

        self._write_idx = (self._write_idx + 1) % len(self._buffers)
        destination = self._buffers[self._write_idx]
        np.copyto(destination, view)
        return self._make_frame(destination, info)

    def capture_into(self, destination: npt.NDArray[np.uint8]) -> CaptureFrame:
        """采集一帧并拷入调用方缓冲（调用方自行交替即可配合 ``adopt=True``）。

        Raises:
            ValueError: ``destination`` 非 uint8 BGRA 或尺寸不匹配当前显示器。
            ScreenCaptureError: 同 :meth:`capture`。
        """
        self._check_open()
        info = self._selector.current
        if destination.dtype != np.uint8:
            raise ValueError(f"目标缓冲 dtype 必须是 uint8，实得 {destination.dtype}")
        if destination.ndim != 3 or destination.shape[2] != BGRA_CHANNELS:
            raise ValueError(f"目标缓冲形状必须是 (H, W, {BGRA_CHANNELS})，实得 {destination.shape}")
        if (destination.shape[1], destination.shape[0]) != (info.width, info.height):
            raise ValueError(
                f"目标缓冲尺寸 {destination.shape[:2]} 与显示器 {info.height}x{info.width} 不符"
            )
        t0 = time.perf_counter()
        view = self._grab_bgra(info)
        if view.shape != destination.shape:
            # 分辨率刚变化：frame_size 已过期，按实际回传尺寸报错让上层重查
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._record_failure(elapsed_ms)
            raise ScreenCaptureError(
                f"回传帧尺寸 {view.shape[:2]} 与目标缓冲 {destination.shape[:2]} 不符（分辨率可能已变化）"
            )
        np.copyto(destination, view)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._record_success(elapsed_ms)
        return self._make_frame(destination, info)

    def set_monitor(self, index: int) -> MonitorInfo:
        """切换采集目标并重建内部缓冲（尺寸随目标变化）。

        Raises:
            MonitorNotFoundError: 索引不存在。
            ScreenCaptureError: 已关闭。
        """
        self._check_open()
        info = self._selector.select(index)
        self._allocate((info.height, info.width, BGRA_CHANNELS))
        _LOG.info(
            "采集目标已切换",
            index=info.index,
            width=info.width,
            height=info.height,
        )
        return info

    @property
    def current_monitor(self) -> MonitorInfo:
        """当前采集目标显示器。"""
        return self._selector.current

    @property
    def frame_size(self) -> tuple[int, int]:
        """当前显示器分辨率 ``(width, height)``。

        Raises:
            ScreenCaptureError: 已关闭——不返回陈旧尺寸（硬性约束 3）。
        """
        self._check_open()
        info = self._selector.current
        return (info.width, info.height)

    def stats(self) -> FrameStats:
        """返回采集性能统计快照。"""
        avg = self._sum_ms / self._captured if self._captured else 0.0
        return FrameStats(
            captured=self._captured,
            failed=self._failed,
            last_capture_ms=self._last_ms,
            avg_capture_ms=avg,
            max_capture_ms=self._max_ms,
        )

    def close(self) -> None:
        """释放 mss 句柄与缓冲池（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._dispose_backend()
        self._buffers = []

    # -- 内部 -------------------------------------------------------------------

    def _check_open(self) -> None:
        """关闭后拒绝一切采集（避免返回陈旧数据被误当实时画面）。"""
        if self._closed:
            raise ScreenCaptureError("MssScreenSource 已关闭")

    def _dispose_backend(self) -> None:
        """关闭 mss 句柄；关闭失败只记日志（资源释放尽力而为，不再抛错）。"""
        backend, self._mss = self._mss, None
        if backend is None:
            return
        try:
            backend.close()
        except Exception as exc:  # noqa: BLE001 - 释放路径必须容错
            _LOG.warning("mss 关闭失败（忽略）", error=str(exc))

    def _read_monitors(self) -> tuple[MonitorInfo, ...]:
        """mss ``monitors[1:]`` → 与后端无关的 :class:`MonitorInfo` 元组。

        Raises:
            ScreenCaptureError: 枚举显示器失败（硬性约束 3，不返回占位列表）。
        """
        if self._mss is None:
            raise ScreenCaptureError("mss 句柄不可用")
        try:
            raw = self._mss.monitors
        except Exception as exc:
            raise ScreenCaptureError(f"枚举显示器失败: {exc}") from exc
        # monitors[0] 是虚拟全屏（由 MonitorSelector 自行推算），物理屏从 1 起
        return tuple(
            MonitorInfo(
                index=i + 1,
                left=int(m["left"]),
                top=int(m["top"]),
                width=int(m["width"]),
                height=int(m["height"]),
                is_primary=bool(m.get("is_primary", False)),
                name=str(m.get("name", "") or ""),
                unique_id=str(m.get("unique_id", "") or ""),
            )
            for i, m in enumerate(raw[1:])
        )

    def _grab_bgra(self, info: MonitorInfo) -> npt.NDArray[np.uint8]:
        """grab 指定区域并零拷贝包装成 ``(H, W, 4)`` uint8 视图。

        Raises:
            ScreenCaptureError: grab 失败或回传字节数与尺寸不符。
        """
        if self._mss is None:
            raise ScreenCaptureError("mss 句柄不可用")
        region = {
            "left": info.left,
            "top": info.top,
            "width": info.width,
            "height": info.height,
        }
        try:
            shot = self._mss.grab(region)
            return bgra_view(shot.bgra, int(shot.width), int(shot.height))
        except ScreenCaptureError:
            raise
        except Exception as exc:
            self._failed += 1
            _LOG.error("屏幕采集失败", error=str(exc), monitor=info.index)
            raise ScreenCaptureError(f"屏幕采集失败: {exc}") from exc

    def _allocate(self, shape: tuple[int, ...]) -> None:
        """按新尺寸重建持久缓冲池（``np.zeros`` 让首帧未写区域确定，便于诊断）。"""
        self._buffers = [np.zeros(shape, dtype=np.uint8) for _ in range(self._buffer_count)]
        self._write_idx = 0

    def _make_frame(self, data: npt.NDArray[np.uint8], info: MonitorInfo) -> CaptureFrame:
        """构造 :class:`CaptureFrame` 并推进序号。"""
        frame = CaptureFrame(
            data=data,
            timestamp=time.time(),
            monitor_index=info.index,
            seq=self._seq,
            left=info.left,
            top=info.top,
        )
        self._seq += 1
        return frame

    def _record_success(self, elapsed_ms: float) -> None:
        """记录一次成功采集的耗时统计。"""
        self._captured += 1
        self._last_ms = elapsed_ms
        self._sum_ms += elapsed_ms
        if elapsed_ms > self._max_ms:
            self._max_ms = elapsed_ms

    def _record_failure(self, elapsed_ms: float) -> None:
        """记录一次失败采集（计数 + 最近耗时）。"""
        self._failed += 1
        self._last_ms = elapsed_ms

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

import ctypes
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import mss
import numpy as np
import numpy.typing as npt

from rdc.capture.monitor import (
    VIRTUAL_SCREEN_INDEX,
    MonitorInfo,
    MonitorNotFoundError,
    MonitorSelector,
)
from rdc.capture.source import BGRA_CHANNELS, CaptureFrame, FrameStats, bgra_view
from rdc.exceptions import ScreenCaptureError
from rdc.logging import get_logger

__all__ = [
    "MssScreenSource",
    "virtual_desktop_bounds",
]

_LOG = get_logger("capture.mss_source")

#: Windows 虚拟桌面 SM_* 索引（GetSystemMetrics）——与 input/windows.py 同源。
_SM_XVIRTUALSCREEN: Final = 76
_SM_YVIRTUALSCREEN: Final = 77
_SM_CXVIRTUALSCREEN: Final = 78
_SM_CYVIRTUALSCREEN: Final = 79

#: 几何变化检测冷却（秒）：分辨率抖动（反复横跳的病态驱动）下，两次重建
#: 至少间隔此值，避免每帧重建 MSS 句柄。人类改分辨率是秒级低频动作，
#: 冷却期内最多延迟这么久才自适应。
_GEOMETRY_COOLDOWN_S: Final = 2.0


def virtual_desktop_bounds() -> tuple[int, int, int, int] | None:
    """Windows 虚拟桌面包络 ``(left, top, width, height)``。

    用途（走查第十一批）：被控端运行中改分辨率时，``MonitorSelector`` 的
    几何与 mss ``.monitors`` 属性都是**缓存值**（mss 源码实测：仅 ``None``
    时才真枚举），``capture()`` 每帧按过期 region 抓屏 → 帧尺寸永远停在
    旧分辨率，主控端画面不跟随。本探针 4 次 ``GetSystemMetrics``（微秒级）
    给出新鲜包络，变化即触发采集源重枚举。

    非 Windows / 任何失败返回 ``None``——探针只做辅助（与桌面探针
    :mod:`rdc.capture.desktop` 同一"绝不引入新故障面"口径）：``None`` 时
    采集路径完全退回原行为，不报错也不改变语义。
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
        top = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
        width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
        height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    except (AttributeError, OSError, ValueError):  # pragma: no cover - 极端精简 Windows
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


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
        geometry_probe: Callable[[], tuple[int, int, int, int] | None] | None = None,
        geometry_cooldown_s: float = _GEOMETRY_COOLDOWN_S,
    ) -> None:
        if buffer_count < 1:
            raise ValueError(f"buffer_count 必须 ≥1，实得 {buffer_count}")
        self._buffer_count = buffer_count
        #: 分辨率动态自适应（走查第十一批）：几何探针（新鲜虚拟桌面包络 vs
        #: 选择器缓存值，不等即重建）。``None``=停用检测（行为与修复前一致
        #: ——单元测试注入假后端，几何比对真 API 会污染，故生产接线由
        #: service 显式传 :func:`_virtual_desktop_bounds`）。
        self._geometry_probe = geometry_probe
        self._geometry_cooldown_s = geometry_cooldown_s
        self._geometry_last_rebuild = 0.0
        self._geometry_baseline: tuple[int, int, int, int] | None = None
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
        # 几何基线=构造期探针值（与探测同一坐标空间，纯"探针漂移检测"；
        # 探针不可用（None，如非 Windows/假后端桩返回 None）则整体停用）
        if self._geometry_probe is not None:
            self._geometry_baseline = self._geometry_probe()

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
        self._check_geometry()  # 第十一批：分辨率动态自适应（region 过期即重建）
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
        self._check_geometry()  # 第十一批：同 capture，先对齐几何再校验目标缓冲
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

    def reopen(self) -> None:
        """重新打开已关闭的采集源（走查第十二批 Bug 14/15：被控端
        pause→resume 生命周期复用同一源实例）。

        复用 :meth:`_rebuild_backend`（新 MSS() 句柄首次枚举天然新鲜，
        与第六批 P0 句柄自愈、第十一批几何漂移重建同一路径），保留选屏
        索引语义；重建失败留给下帧采集入口报错/自愈，本方法不抛。
        """
        if not self._closed:
            return
        self._rebuild_backend()
        self._closed = False

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
        """grab 指定区域并零拷贝包装成 ``(H, W, 4)`` 视图。

        句柄失效自愈（走查第六批 P0）：Windows 安全桌面切换（UAC 提权）后
        GDI 句柄对后续 ``grab`` 永久失效（BitBlt 持续失败），mss 官方推荐
        的恢复方式是 close + 重建 ``MSS()``。本实现把重建挂在失败路径上：

        - grab 抛错 → 递增失败计数 → 尽力重建新句柄（保留当前选屏索引）；
        - 重建失败 → ``self._mss=None``，下一次采集在入口重试重建（自愈）；
        - 无论重建成败，**本帧**一律抛 :class:`ScreenCaptureError`（硬约束 ③），
          由上层 pipeline 退避，恢复后自动用新句柄出帧。

        Raises:
            ScreenCaptureError: 句柄不可用且重建失败，或 grab 失败/尺寸不符。
        """
        if self._mss is None:
            # 上次重建失败的自愈重试；成功则继续本帧采集
            self._rebuild_backend()
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
            self._rebuild_backend()
            raise ScreenCaptureError(f"屏幕采集失败: {exc}") from exc

    def _check_geometry(self) -> None:
        """虚拟桌面几何漂移检测 → 自动重枚举（走查第十一批动态分辨率）。

        病灶：``MonitorSelector`` 几何是构造期快照、mss ``.monitors`` 又是
        缓存属性，被控端运行中改分辨率后 ``capture()`` 按过期 region 抓屏，
        帧尺寸永远停在旧值（1920→1280 画面"卡住"，升方向同样）。本检测每
        采集帧一次：4 次 ``GetSystemMetrics``（微秒级）取**新鲜包络**与基线
        比对，不等 → 复用句柄重建路径 :meth:`_rebuild_backend`（新 MSS()
        的首次 monitors 枚举天然新鲜，顺带绕开缓存；保留选屏索引、该屏
        消失时回退默认）→ 本帧即用新几何。

        边界：
        - 探针未注入 / 探针返回 None（非 Windows、极端精简系统）→ 停用；
        - 冷却 :data:`_GEOMETRY_COOLDOWN_S`：病态驱动反复横跳时限制重建
          频率（正常改分辨率一次即中，不受影响）；
        - 重建失败**不更新基线** → 下一采集帧继续重试（自愈语义，同 P0 批
          句柄重建；失败本身不在此抛错——region 即便过期 grab 也可能成功
          返回旧尺寸帧，"画面短暂停留在旧分辨率"远好于炸采集链路）。
        """
        probe = self._geometry_probe
        if probe is None:
            return
        fresh = probe()
        if fresh is None:
            return  # 探针平台不可用：本帧跳过，不做任何副作用
        if fresh == self._geometry_baseline:
            return
        now = time.monotonic()
        if now - self._geometry_last_rebuild < self._geometry_cooldown_s:
            return
        self._geometry_last_rebuild = now
        _LOG.info(
            "虚拟桌面几何变化，重建采集源",
            old=self._geometry_baseline,
            new=fresh,
        )
        self._rebuild_backend()
        # 基线推进到本次探测值：即便重建内部回退/失败，也在冷却期内不再
        # 抖动重试；真实仍不一致时冷却结束自然再触发（OS 包络是权威源）。
        self._geometry_baseline = fresh

    def _rebuild_backend(self) -> None:
        """close 旧句柄并重建 ``MSS()``（句柄失效自愈，走查第六批 P0）。

        保留当前选屏索引；新后端枚举不到该屏（显示器热变）时回退默认选择。
        重建失败**不抛错**：置空句柄、记日志，下一次采集入口继续重试——
        采集失败方已经由调用链抛出，这里再抛会丢失原始错误上下文。
        """
        self._dispose_backend()
        try:
            # 硬约束 ①：mss.MSS()，不是已废弃的 mss.mss()
            self._mss = mss.MSS()
        except Exception as exc:  # noqa: BLE001 - 重建失败降级，下帧重试
            _LOG.warning("mss 句柄重建失败（下帧重试）", error=str(exc))
            return
        wanted = self._selector.current_index
        try:
            self._selector = MonitorSelector(self._read_monitors(), initial_index=wanted)
        except (MonitorNotFoundError, ValueError):
            # 显示器布局已变（热插拔/该屏消失）：回退默认选择而非报错
            try:
                self._selector = MonitorSelector(self._read_monitors())
            except Exception as exc:  # noqa: BLE001 - 枚举失败同样留给下帧
                _LOG.warning("mss 重建后枚举显示器失败（下帧重试）", error=str(exc))
                self._dispose_backend()
                return
        _LOG.info(
            "mss 句柄已重建",
            index=self._selector.current_index,
            width=self._selector.current.width,
            height=self._selector.current.height,
        )

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

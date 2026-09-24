"""帧间差分：向量化块级变化检测 + 多矩形脏区 + 静止帧门控。

v5 定位（阶段 5 已确认，见 PROGRESS「假设 A」）
------------------------------------------------
H.264 是**有状态帧间编码**，编码器自身完成帧间压缩，因此 v5 **不再**把差分
结果用于"裁剪变化区域再传输"（v4 的做法，且与 H.264 架构不兼容）。本模块的
输出改为两个用途：

1. **静止帧门控**：画面无变化时跳过编码与推流，省 CPU 与带宽
   （:attr:`DiffResult.any_change`）；
2. **脏区列表**：给 ``encoding/raters.py``（自适应码率）与
   ``capture/framerate.py``（动态帧率）提供"画面活跃度"信号
   （:attr:`DiffResult.dirty_rects` / :attr:`DiffResult.change_ratio`）。

:meth:`ScreenDiffer.get_changed_region` 作为 v4 调用面保留（``server/service.py``
仍在使用），但**不在 v5 主路径上**，阶段 9 随 v4 运行时一并删除。

性能实现（阶段 5 实测，见 STAGE5_REPORT）
------------------------------------------
v4 用双重 Python ``for`` 循环逐块 ``np.array_equal``：4K@32px = 8,160 次循环 +
8,160 次数组比较/帧，实测 24-65ms。v5 的做法：

- **像素打包**：BGRA ``(H, W, 4) uint8`` 通过 ``view(np.uint32)`` 变成
  ``(H, W) uint32``——一个像素一个整数，通道轴折进比较里，省掉一次通道归约，
  且比较结果 bool 数组从 24MB 降到 8.3MB；
- **两级归约**：先 ``reshape(H, cols, BS).any(axis=2)`` 折叠块内列，
  再 ``reshape(rows, BS, cols).any(axis=1)`` 折叠块内行。两段都是连续内存
  上的归约，缓存友好；
- **分带并行**：按行切带交给线程池。numpy 的大数组 ufunc 会释放 GIL，
  4K 实测单线程 6.3ms → 8 带并行 2.8ms；
- **零拷贝双缓冲**：比较后交换前后帧引用而非拷贝（4K 每帧省一次 33MB 复制）。

⚠️ 任务书建议的 ``cv2.absdiff`` 路径实测**更慢**：4K 下 ``absdiff`` 要写一块
33MB 的差值数组，加上通道 ``max`` 归约共 27.5ms，比 v4 双重循环还慢。
故 v5 用 ``np.not_equal``（只写 8.3MB bool）而非 ``absdiff``。

修复的 v4 缺陷
--------------
- **B4（块大小可变导致坐标系错乱）**：v4 的 ``_adjust_block_size`` 会在
  ``get_changed_blocks`` 与 ``get_changed_region`` 两次调用之间修改
  ``self.block_size``（±4，范围 16-128），而后者用 ``min_x * self.block_size``
  反算像素坐标 → 裁剪区域偏移。v5 把 ``block_size`` 改为**构造期固定的只读
  属性**，从结构上消除该类 bug（:meth:`set_adaptive` 保留为兼容空壳）。
- **B5（单外接矩形退化为全屏）**：v4 只返回一个外接矩形。变化分散在屏幕两端
  时（左上时钟 + 右下通知），外接矩形会覆盖大半个屏幕，差分收益归零。
  v5 用 ``cv2.connectedComponentsWithStats`` 输出**多矩形脏区列表**
  （上限 ``max_dirty_rects``，超出部分合并为一个"剩余"矩形）。实测同样的
  两端变化场景：v4 外接矩形覆盖 8,160 块（膨胀 453 倍），v5 多矩形合计 18 块。
- 删除 ``_last_image``（v4 每帧 ``img.copy()`` 但从未读取，4K 下白扔 24MB/帧）。
- 消除 ``_adjust_block_size`` 每帧对整图做 ``int16`` 减法（4K 需 2×24MB 临时数组，
  比主差分循环还贵）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import cv2
import numpy as np
import numpy.typing as npt

from rdc.capture.source import BGRA_CHANNELS
from rdc.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期导入
    from collections.abc import Sequence

    from PIL import Image as PILImage

__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_CHANGE_THRESHOLD",
    "DEFAULT_MAX_DIRTY_RECTS",
    "DiffResult",
    "DirtyRect",
    "ScreenDiffer",
]

_LOG = get_logger("capture.diff")

#: 默认块边长（像素）。与 v4 ``constants.DIFF_BLOCK_SIZE`` 一致；
#: 32 是实测折中：块更小→脏区更精确但归约开销上升，块更大→反之。
DEFAULT_BLOCK_SIZE: Final = 32

#: 默认变化比例阈值。超过即判定"整屏活动"，:attr:`DiffResult.full_frame` 为真。
#: 沿用 v4 的 0.15（``screen/diff.py:72`` 的魔法数字，此处提为具名常量）。
DEFAULT_CHANGE_THRESHOLD: Final = 0.15

#: 默认脏区矩形数上限。超出部分合并为单个"剩余"矩形。
DEFAULT_MAX_DIRTY_RECTS: Final = 8

#: uint32 视图要求 BGRA 每像素 4 字节，故每行元素数 = 像素宽。
_U32_PER_PIXEL: Final = 1

#: 像素数超过此值时启用分带并行（4K = 8.29M；1080p = 2.07M 单线程已 <2ms）。
_PARALLEL_PIXEL_THRESHOLD: Final = 4_000_000

#: 自动并行时的默认带数。实测 8 带在 4K 上最优（2.78ms），再增无收益。
_DEFAULT_BANDS: Final = 8


@dataclass(frozen=True)
class DirtyRect:
    """一块脏区（变化区域的外接矩形），同时给出像素坐标与块坐标。

    Attributes:
        x: 左上角 x（像素，帧内坐标，非虚拟桌面坐标）。
        y: 左上角 y（像素）。
        width: 宽（像素），恒为 ``block_width * block_size``。
        height: 高（像素），同上。
        block_x: 左上角块列号。
        block_y: 左上角块行号。
        block_width: 跨多少个块列。
        block_height: 跨多少个块行。
        block_count: 该连通域内**实际变化**的块数（≤ ``block_width * block_height``）。
    """

    x: int
    y: int
    width: int
    height: int
    block_x: int
    block_y: int
    block_width: int
    block_height: int
    block_count: int

    @property
    def right(self) -> int:
        """右边界（不含）。"""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """下边界（不含）。"""
        return self.y + self.height

    @property
    def area(self) -> int:
        """像素面积（外接矩形，非实际变化像素数）。"""
        return self.width * self.height

    @property
    def box(self) -> tuple[int, int, int, int]:
        """``(x, y, right, bottom)``，PIL crop / Qt QRect 友好格式。"""
        return (self.x, self.y, self.right, self.bottom)

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """``(x, y, width, height)``。"""
        return (self.x, self.y, self.width, self.height)

    def union(self, other: DirtyRect) -> DirtyRect:
        """返回覆盖自身与 ``other`` 的最小外接矩形（``block_count`` 相加）。

        用于把超出上限的多个脏区合并成一个"剩余"矩形。
        """
        left = min(self.x, other.x)
        top = min(self.y, other.y)
        right = max(self.right, other.right)
        bottom = max(self.bottom, other.bottom)
        block_left = min(self.block_x, other.block_x)
        block_top = min(self.block_y, other.block_y)
        block_right = max(self.block_x + self.block_width, other.block_x + other.block_width)
        block_bottom = max(self.block_y + self.block_height, other.block_y + other.block_height)
        return DirtyRect(
            x=left,
            y=top,
            width=right - left,
            height=bottom - top,
            block_x=block_left,
            block_y=block_top,
            block_width=block_right - block_left,
            block_height=block_bottom - block_top,
            block_count=self.block_count + other.block_count,
        )


@dataclass(frozen=True)
class DiffResult:
    """一次差分的结果（不可变快照，可安全跨线程持有）。

    Attributes:
        mask: ``(rows, cols)`` bool 块掩码，``True`` = 该块有变化。
            **调用方不得修改**（差分器不持有它，但复用会破坏快照语义）。
        total_blocks: 块总数 ``rows * cols``。
        changed_blocks: 变化块数。
        change_ratio: ``changed_blocks / total_blocks``（``total_blocks`` 为 0 时取 0）。
        full_frame: 是否应按"整屏变化"对待（首帧，或 ``change_ratio`` 超阈值）。
        dirty_rects: 脏区列表（按 ``block_count`` 降序，至多 ``max_dirty_rects`` 个）。
            ``full_frame`` 为真时为空元组。
        is_first: 是否为该差分器的首帧（无前帧可比，必然 ``full_frame``）。
        frame_size: ``(width, height)`` 像素。
        block_size: 本次使用的块边长（固定值，B4 修复后不再变动）。
        elapsed_ms: 本次差分耗时（毫秒，含归约与脏区计算）。
    """

    mask: npt.NDArray[np.bool_]
    total_blocks: int
    changed_blocks: int
    change_ratio: float
    full_frame: bool
    dirty_rects: tuple[DirtyRect, ...]
    is_first: bool
    frame_size: tuple[int, int]
    block_size: int
    elapsed_ms: float

    @property
    def any_change(self) -> bool:
        """画面是否有**任何**变化（静止帧门控信号）。

        首帧返回 ``True``（无前帧可比，必须推一帧让客户端建立画面）。
        """
        return self.is_first or self.changed_blocks > 0

    @property
    def is_static(self) -> bool:
        """:attr:`any_change` 的反义（可读性别名，用于门控分支）。"""
        return not self.any_change

    @property
    def changed_block_coords(self) -> list[tuple[int, int]]:
        """变化块的 ``(block_x, block_y)`` 列表（v4 兼容格式）。"""
        rows, cols = np.nonzero(self.mask)
        return [(int(c), int(r)) for r, c in zip(rows, cols, strict=True)]

    @property
    def bounding_box(self) -> tuple[int, int, int, int] | None:
        """全部脏区的外接矩形 ``(x, y, w, h)``；无变化时 ``None``。

        ⚠️ 这正是 v4 B5 的退化形态（分散变化时膨胀到全屏）。保留仅供
        v4 兼容面 :meth:`ScreenDiffer.get_changed_region` 使用；
        v5 新代码请用 :attr:`dirty_rects`。
        """
        if not self.dirty_rects:
            return None
        left = min(r.x for r in self.dirty_rects)
        top = min(r.y for r in self.dirty_rects)
        right = max(r.right for r in self.dirty_rects)
        bottom = max(r.bottom for r in self.dirty_rects)
        return (left, top, right - left, bottom - top)

    @property
    def dirty_area(self) -> int:
        """脏区像素面积合计（多矩形求和，**不**像外接矩形那样重复计入空白）。"""
        return sum(r.area for r in self.dirty_rects)


class ScreenDiffer:
    """块级帧间差分器（固定块大小 + 多矩形脏区 + 可选分带并行）。

    Args:
        block_size: 块边长（像素），须 ≥1。**构造后不可变**（B4 修复）。
        change_threshold: 变化比例阈值，``(0, 1]``。超过则
            :attr:`DiffResult.full_frame` 为真、脏区列表为空。
        max_dirty_rects: 脏区矩形数上限，须 ≥1。超出的连通域合并为一个"剩余"矩形。
        ignore_alpha: 是否屏蔽 alpha 字节（仅比较 BGR）。默认 ``False``：
            mss 在各平台实测返回的 alpha 恒为 255（阶段 5 探针 A 段），
            四字节整体比较与三字节语义等价且**零额外开销**；置 ``True`` 会
            多做一次 33MB 的掩码运算（4K 实测 +8.9ms），仅在采集后端会产生
            波动 alpha 时才需要。
        workers: 分带并行的线程数。``0`` = 自动（像素数超
            :data:`_PARALLEL_PIXEL_THRESHOLD` 时用 :data:`_DEFAULT_BANDS`，
            否则单线程）；``1`` = 强制单线程；``>1`` = 指定线程数。
        connectivity: 脏区连通性（4 或 8），传给 ``cv2.connectedComponentsWithStats``。
            默认 8（对角相邻的块也算同一脏区，矩形数更少）。

    线程安全
    --------
    **非线程安全**：内部有前后帧状态。一个采集循环对应一个实例。
    内部的线程池只用于单次 :meth:`compare` 的分带并行，调用方无感知。

    示例:
        >>> import numpy as np
        >>> differ = ScreenDiffer(block_size=8)
        >>> a = np.zeros((16, 16, 4), np.uint8)
        >>> differ.compare(a).is_first
        True
        >>> differ.compare(a).any_change        # 完全静止
        False
        >>> b = a.copy(); b[0, 0, 0] = 255
        >>> res = differ.compare(b)
        >>> res.changed_blocks, len(res.dirty_rects)
        (1, 1)
        >>> differ.close()
    """

    def __init__(
        self,
        block_size: int = DEFAULT_BLOCK_SIZE,
        *,
        change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
        max_dirty_rects: int = DEFAULT_MAX_DIRTY_RECTS,
        ignore_alpha: bool = False,
        workers: int = 0,
        connectivity: int = 8,
    ) -> None:
        if block_size < 1:
            raise ValueError(f"block_size 必须 ≥1，实得 {block_size}")
        if not 0.0 < change_threshold <= 1.0:
            raise ValueError(f"change_threshold 必须在 (0, 1]，实得 {change_threshold}")
        if max_dirty_rects < 1:
            raise ValueError(f"max_dirty_rects 必须 ≥1，实得 {max_dirty_rects}")
        if connectivity not in (4, 8):
            raise ValueError(f"connectivity 必须是 4 或 8，实得 {connectivity}")
        if workers < 0:
            raise ValueError(f"workers 不能为负，实得 {workers}")

        self._block_size = block_size
        self._change_threshold = change_threshold
        self._max_dirty_rects = max_dirty_rects
        self._ignore_alpha = ignore_alpha
        self._workers = workers
        self._connectivity = connectivity

        self._prev: npt.NDArray[np.uint32] | None = None
        self._prev_size: tuple[int, int] | None = None
        self._grid: tuple[int, int] = (0, 0)

        self._pool: ThreadPoolExecutor | None = None
        self._pool_workers = 0
        self._band_buffers: list[npt.NDArray[np.bool_]] = []
        # 分带规划结果（由 _ensure_bands 按当前分辨率填充；在此声明以便
        # 尚未比较过任何帧时也有确定值，同时满足 mypy strict 的属性声明要求）
        self._band_starts: list[int] = []
        self._band_rows: int = 0
        self._needs_col_pad: bool = False
        self._pad_buffers: (
            tuple[npt.NDArray[np.uint32], npt.NDArray[np.uint32]] | None
        ) = None
        self._closed = False

        # 统计
        self._frames = 0
        self._static_frames = 0
        self._full_frames = 0
        self._sum_ms = 0.0
        self._max_ms = 0.0
        self._last_ms = 0.0
        self._ratio_window: list[float] = []
        self._ratio_window_max = 10

    # -- 只读配置面（B4：block_size 结构上不可变）------------------------------

    @property
    def block_size(self) -> int:
        """块边长（像素）。构造后固定，v4 的运行时自适应已移除（B4 修复）。"""
        return self._block_size

    @property
    def change_threshold(self) -> float:
        """变化比例阈值。"""
        return self._change_threshold

    @property
    def max_dirty_rects(self) -> int:
        """脏区矩形数上限。"""
        return self._max_dirty_rects

    @property
    def ignore_alpha(self) -> bool:
        """是否屏蔽 alpha 字节。"""
        return self._ignore_alpha

    @property
    def grid(self) -> tuple[int, int]:
        """当前块网格 ``(cols, rows)``；尚未比较过任何帧时为 ``(0, 0)``。"""
        return self._grid

    @property
    def workers(self) -> int:
        """构造时指定的线程数（``0`` 表示自动）。"""
        return self._workers

    @property
    def active_workers(self) -> int:
        """当前生效的带数（自动模式下按分辨率决定）。"""
        return self._pool_workers or 1

    # -- 主 API ----------------------------------------------------------------

    def compare(
        self,
        frame: npt.NDArray[np.uint8],
        *,
        adopt: bool = True,
    ) -> DiffResult:
        """把 ``frame`` 与上一帧比较，返回差分结果。

        Args:
            frame: ``(H, W, 4)`` uint8 BGRA 数组（见 :mod:`rdc.capture.source`
                的像素格式约定）。也接受 ``(H, W, 3)`` BGR。
            adopt: ``True``（默认）时**按引用保留** ``frame`` 作为下一次比较的
                前帧，零拷贝。调用方须保证在下次 :meth:`compare` 前不修改它。
                若采集源复用同一缓冲（如 :meth:`ScreenSource.capture_into`），
                传 ``False`` 让差分器自行拷贝。

        Returns:
            :class:`DiffResult`。首帧 ``is_first=True`` 且 ``full_frame=True``。

        Raises:
            ValueError: ``frame`` 维度/dtype 非法。
            RuntimeError: 差分器已 :meth:`close`。

        Note:
            分辨率变化时（如切换显示器）自动重置状态并按首帧处理，
            不会拿不同尺寸的数组做比较而崩溃。
        """
        self._check_open()
        t0 = time.perf_counter()
        current = self._as_u32(frame)
        size = (int(current.shape[1]), int(current.shape[0]))

        if self._prev is None or self._prev_size != size:
            if self._prev is not None and self._prev_size != size:
                _LOG.debug(
                    "分辨率变化，重置差分状态",
                    old=self._prev_size,
                    new=size,
                )
            self._prev = current if adopt else current.copy()
            self._prev_size = size
            cols, rows = self._grid_of(size)
            self._grid = (cols, rows)
            self._ensure_bands(size)
            self._record(time.perf_counter() - t0, ratio=1.0, static=False, full=True)
            return DiffResult(
                mask=np.zeros((rows, cols), dtype=bool),
                total_blocks=rows * cols,
                changed_blocks=rows * cols,
                change_ratio=1.0,
                full_frame=True,
                dirty_rects=(),
                is_first=True,
                frame_size=size,
                block_size=self._block_size,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        cols, rows = self._grid
        mask = self._block_mask(current, self._prev, rows, cols)
        self._prev = current if adopt else current.copy()

        changed = int(mask.sum())
        total = rows * cols
        ratio = changed / total if total else 0.0
        full = ratio > self._change_threshold
        rects = () if full else self._dirty_rects(mask, size)
        static = changed == 0
        self._record(time.perf_counter() - t0, ratio=ratio, static=static, full=full)
        return DiffResult(
            mask=mask,
            total_blocks=total,
            changed_blocks=changed,
            change_ratio=ratio,
            full_frame=full,
            dirty_rects=rects,
            is_first=False,
            frame_size=size,
            block_size=self._block_size,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def reset(self) -> None:
        """清空前帧状态（下一次 :meth:`compare` 视作首帧）。

        切换显示器、重连、编码器重启后都应调用——否则第一帧会拿旧屏的
        像素做比较，产生大片假脏区。
        """
        self._prev = None
        self._prev_size = None
        self._grid = (0, 0)
        self._band_buffers = []
        self._band_starts = []
        self._band_rows = 0
        self._needs_col_pad = False
        self._pad_buffers = None
        self._ratio_window.clear()

    def close(self) -> None:
        """释放线程池与前帧引用（幂等）。"""
        if self._closed:
            return
        self._closed = True
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)
        self.reset()

    # -- 统计与诊断 ------------------------------------------------------------

    def get_stats(self) -> dict[str, float | int | bool | None]:
        """返回统计快照。

        键包含 v4 的兼容面（``cols``/``rows``/``total_blocks``/``block_size``/
        ``adaptive_enabled``/``avg_change_ratio``/``history_count``）与 v5 新增的
        门控计数（``frames``/``static_frames``/``full_frames``/``last_ms``/
        ``avg_ms``/``max_ms``/``workers``）。

        ``adaptive_enabled`` 恒为 ``False``——B4 修复后块大小不再自适应。
        """
        cols, rows = self._grid
        avg_ratio = (
            sum(self._ratio_window) / len(self._ratio_window) if self._ratio_window else 0.0
        )
        return {
            # v4 兼容键
            "cols": cols,
            "rows": rows,
            "total_blocks": cols * rows,
            "block_size": self._block_size,
            "adaptive_enabled": False,
            "avg_change_ratio": avg_ratio,
            "history_count": len(self._ratio_window),
            # v5 新增
            "frames": self._frames,
            "static_frames": self._static_frames,
            "full_frames": self._full_frames,
            "last_ms": self._last_ms,
            "avg_ms": self._sum_ms / self._frames if self._frames else 0.0,
            "max_ms": self._max_ms,
            "workers": self.active_workers,
            "ignore_alpha": self._ignore_alpha,
            "closed": self._closed,
        }

    def set_adaptive(self, enabled: bool) -> None:
        """v4 兼容空壳：块大小自适应已移除（B4 修复）。

        Args:
            enabled: 被忽略。保留本方法只为不破坏 ``server/service.py`` 的调用面，
                阶段 9 随 v4 运行时一并删除。
        """
        _LOG.debug("set_adaptive 已废弃（B4 修复：块大小固定），忽略入参", enabled=enabled)

    # -- v4 兼容面（PIL 图像；不在 v5 主路径，阶段 9 删除）---------------------

    def get_changed_blocks(self, img: PILImage.Image) -> list[tuple[int, int]] | None:
        """v4 兼容：返回变化块坐标列表，或 ``None`` 表示应发完整帧。

        Args:
            img: PIL 图像（任意模式，内部转 RGB→BGRA）。

        Returns:
            ``None`` 表示首帧或变化超阈值（发完整帧）；否则为
            ``[(block_x, block_y), ...]``。

        Note:
            经 PIL 转换会多一次全图拷贝与通道重排，性能显著低于
            :meth:`compare`。仅供 ``server/service.py`` 的 v4 广播循环使用。
        """
        result = self.compare(self._pil_to_bgra(img), adopt=False)
        if result.full_frame:
            return None
        return result.changed_block_coords

    def get_changed_region(
        self,
        img: PILImage.Image,
        changed_blocks: Sequence[tuple[int, int]],
    ) -> tuple[PILImage.Image, tuple[int, int]]:
        """v4 兼容：按块坐标裁剪出变化区域（**单外接矩形**，即 B5 的退化形态）。

        Args:
            img: 原始 PIL 图像。
            changed_blocks: :meth:`get_changed_blocks` 返回的块坐标。

        Returns:
            ``(裁剪后的图像, (原图 x 偏移, 原图 y 偏移))``。
            ``changed_blocks`` 为空时原样返回 ``img`` 与 ``(0, 0)``。

        Note:
            与 v4 的**行为差异**：v4 用 ``self.block_size`` 反算坐标，而该值可能在
            两次调用间被 ``_adjust_block_size`` 改掉（B4）。v5 块大小固定，
            裁剪区域与块坐标严格一致。B5（外接矩形膨胀）在本方法中**保留原语义**
            ——多矩形脏区请用 :meth:`compare` 的 :attr:`DiffResult.dirty_rects`。
        """
        if not changed_blocks:
            return img, (0, 0)
        bs = self._block_size
        min_bx = min(bx for bx, _ in changed_blocks)
        max_bx = max(bx for bx, _ in changed_blocks)
        min_by = min(by for _, by in changed_blocks)
        max_by = max(by for _, by in changed_blocks)
        width, height = img.size
        left = max(0, min_bx * bs)
        top = max(0, min_by * bs)
        right = min(width, (max_bx + 1) * bs)
        bottom = min(height, (max_by + 1) * bs)
        if right <= left or bottom <= top:
            return img, (0, 0)
        return img.crop((left, top, right, bottom)), (left, top)

    def get_dirty_regions(self, img: PILImage.Image) -> tuple[list[DirtyRect], bool]:
        """便捷面：对 PIL 图像做一次差分，返回多矩形脏区与"是否整屏"标志。

        Args:
            img: PIL 图像。

        Returns:
            ``(脏区列表, full_frame)``。``full_frame`` 为真时脏区列表为空。
        """
        result = self.compare(self._pil_to_bgra(img), adopt=False)
        return list(result.dirty_rects), result.full_frame

    # -- 内部：像素视图与网格 ---------------------------------------------------

    def _check_open(self) -> None:
        """已关闭则拒绝比较（避免返回基于陈旧前帧的错误结果）。"""
        if self._closed:
            raise RuntimeError("ScreenDiffer 已关闭")

    def _grid_of(self, size: tuple[int, int]) -> tuple[int, int]:
        """由像素尺寸算出块网格 ``(cols, rows)``（向上取整）。"""
        width, height = size
        bs = self._block_size
        return (width + bs - 1) // bs, (height + bs - 1) // bs

    def _as_u32(self, frame: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint32]:
        """把 BGRA/BGR uint8 帧转成 ``(H, W)`` uint32 打包视图。

        BGRA 走**零拷贝** ``view``（一个像素一个 uint32）；BGR 每像素 3 字节
        无法整除打包，退化为按通道比较的 ``(H, W, 3)`` bool 路径（调用方少）。

        Raises:
            ValueError: 维度不是 2D 灰度/3D 图像，或通道数不是 3/4，或 dtype 非 uint8。
        """
        if frame.dtype != np.uint8:
            raise ValueError(f"帧 dtype 必须是 uint8，实得 {frame.dtype}")
        if frame.ndim != 3 or frame.shape[2] not in (3, BGRA_CHANNELS):
            raise ValueError(
                f"帧形状必须是 (H, W, 3) 或 (H, W, 4)，实得 {frame.shape}"
            )
        if frame.shape[0] == 0 or frame.shape[1] == 0:
            raise ValueError(f"帧尺寸必须非空，实得 {frame.shape[:2]}")
        arr = frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)
        height, width = arr.shape[:2]
        if arr.shape[2] == BGRA_CHANNELS:
            return arr.reshape(-1).view(np.uint32).reshape(height, width)
        # BGR：无法打包成 uint32，用 uint32 装 3 字节（高字节补 0）
        out = np.zeros((height, width), dtype=np.uint32)
        out.view(np.uint8).reshape(height, width, BGRA_CHANNELS)[:, :, :3] = arr
        return out

    def _ensure_bands(self, size: tuple[int, int]) -> None:
        """按当前分辨率与线程数预分配分带缓冲（避免每帧分配 8.3MB）。"""
        width, height = size
        cols, rows = self._grid_of(size)
        self._grid = (cols, rows)
        bands = self._resolve_bands(width * height, rows)
        self._pool_workers = bands

        band_rows = self._band_row_count(rows, bands)
        starts = list(range(0, rows, band_rows))
        # 每带最多 band_rows*BS 行；末带含不足一块的残行
        self._band_buffers = [
            np.empty((min(band_rows, rows - s) * self._block_size, cols * self._block_size),
                     dtype=bool)
            for s in starts
        ]
        self._band_starts = starts
        self._band_rows = band_rows
        self._needs_col_pad = width != cols * self._block_size
        if self._needs_col_pad:
            pad_w = cols * self._block_size
            self._pad_buffers = (
                np.zeros((height, pad_w), dtype=np.uint32),
                np.zeros((height, pad_w), dtype=np.uint32),
            )
        else:
            self._pad_buffers = None

    def _resolve_bands(self, pixel_count: int, rows: int) -> int:
        """决定带数：显式 ``workers`` 优先，否则按像素数自动。"""
        if self._workers > 0:
            bands = self._workers
        elif pixel_count >= _PARALLEL_PIXEL_THRESHOLD:
            bands = _DEFAULT_BANDS
        else:
            bands = 1
        return max(1, min(bands, rows))

    def _band_row_count(self, rows: int, bands: int) -> int:
        """每带占多少个块行（向上取整，至少 1）。

        注意与实例属性 ``self._band_rows``（每带的块行数）区分：本方法是
        **规划期**计算，属性是**已定案**的结果，二者在 :meth:`_ensure_bands`
        中前后相继。
        """
        return max(1, -(-rows // bands))

    # -- 内部：核心向量化差分 ---------------------------------------------------

    def _block_mask(
        self,
        current: npt.NDArray[np.uint32],
        previous: npt.NDArray[np.uint32],
        rows: int,
        cols: int,
    ) -> npt.NDArray[np.bool_]:
        """计算 ``(rows, cols)`` 块掩码（核心热路径）。

        列方向不整除块大小时，先把两帧拷进列补齐缓冲（少见分辨率才付此代价）。
        行方向的残行由末带单独处理，不需要补齐。
        """
        if self._needs_col_pad:
            cur, prev = self._column_padded(current, previous, cols)
        else:
            cur, prev = current, previous

        mask = np.empty((rows, cols), dtype=bool)
        bands = self._pool_workers
        if bands <= 1:
            self._band_mask(cur, prev, 0, mask, rows, cols)
            return mask

        pool = self._ensure_pool(bands)
        futures = [
            pool.submit(self._band_mask, cur, prev, idx, mask, rows, cols)
            for idx in range(len(self._band_starts))
        ]
        for fut in futures:
            fut.result()  # 传播 worker 异常，不静默吞（硬性约束 6）
        return mask

    def _column_padded(
        self,
        current: npt.NDArray[np.uint32],
        previous: npt.NDArray[np.uint32],
        cols: int,
    ) -> tuple[npt.NDArray[np.uint32], npt.NDArray[np.uint32]]:
        """把两帧拷进列补齐缓冲并返回（宽度补到 ``cols * block_size``）。

        缓冲在 :meth:`_ensure_bands` 中一次性分配（含零填充的列边带）；
        本方法只写入有效像素区，边带恒为 0 且两帧相同 → 不产生假变化，
        也无需每帧清零。
        """
        buffers = self._pad_buffers
        if buffers is None:  # pragma: no cover - 仅防御 _needs_col_pad 与缓冲不一致
            raise RuntimeError("列补齐缓冲未初始化")
        cur_out, prev_out = buffers
        cur_out[:, : current.shape[1]] = current
        prev_out[:, : previous.shape[1]] = previous
        return cur_out, prev_out

    def _band_mask(
        self,
        current: npt.NDArray[np.uint32],
        previous: npt.NDArray[np.uint32],
        band_index: int,
        out: npt.NDArray[np.bool_],
        rows: int,
        cols: int,
    ) -> None:
        """计算第 ``band_index`` 带的块掩码，结果写进 ``out`` 的对应行。

        两级归约：先折叠块内列（``any(axis=2)``），再折叠块内行（``any(axis=1)``）。
        中间 bool 数组写进预分配的分带缓冲，避免每帧分配。
        """
        bs = self._block_size
        row_start = self._band_starts[band_index]
        row_end = min(row_start + self._band_rows, rows)
        if row_start >= row_end:
            return
        pix_start = row_start * bs
        pix_end = min(row_end * bs, current.shape[0])
        n_pix = pix_end - pix_start
        n_full_rows = n_pix // bs
        buf = self._band_buffers[band_index]

        if self._ignore_alpha:
            alpha_mask = np.uint32(0x00FFFFFF)
            sub_cur = np.bitwise_and(current[pix_start:pix_end], alpha_mask)
            sub_prev = np.bitwise_and(previous[pix_start:pix_end], alpha_mask)
        else:
            sub_cur = current[pix_start:pix_end]
            sub_prev = previous[pix_start:pix_end]

        ne = buf[:n_pix]
        np.not_equal(sub_cur, sub_prev, out=ne)

        if n_full_rows:
            part = ne[: n_full_rows * bs].reshape(n_full_rows, bs, cols, bs).any(axis=(1, 3))
            out[row_start : row_start + n_full_rows] = part
        rem = n_pix - n_full_rows * bs
        if rem:
            tail = ne[n_full_rows * bs :].reshape(1, rem, cols, bs).any(axis=(1, 3))
            out[row_start + n_full_rows : row_end] = tail

    def _ensure_pool(self, bands: int) -> ThreadPoolExecutor:
        """惰性创建/扩容线程池（numpy 大数组 ufunc 释放 GIL，故并行有效）。"""
        if self._pool is None or self._pool_workers != bands:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
            self._pool = ThreadPoolExecutor(
                max_workers=bands, thread_name_prefix="rdc-diff"
            )
        return self._pool

    # -- 内部：脏区提取（B5 修复）----------------------------------------------

    def _dirty_rects(
        self,
        mask: npt.NDArray[np.bool_],
        size: tuple[int, int],
    ) -> tuple[DirtyRect, ...]:
        """由块掩码提取多矩形脏区列表（B5：不再退化为单个外接矩形）。

        ``cv2.connectedComponentsWithStats`` 在块网格上跑（4K 网格 120x68，
        实测 0.05ms），把连成一片的变化块合成一个矩形；再按变化块数降序取前
        ``max_dirty_rects - 1`` 个，其余合并为一个"剩余"矩形。
        """
        if not mask.any():
            return ()
        grid = mask.astype(np.uint8)
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            grid, connectivity=self._connectivity
        )
        if count <= 1:
            return ()

        rects = [
            self._stat_to_rect(stats[i], int(mask.shape[1]), int(mask.shape[0]), size)
            for i in range(1, count)
        ]
        rects.sort(key=lambda r: r.block_count, reverse=True)
        limit = self._max_dirty_rects
        if len(rects) <= limit:
            return tuple(rects)

        head = rects[: limit - 1]
        tail = rects[limit - 1 :]
        merged = tail[0]
        for rect in tail[1:]:
            merged = merged.union(rect)
        return (*head, merged)

    def _stat_to_rect(
        self,
        stat: npt.NDArray[np.int32] | Sequence[int],
        cols: int,
        rows: int,
        size: tuple[int, int],
    ) -> DirtyRect:
        """把 ``connectedComponentsWithStats`` 的一行统计转成 :class:`DirtyRect`。

        块坐标乘块边长得像素坐标，并按帧实际尺寸裁剪（末块可能不足一整块）。
        """
        bx, by, bw, bh, area = (int(v) for v in stat[:5])
        bs = self._block_size
        width, height = size
        left = bx * bs
        top = by * bs
        right = min(width, (bx + bw) * bs)
        bottom = min(height, (by + bh) * bs)
        return DirtyRect(
            x=left,
            y=top,
            width=max(0, right - left),
            height=max(0, bottom - top),
            block_x=bx,
            block_y=by,
            block_width=bw,
            block_height=bh,
            block_count=int(area),
        )

    # -- 内部：辅助 -------------------------------------------------------------

    def _record(self, elapsed_s: float, *, ratio: float, static: bool, full: bool) -> None:
        """更新统计（帧数/静止帧/整屏帧/耗时/比例滑动窗口）。"""
        ms = elapsed_s * 1000.0
        self._frames += 1
        self._last_ms = ms
        self._sum_ms += ms
        if ms > self._max_ms:
            self._max_ms = ms
        if static:
            self._static_frames += 1
        if full:
            self._full_frames += 1
        self._ratio_window.append(ratio)
        if len(self._ratio_window) > self._ratio_window_max:
            self._ratio_window.pop(0)

    @staticmethod
    def _pil_to_bgra(img: PILImage.Image) -> npt.NDArray[np.uint8]:
        """PIL 图像 → ``(H, W, 4)`` uint8 BGRA（v4 兼容面用，含一次通道重排）。"""
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        arr = np.asarray(rgb, dtype=np.uint8)
        height, width = arr.shape[:2]
        out = np.empty((height, width, BGRA_CHANNELS), dtype=np.uint8)
        out[:, :, :3] = arr[:, :, ::-1]  # RGB -> BGR
        out[:, :, 3] = 255
        return out

"""多显示器信息模型与选择器。

本模块是**纯逻辑**层：不依赖 mss / 任何平台 API，因此可在无屏幕环境下
完整单测（ASSESSMENT §5.7 把采集列为"高难测"，根因就是 v4 把平台调用与
业务逻辑焊死在一起）。平台相关的枚举由具体 :class:`~rdc.capture.source.ScreenSource`
实现负责，本模块只提供与后端无关的数据模型与选择语义。

坐标约定
--------
沿用 mss / X11 / Windows 的**虚拟桌面坐标系**：主显示器左上角为 ``(0, 0)``，
其余显示器按物理排布可落在负坐标或超出主屏范围。``MonitorInfo`` 的
``left``/``top`` 即该显示器在虚拟桌面中的原点。

索引约定
--------
``index`` 为 **1 起的物理显示器序号**（mss 约定：``monitors[0]`` 是"全部
显示器拼成的虚拟大屏"，不是真实设备）。索引 0 在本模块中保留给"虚拟全屏"，
见 :func:`virtual_screen_bounds`。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

__all__ = [
    "VIRTUAL_SCREEN_INDEX",
    "MonitorInfo",
    "MonitorNotFoundError",
    "MonitorSelector",
    "virtual_screen_bounds",
]

#: 虚拟全屏索引（所有物理显示器拼成的最小外接矩形）。
#: 与 mss 的 ``monitors[0]`` 语义一致。
VIRTUAL_SCREEN_INDEX = 0


class MonitorNotFoundError(LookupError):
    """请求的显示器索引不存在。

    继承 :class:`LookupError` 而非项目根异常，因为这是"调用方给了越界索引"
    的编程/输入错误，不是运行时故障；上层 UI 可直接捕获 ``LookupError``
    提示用户重新选择显示器。
    """


@dataclass(frozen=True)
class MonitorInfo:
    """一台显示器的几何与标识信息（不可变值对象）。

    Attributes:
        index: 1 起的物理显示器序号；:data:`VIRTUAL_SCREEN_INDEX` 表示虚拟全屏。
        left: 虚拟桌面坐标系中的原点 x（可为负）。
        top: 虚拟桌面坐标系中的原点 y（可为负）。
        width: 像素宽（>0）。
        height: 像素高（>0）。
        is_primary: 是否为主显示器。
        name: 后端提供的设备名（可能为空串）。
        unique_id: 后端提供的稳定标识（Windows 为 ``\\\\?\\DISPLAY#...``，
            可能为空串）。用于跨会话记住用户选择，``name`` 可能重复而它不会。
    """

    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str = ""
    unique_id: str = ""

    def __post_init__(self) -> None:
        """校验几何合法性（构造期拒绝非法值，避免下游裁剪出负尺寸）。"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"显示器尺寸必须为正: {self.width}x{self.height} (index={self.index})"
            )

    @property
    def right(self) -> int:
        """右边界（不含），即 ``left + width``。"""
        return self.left + self.width

    @property
    def bottom(self) -> int:
        """下边界（不含），即 ``top + height``。"""
        return self.top + self.height

    @property
    def pixel_count(self) -> int:
        """总像素数（用于并行阈值判定等成本估算）。"""
        return self.width * self.height

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """``(left, top, width, height)`` 四元组，即 mss 的 grab 区域格式。"""
        return (self.left, self.top, self.width, self.height)

    @property
    def box(self) -> tuple[int, int, int, int]:
        """``(left, top, right, bottom)`` 四元组，即 PIL crop 格式。"""
        return (self.left, self.top, self.right, self.bottom)

    def contains_point(self, x: int, y: int) -> bool:
        """判断虚拟桌面坐标点是否落在本显示器内（左闭右开）。"""
        return self.left <= x < self.right and self.top <= y < self.bottom

    def to_local(self, x: int, y: int) -> tuple[int, int]:
        """把虚拟桌面坐标转成本显示器内的局部坐标。

        主控端点击事件带的是虚拟桌面坐标，注入被控端前需先减去原点偏移。
        本方法**不做**边界裁剪——越界是调用方的责任（见 :meth:`contains_point`）。
        """
        return (x - self.left, y - self.top)

    def to_virtual(self, x: int, y: int) -> tuple[int, int]:
        """把本显示器内的局部坐标转回虚拟桌面坐标（:meth:`to_local` 的逆运算）。"""
        return (x + self.left, y + self.top)


def virtual_screen_bounds(monitors: Sequence[MonitorInfo]) -> MonitorInfo | None:
    """由物理显示器列表算出虚拟全屏的最小外接矩形。

    Args:
        monitors: 物理显示器列表（``index >= 1``）。传空返回 ``None``。

    Returns:
        ``index`` 为 :data:`VIRTUAL_SCREEN_INDEX` 的 :class:`MonitorInfo`，
        其范围为所有物理屏的外接矩形；无显示器时返回 ``None``。

    示例:
        两块 1920x1080 横向拼接（右屏 left=1920）→ ``(0, 0, 3840, 1080)``。
    """
    physical = [m for m in monitors if m.index != VIRTUAL_SCREEN_INDEX]
    if not physical:
        return None
    left = min(m.left for m in physical)
    top = min(m.top for m in physical)
    right = max(m.right for m in physical)
    bottom = max(m.bottom for m in physical)
    return MonitorInfo(
        index=VIRTUAL_SCREEN_INDEX,
        left=left,
        top=top,
        width=right - left,
        height=bottom - top,
        is_primary=any(m.is_primary for m in physical),
        name="virtual-screen",
    )


class MonitorSelector:
    """显示器选择器：在一组显示器间按索引/循环切换。

    把"当前选中哪台屏"从采集实现里剥离出来，使切换逻辑可脱离真实屏幕单测，
    也便于 UI 层（阶段 6）直接绑定到下拉框/快捷键。

    构造时自动把 :func:`virtual_screen_bounds` 的结果作为索引 0 加入候选，
    因此默认即"采集全部显示器拼成的大屏"，与 v4 的 ``monitor=1`` 默认行为
    不同（v4 默认只采第一块物理屏）——v5 默认全屏更符合远程桌面直觉，
    需要单屏时显式 :meth:`select`。

    Args:
        monitors: 物理显示器列表（顺序即索引顺序，``index`` 字段应为 1 起连续）。
        initial_index: 初始选中的索引；``None`` 表示选虚拟全屏（索引 0），
            若不存在虚拟全屏则选主显示器，再退化为首个显示器。

    Raises:
        ValueError: ``monitors`` 为空。
        MonitorNotFoundError: ``initial_index`` 不在候选中。
    """

    def __init__(
        self,
        monitors: Sequence[MonitorInfo],
        initial_index: int | None = None,
    ) -> None:
        if not monitors:
            raise ValueError("显示器列表不能为空")
        self._physical: tuple[MonitorInfo, ...] = tuple(monitors)
        virtual = virtual_screen_bounds(self._physical)
        self._all: tuple[MonitorInfo, ...] = (
            (virtual, *self._physical) if virtual is not None else self._physical
        )
        self._by_index: dict[int, MonitorInfo] = {m.index: m for m in self._all}
        if initial_index is None:
            self._current = self._default_index()
        else:
            self._current = self._resolve(initial_index).index

    def _default_index(self) -> int:
        """挑默认索引：虚拟全屏 → 主显示器 → 首个。"""
        if VIRTUAL_SCREEN_INDEX in self._by_index:
            return VIRTUAL_SCREEN_INDEX
        for m in self._physical:
            if m.is_primary:
                return m.index
        return self._physical[0].index

    def _resolve(self, index: int) -> MonitorInfo:
        """按索引取显示器，不存在则抛 :class:`MonitorNotFoundError`。"""
        try:
            return self._by_index[index]
        except KeyError:
            available = ", ".join(str(i) for i in sorted(self._by_index))
            raise MonitorNotFoundError(
                f"显示器索引 {index} 不存在（可用: {available}）"
            ) from None

    @property
    def monitors(self) -> tuple[MonitorInfo, ...]:
        """全部候选显示器（含索引 0 的虚拟全屏，若存在）。"""
        return self._all

    @property
    def physical_monitors(self) -> tuple[MonitorInfo, ...]:
        """仅物理显示器（不含虚拟全屏）。"""
        return self._physical

    @property
    def current(self) -> MonitorInfo:
        """当前选中的显示器。"""
        return self._by_index[self._current]

    @property
    def current_index(self) -> int:
        """当前选中的显示器索引。"""
        return self._current

    def select(self, index: int) -> MonitorInfo:
        """切换到指定索引的显示器。

        Args:
            index: 目标索引（0 = 虚拟全屏，1 起 = 物理屏）。

        Returns:
            切换后的 :class:`MonitorInfo`。

        Raises:
            MonitorNotFoundError: 索引不存在。
        """
        info = self._resolve(index)
        self._current = info.index
        return info

    def select_primary(self) -> MonitorInfo:
        """切换到主显示器；无主显示器标记时退化为首个物理屏。"""
        for m in self._physical:
            if m.is_primary:
                self._current = m.index
                return m
        self._current = self._physical[0].index
        return self._physical[0]

    def next(self) -> MonitorInfo:
        """在**物理**显示器间循环切换到下一台（跳过虚拟全屏）。

        循环语义便于 UI 上绑一个"切屏"快捷键；只有一块屏时返回自身。
        """
        return self._cycle(1)

    def previous(self) -> MonitorInfo:
        """在物理显示器间循环切换到上一台（:meth:`next` 的逆运算）。"""
        return self._cycle(-1)

    def _cycle(self, step: int) -> MonitorInfo:
        """按 ``step`` 在物理显示器列表中循环移动并返回新选中项。"""
        if len(self._physical) <= 1:
            self._current = self._physical[0].index
            return self._physical[0]
        positions = [m.index for m in self._physical]
        try:
            pos = positions.index(self._current)
        except ValueError:
            # 当前选中的是虚拟全屏：step>0 从头开始，step<0 从尾开始
            pos = -1 if step > 0 else 0
        new_pos = (pos + step) % len(positions)
        self._current = positions[new_pos]
        return self._by_index[self._current]

    def __iter__(self) -> Iterator[MonitorInfo]:
        """迭代全部候选显示器（含虚拟全屏）。"""
        return iter(self._all)

    def __len__(self) -> int:
        """候选显示器数量（含虚拟全屏）。"""
        return len(self._all)

    def with_monitor(self, index: int) -> MonitorSelector:
        """返回一个选中了 ``index`` 的**新**选择器（原对象不变）。

        便于函数式风格地派生配置，例如为每个订阅者建立独立的采集选择。
        """
        return MonitorSelector(self._physical, initial_index=index)

    def snapshot(self) -> tuple[MonitorInfo, ...]:
        """当前候选列表的副本。

        ``MonitorInfo`` 是 frozen dataclass，元组本身即为不可变快照，
        调用方无法通过返回值影响内部状态。
        """
        return self._all

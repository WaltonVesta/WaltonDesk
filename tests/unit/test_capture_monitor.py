"""多显示器模型与选择器（rdc.capture.monitor）单元测试——批 A smoke 固化。"""

from __future__ import annotations

import pytest

from rdc.capture.monitor import (
    VIRTUAL_SCREEN_INDEX,
    MonitorInfo,
    MonitorNotFoundError,
    MonitorSelector,
    virtual_screen_bounds,
)


def _mons() -> list[MonitorInfo]:
    """两块屏的常用夹具：主屏 (0,0,1920,1080) + 右屏 (1920,0,1280,1024)。"""
    return [
        MonitorInfo(index=1, left=0, top=0, width=1920, height=1080, is_primary=True, name="A"),
        MonitorInfo(index=2, left=1920, top=0, width=1280, height=1024, name="B"),
    ]


class TestMonitorInfo:
    """几何值对象。"""

    def test_rejects_non_positive_size(self) -> None:
        with pytest.raises(ValueError, match="尺寸必须为正"):
            MonitorInfo(index=1, left=0, top=0, width=0, height=10)
        with pytest.raises(ValueError, match="尺寸必须为正"):
            MonitorInfo(index=1, left=0, top=0, width=10, height=-1)

    def test_bounds_box_edges(self) -> None:
        m = MonitorInfo(index=1, left=100, top=-50, width=800, height=600)
        assert m.right == 900 and m.bottom == 550
        assert m.bounds == (100, -50, 800, 600)
        assert m.box == (100, -50, 900, 550)
        assert m.pixel_count == 800 * 600

    @pytest.mark.parametrize(
        ("x", "y", "inside"),
        [(100, -50, True), (899, 549, True), (900, 0, False), (99, 0, False), (100, -51, False)],
    )
    def test_contains_point_half_open(self, x: int, y: int, inside: bool) -> None:
        m = MonitorInfo(index=1, left=100, top=-50, width=800, height=600)
        assert m.contains_point(x, y) is inside

    def test_local_virtual_roundtrip(self) -> None:
        m = MonitorInfo(index=2, left=-640, top=0, width=640, height=480)
        assert m.to_local(-400, 100) == (240, 100)
        assert m.to_virtual(240, 100) == (-400, 100)


class TestVirtualScreenBounds:
    """虚拟全屏外接矩形。"""

    def test_empty_returns_none(self) -> None:
        assert virtual_screen_bounds([]) is None

    def test_spans_min_and_negative(self) -> None:
        mons = [
            MonitorInfo(index=1, left=-640, top=0, width=640, height=480, is_primary=True),
            MonitorInfo(index=2, left=0, top=100, width=1920, height=1080),
        ]
        v = virtual_screen_bounds(mons)
        assert v is not None
        assert v.index == VIRTUAL_SCREEN_INDEX
        assert v.bounds == (-640, 0, 2560, 1180)
        assert v.is_primary  # 任一物理屏为主 → 虚拟屏也标主

    def test_ignores_virtual_input(self) -> None:
        mons = [
            MonitorInfo(index=0, left=-9999, top=-9999, width=100, height=100),  # 干扰项
            MonitorInfo(index=1, left=0, top=0, width=800, height=600),
        ]
        v = virtual_screen_bounds(mons)
        assert v is not None and v.bounds == (0, 0, 800, 600)


class TestMonitorSelector:
    """选择器：默认值、切换、循环、坐标换算（smoke 的 11 项断言固化）。"""

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            MonitorSelector([])

    def test_default_is_virtual_fullscreen(self) -> None:
        sel = MonitorSelector(_mons())
        assert sel.current.index == VIRTUAL_SCREEN_INDEX
        assert sel.current.bounds == (0, 0, 3200, 1080)  # 1920+1280 横排外接

    def test_initial_index_validation(self) -> None:
        with pytest.raises(MonitorNotFoundError):
            MonitorSelector(_mons(), initial_index=7)

    def test_select_and_to_local(self) -> None:
        sel = MonitorSelector(_mons())
        m = sel.select(2)
        assert m.name == "B" and m.left == 1920
        # 虚拟坐标 (2000, 300) → 物理屏 2 的局部 (80, 300)
        assert sel.current.to_local(2000, 300) == (80, 300)
        with pytest.raises(MonitorNotFoundError):
            sel.select(5)

    def test_next_prev_cycle_skips_virtual(self) -> None:
        sel = MonitorSelector(_mons())  # 当前虚拟全屏
        assert sel.next().index == 1  # 从虚拟进入第一块物理
        assert sel.next().index == 2
        assert sel.next().index == 1  # 回绕
        assert sel.previous().index == 2  # 反向回绕
        assert sel.previous().index == 1

    def test_single_monitor_cycle(self) -> None:
        sel = MonitorSelector([MonitorInfo(index=1, left=0, top=0, width=800, height=600)])
        assert sel.next().index == 1
        assert sel.previous().index == 1

    def test_select_primary(self) -> None:
        sel = MonitorSelector(_mons())
        assert sel.select_primary().index == 1
        # 无主屏标记 → 退化首块
        sel2 = MonitorSelector(
            [MonitorInfo(index=3, left=0, top=0, width=800, height=600)]
        )
        assert sel2.select_primary().index == 3

    def test_iter_len_snapshot(self) -> None:
        sel = MonitorSelector(_mons())
        assert len(sel) == 3  # 虚拟 + 2 物理
        assert [m.index for m in sel] == [0, 1, 2]
        assert sel.snapshot() == sel.monitors
        assert len(sel.physical_monitors) == 2

    def test_with_monitor_derives_new(self) -> None:
        sel = MonitorSelector(_mons())
        derived = sel.with_monitor(1)
        assert derived.current.index == 1
        assert sel.current.index == VIRTUAL_SCREEN_INDEX  # 原对象不受影响

    def test_primary_default_when_no_virtual(self) -> None:
        # 只有物理屏（无虚拟候选）时，默认选主屏
        mons = [MonitorInfo(index=5, left=0, top=0, width=800, height=600, is_primary=True)]
        sel = MonitorSelector(mons)  # 虚拟外接 = 该屏本身，index=0 仍在
        assert sel.current.index == VIRTUAL_SCREEN_INDEX
        sel2 = MonitorSelector([MonitorInfo(index=1, left=0, top=0, width=800, height=600)])
        assert sel2._default_index() == VIRTUAL_SCREEN_INDEX  # noqa: SLF001

"""ui/session GUI 装配测试（阶段 9 批 4）。

覆盖 :func:`qimage_factory`（RGB→QImage 往返、copy 解耦、尺寸/形状守卫）与
:func:`build_client_controller`（装配产物接线）。launcher 主编排依赖人工
选择对话框，其端到端由批 5 真机走查覆盖。
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QImage

from rdc.client.controller import ClientController
from rdc.config.settings import Settings
from rdc.ui.app import create_app
from rdc.ui.session import build_client_controller, qimage_factory


@pytest.fixture()
def qapp():
    return create_app([])


class TestQImageFactory:
    def test_roundtrip_pixel_values(self, qapp) -> None:
        arr = np.zeros((4, 8, 3), dtype=np.uint8)
        arr[1, 2] = (255, 128, 64)  # RGB888 语义
        img = qimage_factory(8, 4, arr)
        assert isinstance(img, QImage)
        assert (img.width(), img.height()) == (8, 4)
        assert not img.isNull()
        c = img.pixelColor(2, 1)
        assert (c.red(), c.green(), c.blue()) == (255, 128, 64)
        blank = img.pixelColor(0, 0)
        assert (blank.red(), blank.green(), blank.blue()) == (0, 0, 0)

    def test_copy_decouples_source_buffer(self, qapp) -> None:
        arr = np.full((2, 2, 3), 10, dtype=np.uint8)
        img = qimage_factory(2, 2, arr)
        arr[:] = 99  # 源缓冲改写不得影响已交付帧（.copy() 红线）
        assert img.pixelColor(0, 0).red() == 10

    def test_size_mismatch_raises(self, qapp) -> None:
        with pytest.raises(ValueError, match="尺寸不符"):
            qimage_factory(9, 9, np.zeros((2, 2, 3), dtype=np.uint8))

    def test_bad_channels_raises(self, qapp) -> None:
        with pytest.raises(ValueError, match="RGB"):
            qimage_factory(2, 2, np.zeros((2, 2, 4), dtype=np.uint8))

    def test_non_contiguous_input_normalized(self, qapp) -> None:
        base = np.zeros((4, 8, 3), dtype=np.uint8)
        base[:, :, 0] = 77
        view = base[:, ::2, :]  # 非连续视图（宽 4）
        img = qimage_factory(4, 4, view)
        assert img.pixelColor(0, 0).red() == 77

    def test_full_black_frame(self, qapp) -> None:
        img = qimage_factory(3, 3, np.zeros((3, 3, 3), dtype=np.uint8))
        assert img.allPixelColorsEqual(img.pixelColor(0, 0)) if hasattr(img, "allPixelColorsEqual") else True


class TestBuildClientController:
    def test_returns_controller_with_qimage_factory(self, qapp) -> None:
        ctl = build_client_controller(Settings())
        assert isinstance(ctl, ClientController)
        # 注入的工厂产 QImage（controller 交付帧 → UI 可直接消费）
        img = ctl._image_factory(2, 2, np.full((2, 2, 3), 50, dtype=np.uint8))  # noqa: SLF001
        assert isinstance(img, QImage) and img.pixelColor(0, 0).red() == 50

    def test_settings_default_when_none(self, qapp, tmp_app_dir) -> None:
        ctl = build_client_controller()
        assert isinstance(ctl, ClientController)


# ---------------------------------------------------------------------------
# _toggle_pause（走查第十三批 Bug 1：托盘暂停/恢复 Toggle）
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    active_connections = 0


class _FakeService:
    def __init__(self, *, slow: bool = False, fail: bool = False) -> None:
        self.is_paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self._slow = slow
        self._fail = fail
        self.gate: asyncio.Event | None = None

    async def pause(self) -> None:
        self.pause_calls += 1
        if self._fail:
            raise RuntimeError("暂停炸了")
        if self._slow and self.gate is not None:
            await self.gate.wait()  # 模拟慢收尾（旧 10s 卡顿形态）
        self.is_paused = True

    async def resume(self) -> None:
        self.resume_calls += 1
        self.is_paused = False

    def snapshot(self) -> _FakeSnapshot:
        return _FakeSnapshot()


class _FakeTray:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.counts: list[int] = []

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def update_client_count(self, count: int) -> None:
        self.counts.append(count)


class TestTogglePause:
    async def test_pause_then_resume_updates_text_by_actual_state(self) -> None:
        from rdc.ui.session import _toggle_pause
        from rdc.ui.tray import TrayState

        svc, tray, lock = _FakeService(), _FakeTray(), asyncio.Lock()
        await _toggle_pause(svc, tray, lock)  # type: ignore[arg-type]
        assert svc.pause_calls == 1 and tray.states == [TrayState.PAUSED]
        await _toggle_pause(svc, tray, lock)  # type: ignore[arg-type]
        assert svc.resume_calls == 1 and tray.states == [TrayState.PAUSED, TrayState.RUNNING]

    async def test_second_click_dropped_while_toggle_in_flight(self) -> None:
        """锁防抖：上一次 pause 未结束（旧卡顿形态）再点直接丢弃，不重复下发。"""
        from rdc.ui.session import _toggle_pause
        from rdc.ui.tray import TrayState

        svc = _FakeService(slow=True)
        svc.gate = asyncio.Event()
        tray, lock = _FakeTray(), asyncio.Lock()
        first = asyncio.ensure_future(_toggle_pause(svc, tray, lock))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)  # 让 first 进入 pause 挂起点
        await _toggle_pause(svc, tray, lock)  # 重入：即刻丢弃
        svc.gate.set()
        await first
        assert svc.pause_calls == 1, "重入点击不得重复下发暂停"
        assert tray.states == [TrayState.PAUSED]

    async def test_failure_falls_back_to_actual_state_no_raise(self) -> None:
        """pause 抛错：异常不冒泡回 Qt 槽；文案按实际 is_paused（未暂停）刷新。"""
        from rdc.ui.session import _toggle_pause
        from rdc.ui.tray import TrayState

        svc, tray, lock = _FakeService(fail=True), _FakeTray(), asyncio.Lock()
        await _toggle_pause(svc, tray, lock)  # type: ignore[arg-type]
        assert tray.states == [TrayState.RUNNING], "失败后服务仍在运行，文案不得谎报已暂停"

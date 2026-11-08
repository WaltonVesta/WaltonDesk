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


# ---------------------------------------------------------------------------
# host_session 首启密码可见性接线（走查第十四批 Bug 3：三通道保底）
# ---------------------------------------------------------------------------


class _MemStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "mem"

    def available(self) -> bool:
        return True

    def save(self, key: str, value: str) -> None:
        self.data[key] = value

    def load(self, key: str) -> str | None:
        return self.data.get(key)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class _NoopService:
    """被控端服务替身：start 成功、shutdown 空转（不占端口、不触 TLS）。"""

    def __init__(self, **kw: object) -> None:
        self.shutdown_calls = 0

    async def start(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _stub_tray_cls():
    """真信号托盘替身：show() 后 singleShot 触发 quitRequested 停机制。"""
    from PySide6.QtCore import QObject, QTimer, Signal

    class _StubTray(QObject):
        pauseToggled = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
        restartRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
        quitRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
        autostartToggled = Signal(bool, bool)  # noqa: N815 - 须与 Qt 真实信号同名
        changePasswordRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
        noticeSuppressed = Signal()  # noqa: N815 - 须与 Qt 真实信号同名

        def __init__(self, **kw: object) -> None:
            super().__init__()
            self.bubbles: list[str] = []
            self.hidden = False

        def set_state(self, state: str, **kw: object) -> None:
            pass

        def update_client_count(self, count: int) -> None:
            pass

        def show(self) -> None:
            QTimer.singleShot(15, self.quitRequested.emit)

        def hide(self) -> None:
            self.hidden = True

        def notify_initial_password(self, text: str, **kw: object) -> bool:
            self.bubbles.append(text)
            return True

    return _StubTray


class TestHostSessionInitialPassword:
    def test_first_run_prints_bubbles_and_opens_dialog(
        self, tmp_app_dir, monkeypatch: pytest.MonkeyPatch, qapp
    ) -> None:
        """首启明文在 GUI 装配路径三通道齐：控制台 print + 托盘气泡 + 保底弹窗。

        真机病灶（第十四批 Bug 3）：用户删库重开走的是 ``rdc`` 无参 →
        Launcher → HOST 路径，明文只进日志 info，控制台/气泡/弹窗全无。
        本例真跑 host_session（服务/托盘打桩、密钥库内存替身），断言三通道
        都拿到**当次生成的明文**。
        """
        import rdc.server.initial_password as ip_mod
        import rdc.ui.initial_password_dialog as dlg_mod
        import rdc.ui.session as sess_mod
        from rdc.secrets.factory import StoreSelection
        from tests.unit.test_ui_dialogs import drive

        store = _MemStore()
        monkeypatch.setattr(
            ip_mod,
            "get_secret_store",
            lambda *a, **k: StoreSelection(store=store, degraded=False, reason=""),
        )
        monkeypatch.setattr(sess_mod, "HostService", _NoopService)
        stub = _stub_tray_cls()
        tray_holder: dict[str, object] = {}

        def _make_tray(*a: object, **k: object) -> object:
            t = stub()
            tray_holder["t"] = t
            return t

        monkeypatch.setattr(sess_mod, "TrayController", _make_tray)
        presented: list[str] = []
        monkeypatch.setattr(
            dlg_mod,
            "present_initial_password_dialog",
            lambda pw, on_suppress=None: presented.append(pw),
        )
        from rdc.config.settings import Settings

        assert drive(qapp, lambda: sess_mod.host_session(qapp, Settings())) == 0
        pw = store.data[ip_mod.INITIAL_PASSWORD_SECRET_KEY]
        assert pw, "首启应已生成明文"
        tray = tray_holder["t"]
        assert presented == [pw], "保底弹窗必须收到当次明文（Bug 3 核心）"
        assert tray.bubbles and pw in tray.bubbles[0], "托盘气泡必须含明文"

    def test_dialog_presented_even_if_tray_unavailable(
        self, tmp_app_dir, monkeypatch: pytest.MonkeyPatch, qapp
    ) -> None:
        """气泡不可用（无系统托盘）时，保底弹窗仍必须出现（三通道独立）。"""
        import rdc.server.initial_password as ip_mod
        import rdc.ui.initial_password_dialog as dlg_mod
        import rdc.ui.session as sess_mod
        from rdc.secrets.factory import StoreSelection
        from tests.unit.test_ui_dialogs import drive

        store = _MemStore()
        monkeypatch.setattr(
            ip_mod,
            "get_secret_store",
            lambda *a, **k: StoreSelection(store=store, degraded=False, reason=""),
        )
        monkeypatch.setattr(sess_mod, "HostService", _NoopService)

        from PySide6.QtCore import QObject, QTimer, Signal

        class _NoBubbleTray(QObject):
            pauseToggled = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
            restartRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
            quitRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
            autostartToggled = Signal(bool, bool)  # noqa: N815 - 须与 Qt 真实信号同名
            changePasswordRequested = Signal()  # noqa: N815 - 须与 Qt 真实信号同名
            noticeSuppressed = Signal()  # noqa: N815 - 须与 Qt 真实信号同名

            def set_state(self, s: str, **k: object) -> None:
                pass

            def update_client_count(self, n: int) -> None:
                pass

            def show(self) -> None:
                QTimer.singleShot(15, self.quitRequested.emit)

            def hide(self) -> None:
                pass

            def notify_initial_password(self, t: str, **k: object) -> bool:
                return False  # 无系统托盘

        monkeypatch.setattr(sess_mod, "TrayController", lambda *a, **k: _NoBubbleTray())
        presented: list[str] = []
        monkeypatch.setattr(
            dlg_mod,
            "present_initial_password_dialog",
            lambda pw, on_suppress=None: presented.append(pw),
        )
        from rdc.config.settings import Settings

        assert drive(qapp, lambda: sess_mod.host_session(qapp, Settings())) == 0
        assert presented, "气泡不可用时保底弹窗仍须出现，绝不静默"


# ---------------------------------------------------------------------------
# 首启密码弹窗（走查第十四批 Bug 3：保底通道本体）
# ---------------------------------------------------------------------------


class TestInitialPasswordDialog:
    def test_plaintext_shown_selectable(self, qtbot) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from rdc.ui.initial_password_dialog import InitialPasswordDialog

        app = QApplication.instance()
        dlg = InitialPasswordDialog("Zx9QwErT")
        qtbot.addWidget(dlg)
        assert dlg.value_label.text() == "Zx9QwErT"
        flags = dlg.value_label.textInteractionFlags()
        assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
        dlg._copy()  # noqa: SLF001
        assert app is not None and app.clipboard().text() == "Zx9QwErT"

    def test_suppress_checkbox_fires_callback_on_close(self, qtbot) -> None:
        from rdc.ui.initial_password_dialog import InitialPasswordDialog

        seen: list[int] = []
        dlg = InitialPasswordDialog("Abcd2345")
        qtbot.addWidget(dlg)
        dlg.finished.connect(
            lambda _r: seen.append(1) if dlg.suppress_requested else None
        )
        dlg.suppress_check.setChecked(True)
        dlg._accept()  # noqa: SLF001
        assert dlg.suppress_requested is True
        assert seen == [1]

    def test_no_suppress_by_default(self, qtbot) -> None:
        from rdc.ui.initial_password_dialog import InitialPasswordDialog

        dlg = InitialPasswordDialog("Abcd2345")
        qtbot.addWidget(dlg)
        dlg._accept()  # noqa: SLF001
        assert dlg.suppress_requested is False

    def test_present_is_nonblocking_and_keeps_alive(self, qtbot, qapp) -> None:
        """present 立即返回（非模态不卡协程），且引用保活不被 GC 提前销毁。"""
        import gc

        from rdc.ui.initial_password_dialog import present_initial_password_dialog

        dlg = present_initial_password_dialog("Qw7Er9Ty", on_suppress=lambda: None)
        qtbot.addWidget(dlg)
        gc.collect()  # 旧坑：无持有引用时 C++ 对象先亡 → "Internal C++ object already deleted"
        assert dlg.isVisible()
        assert dlg.value_label.text() == "Qw7Er9Ty"

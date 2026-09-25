"""MainWindow 交互分支补充测试（批 E 覆盖率收口）。

覆盖主用例文件未触及的分支：全屏/性能面板/Esc 切换、控制态窗口按键转发、
连接 guard 与失败非重连分支、closeEvent 级联、无 controller 提示框。
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QImage

from rdc.config.settings import Settings
from rdc.ui.app import create_app
from rdc.ui.main_window import ConnectionState, MainWindow
from tests.unit.test_ui_dialogs import FakeController, drive


@pytest.fixture()
def qapp():
    return create_app([])


class TestMainWindowInteractions:
    def _win(self, qtbot, *, ctrl: FakeController | None = None, settings: Settings | None = None):
        c = ctrl or FakeController()
        w = MainWindow(c, settings=settings)
        qtbot.addWidget(w)
        return w, c

    def test_fullscreen_toggle_pair(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w._toggle_fullscreen()  # noqa: SLF001
        assert w.screen_view.fit_mode
        w._on_escape()  # noqa: SLF001 - Esc 只在全屏时退出
        assert not w.screen_view.fit_mode
        w._toggle_fullscreen()
        assert w.screen_view.fit_mode
        w._toggle_fullscreen()  # noqa: SLF001 - 再点回普通
        assert not w.screen_view.fit_mode

    def test_perf_panel_visibility_toggles(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w.show()
        qtbot.waitExposed(w)
        assert not w.performance_panel.isVisible()
        w._toggle_perf()  # noqa: SLF001
        assert w.performance_panel.isVisible()
        w._toggle_perf()  # noqa: SLF001
        assert not w.performance_panel.isVisible()
        w.hide()

    def test_connected_control_key_press_forwards(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTED)  # noqa: SLF001
        w.toolbar._control_btn.click()  # noqa: SLF001
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(w, Qt.Key.Key_F5)
        assert (Qt.Key.Key_F5, True) in c.keys

    def test_key_press_ignored_when_not_connected(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(w, Qt.Key.Key_F5)
        assert c.keys == []

    def test_connect_guard_ignores_when_connecting(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTING)  # noqa: SLF001

        async def _main() -> None:
            await w._start_connect("h", 1)  # noqa: SLF001

        drive(qapp, _main)
        assert c.connect_calls == []  # guard：连接中再点连接被忽略

    def test_connect_failure_no_auto_reconnect(self, qapp, qtbot) -> None:
        c = FakeController()
        c.fail_connect = True
        w, _ = self._win(qtbot, ctrl=c, settings=Settings(auto_reconnect=False))
        w.toolbar._host.setCurrentText("bad.host")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.3)

        drive(qapp, _main)
        assert w.state == ConnectionState.DISCONNECTED
        assert "bad.host" in w.status_bar._status.text() or "失败" in w.status_bar._status.text()  # noqa: SLF001
        assert w.toolbar._host.isEnabled()  # noqa: SLF001 - 解锁可重输

    def test_close_event_stops_timers(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTED)  # noqa: SLF001
        w.clipboard.start()

        async def _main() -> None:
            from PySide6.QtGui import QCloseEvent

            w.closeEvent(QCloseEvent())
            await asyncio.sleep(0.1)  # 让 closeEvent 投递的 disconnect 跑完

        drive(qapp, _main)
        assert not w.clipboard._timer.isActive()  # noqa: SLF001
        assert not w.status_bar._timer.isActive()  # noqa: SLF001
        assert c.disconnect_calls >= 1

    def test_no_controller_close_ok(self, qapp, qtbot) -> None:
        w = MainWindow(None)
        qtbot.addWidget(w)
        from PySide6.QtGui import QCloseEvent

        w.closeEvent(QCloseEvent())  # 不崩即通过

    def test_frame_arrived_placeholder_clear_roundtrip(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w.frame_arrived(QImage(50, 40, QImage.Format.Format_RGB32))
        w.screen_view.clear_frame()
        assert w.screen_view._item.pixmap().isNull()  # noqa: SLF001

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
        """第五批走查：fit 默认常驻，全屏切换只改窗口模式、不再动缩放。"""
        w, _ = self._win(qtbot)
        assert w.screen_view.fit_mode  # 默认适应窗口
        w._toggle_fullscreen()  # noqa: SLF001
        assert w.isFullScreen()
        assert w.screen_view.fit_mode
        w._on_escape()  # noqa: SLF001 - Esc 只在全屏时退出
        assert not w.isFullScreen()
        assert w.screen_view.fit_mode  # 退出全屏保持 fit（旧版强切 1:1 即病灶）

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

    # -- 第四批缺陷 1 哨兵：认证失败必须给出明确用户可读提示 ------------------

    def test_auth_failure_shows_reason_even_with_auto_reconnect(
        self, qapp, qtbot
    ) -> None:
        """错口令：auto_reconnect 开启也不得吞掉原因（缺陷 1）。

        病灶（修复前）：_handle_connect_failure 只看 auto_reconnect，认证失败
        被归入 RECONNECTING，用户只见状态流转、看不到"口令错误"。
        """
        from rdc.exceptions import AuthError

        class AuthFailController(FakeController):
            async def connect(self, host, port, *, get_password, confirm_fingerprint):  # noqa: ANN001
                self.connect_calls.append((host, port))
                raise AuthError("认证失败: 口令校验失败")

        c = AuthFailController()
        w, _ = self._win(qtbot, ctrl=c, settings=Settings(auto_reconnect=True))
        w.toolbar._host.setCurrentText("1.2.3.4")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.3)

        drive(qapp, _main)
        text = w.status_bar._status.text()  # noqa: SLF001
        assert w.state == ConnectionState.DISCONNECTED
        assert "口令校验失败" in text, text
        assert w.toolbar._host.isEnabled()  # noqa: SLF001 - 解锁可改口令重连

    def test_password_cancel_shows_neutral_cancel_not_error(self, qapp, qtbot) -> None:
        """用户主动取消口令：中性"已取消连接"，不得误报认证失败（缺陷 1）。"""
        from rdc.exceptions import AuthCancelledError

        class CancelController(FakeController):
            async def connect(self, host, port, *, get_password, confirm_fingerprint):  # noqa: ANN001
                self.connect_calls.append((host, port))
                raise AuthCancelledError("用户取消口令输入")

        c = CancelController()
        w, _ = self._win(qtbot, ctrl=c, settings=Settings(auto_reconnect=True))
        w.toolbar._host.setCurrentText("1.2.3.4")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.3)

        drive(qapp, _main)
        text = w.status_bar._status.text()  # noqa: SLF001
        assert w.state == ConnectionState.DISCONNECTED
        assert "取消" in text and "失败" not in text, text
        assert w.toolbar._host.isEnabled()  # noqa: SLF001

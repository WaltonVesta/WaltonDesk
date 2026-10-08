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


class TestHostStatusHint:
    """走查第六批 P0：安全桌面停滞提示直上状态栏（warn），恢复回常态。"""

    def _win(self, qtbot, *, ctrl: FakeController | None = None, settings: Settings | None = None):
        c = ctrl or FakeController()
        w = MainWindow(c, settings=settings)
        qtbot.addWidget(w)
        return w, c

    def test_secure_desktop_stall_shows_hint(self, qtbot) -> None:
        w, _ = self._win(qtbot)
        w.host_status(stalled=True, secure_desktop=True)
        text = w.status_bar._status.text()  # noqa: SLF001
        assert "安全桌面" in text and "UAC" in text, text

    def test_plain_stall_and_recovery(self, qtbot) -> None:
        w, _ = self._win(qtbot)
        w.host_status(stalled=True, secure_desktop=False)
        text = w.status_bar._status.text()  # noqa: SLF001
        assert "采集异常" in text, text
        w.host_status(stalled=False, secure_desktop=False)
        assert "恢复" in w.status_bar._status.text(), w.status_bar._status.text()  # noqa: SLF001


class TestDeletedUiCallbacks:
    """走查第六批 Bug5：主窗销毁后 controller 迟到回调不得触达已删 C++ 对象。

    生产病灶：client_session 给主窗挂 ``WA_DeleteOnClose``——关窗即销毁
    ScreenView 等子对象，而 controller 仍持其 Python 引用，随后 disconnect
    收尾/画面流 EOF 触发的 ``state_changed`` / ``frame_arrived`` 直达已删对象
    （真机日志 ``event_hook_failed hook=state_changed 'Internal C++ object
    (ScreenView) already deleted'``）。修复两层：事件入口 ``_ui_alive()``
    （shiboken6.isValid）守卫 + closeEvent 先 ``set_events(None)`` 解绑。
    """

    def _closed_win(self, qtbot):
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtTest import QTest

        c = FakeController()
        w = MainWindow(c)
        w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        w.show()
        QTest.qWaitForWindowExposed(w)
        w.close()
        # 立即冲刷 DeferredDelete：C++ 侧此刻真删（生产关窗同效）
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(w)  # 前提成立：窗口（连同子树）已销毁
        return w, c

    def test_state_changed_after_delete_is_noop(self, qapp, qtbot) -> None:
        w, _ = self._closed_win(qtbot)
        # 病灶：直达 _set_state → screen_view.set_input_enabled → RuntimeError
        w.state_changed(ConnectionState.DISCONNECTED)  # 修复后：守卫 no-op

    def test_frame_metrics_host_status_after_delete_is_noop(self, qapp, qtbot) -> None:
        w, _ = self._closed_win(qtbot)
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        w.frame_arrived(img)
        w.metrics_updated(fps=1.0, latency_ms=2.0)
        w.host_status(stalled=True, secure_desktop=True)

    def test_closeevent_unbinds_events_before_disconnect(self, qapp, qtbot) -> None:
        """第一层防御：closeEvent 先解绑回调源，disconnect 收尾不再回触本窗。"""
        from PySide6.QtTest import QTest

        c = FakeController()
        assert c.events is None
        w = MainWindow(c)
        # 镜像生产 run_gui：关最后一个窗口不触发 app.quit（否则 drive 环早停）
        qapp.setQuitOnLastWindowClosed(False)
        w.show()
        QTest.qWaitForWindowExposed(w)

        async def _main() -> None:
            w.close()  # closeEvent：set_events(None) + ensure_future(disconnect)
            await asyncio.sleep(0.05)

        drive(qapp, _main)
        assert c.events is None  # 病灶：closeEvent 不解绑 → 仍指向已关窗口
        assert c.disconnect_calls == 1  # 断开仍被尽力调度（原有语义保留）


class TestBanCountdown:
    """走查第六批 Bug6：封禁期禁用连接按钮 + 倒计时，归零自动恢复。"""

    def _win(self, qtbot, *, ctrl=None, settings=None):
        c = ctrl or FakeController()
        w = MainWindow(c, settings=settings)
        qtbot.addWidget(w)
        return w, c

    def test_banned_error_disables_button_and_starts_countdown(self, qapp, qtbot) -> None:
        """_handle_connect_failure 收到 AuthBannedError → 禁用连接 + 启动倒计时。"""
        from rdc.exceptions import AuthBannedError

        w, _ = self._win(qtbot)
        w._handle_connect_failure(  # noqa: SLF001
            "1.2.3.4", AuthBannedError("已被封禁", remaining_seconds=90)
        )
        assert w.state == ConnectionState.DISCONNECTED
        assert not w.toolbar._connect_btn.isEnabled()  # noqa: SLF001
        assert w._ban_remaining_s == 90  # noqa: SLF001
        assert w._ban_timer is not None and w._ban_timer.isActive()  # noqa: SLF001
        text = w.status_bar._status.text()  # noqa: SLF001
        assert "1 分 30 秒后可重试" in text, text
        w._stop_ban_countdown()  # noqa: SLF001 - 清理，防跨用例残留定时器

    def test_tick_renders_and_zero_restores(self, qapp, qtbot) -> None:
        """倒计时每秒递减；归零停表并恢复连接按钮。"""
        w, _ = self._win(qtbot)
        w._start_ban_countdown(3)  # noqa: SLF001
        assert not w.toolbar._connect_btn.isEnabled()  # noqa: SLF001
        assert w._ban_remaining_s == 3  # noqa: SLF001

        w._tick_ban_countdown()  # noqa: SLF001 → 2
        assert "2 秒后可重试" in w.status_bar._status.text()  # noqa: SLF001
        w._tick_ban_countdown()  # noqa: SLF001 → 1
        assert "1 秒后可重试" in w.status_bar._status.text()  # noqa: SLF001
        w._tick_ban_countdown()  # noqa: SLF001 → 0：停表 + 恢复按钮
        assert w._ban_remaining_s == 0  # noqa: SLF001
        assert not w._ban_timer.isActive()  # noqa: SLF001
        assert w.toolbar._connect_btn.isEnabled()  # noqa: SLF001

    def test_retrigger_resets_to_latest_remaining(self, qapp, qtbot) -> None:
        """封禁期再点连接又被拒：以最新剩余秒重置（不叠加多个定时器）。"""
        from rdc.exceptions import AuthBannedError

        w, _ = self._win(qtbot)
        w._handle_connect_failure(  # noqa: SLF001
            "h", AuthBannedError("", remaining_seconds=120)
        )
        timer_first = w._ban_timer  # noqa: SLF001
        w._handle_connect_failure(  # noqa: SLF001
            "h", AuthBannedError("", remaining_seconds=45)
        )
        assert w._ban_timer is timer_first  # noqa: SLF001 - 复用同一定时器
        assert w._ban_remaining_s == 45  # noqa: SLF001
        w._stop_ban_countdown()  # noqa: SLF001

    def test_connect_success_clears_countdown(self, qapp, qtbot) -> None:
        """倒计时进行中若连接成功（另一主机/解封）→ 清除残留定时器。"""
        w, _ = self._win(qtbot)
        w._start_ban_countdown(50)  # noqa: SLF001
        assert w._ban_timer.isActive()  # noqa: SLF001
        w._on_connect_success("9.9.9.9")  # noqa: SLF001
        assert not w._ban_timer.isActive()  # noqa: SLF001
        assert w._ban_remaining_s == 0  # noqa: SLF001

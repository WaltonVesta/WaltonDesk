"""批 D 测试：password/fingerprint/settings/launcher/tray/clipboard/main_window。

驱动方式：qasync 事件循环 ``run_until_complete``（与 scripts/poc 实测同型），
Qt 定时器在泵送期间可触发；FakeController 实现 ClientController Protocol。
"""

from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from rdc.config.settings import Settings
from rdc.ui.app import create_app, event_loop
from rdc.ui.clipboard_sync import ClipboardSync
from rdc.ui.fingerprint_dialog import FingerprintDialog, confirm_fingerprint
from rdc.ui.launcher import LauncherDialog, LaunchOption, launch_choice
from rdc.ui.main_window import ClientEvents, ConnectionState, MainWindow
from rdc.ui.password_dialog import PasswordDialog, request_password
from rdc.ui.settings_dialog import SettingsDialog
from rdc.ui.tray import TrayController, TrayState, build_tray_icon


@pytest.fixture()
def qapp():
    return create_app([])


def drive(qapp, coro_fn):  # noqa: ANN001, ANN201 - qasync 驱动同步测试
    """在 qasync 循环上跑协程到完成（Qt 事件同步泵送）。"""
    loop = event_loop(qapp)
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def _click_widget(qapp, widget_cls, action) -> None:  # noqa: ANN001
    """下一轮泵送时对新出现的对话框执行 action。"""

    def _do() -> None:
        for w in qapp.topLevelWidgets():
            if isinstance(w, widget_cls):
                action(w)
                return
        QTimer.singleShot(10, lambda: _click_widget(qapp, widget_cls, action))

    QTimer.singleShot(10, _do)


def _raises_in_worker(fn) -> bool:  # noqa: ANN001
    box: list[BaseException] = []

    def _t() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            box.append(e)

    th = threading.Thread(target=_t)
    th.start()
    th.join(2)
    return len(box) == 1 and isinstance(box[0], RuntimeError)


# ---------------------------------------------------------------------------
# FakeController（ClientController Protocol 替身）
# ---------------------------------------------------------------------------


class FakeController:
    def __init__(self) -> None:
        self.events: ClientEvents | None = None
        self.connect_calls: list[tuple[str, int]] = []
        self.disconnect_calls = 0
        self.mice: list[tuple[str, int, int]] = []
        self.keys: list[tuple[int, bool]] = []
        self.control_flags: list[bool] = []
        self.fail_connect = False
        self.get_password_calls: list[str] = []
        self.confirm_calls: list[tuple[str, str]] = []

    def set_events(self, events: ClientEvents) -> None:
        self.events = events

    async def connect(self, host, port, *, get_password, confirm_fingerprint):  # noqa: ANN001, ANN202
        self.connect_calls.append((host, port))
        if self.fail_connect:
            raise ConnectionError("refused")
        pw = await get_password(host)
        self.get_password_calls.append(pw or "")
        ok = await confirm_fingerprint(host, "AA:BB")
        self.confirm_calls.append((host, str(ok)))
        if self.events is not None:
            self.events.state_changed(ConnectionState.CONNECTED)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.events is not None:
            self.events.state_changed(ConnectionState.DISCONNECTED)

    def set_control_enabled(self, enabled: bool) -> None:
        self.control_flags.append(enabled)

    def send_mouse(self, action: str, x: int, y: int) -> None:
        self.mice.append((action, x, y))

    def send_key(self, qt_key: int, press: bool) -> None:
        self.keys.append((qt_key, press))


# ---------------------------------------------------------------------------
# 对话框（B2 修复：协程化，主循环零阻塞）
# ---------------------------------------------------------------------------


class TestPasswordDialog:
    def test_coro_resolves_on_accept(self, qapp) -> None:
        def _fill(dlg: PasswordDialog) -> None:
            dlg._edit.setText("s3cret")  # noqa: SLF001
            dlg.accept()

        async def _main() -> str | None:
            _click_widget(qapp, PasswordDialog, _fill)
            return await request_password(None, "1.2.3.4", timeout_s=5)

        assert drive(qapp, _main) == "s3cret"

    def test_coro_cancel_returns_none(self, qapp) -> None:
        async def _main() -> str | None:
            _click_widget(qapp, PasswordDialog, lambda d: d.reject())
            return await request_password(None, "h", timeout_s=5)

        assert drive(qapp, _main) is None

    def test_timeout_returns_none(self, qapp) -> None:
        async def _main() -> str | None:
            return await request_password(None, "h", timeout_s=0.15)

        assert drive(qapp, _main) is None

    def test_dialog_shape_nonmodal(self, qapp, qtbot) -> None:
        dlg = PasswordDialog("10.0.0.1")
        qtbot.addWidget(dlg)
        assert dlg.isModal() is False  # B2：非模态
        assert dlg.password() == ""


class TestFingerprintDialog:
    def test_accept_path(self, qapp) -> None:
        async def _main() -> bool:
            _click_widget(qapp, FingerprintDialog, lambda d: d.accept())
            return await confirm_fingerprint(None, "host", "FP:FP", timeout_s=5)

        assert drive(qapp, _main) is True

    def test_timeout_denies(self, qapp) -> None:
        async def _main() -> bool:
            return await confirm_fingerprint(None, "h", "FP", timeout_s=0.15)

        assert drive(qapp, _main) is False


# ---------------------------------------------------------------------------
# SettingsDialog（v5 扁平 Settings 绑定）
# ---------------------------------------------------------------------------


class TestSettingsDialog:
    def test_roundtrip_edit_returns_new_object(self, qapp, qtbot) -> None:
        s = Settings(jpeg_quality=55, max_fps=24, port=7000, bandwidth_limit=1500)
        dlg = SettingsDialog(s)
        qtbot.addWidget(dlg)
        assert dlg.quality_slider.value() == 55
        assert dlg.port.value() == 7000
        dlg.quality_slider.setValue(70)
        dlg.bandwidth_limit.setValue(2000)
        new = dlg.apply()
        assert new.jpeg_quality == 70 and s.jpeg_quality == 55  # 原对象不动
        assert new.bandwidth_limit == 2000
        assert new.port == 7000 and new.control_port == s.control_port

    def test_defaults(self, qapp, qtbot) -> None:
        dlg = SettingsDialog(Settings())
        qtbot.addWidget(dlg)
        assert dlg.max_fps.value() == 30
        assert dlg.auto_reconnect.isChecked()
        assert dlg.enable_auth.isChecked()
        assert dlg.min_fps.value() <= dlg.max_fps.value()


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class TestLauncher:
    def test_default_choice_is_quit(self, qapp, qtbot) -> None:
        dlg = LauncherDialog()
        qtbot.addWidget(dlg)
        assert dlg.choice() == LaunchOption.QUIT

    def test_launch_choice_host(self, qapp) -> None:
        async def _main() -> LaunchOption:
            _click_widget(qapp, LauncherDialog, lambda d: d._host_btn.click())  # noqa: SLF001
            return await launch_choice()

        assert drive(qapp, _main) == LaunchOption.HOST

    def test_launch_choice_client(self, qapp) -> None:
        async def _main() -> LaunchOption:
            _click_widget(qapp, LauncherDialog, lambda d: d._client_btn.click())  # noqa: SLF001
            return await launch_choice()

        assert drive(qapp, _main) == LaunchOption.CLIENT


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------


class TestTray:
    def test_icon_builds_per_state(self, qapp) -> None:
        for st in (TrayState.RUNNING, TrayState.PAUSED, TrayState.STOPPED):
            assert not build_tray_icon(st).isNull()

    def test_menu_actions_emit(self, qapp, qtbot) -> None:
        tray = TrayController()
        pass  # QSystemTrayIcon 非 QWidget，不入 qtbot
        menu = tray._icon.contextMenu()  # noqa: SLF001
        acts = {a.text(): a for a in menu.actions() if a.text()}
        hits: list[str] = []
        tray.pauseToggled.connect(lambda: hits.append("pause"))
        tray.restartRequested.connect(lambda: hits.append("restart"))
        tray.quitRequested.connect(lambda: hits.append("quit"))
        acts["⏸ 暂停服务"].trigger()
        acts["🔄 重启服务"].trigger()
        acts["🚪 退出"].trigger()
        assert hits == ["pause", "restart", "quit"]
        tray.hide()

    def test_state_change_updates_pause_label_and_tooltip(self, qapp, qtbot) -> None:
        tray = TrayController()
        pass  # QSystemTrayIcon 非 QWidget，不入 qtbot
        tray.set_state(TrayState.PAUSED, uptime_s=3661.0)
        menu = tray._icon.contextMenu()  # noqa: SLF001
        texts = [a.text() for a in menu.actions()]
        assert any(t.startswith("▶ 恢复服务") for t in texts)
        tip = tray._icon.toolTip()  # noqa: SLF001
        assert "已暂停" in tip and "01:01:01" in tip
        tray.hide()

    def test_client_count_tooltip(self, qapp, qtbot) -> None:
        tray = TrayController()
        pass  # QSystemTrayIcon 非 QWidget，不入 qtbot
        tray.update_client_count(3)
        assert "连接 3" in tray._icon.toolTip()  # noqa: SLF001
        tray.hide()

    def test_worker_blocked(self, qapp, qtbot) -> None:
        tray = TrayController()
        pass  # QSystemTrayIcon 非 QWidget，不入 qtbot
        assert _raises_in_worker(lambda: tray.update_client_count(1))
        tray.hide()


# ---------------------------------------------------------------------------
# ClipboardSync（去抖 + 防回环）
# ---------------------------------------------------------------------------


class TestClipboardSync:
    def _clip(self) -> ClipboardSync:
        return ClipboardSync(interval_ms=20)

    def test_local_change_emits_once(self, qapp, qtbot) -> None:
        cs = self._clip()
        cs.start()
        seen: list[str] = []
        cs.localChanged.connect(seen.append)
        QApplication.clipboard().setText("hello-v5")
        qtbot.waitUntil(lambda: len(seen) == 1, timeout=2000)
        assert seen == ["hello-v5"]
        cs.stop()

    def test_remote_apply_does_not_echo(self, qapp, qtbot) -> None:
        cs = self._clip()
        cs.start()
        seen: list[str] = []
        cs.localChanged.connect(seen.append)
        cs.apply_remote("from-remote")
        QApplication.clipboard().setText("from-remote")
        qtbot.wait(150)
        assert seen == []
        cs.stop()

    def test_disable_stops_and_reenable_sees_pending(self, qapp, qtbot) -> None:
        cs = self._clip()
        cs.start()
        cs.set_enabled(False)
        assert not cs._timer.isActive()  # noqa: SLF001
        seen: list[str] = []
        cs.localChanged.connect(seen.append)
        QApplication.clipboard().setText("ignored-then-seen")
        qtbot.wait(150)
        assert seen == []
        cs.set_enabled(True)
        qtbot.waitUntil(lambda: seen == ["ignored-then-seen"], timeout=2000)
        cs.stop()

    def test_worker_blocked(self, qapp, qtbot) -> None:
        cs = self._clip()
        assert _raises_in_worker(cs.start)
        cs.stop()


# ---------------------------------------------------------------------------
# MainWindow：FakeController 驱动的状态流转 + 编排
# ---------------------------------------------------------------------------


class TestMainWindowStateMachine:
    def _win(self, qtbot, ctrl: FakeController | None = None):  # noqa: ANN202
        c = ctrl or FakeController()
        w = MainWindow(c)
        qtbot.addWidget(w)
        return w, c

    def test_events_registered_on_construct(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        assert c.events is w

    def test_connect_flow_drives_ui(self, qapp, qtbot, monkeypatch) -> None:
        w, c = self._win(qtbot)
        assert w.state == ConnectionState.IDLE

        async def fake_pw(parent, host, *, timeout_s: float = 30.0) -> str:  # noqa: ANN001
            return "abc"

        async def fake_fp(parent, host, fp, *, timeout_s: float = 60.0) -> bool:  # noqa: ANN001
            return True

        import rdc.ui.main_window as mwmod

        monkeypatch.setattr(mwmod, "request_password", fake_pw)
        monkeypatch.setattr(mwmod, "confirm_fingerprint", fake_fp)
        w.toolbar._host.setCurrentText("10.1.2.3")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.4)

        drive(qapp, _main)
        assert w.state == ConnectionState.CONNECTED
        assert c.connect_calls == [("10.1.2.3", w._settings.control_port)]  # noqa: SLF001
        assert c.get_password_calls == ["abc"]
        assert c.confirm_calls == [("10.1.2.3", "True")]
        assert "已连接" in w.status_bar._status.text()  # noqa: SLF001

    def test_connect_failure_with_auto_reconnect(self, qapp, qtbot) -> None:
        c = FakeController()
        c.fail_connect = True
        w, _ = self._win(qtbot, c)
        w._settings = Settings(auto_reconnect=True)  # noqa: SLF001
        w.toolbar._host.setCurrentText("h")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.3)

        drive(qapp, _main)
        assert w.state == ConnectionState.RECONNECTING

    def test_disconnect_resets_state(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTED)  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._disconnect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.2)

        drive(qapp, _main)
        assert c.disconnect_calls == 1
        assert w.state == ConnectionState.DISCONNECTED

    def test_metrics_and_frame_route(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTED)  # noqa: SLF001 - 指标路由前提是已连接
        img = QImage(120, 90, QImage.Format.Format_RGB32)
        w.frame_arrived(img, left=-120, top=0)
        assert w.screen_view._item.pixmap().width() == 120  # noqa: SLF001
        w.metrics_updated(
            fps=25.0, latency_ms=40.0, bandwidth_kbps=800.0,
            quality=28, resolution=(120, 90), total_frames=999,
        )
        fps_texts = [lb.text() for lb in w.performance_panel.findChildren(type(w.performance_panel._fps))]  # noqa: E501, SLF001
        assert any("25.0" in t for t in fps_texts)
        assert "40ms" in w.status_bar._latency.text()  # noqa: SLF001
        assert w.toolbar.indicator.get_quality() == "good"

    def test_control_toggle_gates_screen_input(self, qapp, qtbot) -> None:
        w, c = self._win(qtbot)
        w._set_state(ConnectionState.CONNECTED)  # noqa: SLF001
        assert w.screen_view.input_enabled is False  # 控制默认关
        w.toolbar._control_btn.click()  # noqa: SLF001
        assert c.control_flags == [True]
        assert w.screen_view.input_enabled
        w._on_mouse("press:left", 10, 20)
        assert c.mice == [("press:left", 10, 20)]
        w._on_key(0x41, True)
        assert c.keys == [(0x41, True)]

    def test_state_changed_idempotent(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w.state_changed(ConnectionState.CONNECTED)
        before = w.status_bar._status.text()  # noqa: SLF001
        w.state_changed(ConnectionState.CONNECTED)
        assert w.status_bar._status.text() == before  # noqa: SLF001

    def test_clipboard_remote_gate_by_setting(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        w._settings = Settings(enable_clipboard=False)  # noqa: SLF001
        w.clipboard_from_remote("xyz")
        assert QApplication.clipboard().text() != "xyz"
        w._settings = Settings(enable_clipboard=True)  # noqa: SLF001
        w.clipboard_from_remote("xyz")
        assert QApplication.clipboard().text() == "xyz"

    def test_no_controller_click_shows_hint(self, qapp, qtbot) -> None:
        w = MainWindow(None)
        qtbot.addWidget(w)
        w.toolbar._host.setCurrentText("h")  # noqa: SLF001

        async def _main() -> None:
            w.toolbar._connect_btn.click()  # noqa: SLF001
            await asyncio.sleep(0.1)

        drive(qapp, _main)
        assert w.state == ConnectionState.IDLE  # 仅提示框，状态不动

    def test_worker_blocked(self, qapp, qtbot) -> None:
        w, _ = self._win(qtbot)
        img = QImage(2, 2, QImage.Format.Format_RGB32)
        assert _raises_in_worker(lambda: w.frame_arrived(img))
        assert _raises_in_worker(lambda: w.state_changed("connected"))

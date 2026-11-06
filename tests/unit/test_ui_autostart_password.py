"""开机自启 + 改密 UI 接线测试（走查第十二批 Feature 17/18，UI 层）。

offscreen 下真起 Qt；autostart 读写一律注入替身（**绝不触真实注册表**）。
协程驱动复用 test_ui_dialogs 的 :func:`drive` / :func:`_click_widget` 基建。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from rdc.ui.app import create_app
from rdc.ui.change_password_dialog import ChangePasswordDialog, request_new_password
from rdc.ui.launcher import LauncherDialog
from rdc.ui.tray import TrayController
from tests.unit.test_ui_dialogs import _click_widget, drive


@pytest.fixture()
def qapp():
    app = create_app([])
    yield app
    for w in app.topLevelWidgets():
        w.deleteLater()


# ---------------------------------------------------------------------------
# Launcher 勾选（Feature 17）
# ---------------------------------------------------------------------------


class TestLauncherAutostart:
    def test_reflects_current_state_on_open(self, qtbot) -> None:
        dlg = LauncherDialog(autostart_get=lambda: True, autostart_set=lambda e: True)
        qtbot.addWidget(dlg)
        assert dlg._autostart_check.isChecked()  # noqa: SLF001

    def test_toggle_success_emits_ok(self, qtbot) -> None:
        calls: list[bool] = []
        dlg = LauncherDialog(
            autostart_get=lambda: False,
            autostart_set=lambda e: calls.append(e) or True,
        )
        qtbot.addWidget(dlg)
        seen: list[tuple[bool, bool]] = []
        dlg.autostartChanged.connect(lambda en, ok: seen.append((en, ok)))
        dlg._autostart_check.setChecked(True)  # noqa: SLF001
        assert calls == [True]
        assert seen == [(True, True)]
        assert dlg._autostart_check.isChecked()  # noqa: SLF001

    def test_toggle_failure_rolls_back(self, qtbot) -> None:
        """注册失败 → 勾选回滚 False + emit(ok=False)，绝不静默留错误态。"""
        dlg = LauncherDialog(autostart_get=lambda: False, autostart_set=lambda e: False)
        qtbot.addWidget(dlg)
        seen: list[tuple[bool, bool]] = []
        dlg.autostartChanged.connect(lambda en, ok: seen.append((en, ok)))
        dlg._autostart_check.setChecked(True)  # noqa: SLF001
        assert seen and seen[-1] == (True, False)
        assert not dlg._autostart_check.isChecked()  # noqa: SLF001 回滚

    def test_autostart_get_exception_defaults_off(self, qtbot) -> None:
        def _boom() -> bool:
            raise OSError("读注册表炸了")

        dlg = LauncherDialog(autostart_get=_boom, autostart_set=lambda e: True)
        qtbot.addWidget(dlg)
        assert not dlg._autostart_check.isChecked()  # noqa: SLF001 保守呈现


# ---------------------------------------------------------------------------
# 托盘菜单（Feature 17/18）
# ---------------------------------------------------------------------------


def _find_action(tray: TrayController, needle: str):
    for act in tray._menu.actions():  # noqa: SLF001
        if needle in act.text():
            return act
    return None


class TestTrayMenu:
    def _tray(self, *, ag=lambda: False, ase=lambda e: True) -> TrayController:
        return TrayController(autostart_get=ag, autostart_set=ase)

    def test_autostart_action_reflects_and_toggles(self) -> None:
        tray = self._tray()
        act = _find_action(tray, "开机自启")
        assert act is not None and act.isCheckable()
        assert act.isChecked() is False
        seen: list[tuple[bool, bool]] = []
        tray.autostartToggled.connect(lambda en, ok: seen.append((en, ok)))
        act.setChecked(True)
        assert seen == [(True, True)]

    def test_autostart_rollback_on_failure(self) -> None:
        tray = self._tray(ase=lambda e: False)
        act = _find_action(tray, "开机自启")
        seen: list[tuple[bool, bool]] = []
        tray.autostartToggled.connect(lambda en, ok: seen.append((en, ok)))
        act.setChecked(True)
        assert seen and seen[-1] == (True, False)
        assert act.isChecked() is False  # 回滚

    def test_change_password_action_emits(self) -> None:
        tray = self._tray()
        act = _find_action(tray, "修改密码")
        assert act is not None
        fired: list[int] = []
        tray.changePasswordRequested.connect(lambda: fired.append(1))
        act.trigger()
        assert fired == [1]

    def test_sync_autostart_refreshes(self) -> None:
        state = {"on": False}
        tray = self._tray(ag=lambda: state["on"])
        act = _find_action(tray, "开机自启")
        assert act.isChecked() is False
        state["on"] = True
        assert tray.sync_autostart() is True
        assert act.isChecked() is True


# ---------------------------------------------------------------------------
# 改密对话框（Feature 18）
# ---------------------------------------------------------------------------


class TestChangePasswordDialog:
    def test_weak_rejected_dialog_stays_open(self, qtbot) -> None:
        dlg = ChangePasswordDialog()
        qtbot.addWidget(dlg)
        dlg._new.setText("short")  # noqa: SLF001
        dlg._confirm.setText("short")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert dlg.password() == ""
        assert not dlg._ok  # noqa: SLF001

    def test_mismatch_rejected(self, qtbot) -> None:
        dlg = ChangePasswordDialog()
        qtbot.addWidget(dlg)
        dlg._new.setText("GoodPass123")  # noqa: SLF001
        dlg._confirm.setText("OtherPass123")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert dlg.password() == ""

    def test_valid_returns_password(self, qtbot) -> None:
        dlg = ChangePasswordDialog()
        qtbot.addWidget(dlg)
        dlg._new.setText("GoodPass123")  # noqa: SLF001
        dlg._confirm.setText("GoodPass123")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert dlg.password() == "GoodPass123"

    def test_request_new_password_ok(self, qapp) -> None:
        def _fill(dlg: ChangePasswordDialog) -> None:
            dlg._new.setText("MyPass999")  # noqa: SLF001
            dlg._confirm.setText("MyPass999")  # noqa: SLF001
            dlg._on_accept()  # noqa: SLF001

        async def _main() -> str | None:
            _click_widget(qapp, ChangePasswordDialog, _fill)
            return await request_new_password(None, timeout_s=5.0)

        assert drive(qapp, _main) == "MyPass999"

    def test_request_new_password_cancel(self, qapp) -> None:
        async def _main() -> str | None:
            _click_widget(qapp, ChangePasswordDialog, lambda d: d.reject())
            return await request_new_password(None, timeout_s=5.0)

        assert drive(qapp, _main) is None


# ---------------------------------------------------------------------------
# 初始口令气泡（走查第十三批 Bug 2：延迟投递 + 去 disconnect 警告 + 点击守卫）
# ---------------------------------------------------------------------------


class _FakeTrayIcon:
    """替身 QSystemTrayIcon 类：真信号 + 记录 showMessage（不触通知区）。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            activated = Signal(object)
            messageClicked = Signal()  # noqa: N815 - 须与 Qt 真实信号同名

        self._sig = _Sig()
        self.activated = self._sig.activated
        self.messageClicked = self._sig.messageClicked
        self.shown: list[tuple[str, str]] = []

    @classmethod
    def isSystemTrayAvailable(cls) -> bool:  # noqa: N802 - Qt API 命名
        return True

    def showMessage(self, title: str, msg: str, *a: object, **k: object) -> None:  # noqa: N802
        self.shown.append((title, msg))

    def setContextMenu(self, _menu: object) -> None: ...  # noqa: N802
    def setIcon(self, _icon: object) -> None: ...  # noqa: N802
    def setToolTip(self, _t: str) -> None: ...  # noqa: N802
    def show(self) -> None: ...
    def hide(self) -> None: ...


class TestTrayInitialBubble:
    def _tray_with_fake_icon(self, monkeypatch: pytest.MonkeyPatch):
        import PySide6.QtWidgets as QtWidgets

        monkeypatch.setattr(QtWidgets, "QSystemTrayIcon", _FakeTrayIcon)
        tray = TrayController(autostart_get=lambda: False, autostart_set=lambda e: True)
        return tray, tray._icon  # noqa: SLF001 - 取替身实例断言 showMessage 记录

    def test_showmessage_delayed_not_synchronous(self, qtbot, monkeypatch) -> None:
        """投递延迟到下一轮事件循环（跨过图标注册时序窗，修"气泡被吞"）。"""
        tray, icon = self._tray_with_fake_icon(monkeypatch)
        assert tray.notify_initial_password("当前连接口令：Abcd2345") is True
        assert icon.shown == [], "showMessage 不应同步调用（须 singleShot(0) 延迟）"
        qtbot.wait(30)  # 跑过事件循环
        assert len(icon.shown) == 1 and "Abcd2345" in icon.shown[0][1]

    def test_notify_emits_no_disconnect_runtime_warning(self, qtbot, monkeypatch) -> None:
        """第十三批 Bug 2 警告源头：无连接时不得再调 messageClicked.disconnect()。"""
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tray, _icon = self._tray_with_fake_icon(monkeypatch)
            for _ in range(3):  # 重复通知，旧实现每次 disconnect 累积警告
                tray.notify_initial_password("口令 Xyz12345")
            qtbot.wait(20)
        bad = [str(w.message) for w in caught if "disconnect" in str(w.message).lower()]
        assert not bad, f"不应出现 disconnect 相关 RuntimeWarning：{bad}"

    def test_message_clicked_only_suppresses_when_pending(self, qtbot, monkeypatch) -> None:
        """点击守卫：仅在气泡展示期间触发"不再提示"，非展示期点击不吞抑制态。"""
        tray, icon = self._tray_with_fake_icon(monkeypatch)
        fired: list[int] = []
        tray.noticeSuppressed.connect(lambda: fired.append(1))
        icon.messageClicked.emit()  # 未通知时点击 → 不触发
        assert fired == []
        tray.notify_initial_password("口令 Abcd2345")
        qtbot.wait(20)
        icon.messageClicked.emit()
        assert fired == [1]
        icon.messageClicked.emit()
        assert fired == [1], "一次气泡只允许一次抑制触发"

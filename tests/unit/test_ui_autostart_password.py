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

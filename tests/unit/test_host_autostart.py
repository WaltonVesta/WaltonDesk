"""开机自启（rdc.host.autostart）单元测试（Feature 17）。

真实注册表/launchctl/systemctl 一律不触：Windows 分支经 ``sys.modules`` 注入
假 ``winreg``，平台经 ``platform.system`` 打桩。主断言面是**命令组装**——
Feature 17 的硬要求"自启必须静默进被控端、不弹 Launcher"全在命令串里。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import rdc.host.autostart as am


class _FakeRegKey:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def __enter__(self) -> _FakeRegKey:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class _FakeWinreg:
    """假 winreg：HKCU Run 键用 dict 模拟（SetValueEx/DeleteValue/QueryValueEx）。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []

    def OpenKey(self, hive: str, key: str, *_a: Any, **_k: Any) -> _FakeRegKey:  # noqa: N802 - 模拟 winreg API 名
        assert hive == self.HKEY_CURRENT_USER
        assert key == am.REGISTRY_RUN_KEY
        return _FakeRegKey(self.values)

    def SetValueEx(self, key: _FakeRegKey, name: str, _r: int, _t: int, value: str) -> None:  # noqa: N802
        self.set_calls.append((name, value))
        key._store[name] = value

    def QueryValueEx(self, key: _FakeRegKey, name: str) -> tuple[str, int]:  # noqa: N802
        if name not in key._store:
            raise FileNotFoundError(name)
        return key._store[name], self.REG_SZ

    def DeleteValue(self, key: _FakeRegKey, name: str) -> None:  # noqa: N802
        if name not in key._store:
            raise FileNotFoundError(name)
        del key._store[name]


@pytest.fixture
def fake_winreg(monkeypatch: pytest.MonkeyPatch) -> _FakeWinreg:
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(am.platform, "system", lambda: "Windows")


class TestLaunchCommand:
    """命令组装：静默进被控端（serve + --silent），打包/源码两形态。"""

    def test_source_form_uses_module_and_silent(self) -> None:
        mgr = am.AutostartManager(python_path=r"C:\py\python.exe", frozen=False)
        cmd = mgr.launch_command()
        assert "-m rdc" in cmd
        assert "serve" in cmd and "--silent" in cmd
        # Feature 17 硬要求：静默、不弹 Launcher
        assert "--service" not in cmd  # v4 病灶参数绝迹

    def test_frozen_form_uses_exe_only(self) -> None:
        mgr = am.AutostartManager(frozen=True, exe_path=r"C:\dist\rdc.exe")
        cmd = mgr.launch_command()
        assert cmd.startswith('"C:\\dist\\rdc.exe" serve')
        assert "--tray" in cmd and "--silent" in cmd
        assert "-m rdc" not in cmd

    def test_pythonw_substitution_when_present(self, tmp_path: Any) -> None:
        py = tmp_path / "python.exe"
        py.write_text("")
        pw = tmp_path / "pythonw.exe"
        pw.write_text("")
        mgr = am.AutostartManager(python_path=str(py), frozen=False)
        assert "pythonw.exe" in mgr.launch_command()

    def test_python_kept_when_no_pythonw(self, tmp_path: Any) -> None:
        py = tmp_path / "python.exe"
        py.write_text("")
        mgr = am.AutostartManager(python_path=str(py), frozen=False)
        assert str(py).replace("\\", "\\\\") in mgr.launch_command() or "python.exe" in mgr.launch_command()
        assert "pythonw" not in mgr.launch_command()


class TestWindowsRegistry:
    def test_install_writes_run_value(self, windows: None, fake_winreg: _FakeWinreg) -> None:
        mgr = am.AutostartManager("MyRdc", frozen=True, exe_path=r"C:\d\rdc.exe")
        assert mgr.install() is True
        name, value = fake_winreg.set_calls[0]
        assert name == "MyRdc"
        assert value.startswith('"C:\\d\\rdc.exe" serve --tray --silent')

    def test_is_enabled_roundtrip(self, windows: None, fake_winreg: _FakeWinreg) -> None:
        mgr = am.AutostartManager(frozen=True, exe_path="X")
        assert mgr.is_enabled() is False  # 未注册
        mgr.install()
        assert mgr.is_enabled() is True
        assert mgr.uninstall() is True
        assert mgr.is_enabled() is False

    def test_uninstall_idempotent_when_absent(
        self, windows: None, fake_winreg: _FakeWinreg
    ) -> None:
        mgr = am.AutostartManager()
        assert mgr.uninstall() is True  # 本就未注册：幂等成功，不抛

    def test_install_failure_returns_false_not_raise(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise OSError("注册表权限拒绝")

        monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU", KEY_SET_VALUE=2, REG_SZ=1, OpenKey=_boom
        ))
        assert am.AutostartManager().install() is False  # 边界可观测降级

    def test_query_failure_conservative_false(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            OpenKey=lambda *a, **k: (_ for _ in ()).throw(OSError("坏了")),
        ))
        assert am.AutostartManager().is_enabled() is False


class TestUnsupportedSystem:
    def test_install_unknown_system_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(am.platform, "system", lambda: "FreeBSD")
        assert am.AutostartManager().install() is False

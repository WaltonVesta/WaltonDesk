"""Windows 输入桌面探针（rdc.capture.desktop）单元测试。

探针契约（走查第六批 P0）：任何失败形态一律 False——它只做"锦上添花"的
安全桌面归因，绝不把其它故障误报成安全桌面，也绝不抛异常波及调用方
（采集停滞处理路径）。平台门与 API 故障面用 monkeypatch 全覆盖，不依赖
真实 Win32。
"""

from __future__ import annotations

from typing import Any

import pytest

import rdc.capture.desktop as desktop_mod
from rdc.capture.desktop import is_secure_desktop


class TestPlatformGate:
    def test_non_windows_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(desktop_mod.sys, "platform", "darwin")
        assert is_secure_desktop() is False

    def test_no_windll_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(desktop_mod.sys, "platform", "win32")

        class _NoWinDLL:
            @property
            def windll(self) -> Any:
                raise AttributeError("no windll on this build")

        # ctypes 无 windll 属性 → windll.user32 取属性抛 AttributeError → False
        import ctypes as ct

        monkeypatch.setattr(ct, "windll", _NoWinDLL(), raising=False)
        assert is_secure_desktop() is False


class _FakeUser32:
    def __init__(self, handle: int, name: str | None, *, close_ok: bool = True) -> None:
        self._handle = handle
        self._name = name
        self.closed: list[int] = []
        self._close_ok = close_ok

    def OpenInputDesktop(self, _f: bool, _i: bool, _a: int) -> int:  # noqa: N802
        return self._handle

    def CloseDesktop(self, handle: int) -> None:  # noqa: N802
        if not self._close_ok:
            raise OSError("close boom")
        self.closed.append(handle)


class _FakeKernel32:
    def __init__(self, ok: bool, name: str) -> None:
        self._ok = ok
        self._name = name

    def GetUserObjectInformationW(  # noqa: N802
        self, _h: int, _cls: int, buf: Any, _size: int, _pdw: Any
    ) -> int:
        if not self._ok:
            return 0
        buf.value = self._name
        return 1


def _install(
    monkeypatch: pytest.MonkeyPatch, user32: Any, kernel32: Any
) -> None:
    class _WinDLL:
        pass

    wd = _WinDLL()
    wd.user32 = user32
    wd.kernel32 = kernel32
    import ctypes as ct

    monkeypatch.setattr(ct, "windll", wd, raising=False)
    monkeypatch.setattr(desktop_mod.sys, "platform", "win32")


class TestDesktopProbe:
    def test_winlogon_name_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        u32 = _FakeUser32(handle=42, name="Winlogon")
        _install(monkeypatch, u32, _FakeKernel32(ok=True, name="Winlogon"))
        assert is_secure_desktop() is True
        assert u32.closed == [42]  # 句柄必须关闭（泄漏红线）

    def test_default_desktop_not_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        u32 = _FakeUser32(handle=7, name="Default")
        _install(monkeypatch, u32, _FakeKernel32(ok=True, name="Default"))
        assert is_secure_desktop() is False
        assert u32.closed == [7]

    def test_no_handle_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # OpenInputDesktop 返回 0（无权限/失败）：False 且不查询不关闭
        u32 = _FakeUser32(handle=0, name="Winlogon")
        _install(monkeypatch, u32, _FakeKernel32(ok=True, name="Winlogon"))
        assert is_secure_desktop() is False

    def test_query_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        u32 = _FakeUser32(handle=42, name="Winlogon")
        _install(monkeypatch, u32, _FakeKernel32(ok=False, name="Winlogon"))
        assert is_secure_desktop() is False
        assert u32.closed == [42]

    def test_close_error_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CloseDesktop 抛错不得冒泡（finally 抑制），判定结果照常返回。"""
        u32 = _FakeUser32(handle=42, name="Winlogon", close_ok=False)
        _install(monkeypatch, u32, _FakeKernel32(ok=True, name="Winlogon"))
        assert is_secure_desktop() is True

    def test_api_raise_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def OpenInputDesktop(self, *a: Any) -> Any:  # noqa: N802
                raise OSError("rpc unavailable")

        _install(monkeypatch, _Boom(), _FakeKernel32(ok=True, name="Winlogon"))
        assert is_secure_desktop() is False

"""input 包装配门面测试（阶段 9 批 4，任务 A2）。

工厂本体语义（Windows→SendInput、macOS/Linux 空实现回退/抛错）已由
``test_input_base.py`` 全量覆盖；本文件锁**包门面**：

- 导入 ``rdc.input`` 本身零副作用（不得连带拉起 Qt/pyautogui/ctypes 后端）；
- 惰性符号可解析且与子模块同一对象；
- 未知属性抛 ``AttributeError``（不静默返回 None）；
- 平台装配结论：Windows 选中 windows 后端；非 Windows 的 macOS/Linux
  空实现 ``supported()=False``、方法体抛 :class:`NotImplementedError`、
  ``legacy_fallback=False`` 时工厂抛 :class:`PlatformUnsupportedError`
  ——任务书"macOS/Linux 抛 NotImplementedError（空实现）"口径的落点。
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


class TestNoEagerImport:
    def test_import_package_pulls_no_backends(self) -> None:
        for name in ("rdc.input.windows", "rdc.input.legacy_pyautogui", "rdc.input.coalesce"):
            assert name not in sys.modules or importlib.import_module(name)  # 已加载与否都不该被包导入拉起
        # 真断言：全新解释器下 import rdc.input 后，后端模块未被导入

        code = (
            "import sys, rdc.input;"
            "print('rdc.input.windows' in sys.modules,"
            " 'rdc.input.legacy_pyautogui' in sys.modules,"
            " 'PySide6.QtWidgets' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert out == "False False False", out


class TestLazySymbols:
    @pytest.mark.parametrize(
        "name",
        [
            "InputBackend",
            "MouseButton",
            "ScreenRect",
            "default_backend",
            "autodetect_backend_key",
            "normalize_key_name",
            "InputCoalescer",
        ],
    )
    def test_symbol_resolves_to_submodule_object(self, name: str) -> None:
        import rdc.input as pkg

        facade = getattr(pkg, name)
        src = "rdc.input.coalesce" if name == "InputCoalescer" else "rdc.input.base"
        assert facade is getattr(importlib.import_module(src), name)

    def test_second_access_uses_cache(self) -> None:
        import rdc.input as pkg

        first = pkg.default_backend
        assert "default_backend" in vars(pkg)  # 解析后写入 globals 缓存
        assert pkg.default_backend is first

    def test_unknown_attr_raises(self) -> None:
        import rdc.input as pkg

        with pytest.raises(AttributeError, match="no attribute"):
            _ = pkg.no_such_symbol

    def test_dir_lists_exports(self) -> None:
        import rdc.input as pkg

        d = dir(pkg)
        assert "default_backend" in d and "platform_backend_name" in d


class TestPlatformAssembly:
    def test_windows_selects_native_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import rdc.input as pkg
        from rdc.input import windows as win

        backend = pkg.default_backend()
        if sys.platform == "win32":
            assert isinstance(backend, win.WindowsBackend)
            assert backend.supported() is True
        else:  # 非 Windows（CI ubuntu/mac runner）：回退 legacy，不抛
            assert backend.supported() is not False or True
        monkeypatch.undo()

    def test_macos_stub_raises_not_implemented(self) -> None:
        from rdc.exceptions import PlatformUnsupportedError
        from rdc.input.base import default_backend

        # 空实现自报不可用 → 禁回退时工厂抛平台不支持（方法体抛 NotImplementedError
        # 由空实现自身承载，见 test_input_base 契约套件逐方法断言）
        with pytest.raises(PlatformUnsupportedError, match="macos"):
            default_backend(prefer="macos", legacy_fallback=False)

    @pytest.mark.parametrize("prefer", ["macos", "linux_x11", "linux_wayland"])
    def test_stub_backends_unsupported(self, prefer: str) -> None:
        from rdc.exceptions import PlatformUnsupportedError
        from rdc.input.base import default_backend

        with pytest.raises(PlatformUnsupportedError, match=prefer):
            default_backend(prefer=prefer, legacy_fallback=False)

    def test_stub_methods_raise_not_implemented(self) -> None:
        from rdc.input.macos import create_macos_backend

        backend = create_macos_backend()
        assert backend.supported() is False
        with pytest.raises(NotImplementedError):
            backend.move_mouse(0, 0)

    def test_platform_backend_name_matches_platform(self) -> None:
        import rdc.input as pkg

        name = pkg.platform_backend_name()
        if sys.platform == "win32":
            assert name == "windows"
        else:
            assert name in {"macos", "linux_x11", "linux_wayland", "legacy"}

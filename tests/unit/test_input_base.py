"""rdc.input.base 单元测试（阶段 7 批 A）。

覆盖：键名归一、:class:`ScreenRect` 几何、后端工厂的选择与回退、
:func:`release_all` 的容错面。工厂测试用 monkeypatch 替换
``_build_legacy``，**不导入 pyautogui、不触达真实系统输入**。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from rdc.exceptions import InputControlError, PlatformUnsupportedError
from rdc.input import base as input_base
from rdc.input.base import (
    MODIFIER_NAMES,
    InputBackend,
    MouseButton,
    ScreenRect,
    autodetect_backend_key,
    default_backend,
    normalize_key_name,
    release_all,
)

# ---------------------------------------------------------------------------
# normalize_key_name
# ---------------------------------------------------------------------------


class TestNormalizeKeyName:
    """键名归一是跨平台契约的地基，须覆盖三种来源风格。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("A", "a"),
            ("9", "9"),
            (" ", None),  # 空串单独断言抛错
        ],
    )
    def test_single_char_lowercased(self, raw: str, expected: str | None) -> None:
        if expected is None:
            with pytest.raises(InputControlError):
                normalize_key_name(raw)
        else:
            assert normalize_key_name(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # X11 / pynput 风格
            ("Control_L", "ctrl"),
            ("Control_R", "ctrl"),
            ("Shift_L", "shift"),
            ("Alt_R", "alt"),
            ("Super_L", "win"),
            ("Super_R", "win"),
            ("Page_Up", "pageup"),
            ("Page_Down", "pagedown"),
            ("Return", "enter"),
            ("Escape", "esc"),
            ("KP_1", "kp1"),
            # Qt 风格
            ("CTRL", "ctrl"),
            ("ESC", "esc"),
            ("PAGEUP", "pageup"),
            ("SHIFT", "shift"),
            # v4 KEY_MAP 值域（已是规范名，须原样稳定）
            ("ctrl", "ctrl"),
            ("enter", "enter"),
            ("backspace", "backspace"),
            ("f12", "f12"),
            ("up", "up"),
        ],
    )
    def test_aliases(self, raw: str, expected: str) -> None:
        assert normalize_key_name(raw) == expected

    def test_idempotent(self) -> None:
        """归一两次结果不变（实现端可放心重复调用）。"""
        for raw in ("Control_L", "Page_Up", "A", "Return"):
            once = normalize_key_name(raw)
            assert normalize_key_name(once) == once

    def test_unknown_multi_char_falls_back_to_lower(self) -> None:
        assert normalize_key_name("WhateverKey") == "whateverkey"

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_raises(self, raw: str) -> None:
        with pytest.raises(InputControlError, match="键名不能为空"):
            normalize_key_name(raw)

    def test_modifier_names_are_all_normalize_stable(self) -> None:
        """修饰键集合内每个名字自身必须是规范名。"""
        for name in MODIFIER_NAMES:
            assert normalize_key_name(name) == name

    def test_surrounding_whitespace_stripped(self) -> None:
        assert normalize_key_name("  Return ") == "enter"


# ---------------------------------------------------------------------------
# ScreenRect
# ---------------------------------------------------------------------------


class TestScreenRect:
    def test_edges(self) -> None:
        rect = ScreenRect(-1920, 0, 3840, 1080)
        assert rect.right == 1920
        assert rect.bottom == 1080

    @pytest.mark.parametrize(("width", "height"), [(0, 100), (10, -5), (0, 0)])
    def test_non_positive_size_raises(self, width: int, height: int) -> None:
        with pytest.raises(InputControlError, match="屏幕尺寸必须为正"):
            ScreenRect(0, 0, width, height)

    def test_negative_origin_virtual_desktop_contains(self) -> None:
        """副屏在左/上时负原点坐标必须在范围内（多显示器核心场景）。"""
        rect = ScreenRect(-1920, -1080, 3840, 2160)
        assert rect.contains(-1920, -1080)
        assert rect.contains(1919, 1079)
        assert not rect.contains(1920, 1079)  # 右边缘开区间
        assert not rect.contains(-1921, 0)

    @pytest.mark.parametrize(
        ("x", "y", "expected"),
        [
            (-9999, -9999, (-100, 0)),
            (9999, 9999, (1919, 1079)),
            (500, 500, (500, 500)),
        ],
    )
    def test_clamp(self, x: int, y: int, expected: tuple[int, int]) -> None:
        rect = ScreenRect(-100, 0, 2020, 1080)
        assert rect.clamp(x, y) == expected

    def test_clamp_result_within_bounds(self) -> None:
        rect = ScreenRect(0, 0, 1920, 1080)
        for x, y in [(-5, -5), (5000, 5000), (0, 0), (1919, 1079)]:
            assert rect.contains(*rect.clamp(x, y))

    def test_frozen(self) -> None:
        """frozen 语义：任意属性赋值都被拒（frozen dataclass 重写 ``__setattr__``，
        未声明字段名同样抛 FrozenInstanceError）。不加 slots——frozen+slots 组合在
        3.14 对未知属性赋值抛 TypeError 而非 FrozenInstanceError，属 CPython 已知
        粗糙点；本类型实例极少、slots 无收益，故不用。"""
        rect = ScreenRect(0, 0, 100, 100)
        with pytest.raises(dataclasses.FrozenInstanceError):
            rect.left = 5  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            rect.whatever = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """替换 ``_build_legacy``，避免导入 pyautogui 与触达真实输入。"""
    record: dict[str, Any] = {"called": 0}
    sentinel = object()

    def fake_build() -> Any:
        record["called"] += 1
        return sentinel

    monkeypatch.setattr(input_base, "_build_legacy", fake_build)
    yield record


class TestDefaultBackend:
    def test_unknown_prefer_raises(self) -> None:
        with pytest.raises(InputControlError, match="未知的输入后端"):
            default_backend(prefer="plan9")

    def test_prefer_legacy_uses_legacy(self, legacy_stub: dict[str, Any]) -> None:
        assert default_backend(prefer="legacy") is not None
        assert legacy_stub["called"] == 1

    def test_empty_platform_backends_fall_back_to_legacy(
        self, legacy_stub: dict[str, Any]
    ) -> None:
        """macOS/Linux 空实现 supported()=False → 自动回退 legacy。"""
        for choice in ("macos", "linux_x11", "linux_wayland"):
            assert default_backend(prefer=choice) is not None
        assert legacy_stub["called"] == 3

    def test_empty_platform_without_fallback_raises(
        self, legacy_stub: dict[str, Any]
    ) -> None:
        with pytest.raises(PlatformUnsupportedError, match="无可用输入注入后端"):
            default_backend(prefer="macos", legacy_fallback=False)
        assert legacy_stub["called"] == 0

    @pytest.mark.parametrize("choice", ["macos", "linux_x11", "linux_wayland"])
    def test_stub_modules_construct_protocol_instance(self, choice: str) -> None:
        """空实现模块可导入、工厂存在且产物符合协议（无 ImportError、无结构缺陷）。"""
        module_path, factory_name = input_base._BACKENDS[choice]  # noqa: SLF001
        backend = input_base._construct_backend(module_path, factory_name, choice)  # noqa: SLF001
        assert backend is not None
        assert isinstance(backend, InputBackend)
        assert backend.supported() is False

    @pytest.mark.parametrize("choice", ["macos", "linux_x11", "linux_wayland"])
    def test_stub_backends_fall_back_via_default(
        self, choice: str, legacy_stub: dict[str, Any]
    ) -> None:
        """空实现 supported()=False → default_backend 走回退分支。

        需 legacy_stub 打桩：批 D 落地前 ``rdc.input.legacy_pyautogui`` 尚不存在。
        """
        assert default_backend(prefer=choice, legacy_fallback=True) is not None
        assert legacy_stub["called"] == 1

    def test_missing_factory_raises_not_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, legacy_stub: dict[str, Any]
    ) -> None:
        """结构性缺陷（缺工厂函数）必须报错，不能被回退掩盖。"""
        monkeypatch.setitem(
            input_base._BACKENDS,  # noqa: SLF001 - 测试注入
            "windows",
            ("rdc.input.base", "no_such_factory"),
        )
        with pytest.raises(InputControlError, match="缺少工厂函数"):
            default_backend(prefer="windows")

    def test_broken_backend_contract_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """产物不符协议（无 move_mouse）时报错而非静默回退。"""
        monkeypatch.setitem(
            input_base._BACKENDS,  # noqa: SLF001 - 测试注入
            "windows",
            ("rdc.input.base", "autodetect_backend_key"),  # 返回 str，非后端
        )
        with pytest.raises(InputControlError, match="未实现 InputBackend 契约"):
            default_backend(prefer="windows")

    def test_import_error_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, legacy_stub: dict[str, Any]
    ) -> None:
        """平台专用依赖缺失（pyobjc/Xlib）→ 回退 legacy，不崩。"""
        monkeypatch.setitem(
            input_base._BACKENDS,
            "macos",
            ("this_module_does_not_exist_xyz", "create_macos_backend"),
        )
        assert default_backend(prefer="macos") is not None
        assert legacy_stub["called"] == 1

    def test_import_error_without_fallback_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            input_base._BACKENDS,
            "macos",
            ("this_module_does_not_exist_xyz", "create_macos_backend"),
        )
        with pytest.raises(PlatformUnsupportedError):
            default_backend(prefer="macos", legacy_fallback=False)


class TestAutodetectBackendKey:
    """探测函数只依赖 sys.platform / 环境变量，可全分支覆盖。"""

    def test_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        assert autodetect_backend_key() == "windows"

    def test_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        assert autodetect_backend_key() == "macos"

    def test_linux_x11(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert autodetect_backend_key() == "linux_x11"

    def test_linux_wayland(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert autodetect_backend_key() == "linux_wayland"

    def test_other_platform_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "freebsd14")
        assert autodetect_backend_key() == "legacy"


class TestReleaseAll:
    """异常路径清理工具：单键失败不影响其余键，且不静默吞错。"""

    def test_releases_every_key(self) -> None:
        released: list[str] = []

        class B:
            def key_release(self, key: str) -> None:
                released.append(key)

        release_all(B(), ["ctrl", "shift"])  # type: ignore[arg-type]
        assert released == ["ctrl", "shift"]

    def test_failure_does_not_stop_remaining(self) -> None:
        released: list[str] = []

        class B:
            def key_release(self, key: str) -> None:
                if key == "bad":
                    raise InputControlError("注入失败")
                released.append(key)

        release_all(B(), ["ctrl", "bad", "shift"])  # type: ignore[arg-type]
        assert released == ["ctrl", "shift"]

    def test_empty_iterable_is_noop(self) -> None:
        class B:
            def key_release(self, key: str) -> None:  # pragma: no cover - 不应被调用
                raise AssertionError("不应被调用")

        release_all(B(), [])  # type: ignore[arg-type]


class TestMouseButtonContract:
    """鼠标键枚举必须与 ScreenView 的 ``press:<btn>`` 字符串载荷同名。"""

    @pytest.mark.parametrize(("member", "value"), list(MouseButton.__members__.items()))
    def test_values_match_screen_view_payload(self, member: str, value: str) -> None:
        assert member.lower() == value
        assert value in {"left", "right", "middle"}

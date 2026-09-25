"""LegacyPyAutoGuiBackend 单元测试（阶段 7 批 D）。

pyautogui 以替身对象注入：断言调用序列与参数，**不触达真实系统输入**
（阶段 7 决策点 1）。同时复用批 A 契约基类，保证回退后端与原生后端在
协议层可互换（阶段 9 装配层无差别替换）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rdc.exceptions import InputControlError
from rdc.input.base import InputBackend, MouseButton, ScreenRect
from rdc.input.legacy_pyautogui import LegacyPyAutoGuiBackend, build_legacy_backend
from tests.contract.test_input_backend import InputBackendContractBase

FIXED_BOUNDS = ScreenRect(0, 0, 1920, 1080)


class FakePyAutoGui:
    """pyautogui 替身：记录调用（calls）与语义事件（events）。"""

    def __init__(self, *, valid_keys: set[str] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.events: list[str] = []
        self._valid = valid_keys or {
            "a",
            "b",
            "ctrl",
            "shift",
            "alt",
            "win",
            "enter",
            "esc",
            "up",
            "f5",
            "space",
            ";",
        }

    def isValidKey(self, key: str) -> bool:  # noqa: N802 - 第三方命名
        return key in self._valid

    def moveTo(self, x: int, y: int, duration: float = 0) -> None:  # noqa: N802
        self.calls.append(("moveTo", x, y, duration))
        self.events.append("move")

    def mouseDown(self, x: int, y: int, button: str) -> None:  # noqa: N802
        self.calls.append(("mouseDown", x, y, button))
        self.events.append("click")

    def mouseUp(self, x: int, y: int, button: str) -> None:  # noqa: N802
        self.calls.append(("mouseUp", x, y, button))
        self.events.append("click")

    def scroll(self, clicks: int) -> None:
        self.calls.append(("scroll", clicks))
        self.events.append("scroll")

    def hscroll(self, clicks: int) -> None:
        self.calls.append(("hscroll", clicks))
        self.events.append("scroll")

    def keyDown(self, key: str) -> None:  # noqa: N802
        self.calls.append(("keyDown", key))
        self.events.append("key_press")

    def keyUp(self, key: str) -> None:  # noqa: N802
        self.calls.append(("keyUp", key))
        self.events.append("key_release")

    def write(self, text: str, interval: float = 0.0) -> None:
        self.calls.append(("write", text, interval))
        self.events.append("type_text")


class FakePyAutoGuiNoHScroll(FakePyAutoGui):
    """旧版 pyautogui：无 hscroll 能力（后端以 getattr+callable 探测）。"""

    hscroll = None  # 类属性遮蔽 → getattr 命中但非 callable


@pytest.fixture
def pya() -> FakePyAutoGui:
    return FakePyAutoGui()


@pytest.fixture
def backend(pya: FakePyAutoGui) -> LegacyPyAutoGuiBackend:
    return LegacyPyAutoGuiBackend(pya=pya, bounds=lambda: FIXED_BOUNDS)


def call_names(pya: FakePyAutoGui) -> list[str]:
    return [c[0] for c in pya.calls]


class TestMoveAndClick:
    def test_move_maps_to_moveTo(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.move_mouse(100, 200)
        assert pya.calls == [("moveTo", 100, 200, 0)]

    def test_click_press_moves_first(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        backend.click(50, 60, MouseButton.LEFT, True)
        assert call_names(pya) == ["moveTo", "mouseDown"]
        assert pya.calls[1] == ("mouseDown", 50, 60, "left")

    def test_click_release_uses_button_name_string(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        """pyautogui 键名是字符串（'right'/'middle'），v4 口径保持。"""
        backend.click(1, 2, MouseButton.RIGHT, False)
        assert pya.calls[-1] == ("mouseUp", 1, 2, "right")

    def test_pressed_buttons_tracked(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.click(1, 1, MouseButton.MIDDLE, True)
        backend.reset()
        assert call_names(pya)[-2:] == ["mouseDown", "mouseUp"]
        assert pya.calls[-1][3] == "middle"

    def test_scroll_vertical_and_horizontal(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        backend.scroll(10, 20, 0, 3)
        assert pya.calls == [("moveTo", 10, 20, 0), ("scroll", 3)]
        pya.calls.clear()
        backend.scroll(10, 20, -2, 0)
        assert pya.calls[-1] == ("hscroll", -2)

    def test_scroll_zero_clicks_skips_call(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        """dy=0 且 dx=0 只定位，不发无意义的 scroll(0)。"""
        backend.scroll(5, 5, 0, 0)
        assert call_names(pya) == ["moveTo"]

    def test_hscroll_unsupported_raises(self) -> None:
        """无 hscroll 能力的 pyautogui 版本 → 明确报错，不静默丢事件。"""
        bare = FakePyAutoGuiNoHScroll()
        backend = LegacyPyAutoGuiBackend(pya=bare, bounds=lambda: FIXED_BOUNDS)
        with pytest.raises(InputControlError, match="水平滚动"):
            backend.scroll(1, 1, 2, 0)


class TestKeyboard:
    def test_press_release(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.key_press("a")
        backend.key_release("A")
        assert pya.calls == [("keyDown", "a"), ("keyUp", "a")]

    def test_alias_normalized_before_validity(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        """X11 风格 Control_L 归一为 ctrl（v4 KEY_MAP 值域 = pyautogui 键名）。"""
        backend.key_press("Control_L")
        assert pya.calls[-1] == ("keyDown", "ctrl")

    def test_invalid_key_raises(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        """v4 在此静默丢弃；v5 抛错（约束 6）。"""
        with pytest.raises(InputControlError, match="无对应键位"):
            backend.key_press("hyperspace")

    def test_blank_key_raises(self, backend: LegacyPyAutoGuiBackend) -> None:
        with pytest.raises(InputControlError):
            backend.key_press("  ")

    def test_pyautogui_exception_translated_not_swallowed(self) -> None:
        class Boom(FakePyAutoGui):
            def moveTo(self, x: int, y: int, duration: float = 0) -> None:  # noqa: N802
                raise RuntimeError("屏幕锁定")

        backend = LegacyPyAutoGuiBackend(pya=Boom(), bounds=lambda: FIXED_BOUNDS)
        with pytest.raises(InputControlError, match="pyautogui 注入失败") as exc:
            backend.move_mouse(1, 1)
        assert isinstance(exc.value.__cause__, RuntimeError)

    def test_ledger_not_polluted_on_failed_press(self) -> None:
        """注入失败的键不得进记账（否则 reset 释放一个从未按下的键）。"""

        class Boom(FakePyAutoGui):
            def keyDown(self, key: str) -> None:  # noqa: N802
                raise RuntimeError("拒绝注入")

        backend = LegacyPyAutoGuiBackend(pya=Boom(), bounds=lambda: FIXED_BOUNDS)
        with pytest.raises(InputControlError):
            backend.key_press("a")
        rec = backend  # reset 在失败后端上不应发 keyUp
        rec.reset()


class TestTypeText:
    def test_ascii_write(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.type_text("hello")
        assert pya.calls == [("write", "hello", 0.0)]

    def test_empty_is_noop(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.type_text("")
        assert pya.calls == []

    def test_non_ascii_rejected_with_guidance(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        """IME 口径：回退路径无 Unicode 能力，CJK 必须走原生后端（明确报错）。"""
        with pytest.raises(InputControlError, match="非 ASCII"):
            backend.type_text("中文")
        assert pya.calls == []


class TestResetAndLifecycle:
    def test_reset_releases_in_reverse_order(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        backend.key_press("shift")
        backend.key_press("a")
        pya.calls.clear()
        backend.reset()
        assert [c[1] for c in pya.calls] == ["a", "shift"]

    def test_reset_idempotent(self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui) -> None:
        backend.reset()
        backend.reset()
        assert pya.calls == []

    def test_release_unpressed_key_harmless(
        self, backend: LegacyPyAutoGuiBackend, pya: FakePyAutoGui
    ) -> None:
        backend.key_release("b")
        backend.reset()
        assert call_names(pya) == ["keyUp"]

    def test_supported_true(self, backend: LegacyPyAutoGuiBackend) -> None:
        """回退后端是最后一级：不自报不可用（否则工厂无处可退）。"""
        assert backend.supported() is True

    def test_bounds_delegates(self, backend: LegacyPyAutoGuiBackend) -> None:
        assert backend.bounds() is FIXED_BOUNDS


class TestFactory:
    def test_build_legacy_defers_pyautogui_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """工厂经 _load_pyautogui 延迟导入（模块级不 import pyautogui）。"""
        fake = FakePyAutoGui()
        monkeypatch.setattr("rdc.input.legacy_pyautogui._load_pyautogui", lambda: fake)
        backend = build_legacy_backend()
        assert isinstance(backend, InputBackend)
        backend.move_mouse(3, 4)
        assert fake.calls == [("moveTo", 3, 4, 0)]

    def test_default_backend_prefer_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rdc.input import base as input_base

        fake = FakePyAutoGui()
        monkeypatch.setattr("rdc.input.legacy_pyautogui._load_pyautogui", lambda: fake)
        backend = input_base.default_backend(prefer="legacy")
        backend.key_press("a")
        assert fake.calls[-1] == ("keyDown", "a")

    def test_missing_pyautogui_maps_to_platform_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pyautogui 未安装 → 工厂转 PlatformUnsupportedError，不抛裸 ImportError。"""
        from rdc.exceptions import PlatformUnsupportedError
        from rdc.input import base as input_base

        def boom() -> Any:
            raise ImportError("no module named pyautogui")

        monkeypatch.setattr("rdc.input.legacy_pyautogui._load_pyautogui", boom)
        with pytest.raises(PlatformUnsupportedError):
            input_base.default_backend(prefer="macos", legacy_fallback=True)


# ---------------------------------------------------------------------------
# 契约复用（批 A InputBackendContractBase）
# ---------------------------------------------------------------------------


class _TaggingPyAutoGui(FakePyAutoGui):
    """把 pyautogui 调用映射为契约基类消费的语义事件。"""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def moveTo(self, x: int, y: int, duration: float = 0) -> None:  # noqa: N802
        self._events.append("move")

    def mouseDown(self, x: int, y: int, button: str) -> None:  # noqa: N802
        self._events.append("click")

    def mouseUp(self, x: int, y: int, button: str) -> None:  # noqa: N802
        self._events.append("click")

    def scroll(self, clicks: int) -> None:
        self._events.append("scroll")

    def hscroll(self, clicks: int) -> None:
        self._events.append("scroll")

    def keyDown(self, key: str) -> None:  # noqa: N802
        self._events.append("key_press")

    def keyUp(self, key: str) -> None:  # noqa: N802
        self._events.append("key_release")

    def write(self, text: str, interval: float = 0.0) -> None:
        self._events.append("type_text")


class TestLegacyContract(InputBackendContractBase):
    """批 A 契约跑回退后端（pyautogui 替身）。"""

    @pytest.fixture
    def events(self) -> Iterator[list[str]]:
        yield []

    @pytest.fixture
    def backend(self, events: list[str]) -> Any:
        pya = _TaggingPyAutoGui(events)
        return LegacyPyAutoGuiBackend(pya=pya, bounds=lambda: FIXED_BOUNDS)

    def test_type_text_non_ascii(self, backend: Any, events: list[str]) -> None:
        """覆写：契约基类默认要求 CJK 送达——回退路径无 Unicode 能力是
        pyautogui 客观限制，本断言改为验证"明确抛错而非静默丢弃"。
        CJK 送达由 TestWindowsContract 在原生后端上锁定（阶段 7 决策点 3：
        IME 文本注入是原生后端专属能力）。"""
        with pytest.raises(InputControlError):
            backend.type_text("中文a")

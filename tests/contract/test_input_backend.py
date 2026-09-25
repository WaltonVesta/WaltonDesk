"""InputBackend 契约测试（阶段 7）。

分两层：

1. :class:`InputBackendContractBase` —— **真实后端**必须通过的语义契约
   （坐标 clamp、键名归一、reset 释放、``supported()`` 为真）。只依赖协议
   方法与子类提供的 ``events`` 采集夹具（语义事件名序列），不假设记录的
   元组形状，供批 B ``WindowsBackend`` 与批 D ``legacy_pyautogui`` 复用。
   本文件自带 :class:`_RecordingBackend` 作为契约基类的元验证。
2. 空实现契约（macOS / Linux X11 / Linux Wayland）——逐方法断言抛
   :class:`NotImplementedError` 且 ``supported()`` 为 ``False``。

基类命名不带 ``Test`` 前缀，避免被 pytest 单独收集（沿用阶段 5 约定）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from rdc.exceptions import InputControlError
from rdc.input.base import (
    InputBackend,
    MouseButton,
    ScreenRect,
    normalize_key_name,
)
from rdc.input.linux_wayland import LinuxWaylandBackend, wayland_session_detected
from rdc.input.linux_x11 import LinuxX11Backend
from rdc.input.macos import MacOSBackend

# ---------------------------------------------------------------------------
# 1. 真实后端契约基类
# ---------------------------------------------------------------------------


class InputBackendContractBase:
    """所有真实可用后端必须满足的语义（批 B / 批 D 子类复用）。

    子类须提供两个夹具：

    - ``backend``：注入边界已打桩的后端实例——契约测试**绝不**触达真实
      系统输入，否则劫持操作员鼠标键盘（阶段 7 决策点 1）。
    - ``events``：可读写列表，后端每次注入往里 append 一个语义标签字符串
      （``"move"`` / ``"click"`` / ``"scroll"`` / ``"key_press"`` /
      ``"key_release"`` / ``"type_text"``）。协议不规定记录格式，故本基类
      只消费标签序列。
    """

    def test_supported(self, backend: InputBackend) -> None:
        """可用后端必须自报 supported，否则工厂会跳过它。"""
        assert backend.supported() is True

    def test_contracts_protocol(self, backend: InputBackend) -> None:
        """运行时可检查协议：方法集齐全。"""
        assert isinstance(backend, InputBackend)

    def test_bounds_positive(self, backend: InputBackend) -> None:
        """bounds 必须是正尺寸矩形。"""
        bounds = backend.bounds()
        assert isinstance(bounds, ScreenRect)
        assert bounds.width > 0 and bounds.height > 0

    def test_move_out_of_range_clamped_not_raised(
        self, backend: InputBackend, events: list[str]
    ) -> None:
        """越界坐标 clamp 而非抛错（远端分辨率变化时上层来不及感知）。"""
        bounds = backend.bounds()
        backend.move_mouse(bounds.left - 5000, bounds.top - 5000)
        backend.move_mouse(bounds.right + 5000, bounds.bottom + 5000)
        assert events.count("move") == 2

    def test_click_moves_first(
        self,
        backend: InputBackend,
        events: list[str],
    ) -> None:
        """按键前必须先定位，保证按下与释放落在同一坐标。"""
        events.clear()
        backend.click(120, 240, MouseButton.LEFT, True)
        kinds = _tags(events)
        assert "click" in kinds
        assert "move" in kinds[: kinds.index("click")]

    def test_press_and_release_pair(
        self, backend: InputBackend, events: list[str]
    ) -> None:
        """按下→释放成对出现（各一次 move+click）。"""
        events.clear()
        backend.click(10, 10, MouseButton.RIGHT, True)
        backend.click(10, 10, MouseButton.RIGHT, False)
        assert _tags(events).count("click") == 2

    def test_key_name_normalized(
        self, backend: InputBackend, events: list[str]
    ) -> None:
        """键名注入前归一（``Control_L`` → ``ctrl``），由实现端负责。"""
        events.clear()
        backend.key_press("Control_L")
        assert _tags(events).count("key_press") == 1

    def test_empty_key_name_raises(self, backend: InputBackend) -> None:
        """空键名属调用方 bug，必须显式报错而非静默。"""
        with pytest.raises(InputControlError):
            backend.key_press("   ")

    def test_reset_releases_recorded_state(self, backend: InputBackend, events: list[str]) -> None:
        """reset 释放全部已按下键与鼠标键（v4 无此保证）。"""
        backend.key_press("shift")
        backend.click(1, 1, MouseButton.LEFT, True)
        events.clear()
        backend.reset()
        tags = _tags(events)
        assert "key_release" in tags
        assert "click" in tags

    def test_reset_idempotent(self, backend: InputBackend) -> None:
        """无键按下时 reset 为无害空操作，且可重入。"""
        backend.reset()
        backend.reset()

    def test_type_text_empty_is_noop(self, backend: InputBackend, events: list[str]) -> None:
        """空串不产生注入。"""
        events.clear()
        backend.type_text("")
        assert _tags(events) == []

    def test_type_text_non_ascii(self, backend: InputBackend, events: list[str]) -> None:
        """CJK 文本必须送达（IME 口径：已上屏文本直投，不依赖键盘布局）。"""
        events.clear()
        backend.type_text("中文a")
        assert "type_text" in _tags(events)

    def test_scroll_vertical_and_horizontal(
        self, backend: InputBackend, events: list[str]
    ) -> None:
        """支持垂直与水平滚动（补 v4 的 dx TODO）。"""
        events.clear()
        backend.scroll(50, 50, 0, 3)
        backend.scroll(50, 50, -2, 0)
        assert _tags(events).count("scroll") == 2


def _tags(events: list[Any]) -> list[str]:
    """从采集夹具提取语义标签序列（兼容记录元组/字符串两种形态）。"""
    tags: list[str] = []
    for item in events:
        tags.append(item if isinstance(item, str) else str(item[0]))
    return tags


# ---------------------------------------------------------------------------
# 元验证后端：证明契约基类自身正确（不依赖平台模块）
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """把语义标签写入共享列表的假后端，供契约基类元验证与工厂测试复用。"""

    def __init__(self, events: list[str], bounds: ScreenRect | None = None) -> None:
        self._events = events
        self._bounds = bounds or ScreenRect(0, 0, 1920, 1080)
        self._keys: set[str] = set()
        self._buttons: set[MouseButton] = set()
        self.last_move: tuple[int, int] = (0, 0)

    def move_mouse(self, x: int, y: int) -> None:
        self._events.append("move")
        self.last_move = self._bounds.clamp(x, y)

    def click(self, x: int, y: int, button: MouseButton, press: bool) -> None:
        self.move_mouse(x, y)
        self._events.append("click")
        if press:
            self._buttons.add(button)
        else:
            self._buttons.discard(button)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._events.append("scroll")

    def key_press(self, key: str) -> None:
        normalized = normalize_key_name(key)
        self._events.append("key_press")
        self._keys.add(normalized)

    def key_release(self, key: str) -> None:
        normalized = normalize_key_name(key)
        self._events.append("key_release")
        self._keys.discard(normalized)

    def type_text(self, text: str) -> None:
        if text:
            self._events.append("type_text")

    def bounds(self) -> ScreenRect:
        return self._bounds

    def reset(self) -> None:
        self._events.extend(["key_release"] * len(self._keys))
        self._events.extend(["click"] * len(self._buttons))
        self._keys.clear()
        self._buttons.clear()

    def supported(self) -> bool:
        return True


class TestRecordingBackendContract(InputBackendContractBase):
    """元验证：契约基类可跑，且标签采集约定成立。"""

    @pytest.fixture
    def events(self) -> Iterator[list[str]]:
        yield []

    @pytest.fixture
    def backend(self, events: list[str]) -> InputBackend:
        return _RecordingBackend(events)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 2. 空实现契约（阶段 7 范围调整：Windows 优先）
# ---------------------------------------------------------------------------

EMPTY_BACKENDS: dict[str, Callable[[], Any]] = {
    "macos": MacOSBackend,
    "linux_x11": LinuxX11Backend,
    "linux_wayland": LinuxWaylandBackend,
}

#: 必须抛 NotImplementedError 的方法名 → 调用器。
_METHOD_CALLS: dict[str, Callable[[Any], None]] = {
    "move_mouse": lambda b: b.move_mouse(10, 10),
    "click": lambda b: b.click(10, 10, MouseButton.LEFT, True),
    "scroll": lambda b: b.scroll(10, 10, 0, 1),
    "key_press": lambda b: b.key_press("a"),
    "key_release": lambda b: b.key_release("a"),
    "type_text": lambda b: b.type_text("hi"),
    "bounds": lambda b: b.bounds(),
    "reset": lambda b: b.reset(),
}


@pytest.mark.parametrize("name", sorted(EMPTY_BACKENDS))
class TestEmptyBackendContract:
    """三个空实现：全方法抛 NotImplementedError、supported 为 False。"""

    @pytest.fixture
    def backend(self, name: str) -> Any:
        return EMPTY_BACKENDS[name]()

    def test_protocol_methods_exist(self, backend: Any) -> None:
        """空实现也必须方法齐全（否则将来补实现时上层先炸）。"""
        assert isinstance(backend, InputBackend)

    def test_supported_false(self, backend: Any) -> None:
        assert backend.supported() is False

    @pytest.mark.parametrize("method", sorted(_METHOD_CALLS))
    def test_raises_not_implemented(self, backend: Any, method: str) -> None:
        """逐方法断言未实现（含 reset/bounds，不只注入方法）。"""
        with pytest.raises(NotImplementedError):
            _METHOD_CALLS[method](backend)


class TestWaylandSessionDetection:
    """Wayland 会话探测为纯环境变量判断（供工厂在 Linux 上二选一）。"""

    def test_xdg_session_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert wayland_session_detected() is True

    def test_wayland_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert wayland_session_detected() is True

    def test_x11_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert wayland_session_detected() is False

    def test_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert wayland_session_detected() is False

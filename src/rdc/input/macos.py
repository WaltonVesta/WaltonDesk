"""macOS 输入后端——**阶段 7 空实现**（用户决定：Windows 优先）。

真实实现应为 CGEventPost（Quartz / pyobjc），本机无法验证，故本文件只保留
与 :class:`rdc.input.base.InputBackend` 完全一致的签名 + 抛
:class:`NotImplementedError`。将来补齐 macOS 只需替换本文件方法体，
**协议不变**，上层（阶段 9 装配层）零改动。

真实实现待办（供后续阶段/CI 参考）：
- ``kCGEventMouseMoved`` / ``kCGEventLeftMouseDown`` 等源事件构造；
- ``CGEventSetIntegerValueField``（clickCount）与 ``CGEventSetLocation``；
- 键盘用 ``CGEventKeyboardSetUnicodeString`` 处理 Unicode，键码用
  ``kVK_*``（需 ``Carbon.HIToolbox`` 键码表）；
- 多显示器需 ``CGGetActiveDisplayList`` 求虚拟桌面并集；
- 依赖 Accessibility 授权（``AXIsProcessTrusted``），未授权时应报
  :class:`rdc.exceptions.PlatformUnsupportedError` 而非静默失败。
"""

from __future__ import annotations

from typing import NoReturn

from rdc.input.base import InputBackend, MouseButton, ScreenRect

__all__ = ["MacOSBackend", "create_macos_backend"]

_MESSAGE = "macOS 输入后端为阶段 7 空实现（Windows 优先，见 PROGRESS.md 范围调整）"


def _unsupported() -> NoReturn:
    """统一抛出未实现错误（保持各方法消息一致）。"""
    raise NotImplementedError(_MESSAGE)


class MacOSBackend(InputBackend):
    """macOS 输入后端空实现。"""

    def move_mouse(self, x: int, y: int) -> None:
        """（未实现）移动光标。"""
        _unsupported()

    def click(self, x: int, y: int, button: MouseButton, press: bool) -> None:
        """（未实现）按键按下/释放。"""
        _unsupported()

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """（未实现）滚轮滚动。"""
        _unsupported()

    def key_press(self, key: str) -> None:
        """（未实现）按键按下。"""
        _unsupported()

    def key_release(self, key: str) -> None:
        """（未实现）按键释放。"""
        _unsupported()

    def type_text(self, text: str) -> None:
        """（未实现）文本注入。"""
        _unsupported()

    def bounds(self) -> ScreenRect:
        """（未实现）虚拟桌面范围。"""
        _unsupported()

    def reset(self) -> None:
        """（未实现）释放全部按键。"""
        _unsupported()

    def supported(self) -> bool:
        """空实现恒不可用，令 :func:`rdc.input.base.default_backend` 回退。"""
        return False


def create_macos_backend() -> InputBackend:
    """构造 macOS 后端（空实现，``supported()`` 为 ``False``）。"""
    return MacOSBackend()

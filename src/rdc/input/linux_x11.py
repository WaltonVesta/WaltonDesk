"""Linux X11 输入后端——**阶段 7 空实现**（用户决定：Windows 优先）。

真实实现应为 XTest（Xlib / python-xlib），本机无法验证，故本文件只保留与
:class:`rdc.input.base.InputBackend` 一致的签名 + 抛
:class:`NotImplementedError`。将来补齐只替换方法体，协议不变。

真实实现待办（供后续阶段/CI 参考，REFACTOR_PLAN §7.4）：
- ``display.get_default().display.screen`` + ``XTEST`` 扩展（``xt_test``）；
- 移动：``xtest_fake_input(XTestCompareCursor… )`` 或用
  ``screen.warp_pointer``（XWarpPointer）；
- 按键：keysym → keycode 用 ``display.keysym_to_keysym/keysym_to_keycode``，
  Unicode 走 ``XKB`` 或 ``xdotool`` 式 unicode 键码映射；
- 滚轮：button 4/5（上/下）、6/7（左/右）；
- 多显示器：``xinerache_get_screen_count`` + ``xinerache_get_screen_info``
  求虚拟桌面并集；
- 依赖 ``python-xlib``（LGPL-2.0——⚠️ 约束 9 需按"LGPL 动态链接允许"评估，
  或改用 ``Xlib`` 的替代：ctypes 直接加载 ``libXtst``，无许可证风险）。
"""

from __future__ import annotations

from typing import NoReturn

from rdc.input.base import InputBackend, MouseButton, ScreenRect

__all__ = ["LinuxX11Backend", "create_linux_x11_backend"]

_MESSAGE = "Linux X11 输入后端为阶段 7 空实现（Windows 优先，见 PROGRESS.md 范围调整）"


def _unsupported() -> NoReturn:
    """统一抛出未实现错误（保持各方法消息一致）。"""
    raise NotImplementedError(_MESSAGE)


class LinuxX11Backend(InputBackend):
    """Linux X11 输入后端空实现。"""

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


def create_linux_x11_backend() -> InputBackend:
    """构造 Linux X11 后端（空实现，``supported()`` 为 ``False``）。"""
    return LinuxX11Backend()

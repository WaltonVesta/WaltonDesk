"""Linux Wayland 输入后端——**阶段 7 空实现**（用户决定：Windows 优先）。

与 :mod:`rdc.input.linux_x11` 同为空实现。额外提供会话探测函数
:func:`wayland_session_detected`，供
:func:`rdc.input.base.autodetect_backend_key` 在 Linux 上二选一。

真实实现待办（供后续阶段/CI 参考，REFACTOR_PLAN §7.5）：
- Wayland 合成器禁止客户端直接注入输入，须走外部通道：
  - **libei**（emulated input，Fedora 40+ / ``XDG_TOOLKIT=…``）：
    ``ei_new_from_context`` + ``ei_event`` 序列，需 ``EI_DEFAULT_DISPLAY``；
  - **ydotool**：uinput 后端 + ``ydotoold`` 守护进程，需 root/udev 规则；
- 二者优先 libei（无 root），探测顺序：环境变量
  ``XDG_SESSION_TYPE=wayland`` → ``WAYLAND_DISPLAY`` 存在 →
  ``libei`` 可导入；任一失败按 X11 路径处理；
- 依赖：``libei`` 的 Python 绑定（``ei``）为 LGPL/MIT 混合，落地前按约束 9 核。
"""

from __future__ import annotations

import os
from typing import NoReturn

from rdc.input.base import InputBackend, MouseButton, ScreenRect

__all__ = [
    "LinuxWaylandBackend",
    "create_linux_wayland_backend",
    "wayland_session_detected",
]

_MESSAGE = "Linux Wayland 输入后端为阶段 7 空实现（Windows 优先，见 PROGRESS.md 范围调整）"


def _unsupported() -> NoReturn:
    """统一抛出未实现错误（保持各方法消息一致）。"""
    raise NotImplementedError(_MESSAGE)


def wayland_session_detected() -> bool:
    """当前会话是否为 Wayland（纯环境变量判断，无副作用）。

    Returns:
        ``XDG_SESSION_TYPE`` 含 ``wayland``，或 ``WAYLAND_DISPLAY`` 非空。
    """
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if "wayland" in session_type:
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


class LinuxWaylandBackend(InputBackend):
    """Linux Wayland 输入后端空实现。"""

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


def create_linux_wayland_backend() -> InputBackend:
    """构造 Linux Wayland 后端（空实现，``supported()`` 为 ``False``）。"""
    return LinuxWaylandBackend()

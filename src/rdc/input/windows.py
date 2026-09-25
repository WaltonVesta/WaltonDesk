"""Windows 输入后端：SendInput（ctypes，阶段 7 批 B，本机可验）。

实现 :class:`rdc.input.base.InputBackend` 契约：

- **结构**：``INPUT`` = ``DWORD type`` + 匿名联合（``MOUSEINPUT`` /
  ``KEYBDINPUT`` / ``HARDWAREINPUT``），按 C 对齐规则用 ctypes.Structure +
  Union 复刻（x64 下 ``sizeof(INPUT)`` = 40，测试锁定）；
- **绝对坐标**：``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``，像素坐标
  按虚拟桌面并集归一化到 ``0..65535``，天然支持多显示器负原点；
- **Unicode**：``KEYEVENTF_UNICODE`` 逐 UTF-16 码元直投（IME 口径：已上屏
  文本，不模拟组合态），``\\n``/``\\r`` 译为 ``VK_RETURN``，BMP 外字符拆代理对；
- **按键**：规范键名 → VK 码表（对齐 v4 ``KEY_MAP`` 值域）+ 方向/编辑键扩展
  标志 ``KEYEVENTF_EXTENDEDKEY``；单字符标点经 ``VkKeyScanW`` 兜底，需 Shift
  组合的字符**拒绝**（大小写/符号文本一律走 :meth:`WindowsBackend.type_text`）。

可测性（阶段 7 决策点 1）：注入边界（``send``）、虚拟桌面范围（``bounds``）、
字符扫描（``scan_char``）全部构造注入——单测断言组装出的结构体
（flags/dx/dy/mouseData/wVk/wScan），**绝不触达真实系统输入**。
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable, Sequence
from typing import Final

from rdc.exceptions import InputControlError
from rdc.input.base import InputBackend, MouseButton, ScreenRect, normalize_key_name

__all__ = ["WindowsBackend", "create_windows_backend"]

# ---------------------------------------------------------------------------
# Win32 常量（winuser.h）
# ---------------------------------------------------------------------------

INPUT_MOUSE: Final = 0x0000
INPUT_KEYBOARD: Final = 0x0001

# MOUSEINPUT.dwFlags
MOUSEEVENTF_MOVE: Final = 0x0001
MOUSEEVENTF_LEFTDOWN: Final = 0x0002
MOUSEEVENTF_LEFTUP: Final = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
MOUSEEVENTF_RIGHTUP: Final = 0x0010
MOUSEEVENTF_MIDDLEDOWN: Final = 0x0020
MOUSEEVENTF_MIDDLEUP: Final = 0x0040
MOUSEEVENTF_WHEEL: Final = 0x0800
MOUSEEVENTF_HWHEEL: Final = 0x1000
MOUSEEVENTF_VIRTUALDESK: Final = 0x4000
MOUSEEVENTF_ABSOLUTE: Final = 0x8000

# KEYBDINPUT.dwFlags
KEYEVENTF_EXTENDEDKEY: Final = 0x0001
KEYEVENTF_KEYUP: Final = 0x0002
KEYEVENTF_UNICODE: Final = 0x0004

#: 一次滚轮"格"的像素当量（Win32 WHEEL_DELTA）。
WHEEL_DELTA: Final = 120

#: 绝对坐标归一化上限（SendInput 文档固定 0..65535）。
ABSOLUTE_MAX: Final = 65535

#: 单次 SendInput 调用的输入条数上限（防超长文本一次注入被系统截断）。
MAX_BATCH: Final = 64

# GetSystemMetrics 索引（虚拟桌面并集）
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


# ---------------------------------------------------------------------------
# 结构体（ctypes 复刻 Win32 布局）
# ---------------------------------------------------------------------------


class _MOUSEINPUT(ctypes.Structure):
    """MOUSEINPUT（winuser.h）。"""

    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    """KEYBDINPUT（winuser.h）。"""

    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    """HARDWAREINPUT（winuser.h，仅为联合布局完整，本项目不使用）。"""

    _fields_ = [
        ("uMsg", ctypes.c_ushort),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    """INPUT 的匿名联合成员（x64 下 8 字节对齐，4 字节 padding 由 ctypes 复刻）。"""

    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """Win32 ``INPUT``：``DWORD type`` + 匿名联合。"""

    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUTUNION),
    ]


#: INPUT 的合法尺寸：x64=40，x86=28（测试锁定，防布局改动静默错位）。
EXPECTED_INPUT_SIZE: Final = 40 if sys.maxsize > 2**32 else 28


# ---------------------------------------------------------------------------
# 平台 API 装载（非 Windows 置 None；supported() 返回 False 触发工厂回退）
# ---------------------------------------------------------------------------

_HAS_WINAPI: Final = sys.platform == "win32"

if sys.platform == "win32":  # pragma: no cover - 非 Windows 分支由 CI 矩阵覆盖
    _user32 = ctypes.windll.user32
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint
    _SendInput = _user32.SendInput
    _GetSystemMetrics = _user32.GetSystemMetrics
    _VkKeyScanW = _user32.VkKeyScanW
else:  # pragma: no cover
    _SendInput = None
    _GetSystemMetrics = None
    _VkKeyScanW = None

#: 注入边界类型：一批 INPUT → 返回实际注入条数（真实实现为 SendInput）。
SendFn = Callable[[Sequence[INPUT]], int]
#: 虚拟桌面范围提供器。
BoundsFn = Callable[[], ScreenRect]
#: 字符 → VkKeyScanW 返回值（-1 表示无映射）。
ScanFn = Callable[[str], int]


def _real_send(inputs: Sequence[INPUT]) -> int:
    """调用真实 SendInput（非 Windows 时抛 :class:`InputControlError`）。"""
    if _SendInput is None:
        raise InputControlError(f"当前平台 ({sys.platform}) 无 Win32 SendInput")
    array = (INPUT * len(inputs))(*inputs)
    return int(_SendInput(len(inputs), array, ctypes.sizeof(INPUT)))


def _system_bounds() -> ScreenRect:
    """GetSystemMetrics 读虚拟桌面并集（多显示器含负原点）。"""
    if _GetSystemMetrics is None:
        raise InputControlError(f"当前平台 ({sys.platform}) 无 GetSystemMetrics")
    left = int(_GetSystemMetrics(_SM_XVIRTUALSCREEN))
    top = int(_GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(_GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(_GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        raise InputControlError(
            f"虚拟桌面尺寸非法（{width}x{height}，常见于无桌面会话的服务进程）"
        )
    return ScreenRect(left, top, width, height)


def _win_scan_char(ch: str) -> int:
    """字符 → VkKeyScanW（非 Windows 抛错）。"""
    if _VkKeyScanW is None:
        raise InputControlError(f"当前平台 ({sys.platform}) 无法扫描字符 {ch!r}")
    return int(_VkKeyScanW(ch))


# ---------------------------------------------------------------------------
# 规范键名 → VK 码表（值域与 v4 KEY_MAP 一致 + 方向/编辑/小键盘扩展）
# ---------------------------------------------------------------------------

_VK_TABLE: Final[dict[str, int]] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,  # VK_LWIN
    "altgr": 0xA5,  # VK_RMENU（带扩展标志）
    "numlock": 0x90,
    "scrolllock": 0x91,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},
    **{f"kp{i}": 0x60 + i for i in range(10)},
}

#: 需 KEYEVENTF_EXTENDEDKEY 的键（增强键盘上有独立扫描码）。
_VK_EXTENDED: Final[frozenset[str]] = frozenset(
    {
        "up",
        "down",
        "left",
        "right",
        "insert",
        "delete",
        "home",
        "end",
        "pageup",
        "pagedown",
        "altgr",
    }
)

_MOUSE_BUTTON_FLAGS: Final[dict[MouseButton, tuple[int, int]]] = {
    MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

#: 绝对移动的标志组合（虚拟桌面归一化）。
_MOVE_ABS: Final = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK


def _make_mouse(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
    """组装鼠标 INPUT（``data`` 按 DWORD 位型写入，负值以补码表达）。"""
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi.dx = dx
    inp.u.mi.dy = dy
    inp.u.mi.mouseData = data & 0xFFFFFFFF
    inp.u.mi.dwFlags = flags
    return inp


def _make_key(vk: int, scan: int, flags: int) -> INPUT:
    """组装键盘 INPUT。"""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    return inp


def _utf16_units(ch: str) -> list[int]:
    """字符 → UTF-16 码元列表（BMP 内 1 个，BMP 外代理对 2 个）。"""
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


class WindowsBackend:
    """Windows SendInput 输入后端。

    Args:
        send: 注入边界（默认真实 ``SendInput``；单测传记录器）。
        bounds: 虚拟桌面范围提供器（默认 ``GetSystemMetrics``；单测传固定值）。
        scan_char: 字符扫描函数（默认 ``VkKeyScanW``；单测传固定表）。
    """

    def __init__(
        self,
        *,
        send: SendFn | None = None,
        bounds: BoundsFn | None = None,
        scan_char: ScanFn | None = None,
    ) -> None:
        self._send = send if send is not None else _real_send
        self._bounds_fn = bounds if bounds is not None else _system_bounds
        self._scan_char = scan_char if scan_char is not None else _win_scan_char
        #: 已按下键记账：``(wVk, 按下时的基础 flags)``，reset 时逆序释放。
        self._pressed_keys: list[tuple[int, int]] = []
        self._pressed_buttons: set[MouseButton] = set()

    # -- 协议方法 --------------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> None:
        """移动到虚拟桌面绝对坐标（clamp + 归一化 + VIRTUALDESK）。"""
        nx, ny = self._normalize(x, y)
        self._dispatch([_make_mouse(nx, ny, _MOVE_ABS)])

    def click(self, x: int, y: int, button: MouseButton, press: bool) -> None:
        """先定位再改变按键状态（按下与释放同坐标，契约要求）。"""
        button = MouseButton(button)
        down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
        nx, ny = self._normalize(x, y)
        self._dispatch(
            [_make_mouse(nx, ny, _MOVE_ABS), _make_mouse(nx, ny, down_flag if press else up_flag)]
        )
        if press:
            self._pressed_buttons.add(button)
        else:
            self._pressed_buttons.discard(button)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """定位后滚动：dy>0 上滚（正 mouseData），dx≠0 走 HWHEEL（补 v4 缺口）。"""
        nx, ny = self._normalize(x, y)
        inputs = [_make_mouse(nx, ny, _MOVE_ABS)]
        if dy != 0:
            inputs.append(_make_mouse(nx, ny, MOUSEEVENTF_WHEEL, dy * WHEEL_DELTA))
        if dx != 0:
            inputs.append(_make_mouse(nx, ny, MOUSEEVENTF_HWHEEL, dx * WHEEL_DELTA))
        self._dispatch(inputs)

    def key_press(self, key: str) -> None:
        """按下键（规范名归一 → VK 码 + 扩展标志）。"""
        vk, base_flags = self._resolve_key(key)
        self._dispatch([_make_key(vk, 0, base_flags)])
        self._pressed_keys.append((vk, base_flags))

    def key_release(self, key: str) -> None:
        """释放键。"""
        vk, base_flags = self._resolve_key(key)
        self._dispatch([_make_key(vk, 0, base_flags | KEYEVENTF_KEYUP)])
        for i in range(len(self._pressed_keys) - 1, -1, -1):
            if self._pressed_keys[i][0] == vk:
                del self._pressed_keys[i]
                break

    def type_text(self, text: str) -> None:
        """Unicode 码元直投（IME 口径：已上屏文本；BMP 外拆代理对）。

        ``\\n``/``\\r`` 译为 Enter 按下+释放；其余每码元 down+up。每批至多
        :data:`MAX_BATCH` 条 INPUT，超出自动分批。
        """
        if not text:
            return
        pending: list[INPUT] = []
        for ch in text:
            if ch in ("\n", "\r"):
                enter_vk = _VK_TABLE["enter"]
                pending.append(_make_key(enter_vk, 0, 0))
                pending.append(_make_key(enter_vk, 0, KEYEVENTF_KEYUP))
                continue
            for unit in _utf16_units(ch):
                pending.append(_make_key(0, unit, KEYEVENTF_UNICODE))
                pending.append(_make_key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            if len(pending) >= MAX_BATCH:
                self._dispatch(pending)
                pending = []
        if pending:
            self._dispatch(pending)

    def bounds(self) -> ScreenRect:
        """当前虚拟桌面并集（实时读取，分辨率变化自动跟随）。"""
        return self._bounds_fn()

    def reset(self) -> None:
        """释放全部已按下键与鼠标键（可重入；空状态不产生注入）。"""
        inputs: list[INPUT] = []
        for vk, base_flags in reversed(self._pressed_keys):
            inputs.append(_make_key(vk, 0, base_flags | KEYEVENTF_KEYUP))
        self._pressed_keys = []
        for button in (MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE):
            if button in self._pressed_buttons:
                inputs.append(_make_mouse(0, 0, _MOUSE_BUTTON_FLAGS[button][1]))
                self._pressed_buttons.discard(button)
        if inputs:
            self._dispatch(inputs)

    def supported(self) -> bool:
        """仅 Windows（SendInput 装载成功）时为 True。"""
        return _HAS_WINAPI

    # -- 内部 ------------------------------------------------------------------

    def _normalize(self, x: int, y: int) -> tuple[int, int]:
        """虚拟桌面像素坐标 → SendInput 绝对坐标（0..65535），先 clamp。"""
        rect = self._bounds_fn()
        cx, cy = rect.clamp(x, y)
        span_x = max(rect.width - 1, 1)
        span_y = max(rect.height - 1, 1)
        nx = round((cx - rect.left) * ABSOLUTE_MAX / span_x)
        ny = round((cy - rect.top) * ABSOLUTE_MAX / span_y)
        return nx, ny

    def _resolve_key(self, key: str) -> tuple[int, int]:
        """键名 → ``(wVk, 按下基础 flags)``。

        表内键直接映射；单字符 a-z/0-9 用自身 ASCII VK；其余单字符经
        ``scan_char``（VkKeyScanW）——需 Shift 组合或无映射的字符**拒绝**，
        大小写与符号文本一律走 :meth:`type_text`。
        """
        normalized = normalize_key_name(key)  # 空串在这里抛 InputControlError
        vk = _VK_TABLE.get(normalized)
        if vk is not None:
            extended = KEYEVENTF_EXTENDEDKEY if normalized in _VK_EXTENDED else 0
            return vk, extended
        if len(normalized) == 1:
            if "a" <= normalized <= "z" or "0" <= normalized <= "9":
                return ord(normalized.upper()), 0
            scan = self._scan_char(normalized)
            if scan == -1:
                raise InputControlError(f"键 {key!r} 在当前键盘布局无对应 VK")
            if scan > 0xFF:
                raise InputControlError(
                    f"键 {key!r} 需 Shift 组合；此类字符请用 type_text 注入"
                )
            return scan & 0xFF, 0
        raise InputControlError(f"无法解析键名 {key!r}")

    def _dispatch(self, inputs: Sequence[INPUT]) -> None:
        """经注入边界发送并校验条数（少注入即报错，不静默吞掉）。"""
        if not inputs:
            return
        sent = self._send(inputs)
        if sent != len(inputs):
            raise InputControlError(
                f"SendInput 部分失败：请求 {len(inputs)} 条，实际注入 {sent} 条"
            )


def create_windows_backend() -> InputBackend:
    """构造 Windows 后端（工厂契约，见 :func:`rdc.input.base.default_backend`）。"""
    return WindowsBackend()

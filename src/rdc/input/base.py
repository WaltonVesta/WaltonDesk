"""输入注入后端抽象（阶段 7）。

本模块定义 v5 输入层的**唯一契约**：:class:`InputBackend`。上层（阶段 9 的
client controller / 装配层）只依赖本协议，不感知平台差异；平台实现按
``sys.platform`` 在工厂 :func:`default_backend` 中选择：

- Windows: :mod:`rdc.input.windows`（SendInput，本机可验，阶段 7 主交付）
- macOS:   :mod:`rdc.input.macos`（**空实现**，CI/交付后验证）
- Linux:   :mod:`rdc.input.linux_x11` / :mod:`rdc.input.linux_wayland`
  （**空实现**，CI/交付后验证）
- 回退:    :mod:`rdc.input.legacy_pyautogui`（v4 pyautogui 路径）

设计要点
--------
1. **坐标系**：所有 ``x/y`` 一律是**远端虚拟桌面绝对坐标**（所有显示器拼起来的
   坐标系，副屏可为负原点），与 :class:`rdc.ui.screen_view` 发出的载荷同义。
   上层不做缩放/偏移换算；实现端用 :class:`ScreenRect` 做 clamp 与归一化基准。
2. **键名规范**：跨平台统一小写规范键名（``enter`` / ``esc`` / ``f5`` / ``a``），
   归一入口是 :func:`normalize_key_name`；规范名值域与 v4 ``KEY_MAP`` 的值域
   一致，使 X11/pynput 风格与 Qt 风格键名落到同一字符串。
3. **IME 口径**（阶段 7 决定）：**发送侧不破坏 IME、接收侧不模拟 IME**。已上屏
   文本经 :meth:`InputBackend.type_text` 以 Unicode 码元直投；输入法组合态
   （候选窗）属被控端本地状态，v5 不在协议内表达。
4. **状态兜底**：:meth:`InputBackend.reset` 必须在断连/失去焦点时被调用，释放
   所有已按下的键与按钮（v4 无此保证，会残留"卡住的 Ctrl"）。

注意：本包 ``__init__`` 刻意不做 eager re-export，一律用完整模块路径导入。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from rdc.exceptions import InputControlError, PlatformUnsupportedError
from rdc.logging import get_logger

_LOG = get_logger("input.base")

__all__ = [
    "KEY_ALIASES",
    "MODIFIER_NAMES",
    "InputBackend",
    "MouseButton",
    "ScreenRect",
    "default_backend",
    "normalize_key_name",
    "release_all",
]


class MouseButton(StrEnum):
    """鼠标键（与 :class:`rdc.ui.screen_view` 的 ``press:<btn>`` 载荷同名）。"""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


#: 修饰键规范名（组合键与 :meth:`InputBackend.reset` 兜底用）。
MODIFIER_NAMES: frozenset[str] = frozenset({"ctrl", "shift", "alt", "win", "altgr"})

#: 异名 → v5 规范键名。未命中的多字符键名按 ``strip().lower()`` 处理。
KEY_ALIASES: dict[str, str] = {
    # 功能/编辑键
    "return": "enter",
    "kp_enter": "enter",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "escape": "esc",
    "spacebar": "space",
    "pageup": "pageup",
    "page_up": "pageup",
    "prior": "pageup",
    "pagedown": "pagedown",
    "page_down": "pagedown",
    "next": "pagedown",
    "printscreen": "printscreen",
    "print": "printscreen",
    "capslock": "capslock",
    "numlock": "numlock",
    "scrolllock": "scrolllock",
    # 修饰键（左/右合并；需区分左右时实现端自行扩展 *_l/*_r）
    "control": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "option": "alt",
    "super": "win",
    "super_l": "win",
    "super_r": "win",
    "meta": "win",
    "cmd": "win",
    # 小键盘数字（与主键盘区分，避免被控端 NumLock 关闭时行为漂移）
    "kp_0": "kp0",
    "kp_1": "kp1",
    "kp_2": "kp2",
    "kp_3": "kp3",
    "kp_4": "kp4",
    "kp_5": "kp5",
    "kp_6": "kp6",
    "kp_7": "kp7",
    "kp_8": "kp8",
    "kp_9": "kp9",
}


def normalize_key_name(key: str) -> str:
    """把任意来源的键名归一为 v5 规范键名。

    Args:
        key: 键名，接受 X11/pynput 风格（``Control_L``）、Qt 风格
            （``Escape``）、大小写混合，或单个可打印字符。

    Returns:
        规范键名：单字符返回小写形式；其余查 :data:`KEY_ALIASES`，未命中返回
        ``key.strip().lower()``。

    Raises:
        InputControlError: ``key`` 去掉首尾空白后为空。
    """
    stripped = key.strip()
    if not stripped:
        raise InputControlError("键名不能为空")
    lowered = stripped.lower()
    if len(lowered) == 1:
        return lowered
    return KEY_ALIASES.get(lowered, lowered)


@dataclass(frozen=True)
class ScreenRect:
    """虚拟桌面矩形（所有显示器的并集），单位物理像素。

    Attributes:
        left: 最左边缘 x 坐标（副屏在主屏左侧时为负）。
        top: 最上边缘 y 坐标（副屏在主屏上方时为负）。
        width: 宽度（恒 >0）。
        height: 高度（恒 >0）。
    """

    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise InputControlError(f"屏幕尺寸必须为正，实得 {self.width}x{self.height}")

    @property
    def right(self) -> int:
        """右边缘（不含）。"""
        return self.left + self.width

    @property
    def bottom(self) -> int:
        """下边缘（不含）。"""
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        """点 ``(x, y)`` 是否落在矩形内（右/下边缘为开区间）。"""
        return self.left <= x < self.right and self.top <= y < self.bottom

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        """把点夹进矩形可注入范围（右/下各留 1px，对齐开区间语义）。"""
        return (
            max(self.left, min(x, self.right - 1)),
            max(self.top, min(y, self.bottom - 1)),
        )


@runtime_checkable
class InputBackend(Protocol):
    """平台输入注入后端协议。

    实现类必须满足以下语义（阶段 7 契约测试逐项锁定）：

    - **坐标**：全部为远端虚拟桌面绝对坐标；越界坐标由实现端 clamp，
      **不得抛异常**（远端分辨率变化时上层来不及感知）。
    - **幂等**：重复同向按键调用不得让实现端内部记账错位。
    - **线程**：允许从非 GUI 线程调用；实现须自行保证底层调用线程安全。
    - **失败**：注入失败一律抛 :class:`rdc.exceptions.InputControlError`，
      不得静默吞掉（约束 6）。
    - :meth:`reset` 必须可重入，且在无键按下时调用为无害空操作。
    - 空实现（macOS/Linux）所有方法抛 :class:`NotImplementedError`，
      :meth:`supported` 返回 ``False``，供 :func:`default_backend` 回退。
    """

    def move_mouse(self, x: int, y: int) -> None:
        """把光标移动到虚拟桌面坐标 ``(x, y)``。"""
        ...

    def click(self, x: int, y: int, button: MouseButton, press: bool) -> None:
        """在 ``(x, y)`` 按下（``press=True``）或释放鼠标键。

        实现应先移动再改变按键状态，保证按下与释放落在同一坐标。
        """
        ...

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """在 ``(x, y)`` 滚动滚轮。

        Args:
            dx: 水平滚动格数（>0 右滚；0 表示纯垂直滚动）。
            dy: 垂直滚动格数（>0 上滚，与 Win32 ``wheel`` 语义一致）。
        """
        ...

    def key_press(self, key: str) -> None:
        """按下键 ``key``（实现端先经 :func:`normalize_key_name` 归一）。"""
        ...

    def key_release(self, key: str) -> None:
        """释放键 ``key``。"""
        ...

    def type_text(self, text: str) -> None:
        """注入已上屏文本（Unicode 码元直投，不模拟输入法组合态）。

        空串为合法空操作；BMP 外码元（emoji 等）由实现端拆代理对。
        """
        ...

    def bounds(self) -> ScreenRect:
        """返回当前虚拟桌面范围，供坐标 clamp 与归一化使用。"""
        ...

    def reset(self) -> None:
        """释放本后端记录的所有已按下键与鼠标键（断连兜底）。"""
        ...

    def supported(self) -> bool:
        """本后端在当前机器上是否真的可用（空实现返回 ``False``）。"""
        ...


#: 后端键 → ``(模块路径, 工厂函数名)``。
_BACKENDS: dict[str, tuple[str, str]] = {
    "windows": ("rdc.input.windows", "create_windows_backend"),
    "macos": ("rdc.input.macos", "create_macos_backend"),
    "linux_x11": ("rdc.input.linux_x11", "create_linux_x11_backend"),
    "linux_wayland": ("rdc.input.linux_wayland", "create_linux_wayland_backend"),
}


def autodetect_backend_key() -> str:
    """按 ``sys.platform`` 与会话环境变量给出后端选择键。

    Returns:
        ``"windows"`` / ``"macos"`` / ``"linux_x11"`` / ``"linux_wayland"`` /
        ``"legacy"``（未识别平台）。
    """
    import sys

    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        from rdc.input.linux_wayland import wayland_session_detected

        return "linux_wayland" if wayland_session_detected() else "linux_x11"
    return "legacy"


def default_backend(
    *,
    prefer: str | None = None,
    legacy_fallback: bool = True,
) -> InputBackend:
    """按当前平台构造输入后端。

    Args:
        prefer: 强制指定实现，取值 ``"windows"`` / ``"macos"`` /
            ``"linux_x11"`` / ``"linux_wayland"`` / ``"legacy"``；``None``
            表示按 :func:`autodetect_backend_key` 自动选择。
        legacy_fallback: 平台原生后端不可用（含空实现）时是否回退到
            :mod:`rdc.input.legacy_pyautogui`。为 ``False`` 时直接抛
            :class:`rdc.exceptions.PlatformUnsupportedError`。

    Returns:
        满足 :class:`InputBackend` 的实例。

    Raises:
        InputControlError: ``prefer`` 取值未知，或后端模块缺少工厂函数。
        PlatformUnsupportedError: 目标平台无可用后端且禁用回退，或平台专用
            依赖缺失（pyobjc / Xlib）。
    """
    choice = prefer or autodetect_backend_key()
    if choice == "legacy":
        return _build_legacy()
    if choice not in _BACKENDS:
        raise InputControlError(f"未知的输入后端: {choice!r}")

    module_path, factory_name = _BACKENDS[choice]
    backend = _construct_backend(module_path, factory_name, choice)
    if backend is not None and backend.supported():
        return backend
    if not legacy_fallback:
        raise PlatformUnsupportedError(f"平台 {choice} 无可用输入注入后端")
    _LOG.info("输入后端不可用，回退 pyautogui", backend=choice)
    return _build_legacy()


def _construct_backend(
    module_path: str, factory_name: str, choice: str
) -> InputBackend | None:
    """导入并构造平台后端；平台专用依赖缺失时返回 ``None``（交由上层回退）。

    Raises:
        InputControlError: 模块存在但缺少工厂函数，或产物不符合契约——
            属结构性缺陷，**不回退**（回退会掩盖 bug）。
    """
    import importlib

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # 平台专用扩展缺失（pyobjc / Xlib / libei）
        _LOG.warning("输入后端导入失败", backend=choice, error=str(exc))
        return None
    factory = getattr(module, factory_name, None)
    if factory is None:
        raise InputControlError(f"输入后端 {choice} 缺少工厂函数 {factory_name}")
    backend: InputBackend = factory()
    if not callable(getattr(backend, "move_mouse", None)):
        raise InputControlError(f"输入后端 {choice} 未实现 InputBackend 契约")
    return backend


def _build_legacy() -> InputBackend:
    """延迟导入回退后端（pyautogui 属可选依赖，仅在真正回退时加载）。

    静态 import 会让 ``import rdc.input.base`` 在 pyautogui 缺失的机器上直接
    失败——而契约层本身与平台无关，不应被回退实现的依赖牵连。

    Raises:
        PlatformUnsupportedError: pyautogui 未安装（回退路径本身不可用）。
        InputControlError: 模块存在但工厂缺失或产物不符契约。
    """
    import importlib

    try:
        module = importlib.import_module("rdc.input.legacy_pyautogui")
        factory = getattr(module, "build_legacy_backend", None)
        if factory is None:  # pragma: no cover - 结构性防护
            raise InputControlError("回退后端缺少工厂函数 build_legacy_backend")
        backend: InputBackend = factory()
    except ImportError as exc:
        _LOG.warning("pyautogui 回退后端不可用", error=str(exc))
        raise PlatformUnsupportedError(f"pyautogui 回退后端不可用: {exc}") from exc
    return backend


def release_all(backend: InputBackend, keys: Iterable[str]) -> None:
    """尽力释放一批键（异常路径清理用）。

    单个键释放失败不影响其余键，但以 warning 记录——不静默吞错。

    Args:
        backend: 目标后端。
        keys: 待释放的键名集合（未归一亦可，实现端负责归一）。
    """
    logger = _LOG
    for key in keys:
        try:
            backend.key_release(key)
        except InputControlError as exc:
            logger.warning("释放按键失败", key=key, error=str(exc))

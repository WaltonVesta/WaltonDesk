"""pyautogui 回退输入后端（阶段 7 批 D，v4 ``input/mouse.py`` + ``input/keyboard.py`` 合并）。

定位：**最后一级回退**。当平台原生后端（Windows SendInput / 未来的 macOS、
Linux）不可用时，:func:`rdc.input.base.default_backend` 退到本实现——继承 v4
的 pyautogui 注入路径，保证"最低可用"而非"最优"。

与 v4 的关系（有意保留 / 有意剔除）：

- 保留：注入语义逐一对应 v4 ``MouseController.execute_event`` /
  ``KeyboardController.execute_event``（moveTo / mouseDown / mouseUp /
  scroll / keyDown / keyUp）；键名沿用 v4 ``KEY_MAP`` 值域——恰与
  pyautogui 键名一致（v4 当年就是按 pyautogui 设计的），归一函数直接复用。
- 剔除 ①：v4 的 pynput **监听**侧（Listener/on_move/on_click）不迁移——
  v5 输入捕获归 UI 层 ScreenView（阶段 6），本模块只剩**注入**方向。
- 剔除 ②：v4 ``execute_event(dict)`` 的裸字典协议不迁移——v5 一律走
  :class:`rdc.input.base.InputBackend` 类型化方法。
- 剔除 ③：v4 键盘防抖（50ms 丢弃）不迁移——合并职责已由批 C
  :class:`rdc.input.coalesce.InputCoalescer` 按帧承担，注入层不再私自丢键。
- 修复：v4 ``except Exception: pass`` 式吞错（约束 6）全部改为抛
  :class:`rdc.exceptions.InputControlError`；新增 v4 缺失的按键记账与
  :meth:`reset`（断连不再残留卡住的修饰键）。

能力限制（回退路径的自觉）：pyautogui 仅 ASCII 键位，**不支持 CJK 文本**——
:meth:`type_text` 抛错并指向原生后端；滚轮水平方向在旧 pyautogui 缺失时同样
明确报错，不静默降级。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

from rdc.exceptions import InputControlError
from rdc.input.base import InputBackend, MouseButton, ScreenRect, normalize_key_name

__all__ = ["LegacyPyAutoGuiBackend", "build_legacy_backend"]


class _PyAutoGui(Protocol):
    """本模块实际消费的 pyautogui 表面（单测以替身对象实现）。"""

    def isValidKey(self, key: str) -> bool: ...  # noqa: N802 - 第三方命名
    def moveTo(self, x: int, y: int, duration: float = ...) -> None: ...  # noqa: N802
    def mouseDown(self, x: int, y: int, button: str) -> None: ...  # noqa: N802
    def mouseUp(self, x: int, y: int, button: str) -> None: ...  # noqa: N802
    def scroll(self, clicks: int) -> None: ...
    def hscroll(self, clicks: int) -> None: ...
    def keyDown(self, key: str) -> None: ...  # noqa: N802
    def keyUp(self, key: str) -> None: ...  # noqa: N802
    def write(self, text: str, interval: float = ...) -> None: ...


def _load_pyautogui() -> _PyAutoGui:
    """延迟导入 pyautogui（可选依赖，缺失由工厂转成 PlatformUnsupportedError）。"""
    import pyautogui

    return pyautogui  # type: ignore[no-any-return]


def _system_bounds() -> ScreenRect:
    """虚拟桌面并集（Windows 走 GetSystemMetrics，其余取 pyautogui 主屏）。

    与 :func:`rdc.input.windows._system_bounds` 同语义但不依赖 Win32 装载成功
    ——回退后端可能在非 Windows 使用（如未来 Linux pyautogui 场景）。
    """
    import sys

    if sys.platform == "win32":  # pragma: no cover - 本机真实路径
        import ctypes

        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width > 0 and height > 0:
            return ScreenRect(left, top, width, height)
    import pyautogui

    width, height = pyautogui.size()
    return ScreenRect(0, 0, int(width), int(height))


class LegacyPyAutoGuiBackend:
    """pyautogui 回退后端（实现 InputBackend 协议）。

    Args:
        pya: pyautogui 替身（单测注入；默认真实模块，延迟导入）。
        bounds: 虚拟桌面范围提供器（单测注入固定值）。
    """

    def __init__(
        self,
        *,
        pya: _PyAutoGui | None = None,
        bounds: Callable[[], ScreenRect] | None = None,
    ) -> None:
        self._pya = pya if pya is not None else _load_pyautogui()
        self._bounds_fn = bounds if bounds is not None else _system_bounds
        # pyautogui 非线程安全：注入串行化（契约"实现须自行保证线程安全"）
        self._lock = threading.Lock()
        self._pressed_keys: list[str] = []
        self._pressed_buttons: set[MouseButton] = set()

    # -- 协议方法 --------------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> None:
        """绝对移动（越界由 pyautogui 自行限制在屏幕内，契约要求不抛错）。"""
        with self._lock:
            self._call(self._pya.moveTo, x, y, duration=0)

    def click(self, x: int, y: int, button: MouseButton, press: bool) -> None:
        """先 moveTo 再 mouseDown/mouseUp（与原生后端一致的先定位语义）。"""
        button = MouseButton(button)
        with self._lock:
            self._call(self._pya.moveTo, x, y, duration=0)
            if press:
                self._call(self._pya.mouseDown, x, y, button.value)
                self._pressed_buttons.add(button)
            else:
                self._call(self._pya.mouseUp, x, y, button.value)
                self._pressed_buttons.discard(button)

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """定位后滚动；水平滚动无能力时明确抛错（不静默丢事件）。"""
        with self._lock:
            self._call(self._pya.moveTo, x, y, duration=0)
            if dy != 0:
                self._call(self._pya.scroll, dy)
            if dx != 0:
                hscroll = getattr(self._pya, "hscroll", None)
                if not callable(hscroll):
                    raise InputControlError("回退后端不支持水平滚动，请启用原生后端")
                self._call(hscroll, dx)

    def key_press(self, key: str) -> None:
        """按下键（归一键名须是 pyautogui 合法键，否则抛错——v4 在此静默）。"""
        name = self._resolve(key)
        with self._lock:
            self._call(self._pya.keyDown, name)
            self._pressed_keys.append(name)

    def key_release(self, key: str) -> None:
        """释放键并从记账移除。"""
        name = self._resolve(key)
        with self._lock:
            self._call(self._pya.keyUp, name)
            if name in self._pressed_keys:
                self._pressed_keys.remove(name)

    def type_text(self, text: str) -> None:
        """ASCII 文本经 pyautogui.write；含非 ASCII 一律抛错。

        IME 口径说明：回退路径没有 Unicode 注入能力（pyautogui 限制），
        CJK 文本必须走原生后端（Windows SendInput KEYEVENTF_UNICODE）。
        """
        if not text:
            return
        if any(ord(ch) > 127 for ch in text):
            raise InputControlError(
                "pyautogui 回退后端不支持非 ASCII 文本（CJK 需原生后端）"
            )
        with self._lock:
            self._call(self._pya.write, text, interval=0.0)

    def bounds(self) -> ScreenRect:
        """虚拟桌面范围。"""
        return self._bounds_fn()

    def reset(self) -> None:
        """释放全部记账的键与鼠标键（可重入；空状态零注入）。"""
        with self._lock:
            for name in reversed(self._pressed_keys):
                self._call(self._pya.keyUp, name)
            self._pressed_keys = []
            for button in (MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE):
                if button in self._pressed_buttons:
                    self._call(self._pya.mouseUp, 0, 0, button.value)
                    self._pressed_buttons.discard(button)

    def supported(self) -> bool:
        """pyautogui 装载成功即为 True（回退后端是"最后一级"，不再自我回退）。"""
        return True

    # -- 内部 ------------------------------------------------------------------

    def _resolve(self, key: str) -> str:
        """键名归一 + 合法性检查（v4 ``_translate_key`` 的强类型化替代）。"""
        name = normalize_key_name(key)
        if not self._pya.isValidKey(name):
            raise InputControlError(f"键 {key!r} 在 pyautogui 无对应键位")
        return name

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """调用 pyautogui，异常统一转 InputControlError（不吞栈信息）。"""
        try:
            fn(*args, **kwargs)
        except InputControlError:
            raise
        except Exception as exc:  # 精确转译而非静默（约束 6）
            raise InputControlError(f"pyautogui 注入失败: {exc}") from exc


def build_legacy_backend() -> InputBackend:
    """构造回退后端（工厂契约，见 :func:`rdc.input.base.default_backend`）。"""
    return LegacyPyAutoGuiBackend()

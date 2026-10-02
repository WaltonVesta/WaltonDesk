"""输入注入（阶段 7 交付 · 阶段 9 批 4 装配门面）。

模块地图：
- ``base``：InputBackend 协议 / ScreenRect / 键名归一 / 后端工厂；
- ``windows``：SendInput（ctypes，本机可验，主交付）；
- ``macos`` / ``linux_x11`` / ``linux_wayland``：**空实现**（阶段 7 范围
  调整：Windows 优先；补实现只改本包，协议不动）；
- ``legacy_pyautogui``：v4 路径回退（原生后端不可用时）；
- ``coalesce``：事件流合并 + 帧对齐（纯逻辑）；
- ``keys``：Qt 键码 → 规范键名（IME 双通道的键名侧）。

装配门面（阶段 9 任务 A2）：本 ``__init__`` 经 **PEP 562 ``__getattr__``**
暴露常用符号——包导入本身零 eager import（v4 的
``service/__init__ → daemon → gui.tray → tkinter`` 加载链教训），但访问
``rdc.input.default_backend`` 等符号时惰性转发到子模块。平台语义：

- Windows → ``WindowsBackend``（SendInput）；
- macOS / Linux → 空实现自报 ``supported()=False``，工厂按
  ``legacy_fallback`` 回退 pyautogui 或抛
  :class:`rdc.exceptions.PlatformUnsupportedError`（方法体抛
  :class:`NotImplementedError` 的口径由空实现自身承载）。

v4 的 mouse/keyboard/dpi/modifiers 四件已删除（语义由以上模块承接，
git 历史可查）。其余符号一律用完整模块路径导入。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "InputBackend",
    "InputCoalescer",
    "MouseButton",
    "ScreenRect",
    "autodetect_backend_key",
    "default_backend",
    "normalize_key_name",
]

#: 惰性导出符号 → 源模块（访问时才 import，包加载零副作用）。
_LAZY: dict[str, str] = {
    "InputBackend": "rdc.input.base",
    "MouseButton": "rdc.input.base",
    "ScreenRect": "rdc.input.base",
    "default_backend": "rdc.input.base",
    "autodetect_backend_key": "rdc.input.base",
    "normalize_key_name": "rdc.input.base",
    "InputCoalescer": "rdc.input.coalesce",
}


def __getattr__(name: str) -> Any:  # noqa: ANN401 - PEP 562 协议
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # 一次解析后缓存（后续访问零开销）
    return value


def __dir__() -> list[str]:
    return sorted([*__all__, "platform_backend_name"])


def platform_backend_name() -> str:
    """当前平台将选中的后端键（诊断/文档用，不构造实例）。"""
    from rdc.input.base import autodetect_backend_key

    return autodetect_backend_key()

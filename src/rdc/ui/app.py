"""QApplication 装配（阶段 6.1 + 6.3 桥接入口）。

职责：
1. 高 DPI：``PassThrough`` 取整策略（计划 6.1；**必须在 QApplication 构造前**设置，
   Qt 6 文档硬约束）；
2. QSS：从包内 ``theme/dark.qss`` 读取模板（``$token`` 占位，string.Template——
   CSS 块语法天然带花括号，禁用 str.format），用
   :data:`rdc.ui.theme.tokens.TOKENS` 渲染后整表应用；Python 绘图件与样式表
   共用同一令牌源，色值不漂移；
3. asyncio↔Qt 桥接：**qasync**（PoC 实测选型，scripts/poc/，2026-09-25：
   BSD-2-Clause 合规；竞争负载下 QTimer p95 1.15ms，四项判据全过）。
   asyncio 事件循环跑在 Qt 主线程——网络协程回调天然主线程；
   worker 线程（pipeline executor）触控件由 :func:`ensure_gui_thread`
   运行时断言拦截（结构性约束 + 运行时双保险）。
"""

from __future__ import annotations

import asyncio
import string
from collections.abc import Callable, Coroutine, Sequence
from importlib import resources
from typing import TYPE_CHECKING, Any, Final, TypeVar

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from rdc.brand import BRAND_NAME
from rdc.logging import get_logger
from rdc.ui.icons import build_app_icon
from rdc.ui.theme.tokens import TOKENS

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from qasync import QEventLoop

_T = TypeVar("_T")

__all__ = [
    "create_app",
    "ensure_gui_thread",
    "event_loop",
    "load_stylesheet",
    "run_async",
]

_LOG = get_logger("ui.app")

_QSS_RESOURCE: Final[str] = "dark.qss"


def create_app(argv: Sequence[str] | None = None) -> QApplication:
    """构造（或复用）QApplication 并套用主题。

    高 DPI 取整策略仅在**首次**构造时生效（Qt 硬约束）；复用已存在的
    QApplication（pytest-qt 场景）时跳过，不报错。

    Args:
        argv: 命令行参数；``None`` 时传空（Qt 只需要程序名）。

    Returns:
        应用单例（已 setStyleSheet）。
    """
    inst = QApplication.instance()
    app = inst if isinstance(inst, QApplication) else None
    if app is None:
        # 高 DPI 策略只对新构造的应用生效（Qt 硬约束），复用路径跳过
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(list(argv) if argv is not None else [])
    if not app.styleSheet():
        app.setStyleSheet(load_stylesheet())
    # 阶段 10 品牌接入：应用名 + 默认窗口图标。图标设在 QApplication 上，
    # 主窗/启动器/设置/口令等**全部**窗口自动继承（漏设单个窗体不再可能）；
    # 主窗另显式 setWindowIcon 一次，防 pytest-qt 复用已有 app 实例时
    # 本函数的缓存路径未覆盖到。
    app.setApplicationName(BRAND_NAME)
    icon = build_app_icon()
    if icon is not None and app.windowIcon().isNull():
        app.setWindowIcon(icon)
    return app


def load_stylesheet() -> str:
    """读取并渲染深色主题 QSS（``$token`` 占位 ← TOKENS 字段）。

    Returns:
        可直接 setStyleSheet 的字符串。

    Raises:
        KeyError: 模板引用了不存在的令牌（打包/编辑错误，构造期即爆）。
        FileNotFoundError: 包内资源缺失。
    """
    ref = resources.files("rdc.ui.theme").joinpath(_QSS_RESOURCE)
    template = ref.read_text(encoding="utf-8")
    return string.Template(template).substitute(**vars(TOKENS))


def event_loop(app: QApplication | None = None) -> QEventLoop:
    """创建绑定到 Qt 应用的 qasync 事件循环（桥接选型见模块 docstring）。

    Args:
        app: 目标应用；``None`` 取当前实例。

    Raises:
        RuntimeError: 无 QApplication 实例。
    """
    from qasync import QEventLoop as _QEventLoop

    target = app if app is not None else QApplication.instance()
    if target is None:
        raise RuntimeError("event_loop() 需要先 create_app()")
    return _QEventLoop(target)


def run_async[T](
    coro_factory: Callable[[AbstractEventLoop], Coroutine[Any, Any, T]],
    *,
    argv: Sequence[str] | None = None,
) -> T:
    """GUI 主入口：Qt 循环 + asyncio 协程同线程运行到完成。

    Args:
        coro_factory: ``fn(loop) -> coroutine``；协程返回即退出事件循环。
        argv: 透传给 :func:`create_app`。

    Returns:
        主协程的返回值。
    """
    app = create_app(argv)
    loop = event_loop(app)
    asyncio.set_event_loop(loop)
    return asyncio.run(coro_factory(loop), loop_factory=lambda: loop)


def ensure_gui_thread(widget_name: str = "") -> None:
    """跨线程控件操作守卫（验收硬约束的可执行化）。

    GUI 线程静默返回；否则抛 :class:`RuntimeError`（消息含控件名便于定位）。
    用法：自定义 widget 的**公开写入口**第一行调用本函数。
    注意：qasync 下协程与 Qt 同线程天然合法；本守卫主要拦截
    **worker 线程误触**（pipeline executor、pynput 回调线程等）。

    Raises:
        RuntimeError: 当前线程不是 Qt 主线程。
    """
    app = QCoreApplication.instance()
    if app is None:
        return  # 无应用（纯逻辑单测）：无从判定，放行
    if not app.thread().isCurrentThread():
        raise RuntimeError(
            f"跨线程控件操作被禁止：{widget_name or 'widget'} 的调用发生在非 GUI 线程，"
            f"请改用 Signal 投递（见 rdc/ui/__init__.py 红线）"
        )

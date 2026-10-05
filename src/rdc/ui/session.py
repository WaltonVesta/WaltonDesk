"""GUI 会话装配（阶段 9 批 4）：launcher → 被控端（托盘）/ 主控端（主窗）主编排。

职责边界：

- :func:`qimage_factory` —— controller 解码帧（(H,W,3) uint8 RGB 视图）→
  ``QImage``。⚠️ 必须 ``.copy()``：Qt 侧 QImage 不拷贝构造入参字节，numpy
  临时缓冲释放后会悬空（阶段 7 绑定陷阱系列的同类坑）；
- :func:`build_client_controller` —— 主控端装配（image_factory 注入）；
- :func:`run_gui` —— ``rdc`` 无参/GUI 模式主入口：
  :func:`rdc.ui.launcher.launch_choice` 选模式，HOST →
  :class:`rdc.server.service.HostService` + :class:`TrayController`
  （暂停/恢复/重启/退出），CLIENT → :class:`MainWindow` +
  :class:`ClientController`（关窗即退出）。qasync 的 QEventLoop 由
  ``loop.run_forever()`` 驱动（内部先进 app.exec 并登记 asyncio
  running-loop），协程与 Qt 同线程——事件回调零投递。

v4 对照：``main.py:show_gui()``（Tkinter 启动菜单 + 双分支）→ launcher 协程化；
被控端托盘 v4 起在独立线程跑 pystray（未装即静默降级），v5 用
QSystemTrayIcon 常驻主循环，无跨线程问题。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from rdc.client.controller import ClientController
from rdc.config.settings import Settings, current_settings
from rdc.logging import get_logger
from rdc.server.service import HostService
from rdc.ui.app import create_app, ensure_gui_thread, event_loop
from rdc.ui.launcher import LaunchOption, launch_choice
from rdc.ui.main_window import MainWindow
from rdc.ui.tray import TrayController, TrayState

__all__ = [
    "build_client_controller",
    "client_session",
    "host_session",
    "qimage_factory",
    "run_gui",
]

_LOG = get_logger("ui.session")


def qimage_factory(width: int, height: int, rgb: object) -> QImage:
    """controller 解码帧 → ``QImage``（Format_RGB888）。

    Args:
        width / height: 帧尺寸（controller 从解码数组推导，双保险核对）。
        rgb: ``(H, W, 3)`` uint8 RGB（controller :meth:`_to_image` 交付物）。

    Returns:
        深拷贝的 QImage（与 numpy 缓冲解耦，可安全跨帧持有）。
    """
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"需要 (H,W,3) RGB 数组，实得 {arr.shape}")
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if (w, h) != (width, height):
        raise ValueError(f"尺寸不符：声明 {width}x{height}，数组 {w}x{h}")
    img = QImage(arr.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888)
    return img.copy()  # 脱离临时 bytes 生命周期（Qt 不接管构造缓冲的所有权）


def build_client_controller(settings: Settings | None = None) -> ClientController:
    """装配主控端控制器（QImage 工厂注入，UI 零改动接 MainWindow）。"""
    s = settings if settings is not None else current_settings()
    return ClientController(settings=s, image_factory=qimage_factory)


async def _toggle_pause(service: HostService, tray: TrayController) -> None:
    """托盘"暂停/恢复"：服务态与托盘图标同步。"""
    if service.is_paused:
        await service.resume()
        tray.set_state(TrayState.RUNNING)
    else:
        await service.pause()
        tray.set_state(TrayState.PAUSED)
    tray.update_client_count(service.snapshot().active_connections)


async def _restart(service: HostService, tray: TrayController) -> None:
    """托盘"重启服务"：热更口令/端口/证书的生效路径。"""
    await service.restart()
    tray.set_state(TrayState.RUNNING)


async def host_session(app: Any, settings: Settings | None = None) -> int:
    """被控端 GUI 会话（服务 + 托盘）：运行至托盘"退出"，返回退出码。

    被 :func:`run_gui`（launcher 选 HOST）与 ``rdc serve --tray`` 共用。
    """
    service = HostService(settings=settings, show_tray=True)
    if not await service.start():
        _LOG.error("gui_host_start_failed")
        return 1
    stop = asyncio.Event()
    tray = TrayController()
    tray.pauseToggled.connect(lambda: asyncio.ensure_future(_toggle_pause(service, tray)))
    tray.restartRequested.connect(lambda: asyncio.ensure_future(_restart(service, tray)))
    tray.quitRequested.connect(stop.set)
    tray.set_state(TrayState.RUNNING)
    tray.show()
    try:
        await stop.wait()
    finally:
        tray.hide()
        await service.shutdown()
    return 0


async def client_session(app: Any, settings: Settings | None = None) -> int:
    """主控端 GUI 会话（主窗 + 控制器）：关窗即退出。

    唤醒链：``WA_DeleteOnClose``（close 即销毁，否则 QMainWindow 关闭
    仅隐藏、``destroyed`` 永不触发）→ ``destroyed`` → ``stop`` →
    finally 在**运行中的环**上拆控制器（2026-10-05 走查第二批；
    配合装配处 ``quitOnLastWindowClosed=False``，Qt 不再抢先夭折协程链）。
    """
    controller = build_client_controller(settings)
    stop = asyncio.Event()
    win = MainWindow(controller=controller)
    win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    win.destroyed.connect(stop.set)
    win.show()
    try:
        await stop.wait()
    finally:
        await controller.disconnect()
    return 0


async def _gui_main(app: Any) -> None:
    """GUI 主编排协程（qasync 下与 Qt 同线程）。"""
    choice = await launch_choice()
    if choice is LaunchOption.QUIT:
        app.quit()
        return
    code = await (host_session(app) if choice is LaunchOption.HOST else client_session(app))
    del code
    app.quit()


def run_gui(argv: Sequence[str] | None = None) -> int:
    """GUI 模式主入口（阻塞至退出，返回进程退出码）。

    被控端：托盘常驻，菜单 暂停/恢复/重启/退出；主控端：关窗即退。
    """
    ensure_gui_thread("run_gui")
    app = create_app(argv)
    # 退出由协程链显式控制（会话 stop Event → app.quit）。必须关闭 Qt 的
    # "最后一个窗口关闭即退出"：launcher 对话框是当时唯一可见窗口，它
    # accept 关闭的瞬间 Qt 会抢先 quit，主编排协程在 await 会话处夭折
    # （2026-10-05 走查第二批 bug；哨兵见 tests/integration/test_gui_handoff_smoke.py）
    app.setQuitOnLastWindowClosed(False)
    loop = event_loop(app)
    asyncio.set_event_loop(loop)
    loop.create_task(_gui_main(app))
    # 必须经 run_forever 驱动：qasync 在内部进入 app.exec() 前先登记 asyncio
    # running-loop（qasync/__init__.py run_forever）；裸 app.exec() 跳过该步，
    # Python 3.14 Task 步进检查抛 "is not the running loop"，协程体永不执行
    # （2026-10-04 真机走查 bug；哨兵见 tests/integration/test_gui_startup_smoke.py）
    return int(loop.run_forever())

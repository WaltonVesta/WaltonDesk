"""launcher → 主窗口切换冒烟（2026-10-05 阶段 9 走查第二批 bug 的回归哨兵）。

病灶（三段互证，详见 STAGE9_REPORT.md §6 走查记录·第二批）：
  1. launcher QDialog 是当时唯一可见窗口，accept 关闭 → Qt
     ``quitOnLastWindowClosed`` 触发 app 退出 → ``run_forever`` 在
     ``_gui_main`` 还 pending（正 await client_session）时提前返回；
  2. 拆除期 ``_SimpleTimer``（loop 的定时器 QObject）C++ 侧被删 →
     主窗 ``destroyed → stop.set → call_soon`` 抛
     "Internal C++ object (_SimpleTimer) already deleted"，唤醒丢失；
  3. GC 注入 GeneratorExit 拆协程 → finally ``await
     controller.disconnect()`` → ``_teardown`` 的
     ``asyncio.current_task()`` 抛 "no running event loop"。

修复口径：
  - 装配处（run_gui / _run_gui_with）``app.setQuitOnLastWindowClosed(False)``：
    退出完全由协程链显式控制（stop Event → app.quit），对齐
    scripts/poc/poc_qasync.py mode_live 的显式收尾思路；
  - ``client_session`` 主窗接 ``WA_DeleteOnClose``：close 即销毁 →
    destroyed 可靠触发（普通 QMainWindow close 只隐藏，永不 destroyed，
    病灶 1 修掉后会挂死）；
  - ``controller._teardown`` 无运行环守卫（对齐 _start_reconnect_loop，
    e7b6d9c 修复法）。

测试策略：走 cli.main 完整路径（真 run_gui + 真 launcher 点击
"启动主控端" + 真 MainWindow 出现 + 程序化关窗 + 真 disconnect 链）。
病灶回归时任一段夭折 → 哨兵断言转红；5 秒看门狗防挂死。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from rdc import cli
from rdc.brand import brand_title
from rdc.client.controller import ClientController
from rdc.ui import session as session_mod
from rdc.ui.app import create_app
from rdc.ui.launcher import LauncherDialog
from rdc.ui.main_window import MainWindow

_WATCHDOG_MS = 5000
# 阶段 10 更名：标题从品牌来源取（本哨兵锁的是"launcher→主窗交接真完成"，
# 不是标题字符串本身；写死字面值会让每次改文案都误报成交接坏了）。
_MAIN_TITLE = brand_title("主控端")


@pytest.fixture()
def qapp():
    return create_app([])


@pytest.fixture()
def loop_spy(monkeypatch):
    """收集被测路径创建的 QEventLoop 并在退出后关闭（防 unclosed 警告）。"""
    made = []
    orig = session_mod.event_loop

    def spy(app=None):
        lp = orig(app)
        made.append(lp)
        return lp

    monkeypatch.setattr(session_mod, "event_loop", spy)
    monkeypatch.setattr("rdc.ui.app.event_loop", spy)
    yield made
    for lp in made:
        if not lp.is_closed():
            lp.close()


class _SpyController(ClientController):
    """记录 disconnect 完成与执行时的环状态（病灶 3 的显式探针）。"""

    disconnect_done = 0
    done_on_running_loop: bool | None = None

    async def disconnect(self) -> None:
        await super().disconnect()
        type(self).disconnect_done += 1
        try:
            asyncio.get_running_loop()
            type(self).done_on_running_loop = True
        except RuntimeError:
            type(self).done_on_running_loop = False


def test_launcher_to_main_window_handoff_completes(
    tmp_app_dir, qapp, monkeypatch, loop_spy
) -> None:
    """启动主控端：launcher 关闭 → 主窗出现 → 关窗 → 拆链 → 干净退出。

    病灶回归判据（任一未达成即红）：
    - ``disconnect_done >= 1``：client_session 的 finally 拆链走完
      （病灶 1/2 时协程在 await stop.wait() 夭折，永远到不了）；
    - ``done_on_running_loop is True``：拆链发生在运行中的环上（病灶 3）；
    - 看门狗未触发：run_forever 是被 app.quit 正常结束的。
    """
    monkeypatch.setattr(
        session_mod, "build_client_controller", lambda settings=None: _SpyController()
    )
    _SpyController.disconnect_done = 0
    _SpyController.done_on_running_loop = None

    seen: dict[str, object] = {"clicked": False, "main": False}
    watchdog = []

    def _spin() -> None:
        # 每 50ms 轮询：找到 launcher → 程序化点击"启动主控端"；
        # 找到主窗 → 记录可见性后延迟关闭（病灶回归时 loop 提前退出，
        # _spin 链随 Qt 事件循环终止，主窗永不被发现）
        if not seen["clicked"]:
            for w in qapp.topLevelWidgets():
                if isinstance(w, LauncherDialog) and w.isVisible():
                    for b in w.findChildren(QPushButton):
                        if "启动主控端" in b.text():
                            b.click()
                            seen["clicked"] = True
                            break
                    break
        if seen["clicked"] and not seen["main"]:
            for w in qapp.topLevelWidgets():
                if isinstance(w, MainWindow):
                    seen["main"] = w.isVisible() and w.windowTitle() == _MAIN_TITLE
                    QTimer.singleShot(200, w.close)
                    break
        QTimer.singleShot(50, _spin)

    QTimer.singleShot(0, _spin)
    # 看门狗：记录"会话未自行退出"并兜底 quit（病灶若以挂死形式回归，
    # 没有这句测试会无限等待；watchdog 非空 → 断言转红）
    QTimer.singleShot(_WATCHDOG_MS, lambda: (watchdog.append(1), qapp.quit()))

    rc = cli.main(["--gui"])

    assert seen["clicked"] is True, "launcher 未弹出（第一批病灶回归）"
    assert seen["main"] is True, "主窗口未出现（切换链路夭折）"
    assert _SpyController.disconnect_done >= 1, (
        "client_session 拆链未完成（病灶 1/2 回归：协程被 Qt 隐式退出夭折）"
    )
    assert _SpyController.done_on_running_loop is True, (
        "disconnect 结束时已无运行环（病灶 3 回归：_teardown 守卫缺失）"
    )
    assert not watchdog, "会话未自行退出（关窗唤醒源失效，看门狗兜底）"
    assert rc == 0

"""真实 GUI 启动冒烟：cli.main → _run_gui / _run_gui_with → run_gui 完整驱动路径。

背景（2026-10-04 阶段 9 真机走查 bug）：``ui/session.py:run_gui`` 与
``cli.py:_run_gui_with`` 用裸 ``app.exec()`` 驱动 qasync——跳过 asyncio
running-loop 登记，Python 3.14 的 Task 步进检查随即抛
``RuntimeError: loop ... is not the running loop``，主编排协程（launcher
对话框 / 托盘会话）永不执行 → 窗口不出现。修复口径与
``scripts/poc/poc_qasync.py:mode_live`` 一致：``loop.run_forever()``
（qasync 内部先 ``_set_running_loop(self)`` 再进 ``app.exec()``）。

测试策略：offscreen 下把交互点（launch_choice / host_session）打桩，
断言**协程真的被调度执行**。病灶未修时协程不会跑，看门狗 QTimer 在
5 秒后强制结束 Qt 会话（防挂死），断言转红；修复后立即返回（<1s）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer

from rdc import cli
from rdc.ui import session as session_mod
from rdc.ui.app import create_app
from rdc.ui.launcher import LaunchOption

_WATCHDOG_MS = 5000


@pytest.fixture()
def qapp():
    return create_app([])


@pytest.fixture()
def loop_spy(monkeypatch):
    """收集被测路径经 event_loop() 创建的 QEventLoop 并在退出时关闭。

    两处 patch：``session.run_gui`` 用模块顶层 from-import（打 session 命名
    空间）；``cli._run_gui_with`` 在函数内 from-import（打 rdc.ui.app 源）。
    不 close 会触发 "unclosed event loop" ResourceWarning（filterwarnings=error）。
    """
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


def test_run_gui_main_coroutine_is_scheduled(tmp_app_dir, qapp, monkeypatch, loop_spy) -> None:
    """``rdc --gui``：launcher 协程必须真正跑起来（病灶回归哨兵）。"""
    calls: list[str] = []

    async def fake_choice(parent=None):
        calls.append("choice")
        return LaunchOption.QUIT

    monkeypatch.setattr(session_mod, "launch_choice", fake_choice)
    QTimer.singleShot(_WATCHDOG_MS, qapp.quit)  # 看门狗：协程不调 quit 时兜底退出
    rc = cli.main(["--gui"])
    assert calls == ["choice"], "主编排协程未被调度（裸 app.exec 病灶回归）"
    assert rc == 0


def test_serve_tray_session_coroutine_is_scheduled(
    tmp_app_dir, qapp, monkeypatch, loop_spy
) -> None:
    """``rdc serve --tray``：_run_gui_with 驱动 host_session 协程（同病灶第二处）。"""
    calls: list[str] = []

    async def fake_host_session(app, settings=None, **kwargs):  # first_run_password 等
        calls.append("host")
        return 0

    monkeypatch.setattr(session_mod, "host_session", fake_host_session)
    QTimer.singleShot(_WATCHDOG_MS, qapp.quit)
    rc = cli.main(["serve", "--tray"])
    assert calls == ["host"], "host_session 协程未被调度（裸 app.exec 病灶回归）"
    assert rc == 0

"""rdc.ui.app 单元测试（pytest-qt，offscreen）。

覆盖：create_app 复用、load_stylesheet 全占位渲染、qasync 事件循环装配、
ensure_gui_thread 主线程放行 + **worker 线程拦截**（跨线程控件红线断言）。
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from rdc.ui.app import (
    create_app,
    ensure_gui_thread,
    event_loop,
    load_stylesheet,
)
from rdc.ui.theme.tokens import TOKENS


@pytest.fixture()
def qapp() -> QApplication:
    return create_app([])


class TestAppAssembly:
    def test_create_app_singleton(self, qapp: QApplication) -> None:
        assert QApplication.instance() is qapp
        again = create_app([])
        assert again is qapp  # 复用不重建

    def test_create_app_applies_stylesheet(self, qapp: QApplication) -> None:
        sheet = qapp.styleSheet()
        assert sheet  # 非空
        for color in (TOKENS.bg, TOKENS.accent):
            assert color in sheet

    def test_load_stylesheet_no_leftover_placeholders(self) -> None:
        rendered = load_stylesheet()
        assert "$" not in rendered  # Template 全替换
        assert "#1e2124" in rendered
        assert "6px" in rendered  # radius 带单位

    def test_event_loop_requires_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import rdc.ui.app as app_mod

        class _NoApp:
            @staticmethod
            def instance():  # noqa: ANN205 - 替身：模拟无 QApplication 上下文
                return None

        monkeypatch.setattr(app_mod, "QApplication", _NoApp)
        with pytest.raises(RuntimeError, match="create_app"):
            app_mod.event_loop()

    def test_event_loop_binds_qasync(self, qapp: QApplication) -> None:
        import qasync

        loop = event_loop(qapp)
        assert isinstance(loop, qasync.QEventLoop)
        loop.close()


class TestGuiThreadGuard:
    def test_main_thread_passes(self, qapp: QApplication) -> None:
        ensure_gui_thread("Test")  # 不抛即通过

    def test_worker_thread_raises(self, qapp: QApplication) -> None:
        """跨线程控件操作红线：worker 线程触达 setter 必须 RuntimeError。"""
        box: list[BaseException] = []

        def _bad() -> None:
            try:
                ensure_gui_thread("WorkerTouchedWidget")
            except BaseException as e:  # noqa: BLE001 - 收集断言
                box.append(e)

        th = threading.Thread(target=_bad)
        th.start()
        th.join(2)
        assert len(box) == 1
        assert isinstance(box[0], RuntimeError)
        assert "跨线程控件操作被禁止" in str(box[0])
        assert "WorkerTouchedWidget" in str(box[0])

    def test_guard_noop_without_app(self, qapp: QApplication) -> None:
        # create_app 后 app.thread() 归属主线程；isCurrentThread 为真
        ensure_gui_thread()

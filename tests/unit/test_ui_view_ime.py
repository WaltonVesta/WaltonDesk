"""ScreenView IME 提交通道测试（阶段 7 批 E）。

口径（决策点 3）：物理按键一律 keyEvent（阶段 6 基线不变）；
IME 候选窗提交串走 textCommitted；preedit 不外发。

**重要**：PySide6 的 ``QInputMethodEvent(commit_or_preedit, attrs)`` 两参构造
把第一个字符串填入 **preeditString**，``commitString()`` 恒为空串（探针实测）。
测试用 ``_CommitEvent`` 子类覆写 ``commitString()`` 来模拟真实 IME 提交，
不依赖构造器语义。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rdc.ui.screen_view import ScreenView


class _CommitEvent(QInputMethodEvent):
    """覆写 commitString() 的测试事件（模拟真实 IME 提交）。"""

    def __init__(self, commit: str) -> None:
        super().__init__("", [])
        self._commit = commit

    def commitString(self) -> str:  # noqa: N802
        return self._commit


@pytest.fixture
def view(qapp, qtbot) -> ScreenView:  # noqa: ANN001
    v = ScreenView()
    qtbot.addWidget(v)
    v.present_image(QImage(640, 480, QImage.Format.Format_RGB32))
    v.show()
    v.set_input_enabled(True)
    return v


class TestPhysicalKeysStayKeyEvent:
    """基线保护：可打印与控制键都走 keyEvent（阶段 6 语义原样）。"""

    def test_printable_key_press_release(self, view: ScreenView, qtbot) -> None:
        keys: list[tuple[int, bool]] = []
        texts: list[str] = []
        view.keyEvent.connect(lambda k, d: keys.append((k, d)))
        view.textCommitted.connect(texts.append)
        QTest.keyClick(view.viewport(), Qt.Key.Key_A)
        assert keys == [(int(Qt.Key.Key_A), True), (int(Qt.Key.Key_A), False)]
        assert texts == []  # 物理打字不产生 IME 提交事件

    def test_function_key(self, view: ScreenView, qtbot) -> None:
        keys: list[tuple[int, bool]] = []
        view.keyEvent.connect(lambda k, d: keys.append((k, d)))
        QTest.keyClick(view.viewport(), Qt.Key.Key_F5)
        assert keys[0] == (int(Qt.Key.Key_F5), True)


class TestImeCommitChannel:
    """IME 提交经 view 覆写与 viewport 过滤器双路径均可达。"""

    def test_commit_via_view(self, view: ScreenView, qtbot) -> None:
        texts: list[str] = []
        view.textCommitted.connect(texts.append)
        QApplication.sendEvent(view, _CommitEvent("你好"))
        assert texts == ["你好"]

    def test_commit_via_viewport_filter(self, view: ScreenView, qtbot) -> None:
        """生产路径：事件落在 viewport（焦点代理），由 eventFilter 截获。"""
        texts: list[str] = []
        view.textCommitted.connect(texts.append)
        QApplication.sendEvent(view.viewport(), _CommitEvent("拼音"))
        assert texts == ["拼音"]

    def test_empty_commit_not_emitted(self, view: ScreenView, qtbot) -> None:
        """commitString 空串（preedit 只有）不外发。"""
        texts: list[str] = []
        view.textCommitted.connect(texts.append)
        QApplication.sendEvent(view.viewport(), _CommitEvent(""))
        assert texts == []


class TestDisabledView:
    def test_no_commit_when_input_disabled(self, qapp, qtbot) -> None:
        v = ScreenView()
        qtbot.addWidget(v)
        v.present_image(QImage(100, 100, QImage.Format.Format_RGB32))
        v.show()
        # 默认 input_enabled=False
        texts: list[str] = []
        keys: list[tuple[int, bool]] = []
        v.textCommitted.connect(texts.append)
        v.keyEvent.connect(lambda k, d: keys.append((k, d)))
        QApplication.sendEvent(v.viewport(), _CommitEvent("ab"))
        assert texts == [] and keys == []

"""批 C 可视件测试：quality_indicator / toolbar / screen_view。

红线断言延续：所有公开写入口 worker 线程触达必抛 RuntimeError。
screen_view 坐标变换用真实 show()+resize 后 mapToScene 验证（offscreen 平台
支持布局与变换矩阵）。
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rdc.ui.app import create_app
from rdc.ui.quality_indicator import QualityIndicator
from rdc.ui.screen_view import ScreenView
from rdc.ui.theme.tokens import TOKENS
from rdc.ui.toolbar import Toolbar


@pytest.fixture()
def qapp():
    return create_app([])


def _raises_in_worker(fn) -> bool:  # noqa: ANN001
    box: list[BaseException] = []

    def _t() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            box.append(e)

    th = threading.Thread(target=_t)
    th.start()
    th.join(2)
    return len(box) == 1 and isinstance(box[0], RuntimeError)


# ---------------------------------------------------------------------------
# QualityIndicator
# ---------------------------------------------------------------------------


class TestQualityIndicator:
    def test_initial_wait(self, qapp, qtbot) -> None:
        q = QualityIndicator()
        qtbot.addWidget(q)
        assert q.get_quality() == "unknown"
        assert "等待" in q._label.text()  # noqa: SLF001

    @pytest.mark.parametrize(
        ("lat", "fps", "loss", "expect"),
        [
            (30, 30, 0.0, "good"),      # 优秀
            (80, 20, 0.02, "good"),     # 良好
            (150, 12, 0.05, "medium"),  # 一般
            (300, 5, 0.10, "poor"),     # 较差
            (600, 1, 0.30, "poor"),     # 极差
        ],
    )
    def test_grade_thresholds_match_v4(self, qapp, qtbot, lat, fps, loss, expect) -> None:
        q = QualityIndicator()
        qtbot.addWidget(q)
        q.update_quality(lat, fps, loss)
        assert q.get_quality() == expect

    def test_detail_text_and_offline(self, qapp, qtbot) -> None:
        q = QualityIndicator()
        qtbot.addWidget(q)
        q.update_quality(45, 30)
        assert "45ms" in q._detail.text() and "30.0fps" in q._detail.text()  # noqa: SLF001
        q.set_offline()
        assert q.get_quality() == "offline"
        assert q._detail.text() == ""  # noqa: SLF001
        q.update_quality(10, 30, 0, is_connected=False)  # 未连接路径同离线
        assert q.get_quality() == "offline"

    def test_pulse_animation_property(self, qapp, qtbot) -> None:
        q = QualityIndicator()
        qtbot.addWidget(q)
        dot = q._dot  # noqa: SLF001
        assert dot.pulsePhase >= 0.0
        dot.set_pulse_phase(0.5)
        assert dot.pulsePhase == 0.5
        # 颜色切换即时生效（good 绿）
        q.update_quality(10, 60)
        assert dot._color == QColor(TOKENS.ok)  # noqa: SLF001

    def test_worker_blocked(self, qapp, qtbot) -> None:
        q = QualityIndicator()
        qtbot.addWidget(q)
        assert _raises_in_worker(lambda: q.update_quality(1, 1))


# ---------------------------------------------------------------------------
# Toolbar
# ---------------------------------------------------------------------------


class TestToolbar:
    def test_connect_signal_carries_host_port(self, qapp, qtbot) -> None:
        tb = Toolbar(default_host="10.0.0.9", default_port=5500)
        qtbot.addWidget(tb)
        with qtbot.waitSignal(tb.connectRequested) as sig:
            tb._connect_btn.click()  # noqa: SLF001
        assert sig.args == ["10.0.0.9", 5500]

    def test_empty_host_does_not_emit(self, qapp, qtbot) -> None:
        tb = Toolbar()
        qtbot.addWidget(tb)
        calls: list = []
        tb.connectRequested.connect(lambda *_: calls.append(1))
        tb._connect_btn.click()  # noqa: SLF001
        assert not calls

    def test_connection_state_matrix(self, qapp, qtbot) -> None:
        tb = Toolbar(default_port=1)
        qtbot.addWidget(tb)
        tb.set_connection_state("connecting")
        assert "连接中" in tb._connect_btn.text() and not tb._connect_btn.isEnabled()  # noqa: SLF001
        assert not tb._disconnect_btn.isEnabled()  # noqa: SLF001
        tb.set_connection_state("connected")
        assert tb._connect_btn.text() == "已连" and tb._disconnect_btn.isEnabled()  # noqa: SLF001
        assert tb._control_btn.isEnabled()  # noqa: SLF001
        tb.set_connection_state("reconnecting")
        assert "重连中" in tb._connect_btn.text()  # noqa: SLF001
        tb.set_connection_state("disconnected")
        assert tb._connect_btn.text() == "连接" and tb._connect_btn.isEnabled()  # noqa: SLF001
        assert not tb._control_btn.isChecked()  # noqa: SLF001

    def test_control_toggle_emits_once_with_text(self, qapp, qtbot) -> None:
        tb = Toolbar()
        qtbot.addWidget(tb)
        tb.set_connection_state("connected")
        with qtbot.waitSignal(tb.controlToggled) as sig:
            tb._control_btn.click()  # noqa: SLF001
        assert sig.args == [True]
        assert tb._control_btn.text() == "禁用控制"  # noqa: SLF001
        tb.set_control_state(False)  # 外部同步不触发信号
        assert tb._control_btn.text() == "启用控制"  # noqa: SLF001

    def test_recent_hosts_dedupe_cap(self, qapp, qtbot) -> None:
        tb = Toolbar(recent_hosts=("a", "b"))
        qtbot.addWidget(tb)
        tb.add_recent_host("a")  # 去重不增
        tb.add_recent_host("c")
        items = [tb._host.itemText(i) for i in range(tb._host.count())]  # noqa: SLF001
        assert items[:3] == ["c", "a", "b"] or items[:3] == ["a", "c", "b"]
        for i in range(20):
            tb.add_recent_host(f"h{i}")
        assert tb._host.count() <= 8  # noqa: SLF001

    def test_indicator_embedded_and_quality_label(self, qapp, qtbot) -> None:
        tb = Toolbar()
        qtbot.addWidget(tb)
        assert isinstance(tb.indicator, QualityIndicator)
        tb.set_quality(28)
        assert "28" in tb._quality_label.text()  # noqa: SLF001

    def test_worker_blocked(self, qapp, qtbot) -> None:
        tb = Toolbar()
        qtbot.addWidget(tb)
        assert _raises_in_worker(lambda: tb.set_connection_state("connected"))
        assert _raises_in_worker(lambda: tb.add_recent_host("x"))


# ---------------------------------------------------------------------------
# ScreenView
# ---------------------------------------------------------------------------


class TestScreenView:
    def _shown(self, qtbot) -> ScreenView:
        v = ScreenView()
        v.resize(400, 300)
        v.show()
        QTest.qWaitForWindowExposed(v)
        return v

    def test_present_sets_scene_and_pixmap(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        img = QImage(640, 480, QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        v.present_image(img)
        assert v._item.pixmap().width() == 640  # noqa: SLF001
        assert v.scene().sceneRect().width() == 640  # noqa: SLF001

    def test_coord_identity_1to1(self, qapp, qtbot) -> None:
        """1:1 模式（set_fit_mode(False)）无缩放无偏移：视口坐标 ≈ 远端坐标。"""
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.set_fit_mode(False)  # 第五批走查：默认已是 fit，1:1 语义显式切换验证
        img = QImage(640, 480, QImage.Format.Format_RGB32)
        v.present_image(img)
        rp = v.remote_pos(QPoint(100, 80))
        assert rp is not None
        assert abs(rp.x() - 100) < 2 and abs(rp.y() - 80) < 2

    def test_coord_negative_origin_multi_display(self, qapp, qtbot) -> None:
        """副屏原点 (-1920, 0)：场景坐标必须带负原点。"""
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        img = QImage(800, 600, QImage.Format.Format_RGB32)
        v.present_image(img, left=-800, top=0)
        # 视口点 (200,200) 在 fit/reset 前的场景坐标 = 远端绝对坐标
        rp = v.remote_pos(QPoint(200, 200))
        assert rp is not None
        assert rp.x() < 0  # 落在负 x 区（左屏）

    def test_out_of_remote_bounds_returns_none(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.set_fit_mode(False)  # 1:1：右下多余视口空间是黑边
        # 100x100 帧放进 400x300 视口
        v.present_image(QImage(100, 100, QImage.Format.Format_RGB32))
        assert v.remote_pos(QPoint(5, 5)) is not None
        assert v.remote_pos(QPoint(350, 250)) is None

    def test_fit_mode_black_borders_out_of_bounds(self, qapp, qtbot) -> None:
        """第五批走查哨兵：默认 fit 等比放大居中，黑边区域不发输入。

        100x100 帧进 400x300 视口 → 等比放大 3 倍（300x300）居中，
        左右各 50px 黑边：视口角点落黑边返回 None，中心落画面内。
        """
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        assert v.fit_mode  # 默认适应窗口（病灶：默认 False 画面卡左上）
        v.present_image(QImage(100, 100, QImage.Format.Format_RGB32))
        assert v.transform().m11() == pytest.approx(3.0, abs=0.1)  # 撑满高度维度
        center = v.remote_pos(QPoint(200, 150))
        assert center is not None
        assert abs(center.x() - 50) < 2 and abs(center.y() - 50) < 2  # 帧正中
        assert v.remote_pos(QPoint(5, 5)) is None  # 左侧黑边
        assert v.remote_pos(QPoint(395, 5)) is None  # 右侧黑边

    def test_move_disabled_by_default_then_signals(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        img = QImage(640, 480, QImage.Format.Format_RGB32)
        v.present_image(img)
        got: list = []
        v.mouseMoved.connect(lambda *a: got.append(a))
        QTest.mouseMove(v.viewport(), pos=QPoint(50, 50))
        QTest.qWait(30)
        assert not got  # 默认只读
        v.set_input_enabled(True)
        assert v.cursor().shape() == Qt.CursorShape.ArrowCursor
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
        QTest.qWait(30)
        assert got and got[0][0].startswith("press:left")
        assert got[-1][0].startswith("release:left")
        assert 0 <= got[0][1] < 640 and 0 <= got[0][2] < 480

    def test_keyboard_passthrough_when_enabled(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.present_image(QImage(100, 100, QImage.Format.Format_RGB32))
        keys: list = []
        v.keyEvent.connect(lambda k, d: keys.append((k, d)))
        v.set_input_enabled(True)
        QTest.keyClick(v.viewport(), Qt.Key.Key_A)
        QTest.qWait(20)
        assert keys and keys[0][1] is True
        assert all(k == Qt.Key.Key_A for k, _ in keys)

    def test_wheel_control_mode_emits_and_swallows(self, qapp, qtbot) -> None:
        from PySide6.QtGui import QWheelEvent

        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.present_image(QImage(640, 480, QImage.Format.Format_RGB32))
        got: list = []
        v.mouseMoved.connect(lambda *a: got.append(a))
        v.set_input_enabled(True)
        ev = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(v.viewport(), ev)
        QTest.qWait(20)
        assert got and got[0][0] == "wheel:up"

    def test_clear_frame(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.present_image(QImage(100, 100, QImage.Format.Format_RGB32))
        v.clear_frame()
        assert v._item.pixmap().isNull()  # noqa: SLF001
        # 清屏后输入不发（placeholder 保护）
        got: list = []
        v.mouseMoved.connect(lambda *a: got.append(a))
        v.set_input_enabled(True)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QTest.qWait(20)
        assert not got

    def test_fit_mode_toggle(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        v.present_image(QImage(2000, 1200, QImage.Format.Format_RGB32))
        with qtbot.waitSignal(v.fitModeChanged):
            v.set_fit_mode(True)
        assert v.fit_mode
        scale = v.transform().m11()
        assert scale < 1.0  # 2000px 帧缩进 400px 视口
        v.set_fit_mode(False)
        assert v.transform().m11() == 1.0

    def test_worker_blocked(self, qapp, qtbot) -> None:
        v = self._shown(qtbot)
        qtbot.addWidget(v)
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        assert _raises_in_worker(lambda: v.present_image(img))
        assert _raises_in_worker(lambda: v.set_input_enabled(True))

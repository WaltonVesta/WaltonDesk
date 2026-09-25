"""rdc.ui 低风险件测试：status_bar / performance_panel / status_panel。

口径：pytest-qt offscreen 真实控件 + qtbot 信号/时序等待；全部 setter 的
主线程直调合法性 + worker 线程触达必抛（红线断言，叮嘱 e）。
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from rdc.ui.app import create_app
from rdc.ui.performance_panel import PerformancePanel
from rdc.ui.status_bar import StatusBar
from rdc.ui.status_panel import HostSnapshot, HostStatsProvider, StatusPanel
from rdc.ui.theme.tokens import TOKENS


@pytest.fixture()
def qapp():
    return create_app([])


def _assert_worker_raises(fn, name: str) -> None:
    """worker 线程调用 fn 必须收到 RuntimeError（跨线程红线）。"""
    box: list[BaseException] = []
    th = threading.Thread(target=lambda: _call(fn, box), daemon=True)
    th.start()
    th.join(2)
    assert box and isinstance(box[0], RuntimeError), f"{name} 未拦截 worker 线程"


def _call(fn, box: list) -> None:
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        box.append(e)


# ---------------------------------------------------------------------------
# StatusBar
# ---------------------------------------------------------------------------


class TestStatusBar:
    def test_initial_text(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        assert "未连接" in bar._status.text()  # noqa: SLF001

    def test_set_status_colors(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        bar.set_status("已连接", "connected")
        assert "已连接" in bar._status.text()  # noqa: SLF001
        assert TOKENS.ok in bar._status.styleSheet()
        bar.set_status("错误!", "error")
        assert TOKENS.error in bar._status.styleSheet()
        bar.set_status("神秘", "unknown-type")  # 未知类型回落 info 不抛
        assert TOKENS.text_dim in bar._status.styleSheet()

    def test_latency_quality_info(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        bar.set_latency(45.0)
        assert "45ms" in bar._latency.text()  # noqa: SLF001
        bar.set_latency(500.0)
        assert TOKENS.error in bar._latency.styleSheet()  # >300 红
        bar.set_latency(None)
        assert bar._latency.text() == ""  # noqa: SLF001
        bar.set_quality(72)
        assert "72%" in bar._quality.text()  # noqa: SLF001
        bar.set_quality(None)
        assert bar._quality.text() == ""  # noqa: SLF001
        bar.set_info("192.168.1.5")
        assert "192.168.1.5" in bar._info.text()  # noqa: SLF001

    def test_fps_measured_via_timer(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        for _ in range(5):
            bar.notify_frame()
        qtbot.waitUntil(lambda: bar.measured_fps > 0, timeout=3000)

    def test_worker_thread_blocked(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        _assert_worker_raises(lambda: bar.set_status("x"), "StatusBar.set_status")

    def test_stop_idempotent(self, qapp, qtbot) -> None:
        bar = StatusBar()
        qtbot.addWidget(bar)
        bar.stop()
        bar.stop()
        assert not bar._timer.isActive()  # noqa: SLF001


# ---------------------------------------------------------------------------
# PerformancePanel
# ---------------------------------------------------------------------------


class TestPerformancePanel:
    def test_update_metrics_all(self, qapp, qtbot) -> None:
        p = PerformancePanel(bandwidth_limit_kbps=2000)
        qtbot.addWidget(p)
        p.update_metrics(
            fps=29.7, latency_ms=33, bandwidth_kbps=1000, quality=28,
            resolution=(1920, 1080), total_frames=12345,
        )
        texts = [lb.text() for lb in p.findChildren(type(p._fps))]  # noqa: SLF001
        joined = " | ".join(texts)
        assert "29.7" in joined and "33ms" in joined and "1000.0KB/s" in joined
        assert "28" in joined and "1920x1080" in joined and "12345" in joined
        assert p._usage.value() == 50  # noqa: SLF001

    def test_none_keeps_placeholder(self, qapp, qtbot) -> None:
        p = PerformancePanel()
        qtbot.addWidget(p)
        p.update_metrics(fps=0)
        assert "FPS: --" in p._fps.text()  # noqa: SLF001
        p.update_metrics(fps=None, latency_ms=None)  # 全 None 不动
        assert "FPS: --" in p._fps.text()  # noqa: SLF001

    def test_usage_without_limit(self, qapp, qtbot) -> None:
        p = PerformancePanel()  # limit=0
        qtbot.addWidget(p)
        p.update_metrics(bandwidth_kbps=500)
        assert "--%" in p._usage.format()  # noqa: SLF001
        p.set_bandwidth_limit(1000)
        p.update_metrics(bandwidth_kbps=900)
        assert p._usage.value() == 90  # noqa: SLF001

    def test_worker_thread_blocked(self, qapp, qtbot) -> None:
        p = PerformancePanel()
        qtbot.addWidget(p)
        _assert_worker_raises(lambda: p.update_metrics(fps=1), "PerformancePanel.update_metrics")


# ---------------------------------------------------------------------------
# StatusPanel
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self) -> None:
        self.snap = HostSnapshot()

    def snapshot(self) -> HostSnapshot:
        return self.snap


class TestStatusPanel:
    def test_running_state(self, qapp, qtbot) -> None:
        prov = FakeProvider()
        panel = StatusPanel(prov)
        qtbot.addWidget(panel)
        prov.snap = HostSnapshot(
            running=True, active_connections=2, subscriber_count=1,
            bytes_sent_total=5_120_000, uptime_s=100, encoder_quality=28,
            encoder_avg_kb=32.5, frames_encoded=3000,
        )
        panel.refresh()
        labels = [lb.text() for lb in panel.findChildren(type(panel._status))]  # noqa: SLF001
        joined = " | ".join(labels)
        assert "运行中" in joined and "连接数: 2" in joined and "订阅者: 1" in joined
        assert "50.0 KB/s" in joined and "00:01:40" in joined
        assert "CRF 28" in joined and "3000" in joined
        assert TOKENS.ok in panel._status.styleSheet()  # noqa: SLF001
        panel.stop()

    def test_paused_stopped_states(self, qapp, qtbot) -> None:
        prov = FakeProvider()
        panel = StatusPanel(prov)
        qtbot.addWidget(panel)
        prov.snap = HostSnapshot(paused=True)
        panel.refresh()
        assert "已暂停" in panel._status.text()  # noqa: SLF001
        assert TOKENS.warn in panel._status.styleSheet()  # noqa: SLF001
        prov.snap = HostSnapshot()
        panel.refresh()
        assert "已停止" in panel._status.text()  # noqa: SLF001
        assert TOKENS.error in panel._status.styleSheet()  # noqa: SLF001
        panel.stop()

    def test_worker_thread_blocked(self, qapp, qtbot) -> None:
        panel = StatusPanel(FakeProvider())
        qtbot.addWidget(panel)
        _assert_worker_raises(panel.refresh, "StatusPanel.refresh")
        panel.stop()

    def test_provider_protocol_shape(self) -> None:
        # HostStatsProvider 是 runtime-checkable 的窄接口
        assert isinstance(FakeProvider(), HostStatsProvider)

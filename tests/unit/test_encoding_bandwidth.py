"""带宽探测器（rdc.encoding.bandwidth）单元测试（计时源 monkeypatch，零 sleep）。

实现语义（写断言前先对齐）：``record_send(b>0)`` 先累计字节，若距窗口起点
≥ ``window_sec`` 则**把含本笔在内的累计字节**按实际经过时长折算成一个样本。
"""

from __future__ import annotations

import pytest

import rdc.encoding.bandwidth as bw_module
from rdc.encoding.bandwidth import BandwidthProbe


class FakeClock:
    """可控单调时钟。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(bw_module.time, "monotonic", fake.monotonic)
    return fake


class TestValidation:
    def test_bad_window(self) -> None:
        with pytest.raises(ValueError, match="window_sec"):
            BandwidthProbe(window_sec=0)

    def test_bad_cap(self) -> None:
        with pytest.raises(ValueError, match="sample_cap"):
            BandwidthProbe(sample_cap=0)


class TestWindows:
    def test_initial_zero(self) -> None:
        assert BandwidthProbe().get_bandwidth() == 0.0

    def test_zero_and_negative_ignored(self, clock: FakeClock) -> None:
        probe = BandwidthProbe(window_sec=1.0)
        probe.record_send(0)
        probe.record_send(-5)
        assert probe.window_count == 0
        assert probe.get_bandwidth() == 0.0

    def test_window_folds_at_boundary(self, clock: FakeClock) -> None:
        probe = BandwidthProbe(window_sec=1.0)
        probe.record_send(10_000)  # 开窗，未达窗口长度
        assert probe.get_bandwidth() == 0.0
        clock.advance(1.0)
        probe.record_send(2_000)  # 触发折算：(10000+2000)/1.0
        assert probe.get_bandwidth() == pytest.approx(12_000.0)
        assert probe.window_count == 1

    def test_uses_real_elapsed_not_nominal(self, clock: FakeClock) -> None:
        """实际经过 2.0s 才触发 → 按 2.0s 折算（调度抖动不产生虚高）。"""
        probe = BandwidthProbe(window_sec=1.0)
        probe.record_send(10_000)
        clock.advance(2.0)
        probe.record_send(2_000)
        assert probe.get_bandwidth() == pytest.approx(6_000.0)  # 12000/2.0

    def test_moving_average_capped(self, clock: FakeClock) -> None:
        probe = BandwidthProbe(window_sec=1.0, sample_cap=2)
        probe.record_send(1_000)
        clock.advance(1.0)
        probe.record_send(1_000)  # 窗口1 = 2000B/s
        clock.advance(1.0)
        probe.record_send(4_000)  # 窗口2 = 4000B/s
        clock.advance(1.0)
        probe.record_send(2_000)  # 窗口3 = 2000B/s，挤出窗口1
        assert probe.window_count == 2
        assert probe.get_bandwidth() == pytest.approx(3_000.0)  # mean(4000, 2000)

    def test_multiple_sends_accumulate_in_window(self, clock: FakeClock) -> None:
        probe = BandwidthProbe(window_sec=1.0)
        probe.record_send(400)
        clock.advance(0.4)
        probe.record_send(600)  # 窗口内累计 1000，未到期
        assert probe.window_count == 0
        clock.advance(0.7)
        probe.record_send(1)  # 1.1s → (1000+1)/1.1
        assert probe.get_bandwidth() == pytest.approx(1001 / 1.1)


class TestReset:
    def test_reset_clears_everything(self, clock: FakeClock) -> None:
        probe = BandwidthProbe(window_sec=1.0)
        probe.record_send(10_000)
        clock.advance(1.0)
        probe.record_send(2_000)
        assert probe.get_bandwidth() > 0
        probe.reset()
        assert probe.get_bandwidth() == 0.0
        assert probe.window_count == 0
        # reset 后从当前时刻重新开窗（长空闲不背陈旧窗口）
        clock.advance(100.0)
        probe.record_send(500)
        clock.advance(1.0)
        probe.record_send(500)
        assert probe.get_bandwidth() == pytest.approx(1_000.0)

    def test_monotonic_not_wallclock(self) -> None:
        """实现必须用 time.monotonic（系统时钟回拨免疫）——函数体级锁定。

        只看**函数体**（AST），绕开模块/类 docstring 里对 v4 历史的描述文字。
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(bw_module))
        bodies = "\n".join(
            ast.get_source_segment(inspect.getsource(bw_module), node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        )
        assert "time.monotonic()" in bodies
        assert "time.time()" not in bodies

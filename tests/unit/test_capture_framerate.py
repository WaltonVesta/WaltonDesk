"""帧率控制器（rdc.capture.framerate，v5 重写）单元测试。

核心断言：
- 纯决策不睡眠（decide/wait_frame 零阻塞，注入假时钟全确定性）；
- 静止门控 + 强制刷新节奏；
- 变化率线性调速、网络压制（v4 阈值语义兼容）；
- v4 兼容面（构造签名、min/max_fps 可变、set_fps、measured/target/reset）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from rdc.capture.diff import DiffResult
from rdc.capture.framerate import FrameAction, FramerateController
from rdc.encoding.raters import RaterSignals


class FakeClock:
    """可推进的假单调时钟。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


def make_diff(
    *,
    is_first: bool = False,
    changed_blocks: int = 10,
    total_blocks: int = 100,
) -> DiffResult:
    return DiffResult(
        mask=np.zeros((1, 1), dtype=bool),
        total_blocks=total_blocks,
        changed_blocks=changed_blocks,
        change_ratio=changed_blocks / total_blocks,
        full_frame=False,
        dirty_rects=(),
        is_first=is_first,
        frame_size=(1920, 1080),
        block_size=32,
        elapsed_ms=1.0,
    )


# ---------------------------------------------------------------------------
# 决策：首帧 / 静止 / 内容
# ---------------------------------------------------------------------------


class TestDecide:
    def test_first_frame_always_captures(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        d = c.decide(make_diff(is_first=True, changed_blocks=100, total_blocks=100))
        assert d.action == FrameAction.CAPTURE
        assert d.reason == "first"
        assert d.delay_s == pytest.approx(1 / 30)

    def test_none_diff_is_first(self) -> None:
        c = FramerateController(clock=FakeClock())
        d = c.decide(None)
        assert d.should_capture and d.reason == "first"

    def test_static_gates_to_idle(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))  # 建立 last_capture
        clock.advance(0.1)
        d = c.decide(make_diff(changed_blocks=0))
        assert d.action == FrameAction.IDLE
        assert d.reason == "static"
        # idle 档 = min(idle_fps, network)：默认 2fps → 0.5s
        assert d.delay_s == pytest.approx(0.5)

    def test_static_force_refresh_after_interval(self) -> None:
        clock = FakeClock()
        c = FramerateController(idle_force_interval_s=5.0, clock=clock)
        c.decide(make_diff(is_first=True))
        clock.advance(5.0)
        d = c.decide(make_diff(changed_blocks=0))
        assert d.action == FrameAction.CAPTURE
        assert d.reason == "forced"
        assert d.fps == 2.0  # 强制刷新按 idle 档节奏

    def test_force_refresh_resets_timer(self) -> None:
        clock = FakeClock()
        c = FramerateController(idle_force_interval_s=5.0, clock=clock)
        c.decide(make_diff(is_first=True))
        clock.advance(5.0)
        assert c.decide(make_diff(changed_blocks=0)).reason == "forced"
        clock.advance(1.0)  # 距上次采集仅 1s
        assert c.decide(make_diff(changed_blocks=0)).reason == "static"

    def test_high_change_gives_max_fps(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))
        clock.advance(0.1)
        d = c.decide(make_diff(changed_blocks=15, total_blocks=100))  # ratio 0.15 ≥ 0.10
        assert d.action == FrameAction.CAPTURE
        assert d.fps == 30.0
        assert d.reason == "content"

    def test_content_ratio_scales_linearly(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))
        fps_seen = []
        for blocks in (1, 5, 10):  # ratio 0.01 / 0.05 / 0.10
            clock.advance(0.1)
            fps_seen.append(c.decide(make_diff(changed_blocks=blocks, total_blocks=100)).fps)
        assert fps_seen[0] < fps_seen[1] < fps_seen[2]
        assert fps_seen[2] == 30.0

    def test_low_ratio_falls_to_idle_floor(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))
        clock.advance(0.1)
        d = c.decide(make_diff(changed_blocks=40, total_blocks=10_000))  # ratio 0.004 < 0.005
        assert d.fps == max(c.min_fps, 2.0)


# ---------------------------------------------------------------------------
# 网络压制
# ---------------------------------------------------------------------------


class TestNetwork:
    def test_signals_via_decide(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        signals = RaterSignals(rtt_ms=300.0, loss_rate=0.0)  # 0.3s > 0.1s 阈值
        d = c.decide(None, signals)
        # 首帧仍采集，但目标帧率被压：max_fps*(1-(0.3-0.1)/0.5)=30*0.6=18
        assert d.fps == pytest.approx(18.0)
        assert d.delay_s == pytest.approx(1 / 18)

    def test_loss_suppresses(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        d = c.decide(None, RaterSignals(rtt_ms=10, loss_rate=0.1))
        assert d.fps == pytest.approx(30 * (1 - 0.2))

    def test_network_floor_at_min_fps(self) -> None:
        clock = FakeClock()
        c = FramerateController(min_fps=5, max_fps=30, clock=clock)
        d = c.decide(None, RaterSignals(rtt_ms=1000, loss_rate=0.9))
        assert d.fps == 5.0

    def test_adjust_fps_v4_semantics(self) -> None:
        c = FramerateController(clock=FakeClock())
        assert c.adjust_fps(0.05, 0.0) == 30  # 低延迟无丢包 → 满速
        assert c.adjust_fps(0.1, 0.01) == 30  # 阈值边界不触发
        assert c.adjust_fps(0.35, 0.0) == 30 * (1 - 0.25 / 0.5)  # 0.35s → 15fps
        assert c.adjust_fps(0.05, 0.1) == 30 * (1 - 0.2)  # 丢包 10% → ×0.8
        assert c.get_target_fps() == 24.0


# ---------------------------------------------------------------------------
# v4 兼容面
# ---------------------------------------------------------------------------


class TestV4Compat:
    def test_v4_ctor_signature(self) -> None:
        c = FramerateController(min_fps=5, max_fps=30)
        assert c.min_fps == 5 and c.max_fps == 30
        assert c.current_fps == 30  # v4：初值 = max_fps

    def test_mutable_min_max(self) -> None:
        c = FramerateController(clock=FakeClock())
        c.max_fps = 15  # service.py 直接赋值属性的用法
        c.set_fps(999)  # 手动钳制走新 max
        assert c.get_target_fps() == 15

    def test_set_fps_clamps(self) -> None:
        c = FramerateController(clock=FakeClock())
        c.set_fps(1)
        assert c.get_target_fps() == 5
        c.set_fps(999)
        assert c.get_target_fps() == 30

    def test_wait_frame_does_not_sleep(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))
        clock.advance(0.01)  # 距上帧 10ms，30fps 间隔 33.3ms
        t0 = time.perf_counter()
        remain = c.wait_frame()
        assert time.perf_counter() - t0 < 0.005  # 关键：不睡眠
        assert remain == pytest.approx(1 / 30 - 0.01)

    def test_wait_frame_no_history_returns_zero(self) -> None:
        c = FramerateController(clock=FakeClock())
        assert c.wait_frame() == 0.0

    def test_measured_fps_window(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        for _ in range(8):
            clock.advance(0.125)  # 二进制精确：8 帧恰满 1.0s 窗口 → 8fps
            c.wait_frame()
        assert c.get_measured_fps() == pytest.approx(8.0)

    def test_reset(self) -> None:
        clock = FakeClock()
        c = FramerateController(clock=clock)
        c.decide(make_diff(is_first=True))
        c.reset()
        assert c.get_measured_fps() == 0.0
        assert c.get_target_fps() == 30.0
        # reset 后静止判定重新需要首帧（last_capture 清空）
        d = c.decide(make_diff(changed_blocks=0))
        assert d.reason in {"first", "forced"}


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_fps": 0},
            {"min_fps": 60, "max_fps": 30},
            {"idle_fps": 0},
            {"idle_force_interval_s": 0},
        ],
    )
    def test_bad_params_raise(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            FramerateController(**kwargs)

    def test_real_monotonic_clock_default(self) -> None:
        c = FramerateController()  # 不注入 clock → time.monotonic
        d = c.decide(None)
        assert d.should_capture and d.delay_s > 0

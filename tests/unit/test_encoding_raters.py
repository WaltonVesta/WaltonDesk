"""自适应码率状态机（rdc.encoding.raters）单元测试。

对应阶段 5 任务 8 + 叮嘱 d（收敛测试）：
- 信号→得分单调性（纯映射层）
- 恶化路径收敛（CRF 升到 CRF_MAX）与好转路径收敛（CRF 降回 CRF_MIN，含迟滞）
- 边界钳制、参数校验
- 振荡检测触发冻结
- 参考码率随 CRF 单调下降
"""

from __future__ import annotations

import pytest

from rdc.encoding.raters import (
    CRF_MAX,
    CRF_MIN,
    AdaptiveRater,
    RaterDecision,
    RaterSignals,
)


def _run(rater: AdaptiveRater, windows: int, signals: RaterSignals) -> RaterDecision:
    """喂 ``windows`` 个完整窗口的同一信号，返回最终决策。"""
    decision = rater.current()
    for _ in range(windows * rater._window_frames):  # noqa: SLF001 - 白盒驱动窗口
        decision = rater.observe(signals)
    return decision


class TestSignalsValidation:
    """RaterSignals 构造期校验。"""

    def test_negative_rtt_rejected(self) -> None:
        with pytest.raises(ValueError, match="rtt_ms"):
            RaterSignals(rtt_ms=-1.0, loss_rate=0.0)

    @pytest.mark.parametrize("loss", [-0.01, 1.01, 2.0])
    def test_loss_out_of_range_rejected(self, loss: float) -> None:
        with pytest.raises(ValueError, match="loss_rate"):
            RaterSignals(rtt_ms=10.0, loss_rate=loss)

    def test_negative_queue_rejected(self) -> None:
        with pytest.raises(ValueError, match="queue_depth"):
            RaterSignals(rtt_ms=10.0, loss_rate=0.0, queue_depth=-1)

    def test_dropped_ratio_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="dropped_ratio"):
            RaterSignals(rtt_ms=10.0, loss_rate=0.0, dropped_ratio=1.5)

    def test_valid_signals_accepts_defaults(self) -> None:
        sig = RaterSignals(rtt_ms=50.0, loss_rate=0.01)
        assert sig.queue_depth == 0
        assert sig.dropped_ratio == 0.0


class TestConstructorValidation:
    """AdaptiveRater 参数校验。"""

    def test_window_frames_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="window_frames"):
            AdaptiveRater(window_frames=0)

    def test_initial_crf_must_be_in_range(self) -> None:
        with pytest.raises(ValueError, match="initial_crf"):
            AdaptiveRater(initial_crf=CRF_MAX + 1)

    def test_target_fps_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="target_fps"):
            AdaptiveRater(target_fps=0.0)

    def test_step_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="step"):
            AdaptiveRater(step=0)

    def test_triggers_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="down_trigger"):
            AdaptiveRater(down_trigger=1.0, up_trigger=0.5)

    def test_reset_rejects_out_of_range_crf(self) -> None:
        rater = AdaptiveRater()
        with pytest.raises(ValueError, match="crf"):
            rater.reset(crf=CRF_MIN - 1)


class TestScoreMonotonic:
    """得分对每个信号单调不减（纯映射层，无迟滞）。"""

    def test_score_monotonic_in_rtt(self) -> None:
        rater = AdaptiveRater()
        scores = [rater.score_of(rtt, 0.0, 0, 0.0) for rtt in (0, 100, 120, 200, 300, 500, 1000)]
        assert scores == sorted(scores)
        assert scores[0] == 0.0
        assert scores[-1] > scores[2]

    def test_score_monotonic_in_loss(self) -> None:
        rater = AdaptiveRater()
        scores = [rater.score_of(10.0, loss, 0, 0.0) for loss in (0, 0.02, 0.05, 0.08, 0.2, 0.5)]
        assert scores == sorted(scores)
        assert scores[-1] > 0.8

    def test_score_monotonic_in_queue(self) -> None:
        rater = AdaptiveRater()
        scores = [rater.score_of(10.0, 0.0, q, 0.0) for q in (0, 5, 8, 12, 20, 100)]
        assert scores == sorted(scores)

    def test_dropping_amps_queue_score(self) -> None:
        rater = AdaptiveRater()
        without = rater.score_of(10.0, 0.0, 20, 0.0)
        with_drop = rater.score_of(10.0, 0.0, 20, 0.2)
        assert with_drop > without

    def test_good_network_scores_zero(self) -> None:
        rater = AdaptiveRater()
        assert rater.score_of(20.0, 0.0, 0, 0.0) == 0.0


class TestCrfForScoreMonotonic:
    """CRF 映射对得分单调不减且在区间内（叮嘱 d 的"并验证不振荡"前提）。"""

    def test_monotonic_non_decreasing(self) -> None:
        rater = AdaptiveRater(initial_crf=28, cooldown_windows=0)
        crfs = [rater.crf_for_score(s / 10.0) for s in range(25)]
        assert crfs == sorted(crfs)

    def test_clamped_to_range(self) -> None:
        rater = AdaptiveRater(initial_crf=CRF_MIN, cooldown_windows=0)
        for _ in range(50):
            rater._crf = rater.crf_for_score(10.0)  # noqa: SLF001 - 直接验证钳制
        assert rater._crf == CRF_MAX  # noqa: SLF001
        rater2 = AdaptiveRater(initial_crf=CRF_MAX, cooldown_windows=1)
        rater2._good_streak = 100  # noqa: SLF001
        for _ in range(50):
            rater2._crf = rater2.crf_for_score(0.0)  # noqa: SLF001
        assert rater2._crf == CRF_MIN  # noqa: SLF001


class TestDegradationConvergence:
    """恶化收敛：高 RTT + 高丢包 + 队列堆积 → CRF 必须持续上升到 CRF_MAX。"""

    def test_bad_network_converges_to_crf_max(self) -> None:
        rater = AdaptiveRater(window_frames=5, initial_crf=28)
        bad = RaterSignals(rtt_ms=500.0, loss_rate=0.3, queue_depth=30, dropped_ratio=0.5)
        history = [rater.observe(bad).crf for _ in range(100)]
        assert history[-1] == CRF_MAX
        # 单调不减（恶化场景绝不允许"反而提画质"）
        assert history == [h for h in history if h >= min(history[: history.index(h) + 1])]
        assert sorted(history) == history

    def test_only_rtt_bad_also_degrades(self) -> None:
        rater = AdaptiveRater(window_frames=5)
        before = rater.current().crf
        after = _run(rater, 4, RaterSignals(rtt_ms=400.0, loss_rate=0.0)).crf
        assert after > before

    def test_only_loss_bad_also_degrades(self) -> None:
        rater = AdaptiveRater(window_frames=5)
        before = rater.current().crf
        after = _run(rater, 4, RaterSignals(rtt_ms=10.0, loss_rate=0.2)).crf
        assert after > before

    def test_severe_degrades_twice_as_fast(self) -> None:
        mild = AdaptiveRater(window_frames=5)
        severe = AdaptiveRater(window_frames=5)
        mild_after = _run(mild, 2, RaterSignals(rtt_ms=150.0, loss_rate=0.03)).crf
        severe_after = _run(severe, 2, RaterSignals(rtt_ms=800.0, loss_rate=0.5)).crf
        assert severe_after - 28 > mild_after - 28


class TestRecoveryConvergence:
    """好转收敛：恢复良好后 CRF 必须回落，且慢升（迟滞生效）。"""

    def test_good_network_converges_to_crf_min(self) -> None:
        rater = AdaptiveRater(window_frames=5, initial_crf=CRF_MAX)
        good = RaterSignals(rtt_ms=15.0, loss_rate=0.0)
        final = _run(rater, 30, good).crf
        assert final == CRF_MIN

    def test_recovery_is_slower_than_degradation(self) -> None:
        # 恶化 1 个窗口立即动手；好转要攒满 cooldown_windows 个窗口
        rater = AdaptiveRater(window_frames=4, initial_crf=28, cooldown_windows=3)
        bad = RaterSignals(rtt_ms=500.0, loss_rate=0.3, queue_depth=20)
        first_bad = rater.observe(bad)
        for _ in range(3):
            first_bad = rater.observe(bad)  # 凑满第一个窗口→评估
        assert first_bad.crf > 28
        good = RaterSignals(rtt_ms=10.0, loss_rate=0.0)
        _run(rater, 1, good)  # 第 1 个良好窗口：未到 cooldown，不动
        d2 = _run(rater, 1, good)  # 第 2 个：仍不动
        assert d2.crf == first_bad.crf and not d2.adjusted
        # 第 3 个良好窗口：允许好转一档
        d3 = _run(rater, 1, good)
        assert d3.crf < first_bad.crf and d3.adjusted

    def test_downgrade_requires_sustained_good(self) -> None:
        # 单个良好窗口夹在恶劣窗口之间 → 计数被打断，不下调
        rater = AdaptiveRater(window_frames=4, initial_crf=34, cooldown_windows=2)
        bad = RaterSignals(rtt_ms=500.0, loss_rate=0.3)
        good = RaterSignals(rtt_ms=10.0, loss_rate=0.0)
        _run(rater, 1, bad)
        _run(rater, 1, good)
        d = _run(rater, 1, bad)
        assert d.crf >= 34  # 未被误导下调


class TestStabilityNoOscillation:
    """正常网络 + 死区：输出恒定，adjusted 恒 False（叮嘱 d 的反面）。"""

    def test_steady_good_network_never_adjusts(self) -> None:
        # 初始已在 CRF_MIN：无进一步好转空间，任何决策都不应动作
        rater = AdaptiveRater(window_frames=5, initial_crf=CRF_MIN)
        decisions = [rater.observe(RaterSignals(rtt_ms=20.0, loss_rate=0.0)) for _ in range(200)]
        assert {d.crf for d in decisions} == {CRF_MIN}
        assert not any(d.adjusted for d in decisions)
        assert not any(d.oscillating for d in decisions)

    def test_median_resists_spike(self) -> None:
        # 窗口内 4 个好样本 + 1 个尖刺 → 中位数仍是好样本，不触发调整
        rater = AdaptiveRater(window_frames=5, initial_crf=28)
        good = RaterSignals(rtt_ms=20.0, loss_rate=0.0)
        spike = RaterSignals(rtt_ms=2000.0, loss_rate=0.9, queue_depth=99)
        for _ in range(4):
            rater.observe(good)
        d = rater.observe(spike)  # 第 5 帧触发评估
        assert d.crf == 28


class TestOscillationFreeze:
    """方向频繁翻转 → 置振荡标志并冻结 CRF。"""

    def test_alternating_signals_freeze(self) -> None:
        # 必须**按整窗口**交替：同一窗口内混入两种信号时，中位数会落在死区，
        # 方向恒 0，振荡无从发生（这也是该机制不误报的原因）。
        rater = AdaptiveRater(
            window_frames=4, initial_crf=28, cooldown_windows=0, freeze_windows=6
        )
        bad = RaterSignals(rtt_ms=400.0, loss_rate=0.3, queue_depth=30)
        good = RaterSignals(rtt_ms=10.0, loss_rate=0.0)
        frozen_at = None
        for _ in range(10):
            _run(rater, 1, bad)  # 一个坏窗口 → 方向 +1
            _run(rater, 1, good)  # 一个好窗口 → 方向 -1
            if rater.current().oscillating:
                frozen_at = rater.current().crf
                break
        assert frozen_at is not None, "整窗口交替信号应触发振荡检测"
        # 冻结后持续恶劣信号也不再调整
        before = rater.current().crf
        for _ in range(40):
            rater.observe(bad)
        assert rater.current().crf == before

    def test_reset_clears_oscillation(self) -> None:
        rater = AdaptiveRater(window_frames=2, freeze_windows=3)
        rater._oscillating = True  # noqa: SLF001 - 直接置位验证复位
        rater.reset(crf=28)
        assert not rater.current().oscillating
        assert rater.current().crf == 28


class TestBitrateCalibration:
    """参考码率随 CRF 单调下降，且量级与 §2.4 实测吻合。"""

    def test_monotonic_decreasing(self) -> None:
        rater = AdaptiveRater(target_fps=30.0)
        rates = []
        for crf in range(CRF_MIN, CRF_MAX + 1):
            rater.reset(crf=crf)
            rates.append(rater.current().bitrate_bps)
        assert rates == sorted(rates, reverse=True)

    def test_crf18_4k_matches_measured_band(self) -> None:
        # §2.4：CRF18 4K = 10.30 Mbps ±10%
        rater = AdaptiveRater(target_fps=30.0)
        rater.reset(crf=18)
        assert 9.2e6 <= rater.current().bitrate_bps <= 11.3e6

    def test_crf45_is_about_seven_times_smaller(self) -> None:
        # §2.4：CRF18→45 带宽压 7×（10.30→1.46 Mbps）
        rater = AdaptiveRater(target_fps=30.0)
        rater.reset(crf=18)
        high = rater.current().bitrate_bps
        rater.reset(crf=45)
        low = rater.current().bitrate_bps
        assert 6.0 <= high / low <= 8.0


class TestResolutionScale:
    """只有 CRF 打满且仍 severe 才降分辨率。"""

    def test_scale_drops_only_at_crf_max(self) -> None:
        rater = AdaptiveRater(window_frames=5, initial_crf=CRF_MAX)
        bad = RaterSignals(rtt_ms=900.0, loss_rate=0.6, queue_depth=50, dropped_ratio=0.9)
        d = _run(rater, 3, bad)
        assert d.crf == CRF_MAX
        assert d.resolution_scale == pytest.approx(0.75)

    def test_scale_stays_one_below_max(self) -> None:
        rater = AdaptiveRater(window_frames=5, initial_crf=30)
        bad = RaterSignals(rtt_ms=500.0, loss_rate=0.3, queue_depth=30)
        d = _run(rater, 2, bad)
        assert d.crf < CRF_MAX
        assert d.resolution_scale == 1.0


class TestDecisionSnapshot:
    """RaterDecision 快照字段齐备。"""

    def test_initial_decision_available_without_observe(self) -> None:
        rater = AdaptiveRater(initial_crf=23)
        d = rater.current()
        assert d.crf == 23
        assert d.bitrate_bps > 0
        assert d.resolution_scale == 1.0
        assert not d.adjusted and not d.oscillating

    def test_decision_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        rater = AdaptiveRater()
        d = rater.current()
        with pytest.raises(FrozenInstanceError):
            d.crf = 10  # type: ignore[misc]

"""自适应码率控制器（网络质量 → CRF / 码率 / 分辨率缩放）。

替换 v4 分散在 ``JPEGEncoder.adjust_quality`` 里的"魔法数字调参"：
v4 用 fps 偏差、包大小比例、移动平均平滑等五六个分支互相打架，
既不可解释也不可收敛测试。v5 把决策收敛到**单一纯函数** :meth:`Rater.decide`
+ 一个带迟滞的状态机 :class:`AdaptiveRater`，可脱离编码器和网络独立单测。

输入信号（三个，全部来自已有组件）
----------------------------------
- ``rtt_ms``：传输层往返时延（心跳测得）；
- ``loss_rate``：``[0, 1]`` 丢包率；
- ``queue_depth``：发送队列积压（配合 ``dropped_ratio`` 判"拥塞且在丢"）。

输出（三者联动，供编码器与采集层消费）
--------------------------------------
- ``crf``：H.264 质量参数，区间 ``[CRF_MIN=18, CRF_MAX=45]``。
  区间来自阶段 5 实测（STAGE5_INPROGRESS §2.4）：CRF 18→45 可把 4K 带宽压 7×
  （10.3 Mbps→1.46 Mbps），且 PSNR 单调（35.2→31.4 dB），是有效的调控旋钮；
- ``bitrate_bps``：按 CRF↔码率标定表线性插值给出的**参考码率**（ABR 上限用），
  标定数据即 §2.4 的 4K 实测列；
- ``resolution_scale``：``1.0`` 或降采样档。仅在"网络已劣化到 CRF 打满仍预期超限"
  时才降，避免默认路径引入缩放开销。

状态机与防振荡
--------------
每 ``window_frames`` 帧（默认 30 = 1s@30fps）评估一次窗口内三信号的**中位数**
（中位数抗单点尖刺）：综合拥塞得分超 ``UP`` 阈值则 CRF 恶化一档、超 ``SEVERE``
阈值则双档；低于 ``DOWN`` 阈值持续 ``cooldown_windows`` 个窗口才允许好转一档
（快降慢升 + 迟滞）。:attr:`RaterDecision.oscillating` 供上层诊断：
窗口间方向频繁翻转即置真，此时**冻结**调整（保持当前 CRF），等信号稳定。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final

from rdc.logging import get_logger

__all__ = [
    "CRF_MAX",
    "CRF_MIN",
    "RaterDecision",
    "RaterSignals",
    "AdaptiveRater",
]

_LOG = get_logger("encoding.raters")

#: CRF 下限（最高画质）。低于 18 码率暴涨而屏幕内容画质增益不明显（§2.4 实测）。
CRF_MIN: Final = 18
#: CRF 上限（最低画质）。高于 45 文字边缘开始碎裂，不可读。
CRF_MAX: Final = 45

#: CRF↔平均包大小标定（4K 真实屏幕内容，30fps，§2.4 实测列）。
#: 键 = CRF，值 = 平均字节/帧。用于把 CRF 换算成参考码率。
_CRF_BITRATE_CALIBRATION: Final = (
    (18, 41_930),
    (23, 23_410),
    (28, 16_430),
    (32, 13_750),
    (38, 9_480),
    (45, 5_940),
)


@dataclass(frozen=True)
class RaterSignals:
    """一次网络/队列状态采样（值对象，调用方构造）。

    Attributes:
        rtt_ms: 往返时延（毫秒），须 ≥0。
        loss_rate: 丢包率 ``[0, 1]``。
        queue_depth: 发送队列积压帧数，须 ≥0。
        dropped_ratio: 窗口内主动丢弃（DropOldest）占应发比例 ``[0, 1]``。
    """

    rtt_ms: float
    loss_rate: float
    queue_depth: int = 0
    dropped_ratio: float = 0.0

    def __post_init__(self) -> None:
        if self.rtt_ms < 0:
            raise ValueError(f"rtt_ms 不能为负: {self.rtt_ms}")
        if not 0.0 <= self.loss_rate <= 1.0:
            raise ValueError(f"loss_rate 必须在 [0,1]: {self.loss_rate}")
        if self.queue_depth < 0:
            raise ValueError(f"queue_depth 不能为负: {self.queue_depth}")
        if not 0.0 <= self.dropped_ratio <= 1.0:
            raise ValueError(f"dropped_ratio 必须在 [0,1]: {self.dropped_ratio}")


@dataclass(frozen=True)
class RaterDecision:
    """rater 的一次输出快照。

    Attributes:
        crf: 建议的 H.264 CRF（``[CRF_MIN, CRF_MAX]``）。
        bitrate_bps: 按标定表换算的参考码率（bps），供 ABR 上限/带宽核对。
        resolution_scale: 分辨率缩放系数，``(0, 1]``；1.0 = 不缩放。
        score: 本次评估的拥塞得分（``[0, ~2]``，调试/测试断言用）。
        adjusted: 本次评估是否实际改变了 CRF（迟滞/冻结时为 False）。
        oscillating: 是否检测到方向振荡（此时调整被冻结）。
    """

    crf: int
    bitrate_bps: int
    resolution_scale: float
    score: float
    adjusted: bool
    oscillating: bool


def _median(values: deque[float]) -> float:
    """ deque 中位数（窗口最多几十个点，排序法足够快且免导入 statistics）。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _calibrate_bitrate(crf: int, fps: float) -> int:
    """CRF → 参考码率（bps）：标定表线性插值 × 帧率。"""
    table = _CRF_BITRATE_CALIBRATION
    size: float
    if crf <= table[0][0]:
        size = table[0][1]
    elif crf >= table[-1][0]:
        size = table[-1][1]
    else:
        size = float(table[-1][1])
        for (c_lo, b_lo), (c_hi, b_hi) in zip(table, table[1:], strict=False):
            if c_lo <= crf <= c_hi:
                frac = (crf - c_lo) / (c_hi - c_lo)
                size = b_lo + (b_hi - b_lo) * frac
                break
    return int(size * fps * 8)


class AdaptiveRater:
    """帧窗口驱动的自适应码率状态机。

    用法：每帧把 :class:`RaterSignals` 喂 :meth:`observe`，取 :meth:`current`
    （O(1)，返回最近一次决策）应用到编码器；内部每 ``window_frames`` 帧才真正
    评估一次，其余帧只累积样本——评估与调用解耦，热路径零分支成本。

    Args:
        window_frames: 评估窗口帧数（默认 30 = 1s@30fps）。
        initial_crf: 初始 CRF（默认 28，§2.4 实测 4K 4.04Mbps 的均衡点）。
        target_fps: 标定码率换算用的目标帧率。
        rtt_bad_ms / rtt_severe_ms: RTT 恶化/严重阈值（超过开始计分/双倍计分）。
        loss_bad / loss_severe: 丢包率恶化/严重阈值。
        queue_bad: 队列积压开始计分的帧数。
        step: 单次调整的 CRF 步长（默认 3，约 1.5× 码率变化）。
        cooldown_windows: 好转方向需要的连续"良好窗口"数（慢升）。
        down_trigger / up_trigger / severe_trigger: 得分的死区与升档阈值。
        freeze_windows: 振荡检测滑窗（方向翻转次数达窗长一半即冻结）。

    Raises:
        ValueError: 参数越界。

    示例:
        >>> rater = AdaptiveRater(window_frames=2)
        >>> for _ in range(2):
        ...     rater.observe(RaterSignals(rtt_ms=500, loss_rate=0.3))
        >>> rater.current().crf > 28
        True
    """

    def __init__(
        self,
        *,
        window_frames: int = 30,
        initial_crf: int = 28,
        target_fps: float = 30.0,
        rtt_bad_ms: float = 120.0,
        rtt_severe_ms: float = 300.0,
        loss_bad: float = 0.02,
        loss_severe: float = 0.08,
        queue_bad: int = 5,
        step: int = 3,
        cooldown_windows: int = 3,
        down_trigger: float = 0.25,
        up_trigger: float = 0.85,
        severe_trigger: float = 1.55,
        freeze_windows: int = 6,
    ) -> None:
        if window_frames < 1:
            raise ValueError(f"window_frames 必须 ≥1，实得 {window_frames}")
        if not CRF_MIN <= initial_crf <= CRF_MAX:
            raise ValueError(f"initial_crf 必须在 [{CRF_MIN},{CRF_MAX}]，实得 {initial_crf}")
        if target_fps <= 0:
            raise ValueError(f"target_fps 必须 >0，实得 {target_fps}")
        if step < 1:
            raise ValueError(f"step 必须 ≥1，实得 {step}")
        if not down_trigger < up_trigger:
            raise ValueError("必须满足 down_trigger < up_trigger（死区约束）")

        self._window_frames = window_frames
        self._target_fps = target_fps
        self._step = step
        self._cooldown_windows = cooldown_windows
        self._down_trigger = down_trigger
        self._up_trigger = up_trigger
        self._severe_trigger = severe_trigger
        self._freeze_windows = freeze_windows

        self._rtt_bad = rtt_bad_ms
        self._rtt_severe = rtt_severe_ms
        self._loss_bad = loss_bad
        self._loss_severe = loss_severe
        self._queue_bad = queue_bad

        self._crf = initial_crf
        self._good_streak = 0
        self._directions: deque[int] = deque(maxlen=freeze_windows)
        self._oscillating = False
        self._rtt_window: deque[float] = deque(maxlen=window_frames)
        self._loss_window: deque[float] = deque(maxlen=window_frames)
        self._queue_window: deque[float] = deque(maxlen=window_frames)
        self._drop_window: deque[float] = deque(maxlen=window_frames)
        self._frame_in_window = 0
        self._last_score = 0.0
        self._current = RaterDecision(
            crf=initial_crf,
            bitrate_bps=_calibrate_bitrate(initial_crf, target_fps),
            resolution_scale=1.0,
            score=0.0,
            adjusted=False,
            oscillating=False,
        )

    # -- 主 API --------------------------------------------------------------

    def observe(self, signals: RaterSignals) -> RaterDecision:
        """喂一帧的网络采样；满窗口时评估并可能调整。

        Args:
            signals: 本帧的网络/队列状态。

        Returns:
            当前决策（评估发生在本次调用内，返回值即反映本次评估结果）。
        """
        self._rtt_window.append(signals.rtt_ms)
        self._loss_window.append(signals.loss_rate)
        self._queue_window.append(float(signals.queue_depth))
        self._drop_window.append(signals.dropped_ratio)
        self._frame_in_window += 1
        if self._frame_in_window >= self._window_frames:
            self._evaluate()
            self._frame_in_window = 0
        return self._current

    def current(self) -> RaterDecision:  # noqa: D401 - 名词式访问器
        """最近一次决策快照（不喂样本也能拿初始值，热路径 O(1)）。"""
        return self._current

    def reset(self, *, crf: int | None = None) -> None:
        """清空窗口与迟滞状态（断线重连/切显示器后调用）。

        Args:
            crf: 重置后的 CRF；``None`` 保持当前值。
        """
        if crf is not None:
            if not CRF_MIN <= crf <= CRF_MAX:
                raise ValueError(f"crf 必须在 [{CRF_MIN},{CRF_MAX}]，实得 {crf}")
            self._crf = crf
        for window in (self._rtt_window, self._loss_window, self._queue_window, self._drop_window):
            window.clear()
        self._frame_in_window = 0
        self._good_streak = 0
        self._directions.clear()
        self._oscillating = False
        self._last_score = 0.0
        self._publish(adjusted=False)

    # -- 纯函数决策面（可独立测试，叮嘱 d 的收敛断言主战场）-------------------

    def score_of(self, rtt: float, loss: float, queue: int, dropped: float = 0.0) -> float:
        """把三/四信号折算成拥塞得分（``[0, 2]`` 附近，越大越糟）。

        分段线性计分：RTT 超 ``rtt_bad`` 起按 ``(rtt-bad)/(severe-bad)`` 封顶 1 分，
        再超 ``rtt_severe`` 部分半权累加（不封顶，保证极端值仍可区分）；
        丢包同理；队列积压按 ``queue/bad`` 封顶 1，正在丢弃时×1.5。
        """
        score = 0.0
        if rtt > self._rtt_bad:
            span = max(self._rtt_severe - self._rtt_bad, 1.0)
            score += min(1.0, (rtt - self._rtt_bad) / span)
            if rtt > self._rtt_severe:
                score += min(0.5, (rtt - self._rtt_severe) / span * 0.5)
        if loss > self._loss_bad:
            span = max(self._loss_severe - self._loss_bad, 1e-9)
            score += min(1.0, (loss - self._loss_bad) / span)
            if loss > self._loss_severe:
                score += min(0.5, (loss - self._loss_severe) / span * 0.5)
        if queue > self._queue_bad:
            q = min(1.0, (queue - self._queue_bad) / max(self._queue_bad, 1))
            score += q * (1.5 if dropped > 0 else 1.0)
        return score

    def crf_for_score(self, score: float) -> int:
        """得分 → 目标 CRF（纯映射，不含迟滞；单调不减，可参数化测试）。"""
        crf = self._crf
        if score >= self._severe_trigger:
            crf += self._step * 2
        elif score >= self._up_trigger:
            crf += self._step
        elif score <= self._down_trigger and self._good_streak >= self._cooldown_windows:
            crf -= self._step
        return max(CRF_MIN, min(CRF_MAX, crf))

    # -- 内部 -----------------------------------------------------------------

    def _evaluate(self) -> None:
        """窗口评估：中位数计分 → 迟滞决策 → 发布快照。"""
        score = self.score_of(
            _median(self._rtt_window),
            _median(self._loss_window),
            int(_median(self._queue_window)),
            _median(self._drop_window),
        )
        self._last_score = score

        # 先更新好转连击计数，再算目标 CRF——cooldown_windows 的语义是
        # "第 N 个连续良好窗口即允许下调"，计数必须先于判定。
        if score <= self._down_trigger:
            self._good_streak += 1
        else:
            self._good_streak = 0

        old_crf = self._crf
        target = self.crf_for_score(score)

        direction = 0
        if target < old_crf:
            direction = -1
        elif target > old_crf:
            direction = 1

        # 振荡检测：滑窗内非零方向的符号翻转次数 ≥ 窗口数-1 → 冻结调整。
        # 只在"窗口间"记录方向（observe 每帧调用不会重复 push 同一决策点）。
        self._directions.append(direction)
        nz = [d for d in self._directions if d != 0]
        flips = sum(1 for a, b in zip(nz, nz[1:], strict=False) if a != b)
        if len(nz) >= 3 and flips >= max(2, len(nz) - 1):
            self._oscillating = True
            _LOG.warning("检测到码率调整振荡，冻结调整直至 reset", flips=flips)

        if not self._oscillating:
            self._crf = target

        # CRF 打满仍 severe → 才引入分辨率降采样一档（0.75 ≈ 面积 56%）
        scale = 1.0
        if self._crf >= CRF_MAX and self._last_score >= self._severe_trigger:
            scale = 0.75
        elif self._crf <= CRF_MIN and self._last_score <= self._down_trigger:
            scale = 1.0
        adjusted = target != old_crf
        self._publish(adjusted=adjusted, resolution_scale=scale)

    def _publish(self, *, adjusted: bool, resolution_scale: float = 1.0) -> None:
        """刷新 :attr:`current` 快照。"""
        self._current = RaterDecision(
            crf=self._crf,
            bitrate_bps=_calibrate_bitrate(self._crf, self._target_fps),
            resolution_scale=resolution_scale,
            score=self._last_score,
            adjusted=adjusted,
            oscillating=self._oscillating,
        )

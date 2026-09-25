"""帧率自适应控制器（阶段 6 重写，v5）。

v4 病灶（ASSESSMENT 关联项）：
- ``wait_frame()`` 内部 ``time.sleep`` 阻塞调用线程，与 asyncio 事件循环不兼容；
- 帧率调整只看网络（RTT/丢包），完全无视画面内容（静止时仍满帧率空转）；
- 统计用墙上时钟 ``time.time()``（NTP 回拨会污染 FPS 测量）。

v5 设计：**纯决策器，零线程原语、零睡眠**。调用方（采集循环 / pipeline）
每个周期把差分结果与网络信号喂进来，拿回一个 :class:`FrameDecision`：

- ``action``：本周期要不要采集+编码+发布（静止且未到强制间隔 → ``IDLE``）；
- ``delay_s``：下一周期之前应等待的秒数（由调用方用 ``asyncio.sleep`` 落实，
  本模块绝不自行阻塞——这是与 v4 的根本区别）；
- ``fps``：当前生效的目标帧率（供诊断/展示）。

调速三路输入（优先级从高到低）：
1. **静止门控**：``DiffResult.is_static`` 为真时帧率降到 ``idle_fps``，并以
   ``idle_force_interval_s`` 周期性强制刷新（防客户端解码器状态漂移）；
2. **内容变化率**：``change_ratio`` 高 → 拉满 ``max_fps``；低 → 按线性区间回落；
3. **网络信号**：RTT/丢包按 :meth:`FramerateController.adjust_fps` 的阈值规则
   乘性压制（与 v4 语义兼容，v4 用户可见行为不突变）。

v4 兼容面（``server/service.py`` 实际用到）：构造签名
``FramerateController(min_fps=5, max_fps=30)`` 与 ``min_fps`` / ``max_fps``
可变属性保持不动；``wait_frame/adjust_fps/set_fps/get_measured_fps/
get_target_fps/reset`` 全部保留（``wait_frame`` 语义改为**只返回建议等待时长、
不再睡眠**——实测 v4 运行时无调用点，属零风险收紧）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rdc.encoding.raters import RaterSignals
from rdc.logging import get_logger

if TYPE_CHECKING:
    from rdc.capture.diff import DiffResult

__all__ = ["FrameAction", "FrameDecision", "FramerateController"]

_LOG = get_logger("capture.framerate")

#: 满速档——变化率达到该比例直接给 max_fps（10% 像素块变化 ≈ 视频/滚动场景）。
FULL_SPEED_RATIO: Final[float] = 0.10
#: 静止/微变时的保底帧率（可被 ``idle_fps`` 参数覆盖）。
DEFAULT_IDLE_FPS: Final[float] = 2.0
#: 静止状态下最长强制刷新间隔（秒）：保证客户端画面最终一致。
DEFAULT_IDLE_FORCE_INTERVAL_S: Final[float] = 5.0
#: 内容调速线性区间的下界：变化率低于该值不再继续压低（回到 idle 档）。
SLOW_RATIO: Final[float] = 0.005


@dataclass(frozen=True)
class FrameAction:
    """本采集周期的动作枚举。"""

    #: 采集 + 编码 + 发布
    CAPTURE = "capture"
    #: 画面静止，跳过本周期（调用方只等待）
    IDLE = "idle"


@dataclass(frozen=True)
class FrameDecision:
    """一个决策周期的输出。

    Attributes:
        action: 本周期动作（是否采集）。
        fps: 决策后的目标帧率（浮点，``[min_fps, max_fps]`` 或 idle 档）。
        delay_s: 本动作完成后到下一周期开始应等待的秒数（≥0）。
        reason: 人读诊断标签（``static`` / ``content`` / ``network`` / ``forced``）。
    """

    action: str
    fps: float
    delay_s: float
    reason: str

    @property
    def should_capture(self) -> bool:
        """本周期是否需要采集+编码+发布。"""
        return self.action == FrameAction.CAPTURE


class FramerateController:
    """帧率自适应决策器（纯逻辑，可注入时钟，无副作用）。

    Args:
        min_fps: 内容/网络调速的下限帧率。
        max_fps: 满速帧率上限。
        idle_fps: 静止档帧率（默认 2.0，仅用于强制刷新节奏）。
        idle_force_interval_s: 静止时最长强制刷新间隔（秒）。
        clock: 单调时钟函数（测试可注入假时钟）；默认 ``time.monotonic``。

    Raises:
        ValueError: 参数非法（min>max、非正数等）。
    """

    def __init__(
        self,
        min_fps: int = 5,
        max_fps: int = 30,
        *,
        idle_fps: float = DEFAULT_IDLE_FPS,
        idle_force_interval_s: float = DEFAULT_IDLE_FORCE_INTERVAL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if min_fps < 1 or max_fps < min_fps:
            raise ValueError(f"帧率区间非法: [{min_fps}, {max_fps}]")
        if not idle_fps > 0:
            raise ValueError(f"idle_fps 必须 >0，实得 {idle_fps}")
        if idle_force_interval_s <= 0:
            raise ValueError(f"idle_force_interval_s 必须 >0，实得 {idle_force_interval_s}")
        self.min_fps = min_fps
        self.max_fps = max_fps
        self._idle_fps = idle_fps
        self._idle_force_interval_s = idle_force_interval_s
        self._clock = clock if callable(clock) else time.monotonic

        # v4 兼容面：current_fps 初值 = max_fps（v4 行为）
        self.current_fps: float = float(max_fps)
        self._last_capture: float | None = None
        self._network_fps: float = float(max_fps)
        # v4 统计面（get_measured_fps 保留）
        self._frame_count = 0
        self._fps_measure_start: float = self._now()
        self._measured_fps = 0.0

    # -- 核心决策 ---------------------------------------------------------------

    def decide(self, diff: DiffResult | None, signals: RaterSignals | None = None) -> FrameDecision:
        """给定本帧差分结果与网络信号，决策下一周期节奏。

        Args:
            diff: 上一周期差分结果；``None`` 视为首帧（必须采集）。
            signals: 网络信号（RTT/丢包等）；``None`` 表示网络无恙。

        Returns:
            :class:`FrameDecision`。首帧、强制刷新、静止等分支均有 ``reason``。
        """
        self._apply_network(signals)
        now = self._now()

        if diff is None or diff.is_first:
            return self._capture(now, fps=self._capped(), reason="first")

        if diff.is_static:
            since = (
                math.inf
                if self._last_capture is None
                else now - self._last_capture
            )
            if since >= self._idle_force_interval_s:
                return self._capture(now, fps=self._idle_fps, reason="forced")
            decision_fps = min(self._idle_fps, self._network_fps)
            return FrameDecision(
                action=FrameAction.IDLE,
                fps=decision_fps,
                delay_s=self._interval(decision_fps),
                reason="static",
            )

        fps = self._content_fps(diff.change_ratio)
        return self._capture(now, fps=fps, reason="content")

    # -- v4 兼容面 ---------------------------------------------------------------

    def wait_frame(self) -> float:
        """v4 兼容：返回距离下一帧还应等待的秒数（**不再睡眠**）。

        v4 实现内部 ``time.sleep`` 阻塞线程；v5 运行在事件循环上，等待由调用方
        ``await asyncio.sleep(...)`` 完成。实测 v4 运行时（service.py）无调用点。
        """
        self._frame_count += 1
        self._maybe_update_measured()
        now = self._now()
        if self._last_capture is None:
            return 0.0
        remain = self._interval(self.current_fps) - (now - self._last_capture)
        return max(0.0, remain)

    def adjust_fps(self, network_latency: float, packet_loss: float) -> float:
        """按网络状况压制帧率上限（v4 阈值规则原样保留）。

        Args:
            network_latency: 往返延迟（**秒**，v4 口径）。
            packet_loss: 丢包率 ``[0,1]``。

        Returns:
            调整后的目标帧率（同时写入 ``current_fps``）。
        """
        if network_latency > 0.1:
            new_fps = self.max_fps * (1 - (network_latency - 0.1) / 0.5)
            new_fps = max(self.min_fps, min(self.max_fps, new_fps))
        else:
            new_fps = float(self.max_fps)
        if packet_loss > 0.01:
            new_fps = new_fps * (1 - packet_loss * 2)
            new_fps = max(self.min_fps, new_fps)
        self._network_fps = new_fps
        self.current_fps = new_fps  # v4 语义：直接生效，不取 min（否则只能降不能升）
        return new_fps

    def set_fps(self, fps: int) -> None:
        """手动设置目标帧率（钳入 ``[min_fps, max_fps]``，v4 语义）。"""
        self.current_fps = float(max(self.min_fps, min(self.max_fps, fps)))
        self._network_fps = self.current_fps

    def get_measured_fps(self) -> float:
        """最近一次窗口（≥1s）的实测发布帧率。"""
        return self._measured_fps

    def get_target_fps(self) -> float:
        """当前目标帧率。"""
        return self.current_fps

    def reset(self) -> None:
        """重置统计与状态（v4 语义 + 清强制刷新计时）。"""
        self._frame_count = 0
        self._fps_measure_start = self._now()
        self._measured_fps = 0.0
        self._last_capture = None
        self.current_fps = float(self.max_fps)
        self._network_fps = float(self.max_fps)

    # -- 内部 ---------------------------------------------------------------------

    def _now(self) -> float:
        return float(self._clock())

    def _capped(self) -> float:
        return min(float(self.max_fps), self._network_fps)

    def _content_fps(self, change_ratio: float) -> float:
        """变化率 → 帧率：满速阈值以上线性拉满，以下向 min_fps 线性回落。"""
        lo = float(self.min_fps)
        hi = self._capped()
        if change_ratio >= FULL_SPEED_RATIO:
            return hi
        if change_ratio <= SLOW_RATIO:
            return max(lo, self._idle_fps)
        t = (change_ratio - SLOW_RATIO) / (FULL_SPEED_RATIO - SLOW_RATIO)
        return max(lo, min(hi, lo + (hi - lo) * t))

    def _apply_network(self, signals: RaterSignals | None) -> None:
        if signals is None:
            return
        # RaterSignals.rtt_ms 是毫秒；v4 adjust_fps 口径是秒
        self.adjust_fps(signals.rtt_ms / 1000.0, signals.loss_rate)

    def _capture(self, now: float, *, fps: float, reason: str) -> FrameDecision:
        self._last_capture = now
        self.current_fps = fps
        self._frame_count += 1
        self._maybe_update_measured()
        return FrameDecision(
            action=FrameAction.CAPTURE, fps=fps, delay_s=self._interval(fps), reason=reason
        )

    def _interval(self, fps: float) -> float:
        return 1.0 / max(fps, 0.1)

    def _maybe_update_measured(self) -> None:
        now = self._now()
        span = now - self._fps_measure_start
        if span >= 1.0:
            self._measured_fps = self._frame_count / span
            self._frame_count = 0
            self._fps_measure_start = now

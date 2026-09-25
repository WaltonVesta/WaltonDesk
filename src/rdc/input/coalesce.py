"""输入事件流合并（coalescing）与帧对齐（阶段 7 批 C）。

背景（v4 病灶）：每个像素的鼠标移动都单独发一个包——30fps 下手速稍快就是
每秒上百个输入包，抢占画面带宽（v4 ASSESSMENT §4 网络层问题）。v5 策略：

- **移动只留末值**：两次发送之间无论经过多少像素，远端只需最终位置——
  按键/滚轮/文本事件自带坐标（:class:`rdc.input.base.InputBackend` 契约
  "先移动再动作"），中间移动点可安全丢弃；
- **按帧率合并**：发送节奏与画面帧对齐——每个视频帧周期最多一个输入批次。
  节拍取 ``max(画面 fps, min_input_fps)``：画面静止降到 idle_fps 时输入不能
  跟着变 500ms 一批（指针移动常不改变远端画面差分，静止门控会误判）；
- **不丢非移动事件**：按下/释放/滚轮/按键/文本按到达顺序排队，绝不合并
  丢弃；队列超过上限立即提前出队（背压），保证按键不卡在缓冲里；
- **文本累加**：连续 :meth:`InputCoalescer.feed_text` 拼接为单事件（IME 已
  上屏短语一次发送），中间被其他事件打断则不跨事件拼接。

纯逻辑件：**不触时钟副作用**（clock 注入，默认 ``time.monotonic``），不依赖
Qt/ctypes/网络——批 A 契约里的 ``FrameDecision`` 仅按鸭子类型消费其 ``fps``
属性，避免输入层反向依赖 capture 层。

帧对齐语义说明：批次序号 ``seq`` 单调递增，装配层（阶段 9）可将其随输入消息
发送，供被控端按批回放；与视频帧的"对齐"体现在**发送节奏同步**而非序号配对
（v5 协议输入消息不含帧号，阶段 3 已冻结）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from rdc.exceptions import InputControlError
from rdc.input.base import MouseButton, normalize_key_name

__all__ = ["InputCoalescer", "InputEvent", "InputKind", "WheelDirection"]


class InputKind(StrEnum):
    """合并后事件的种类（装配层据此分派到后端方法/协议消息）。"""

    MOVE = "move"
    PRESS = "press"
    RELEASE = "release"
    WHEEL = "wheel"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    TEXT = "text"


class WheelDirection(StrEnum):
    """滚轮方向（与 ScreenView ``wheel:<dir>`` 载荷同名）。"""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"

    @property
    def axis_delta(self) -> tuple[int, int]:
        """映射为后端 ``scroll(dx, dy)`` 增量（dy>0 上滚，与 Win32 一致）。"""
        return {
            WheelDirection.UP: (0, 1),
            WheelDirection.DOWN: (0, -1),
            WheelDirection.RIGHT: (1, 0),
            WheelDirection.LEFT: (-1, 0),
        }[self]


@dataclass(frozen=True)
class InputEvent:
    """一次合并输出事件。

    Attributes:
        kind: 事件种类。
        x: 远端虚拟桌面绝对坐标 x（move/press/release/wheel 有效）。
        y: 远端虚拟桌面绝对坐标 y。
        button: press/release 的鼠标键。
        direction: wheel 的方向。
        key: key_press/key_release 的**规范键名**（feed 时已归一）。
        text: TEXT 事件的已上屏文本（可能由连续 feed 拼接而成）。
        seq: 批次内全局单调序号（drain 时赋值，0 起始）。
    """

    kind: InputKind
    x: int = 0
    y: int = 0
    button: MouseButton | None = None
    direction: WheelDirection | None = None
    key: str = ""
    text: str = ""
    seq: int = 0


class _HasFps(Protocol):
    """鸭子协议：携带 ``fps`` 的帧率决策（兼容 capture.framerate.FrameDecision）。"""

    fps: float


#: ScreenView ``mouseMoved(action, x, y)`` 的 action → (kind, 附属值)。
_MOUSE_PREFIX: dict[str, InputKind] = {
    "move": InputKind.MOVE,
    "press": InputKind.PRESS,
    "release": InputKind.RELEASE,
    "wheel": InputKind.WHEEL,
}


def _parse_screen_action(action: str) -> tuple[InputKind, str | None]:
    """解析 ScreenView 载荷 action 串（``move`` / ``press:left`` / ``wheel:up``）。

    Returns:
        ``(kind, 附属名)``：move 的附属为 None；press/release 为键名；
        wheel 为方向名。
    """
    kind_part, _, detail = action.partition(":")
    try:
        kind = _MOUSE_PREFIX[kind_part]
    except KeyError as exc:
        raise InputControlError(f"未知的鼠标动作载荷: {action!r}") from exc
    return kind, detail or None


class InputCoalescer:
    """鼠标/键盘事件流合并器（纯逻辑，时钟可注入）。

    Args:
        clock: 单调时钟源（测试注入假时钟，全确定性）。
        min_input_fps: 输入发送节奏的帧率下限——画面 fps 低于它时输入仍按
            此节奏出批（默认 30，对应"每 33ms 合并"的计划口径）。
        max_pending: 非移动事件排队上限；超过后 feed 立即返回提前出队的
            批次（背压，按键/滚轮绝不因缓冲被丢）。

    Raises:
        InputControlError: fps 参数非法。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_input_fps: float = 30.0,
        max_pending: int = 256,
    ) -> None:
        if min_input_fps <= 0:
            raise InputControlError(f"min_input_fps 必须 >0，实得 {min_input_fps}")
        if max_pending < 1:
            raise InputControlError(f"max_pending 必须 ≥1，实得 {max_pending}")
        self._clock = clock
        self._min_input_fps = min_input_fps
        self._max_pending = max_pending
        self._interval = 1.0 / min_input_fps
        self._next_due = clock() + self._interval
        self._queue: list[InputEvent] = []
        self._pending_move: InputEvent | None = None
        self._seq = 0

    # -- 喂入 ------------------------------------------------------------------

    def feed_mouse(self, action: str, x: int, y: int) -> list[InputEvent]:
        """消费 ScreenView ``mouseMoved`` 载荷（原样三参数）。

        Args:
            action: ``move`` / ``press:<btn>`` / ``release:<btn>`` /
                ``wheel:<dir>``（远端绝对坐标口径）。
            x: 远端虚拟桌面绝对坐标。
            y: 同上。

        Returns:
            通常为空列表；触发队列上限背压时返回提前出队的批次，调用方
            **必须发送**而不能丢弃返回值。

        Raises:
            InputControlError: action 不可解析，或鼠标键/滚轮方向非法。
        """
        kind, detail = _parse_screen_action(action)
        if kind is InputKind.MOVE:
            if self._pending_move is not None and (
                self._pending_move.x == x and self._pending_move.y == y
            ):
                return []  # 完全相同的移动去重
            self._pending_move = InputEvent(kind=kind, x=x, y=y)
            return []
        if kind in (InputKind.PRESS, InputKind.RELEASE):
            if detail is None:
                raise InputControlError(f"缺少鼠标键名: {action!r}")
            try:
                button = MouseButton(detail)
            except ValueError as exc:
                raise InputControlError(f"未知鼠标键: {detail!r}") from exc
            event = InputEvent(kind=kind, x=x, y=y, button=button)
        else:  # WHEEL
            if detail is None:
                raise InputControlError(f"缺少滚轮方向: {action!r}")
            try:
                direction = WheelDirection(detail)
            except ValueError as exc:
                raise InputControlError(f"未知滚轮方向: {detail!r}") from exc
            event = InputEvent(kind=kind, x=x, y=y, direction=direction)
        return self._enqueue(event)

    def feed_key(self, key: str, press: bool) -> list[InputEvent]:
        """键盘事件（键名在此归一为 :func:`rdc.input.base.normalize_key_name` 口径）。

        Returns:
            同 :meth:`feed_mouse`——空或背压提前出队的批次。
        """
        kind = InputKind.KEY_PRESS if press else InputKind.KEY_RELEASE
        return self._enqueue(InputEvent(kind=kind, key=normalize_key_name(key)))

    def feed_text(self, text: str) -> list[InputEvent]:
        """已上屏文本（IME 口径）。空串忽略；队尾是 TEXT 时**拼接**入同一事件。

        Returns:
            同 :meth:`feed_mouse`。
        """
        if not text:
            return []
        if self._queue and self._queue[-1].kind is InputKind.TEXT:
            merged = replace(self._queue[-1], text=self._queue[-1].text + text)
            self._queue[-1] = merged
            return self._check_overflow()
        return self._enqueue(InputEvent(kind=InputKind.TEXT, text=text))

    # -- 节拍 ------------------------------------------------------------------

    def set_fps(self, fps: float) -> None:
        """手动设定画面帧率（等效 :meth:`sync_to_frame`，无决策对象时备用）。

        节拍变化**只提前不推迟**：提速立即生效；装配层每帧调用本方法时，
        若把 ``next_due`` 顺延到未来会导致出批被无限推后（饿死）。

        Raises:
            InputControlError: fps ≤0。
        """
        if fps <= 0:
            raise InputControlError(f"fps 必须 >0，实得 {fps}")
        effective = max(fps, self._min_input_fps)
        self._interval = 1.0 / effective
        self._next_due = min(self._next_due, self._clock() + self._interval)

    def sync_to_frame(self, decision: _HasFps) -> None:
        """消费一帧的帧率决策，更新发送节拍。

        Args:
            decision: 携带 ``fps`` 的帧率决策（鸭子类型，兼容
                :class:`rdc.capture.framerate.FrameDecision`）。
        """
        self.set_fps(decision.fps)

    def due(self, now: float | None = None) -> bool:
        """是否到发送时刻（到点即出批，含队列恰空的轻触发）。"""
        return (now if now is not None else self._clock()) >= self._next_due

    def next_delay_s(self, now: float | None = None) -> float:
        """距下次发送的剩余秒数（装配层据此安排定时器，≤0 表示立即可发）。"""
        current = now if now is not None else self._clock()
        return max(0.0, self._next_due - current)

    def poll(self, now: float | None = None) -> list[InputEvent]:
        """按节拍出批：未到点返回空列表且**不消费队列**（装配层定时器用本方法）。

        与 :meth:`drain` 的区别：drain 是强制刷新（断连、切换标签页等场景），
        永远出队；poll 只在 :meth:`due` 为真时出队。
        """
        current = now if now is not None else self._clock()
        if not self.due(current):
            return []
        return self.drain(current)

    # -- 出队 ------------------------------------------------------------------

    def drain(self, now: float | None = None) -> list[InputEvent]:
        """取出当前批次（移动末值置前，其余保持到达序）并顺延节拍。

        空队列调用合法（返回空列表并顺延——避免"静止时永远 due"的忙轮询）。
        每个事件在此刻获得全局单调 ``seq``。
        """
        current = now if now is not None else self._clock()
        self._next_due = current + self._interval
        batch: Iterable[InputEvent] = (
            ([self._pending_move] if self._pending_move is not None else []) + self._queue
        )
        out = [replace(event, seq=self._seq + i) for i, event in enumerate(batch)]
        self._seq += len(out)
        self._queue.clear()
        self._pending_move = None
        return out

    # -- 观测/控制 ---------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """队列中未出批的事件数（不含待合并的移动末值）。"""
        return len(self._queue)

    def has_pending(self) -> bool:
        """是否有待发送内容（含移动）。"""
        return bool(self._queue) or self._pending_move is not None

    def reset(self) -> None:
        """丢弃全部缓冲（断连/失去焦点时调用；不产生出队批次）。"""
        self._queue.clear()
        self._pending_move = None
        self._next_due = self._clock() + self._interval

    def _enqueue(self, event: InputEvent) -> list[InputEvent]:
        self._queue.append(event)
        return self._check_overflow()

    def _check_overflow(self) -> list[InputEvent]:
        """队列超上限 → 立即提前出队（背压），返回必须发送的批次。"""
        if len(self._queue) > self._max_pending:
            return self.drain()
        return []

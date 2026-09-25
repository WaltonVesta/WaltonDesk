"""InputCoalescer 单元测试（阶段 7 批 C）。

假时钟全确定性：无 sleep、无真实定时器，节拍推进一律手动设定 ``now``。
覆盖：移动末值合并、ScreenView 载荷解析、按键/滚轮保序不丢、帧率决策
消费（``sync_to_frame`` 鸭子类型）、队列背压、文本拼接、序号单调、reset。
"""

from __future__ import annotations

import pytest

from rdc.exceptions import InputControlError
from rdc.input.base import MouseButton
from rdc.input.coalesce import (
    InputCoalescer,
    InputEvent,
    InputKind,
    WheelDirection,
)


class FakeClock:
    """可手动推进的单调时钟（喂入时刻由测试控制，零依赖）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def coalescer(clock: FakeClock) -> InputCoalescer:
    return InputCoalescer(clock=clock, min_input_fps=30.0)


def kinds(events: list[InputEvent]) -> list[InputKind]:
    return [e.kind for e in events]


# ---------------------------------------------------------------------------
# 移动合并
# ---------------------------------------------------------------------------


class TestMoveCoalescing:
    def test_many_moves_emit_last_only(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """一帧内 100 次移动 → 出批仅 1 条，且坐标是末值（v4 病灶消除）。"""
        for i in range(100):
            assert coalescer.feed_mouse("move", i, i * 2) == []
        batch = coalescer.drain(now=clock.now)
        assert len(batch) == 1
        assert (batch[0].x, batch[0].y) == (99, 198)

    def test_identical_move_deduped(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("move", 5, 5)
        coalescer.feed_mouse("move", 5, 5)
        assert len(coalescer.drain(now=clock.now)) == 1

    def test_move_precedes_other_events(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """批次内移动末值置前（后端先定位再动作，契约一致）。"""
        coalescer.feed_key("a", True)
        coalescer.feed_mouse("move", 7, 8)
        batch = coalescer.drain(now=clock.now)
        assert kinds(batch) == [InputKind.MOVE, InputKind.KEY_PRESS]
        assert batch[0].x == 7 and batch[0].y == 8

    def test_no_move_no_move_event(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_key("a", True)
        assert kinds(coalescer.drain(now=clock.now)) == [InputKind.KEY_PRESS]


# ---------------------------------------------------------------------------
# ScreenView 载荷解析（批 A 起点：move / press:btn / release:btn / wheel:dir）
# ---------------------------------------------------------------------------


class TestScreenViewPayload:
    def test_press_release_with_button(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("press:left", 100, 200)
        coalescer.feed_mouse("release:left", 100, 200)
        batch = coalescer.drain(now=clock.now)
        assert kinds(batch) == [InputKind.PRESS, InputKind.RELEASE]
        assert batch[0].button is MouseButton.LEFT
        assert (batch[0].x, batch[0].y) == (100, 200)

    def test_wheel_directions(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        for direction in ("up", "down", "left", "right"):
            coalescer.feed_mouse(f"wheel:{direction}", 1, 2)
        batch = coalescer.drain(now=clock.now)
        assert [e.direction for e in batch] == [
            WheelDirection.UP,
            WheelDirection.DOWN,
            WheelDirection.LEFT,
            WheelDirection.RIGHT,
        ]

    @pytest.mark.parametrize(
        ("direction", "expected"),
        [
            (WheelDirection.UP, (0, 1)),
            (WheelDirection.DOWN, (0, -1)),
            (WheelDirection.RIGHT, (1, 0)),
            (WheelDirection.LEFT, (-1, 0)),
        ],
    )
    def test_axis_delta_matches_backend_convention(
        self, direction: WheelDirection, expected: tuple[int, int]
    ) -> None:
        """dy>0=上滚（与 WindowsBackend.scroll 口径一致）。"""
        assert direction.axis_delta == expected

    def test_middle_button_accepted(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("press:middle", 0, 0)
        assert coalescer.drain(now=clock.now)[0].button is MouseButton.MIDDLE

    @pytest.mark.parametrize(
        "action",
        [
            "hover",  # 未知动作
            "press",  # 缺键名
            "wheel",  # 缺方向
            "press:thumb",  # 未知键
            "wheel:sideways",  # 未知方向
        ],
    )
    def test_malformed_action_raises(self, coalescer: InputCoalescer, action: str) -> None:
        with pytest.raises(InputControlError):
            coalescer.feed_mouse(action, 1, 1)


# ---------------------------------------------------------------------------
# 保序与不丢
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_events_keep_arrival_order(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("press:left", 1, 1)
        coalescer.feed_key("ctrl", True)
        coalescer.feed_key("c", True)
        coalescer.feed_key("c", False)
        coalescer.feed_mouse("release:left", 1, 1)
        batch = coalescer.drain(now=clock.now)
        assert kinds(batch) == [
            InputKind.PRESS,
            InputKind.KEY_PRESS,
            InputKind.KEY_PRESS,
            InputKind.KEY_RELEASE,
            InputKind.RELEASE,
        ]

    def test_seq_monotonic_across_batches(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """全局单调序号跨批次递增，供装配层按批回放排序。"""
        coalescer.feed_key("a", True)
        first = coalescer.drain(now=clock.now)
        clock.advance(0.1)
        coalescer.feed_key("b", True)
        second = coalescer.drain(now=clock.now)
        assert [e.seq for e in first] == [0]
        assert [e.seq for e in second] == [1]

    def test_clicks_never_merged_away(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """同坐标的 5 次连点必须 5 条都在（只有 move 可合并）。"""
        for _ in range(5):
            coalescer.feed_mouse("press:left", 9, 9)
            coalescer.feed_mouse("release:left", 9, 9)
        batch = coalescer.drain(now=clock.now)
        assert len(batch) == 10


# ---------------------------------------------------------------------------
# 帧对齐 / 节拍
# ---------------------------------------------------------------------------


class TestCadence:
    def test_not_due_before_interval(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_key("a", True)
        clock.advance(1 / 60)  # 16ms < 33ms（min_input_fps=30）
        assert coalescer.due() is False
        assert coalescer.poll(now=clock.now) == []  # poll 未到点不消费
        assert coalescer.pending_count == 1
        assert len(coalescer.drain(now=clock.now)) == 1  # drain 是强制刷新，永远出队
        assert coalescer.pending_count == 0

    def test_due_after_interval(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_key("a", True)
        clock.advance(1 / 30 + 1e-9)
        assert coalescer.due() is True
        assert len(coalescer.drain(now=clock.now)) == 1

    def test_idle_fps_does_not_slow_input(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """画面静止降到 idle_fps（帧决策 fps=2），输入仍按 min_input_fps=30 出批。"""
        from dataclasses import dataclass as dc

        @dc
        class FakeDecision:
            fps: float

        coalescer.sync_to_frame(FakeDecision(fps=2.0))
        coalescer.feed_key("a", True)
        clock.advance(1 / 30 + 1e-9)
        assert coalescer.due() is True
        assert len(coalescer.drain(now=clock.now)) == 1

    def test_high_fps_uses_frame_cadence(self, clock: FakeClock) -> None:
        coalescer = InputCoalescer(clock=clock, min_input_fps=30.0)

        class Decision:
            fps = 60.0

        coalescer.sync_to_frame(Decision())
        coalescer.feed_key("a", True)
        clock.advance(1 / 60 + 1e-9)
        assert coalescer.due() is True

    def test_next_delay_s(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        assert coalescer.next_delay_s(now=clock.now) == pytest.approx(1 / 30)
        clock.advance(1 / 30 + 1e-9)
        assert coalescer.next_delay_s(now=clock.now) == 0.0

    def test_set_fps_validation(self, coalescer: InputCoalescer) -> None:
        with pytest.raises(InputControlError):
            coalescer.set_fps(0)
        with pytest.raises(InputControlError):
            InputCoalescer(min_input_fps=-1)
        with pytest.raises(InputControlError):
            InputCoalescer(max_pending=0)

    def test_drain_empty_still_reschedules(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """空队到点出批返回空并顺延——避免静止时 due 恒真导致忙轮询。"""
        clock.advance(1 / 30 + 1e-9)
        assert coalescer.drain(now=clock.now) == []
        clock.advance(1e-9)
        assert coalescer.due() is False


# ---------------------------------------------------------------------------
# 背压
# ---------------------------------------------------------------------------


class TestBackpressure:
    def test_overflow_flushes_early(self, clock: FakeClock) -> None:
        """超 max_pending 立即提前出队，返回值必须被发送（按键不卡缓冲）。"""
        coalescer = InputCoalescer(clock=clock, min_input_fps=30.0, max_pending=4)
        flushed: list[InputEvent] = []
        for i in range(10):
            flushed.extend(coalescer.feed_key("a", i % 2 == 0))
        assert len(flushed) == 10  # 第 5、10 条各触发一次，每次带出 5 条
        assert coalescer.pending_count == 0
        assert coalescer.drain(now=clock.now) == []
        # 序号连续，无丢失无重复
        assert [e.seq for e in flushed] == list(range(10))

    def test_overflow_preserves_order(self, clock: FakeClock) -> None:
        coalescer = InputCoalescer(clock=clock, min_input_fps=30.0, max_pending=2)
        all_events: list[InputEvent] = []
        for i in range(5):
            all_events.extend(coalescer.feed_mouse("press:left", i, 0))
        all_events.extend(coalescer.drain(now=clock.now))
        assert [e.x for e in all_events] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 文本（IME 已上屏短语）
# ---------------------------------------------------------------------------


class TestText:
    def test_empty_ignored(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        assert coalescer.feed_text("") == []
        assert not coalescer.has_pending()

    def test_consecutive_feed_merges(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        """连续 feed（候选词逐段上屏）拼接为单事件。"""
        coalescer.feed_text("你好")
        coalescer.feed_text("世界")
        batch = coalescer.drain(now=clock.now)
        assert len(batch) == 1
        assert batch[0].text == "你好世界"

    def test_text_not_merged_across_other_events(
        self, coalescer: InputCoalescer, clock: FakeClock
    ) -> None:
        coalescer.feed_text("a")
        coalescer.feed_key("enter", True)
        coalescer.feed_text("b")
        texts = [e for e in coalescer.drain(now=clock.now) if e.kind is InputKind.TEXT]
        assert [t.text for t in texts] == ["a", "b"]

    def test_text_seq_and_order(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_text("hi")
        batch = coalescer.drain(now=clock.now)
        assert batch[0].kind is InputKind.TEXT
        assert batch[0].seq == 0


# ---------------------------------------------------------------------------
# reset / 状态
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_drops_buffer(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("move", 1, 1)
        coalescer.feed_key("ctrl", True)
        coalescer.reset()
        assert not coalescer.has_pending()
        assert coalescer.drain(now=clock.now + 1) == []

    def test_has_pending_and_count(self, coalescer: InputCoalescer, clock: FakeClock) -> None:
        coalescer.feed_mouse("move", 1, 1)
        assert coalescer.has_pending()
        assert coalescer.pending_count == 0  # move 不算队列（待合并）
        coalescer.feed_key("a", True)
        assert coalescer.pending_count == 1

    def test_seq_not_reset_to_avoid_replay_ambiguity(
        self, coalescer: InputCoalescer, clock: FakeClock
    ) -> None:
        """reset 后序号继续递增（避免装配层把新事件误判为旧批次重复）。"""
        coalescer.feed_key("a", True)
        coalescer.drain(now=clock.now)
        coalescer.reset()
        coalescer.feed_key("b", True)
        batch = coalescer.drain(now=clock.now + 1)
        assert batch[0].seq == 1

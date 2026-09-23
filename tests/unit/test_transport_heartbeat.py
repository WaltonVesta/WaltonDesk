"""transport.heartbeat 测试：asyncio 心跳。

驱动方式：interval 用真实小值（10ms），"推进循环"= 真实等待若干 tick；
超时判定用假时钟手动 advance，测试稳定且无长等待。
"""

from __future__ import annotations

import pytest

from rdc.transport.heartbeat import HeartbeatController


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


_INTERVAL = 0.01  # 10ms 真实节拍
#: 等待约 5 个 tick（留余量，CI 慢机不误判）
async def _ticks(n: int = 5) -> None:
    import asyncio

    await asyncio.sleep(_INTERVAL * n * 1.5)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class TestBasics:
    async def test_start_stop_lifecycle(self, clock: FakeClock) -> None:
        sent: list[int] = []

        async def send() -> None:
            sent.append(1)

        hb = HeartbeatController(
            send, lambda: None, interval=_INTERVAL, timeout=1e6, clock=clock
        )
        hb.start()
        assert hb.is_running
        await _ticks()
        assert sent
        await hb.stop()
        assert not hb.is_running

    async def test_stop_idempotent(self, clock: FakeClock) -> None:
        async def send() -> None:
            return None

        hb = HeartbeatController(send, lambda: None, interval=_INTERVAL, clock=clock)
        hb.start()
        await hb.stop()
        await hb.stop()

    async def test_double_start_reuses_task(self, clock: FakeClock) -> None:
        async def send() -> None:
            return None

        hb = HeartbeatController(send, lambda: None, interval=_INTERVAL, clock=clock)
        hb.start()
        first = hb._task  # noqa: SLF001
        hb.start()
        assert hb._task is first  # noqa: SLF001
        await hb.stop()

    def test_invalid_params_rejected(self) -> None:
        async def send() -> None:
            return None

        with pytest.raises(ValueError):
            HeartbeatController(send, lambda: None, interval=0)
        with pytest.raises(ValueError):
            HeartbeatController(send, lambda: None, timeout=0)
        with pytest.raises(ValueError):
            HeartbeatController(send, lambda: None, max_failures=0)


class TestTimeoutPaths:
    async def test_peer_silent_triggers_timeout_once(self, clock: FakeClock) -> None:
        calls: list[str] = []

        async def send() -> None:
            return None

        hb = HeartbeatController(
            send,
            lambda: calls.append("t"),
            interval=_INTERVAL,
            timeout=5.0,
            max_failures=99,
            clock=clock,
        )
        hb.start()
        await _ticks()
        assert calls == []  # 假时钟没动 → 未超时
        clock.advance(5.0)
        await _ticks(2)
        assert calls == ["t"]
        assert not hb.is_alive()
        await _ticks(2)
        assert calls == ["t"]  # 判死后循环退出，回调至多一次
        await hb.stop()

    async def test_send_failures_trip_after_max(self, clock: FakeClock) -> None:
        calls: list[str] = []

        async def send_fail() -> None:
            raise ConnectionResetError("down")

        hb = HeartbeatController(
            send_fail,
            lambda: calls.append("x"),
            interval=_INTERVAL,
            timeout=1e6,
            max_failures=3,
            clock=clock,
        )
        hb.start()
        await _ticks(1)
        assert calls == []  # 首次 tick 失败 1 次 < 阈值 3
        await _ticks(4)
        assert calls == ["x"]
        await hb.stop()

    async def test_on_pong_resets_silence(self, clock: FakeClock) -> None:
        calls: list[str] = []

        async def send() -> None:
            return None

        hb = HeartbeatController(
            send,
            lambda: calls.append("t"),
            interval=_INTERVAL,
            timeout=5.0,
            max_failures=99,
            clock=clock,
        )
        hb.start()
        clock.advance(4.0)
        await _ticks(2)
        hb.on_pong()  # 活跃刷新
        clock.advance(4.0)  # 距上次 pong 仅 4s < 5s
        await _ticks(2)
        assert calls == []
        await hb.stop()

    async def test_on_pong_resets_failure_count(self, clock: FakeClock) -> None:
        ok = False
        fail_count = 0

        async def flaky_send() -> None:
            nonlocal fail_count
            if not ok:
                fail_count += 1
                raise ConnectionResetError

        calls: list[str] = []
        hb = HeartbeatController(
            flaky_send,
            lambda: calls.append("t"),
            interval=_INTERVAL,
            timeout=1e6,
            max_failures=10,
            clock=clock,
        )
        hb.start()
        await _ticks(3)
        assert fail_count > 0  # 确实验证过失败路径
        ok = True
        hb.on_pong()  # 失败计数清零
        await _ticks(3)
        assert calls == []  # max=10：即便不清零也远不到阈值，清零逻辑由下式验证
        # 直接断言内部计数已清
        assert hb._failures == 0  # noqa: SLF001
        await hb.stop()

    async def test_timeout_callback_exception_contained(self, clock: FakeClock) -> None:
        def boom() -> None:
            raise RuntimeError("callback crash")

        async def send() -> None:
            return None

        hb = HeartbeatController(
            send, boom, interval=_INTERVAL, timeout=1.0, max_failures=99, clock=clock
        )
        hb.start()
        clock.advance(1.0)
        await _ticks(2)
        assert not hb.is_alive()  # 判死；回调异常被记录不外逸
        assert hb._task.done()  # noqa: SLF001
        await hb.stop()

    async def test_reset_refreshes_state(self, clock: FakeClock) -> None:
        async def send() -> None:
            return None

        hb = HeartbeatController(
            send, lambda: None, interval=_INTERVAL, timeout=5.0, clock=clock
        )
        clock.advance(100)
        hb.reset()  # last_recv = 100
        hb.start()
        await _ticks(2)
        assert hb.is_alive()  # 100-100=0 < 5 → 不误判
        await hb.stop()

    async def test_send_success_clears_failures(self, clock: FakeClock) -> None:
        remaining_failures = 1

        async def send() -> None:
            nonlocal remaining_failures
            if remaining_failures > 0:
                remaining_failures -= 1
                raise ConnectionResetError

        hb = HeartbeatController(
            send, lambda: None, interval=_INTERVAL, timeout=1e6, max_failures=2, clock=clock
        )
        hb.start()
        await _ticks(4)
        assert hb.is_alive()  # 失败 1 次即恢复，未达 2 次阈值
        await hb.stop()

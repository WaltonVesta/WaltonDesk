"""transport.reconnect 测试：指数退避 + 上限 + stop 取消 + 观测面。"""

from __future__ import annotations

import asyncio

import pytest

from rdc.transport.reconnect import ReconnectController, backoff_delay


class TestBackoff:
    def test_exponential_sequence(self) -> None:
        assert [backoff_delay(i, 2.0, 60.0) for i in range(1, 6)] == [2.0, 4.0, 8.0, 16.0, 32.0]

    def test_capped(self) -> None:
        assert backoff_delay(10, 2.0, 60.0) == 60.0
        assert backoff_delay(100, 5.0, 60.0) == 60.0

    def test_custom_cap(self) -> None:
        assert backoff_delay(4, 1.0, 5.0) == 5.0


class TestRun:
    async def test_first_attempt_success(self) -> None:
        calls = 0

        async def connect() -> bool:
            nonlocal calls
            calls += 1
            return True

        rc = ReconnectController(connect, max_attempts=5, delay=0.01)
        assert await rc.run() is True
        assert calls == 1
        assert rc.get_last_result() is True
        assert rc.get_attempts() == 1
        assert rc.get_progress() == 0.2

    async def test_success_on_third_attempt(self) -> None:
        calls = 0

        async def connect() -> bool:
            nonlocal calls
            calls += 1
            return calls == 3

        rc = ReconnectController(connect, max_attempts=5, delay=0.01)
        assert await rc.run() is True
        assert calls == 3

    async def test_exhausted_returns_false(self) -> None:
        async def connect() -> bool:
            return False

        rc = ReconnectController(connect, max_attempts=3, delay=0.01)
        assert await rc.run() is False
        assert rc.get_attempts() == 3
        assert rc.get_progress() == 1.0
        assert rc.get_last_result() is False

    async def test_oserror_counts_as_attempt(self) -> None:
        calls = 0

        async def connect() -> bool:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ConnectionRefusedError("nope")
            return True

        rc = ReconnectController(connect, max_attempts=5, delay=0.01)
        assert await rc.run() is True
        assert calls == 2

    async def test_programming_error_propagates(self) -> None:
        """非网络异常（编程错误）不吞——上抛暴露（约束 6 精神）。"""

        async def connect() -> bool:
            raise KeyError("bug")

        rc = ReconnectController(connect, max_attempts=3, delay=0.01)
        with pytest.raises(KeyError):
            await rc.run()

    async def test_fixed_interval_mode(self) -> None:
        stamps: list[float] = []

        async def connect() -> bool:
            stamps.append(asyncio.get_running_loop().time())
            return len(stamps) >= 3

        rc = ReconnectController(connect, max_attempts=5, delay=0.02, use_backoff=False)
        assert await rc.run()
        assert len(stamps) == 3
        # 固定间隔：两次间隔接近 delay（远小于指数增长）
        assert stamps[1] - stamps[0] < 0.15

    async def test_invalid_params_rejected(self) -> None:
        async def connect() -> bool:
            return True

        with pytest.raises(ValueError):
            ReconnectController(connect, max_attempts=0)
        with pytest.raises(ValueError):
            ReconnectController(connect, delay=0)


class TestStartStop:
    async def test_start_then_stop_cancels(self) -> None:
        started = asyncio.Event()

        async def never() -> bool:
            started.set()
            await asyncio.sleep(3600)
            return True

        rc = ReconnectController(never, max_attempts=100, delay=0.01)
        task = rc.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        assert rc.is_running()
        rc.stop()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not rc.is_running()

    async def test_start_returns_same_task_while_running(self) -> None:
        async def slow() -> bool:
            await asyncio.sleep(3600)
            return True

        rc = ReconnectController(slow, delay=0.01)
        t1 = rc.start()
        t2 = rc.start()
        assert t1 is t2
        rc.stop()
        with pytest.raises(asyncio.CancelledError):
            await t1

    async def test_reset_clears_counters(self) -> None:
        async def connect() -> bool:
            return False

        rc = ReconnectController(connect, max_attempts=2, delay=0.01)
        await rc.run()
        assert rc.get_attempts() == 2
        rc.reset()
        assert rc.get_attempts() == 0
        assert rc.get_last_result() is False

    async def test_stop_idempotent_without_start(self) -> None:
        async def connect() -> bool:
            return True

        ReconnectController(connect).stop()  # 未启动也不炸

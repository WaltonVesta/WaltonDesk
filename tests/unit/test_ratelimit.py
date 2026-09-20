"""速率限制器回归测试（阶段 1）。

覆盖 ``rdc.client.ratelimit.RateLimiter`` 的滑动窗口逻辑。用 monkeypatch
控制 ``time.time``，使时间推进完全确定，不依赖真实 sleep（快、稳、可复现）。
"""

from __future__ import annotations

import pytest

from rdc.client import ratelimit
from rdc.client.ratelimit import RateLimiter


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """提供一个可控时钟。

    返回一个 ``advance(dt)`` 函数，每次调用把 ``time.time`` 的返回值前移
    ``dt`` 秒。RateLimiter 内部通过 ``time.time()`` 取时间，patch 该属性
    即可完全掌控窗口推进。
    """
    current = [1_000_000.0]

    def fake_time() -> float:
        return current[0]

    def advance(dt: float) -> None:
        current[0] += dt

    monkeypatch.setattr(ratelimit.time, "time", fake_time)
    return advance


class TestRateLimiterAllow:
    """allow() 的准入判定。"""

    def test_allows_up_to_limit(self, clock) -> None:
        """窗口内前 N 次放行，第 N+1 次拒绝。"""
        rl = RateLimiter(max_connections_per_second=3, window_size=1)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False  # 第 4 次超限

    def test_window_slide_re_admits(self, clock) -> None:
        """超过窗口时长后，旧时间戳过期，重新放行。"""
        rl = RateLimiter(max_connections_per_second=2, window_size=1)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False
        clock(1.1)  # 推进超过 window_size
        assert rl.allow() is True  # 窗口已滑过，重新准入

    def test_partial_window_expiry(self, clock) -> None:
        """部分时间戳过期时，按剩余数量放行。"""
        rl = RateLimiter(max_connections_per_second=2, window_size=1)
        assert rl.allow() is True
        clock(0.6)
        assert rl.allow() is True
        assert rl.allow() is False
        clock(0.5)  # 第一个时间戳（t=0）已过期，第二个（t=0.6）仍在窗口内
        assert rl.allow() is True  # 只剩 1 个在窗口内，可再放行 1 个
        assert rl.allow() is False


class TestRateLimiterCount:
    """get_connection_count() 窗口内计数。"""

    def test_count_reflects_admitted(self, clock) -> None:
        """计数等于窗口内已放行的连接数。"""
        rl = RateLimiter(max_connections_per_second=5, window_size=1)
        rl.allow()
        rl.allow()
        assert rl.get_connection_count() == 2

    def test_count_drops_expired(self, clock) -> None:
        """过期时间戳被清理，计数回落。"""
        rl = RateLimiter(max_connections_per_second=5, window_size=1)
        rl.allow()
        rl.allow()
        clock(1.1)
        assert rl.get_connection_count() == 0


class TestRateLimiterReset:
    """reset() 清空窗口。"""

    def test_reset_clears_window(self, clock) -> None:
        """重置后计数归零，且可重新放行满额度。"""
        rl = RateLimiter(max_connections_per_second=2, window_size=1)
        rl.allow()
        rl.allow()
        assert rl.allow() is False
        rl.reset()
        assert rl.get_connection_count() == 0
        assert rl.allow() is True
        assert rl.allow() is True


class TestRateLimiterThreadSafety:
    """并发准入不超发（锁的正确性）。"""

    def test_concurrent_allow_respects_limit(self) -> None:
        """多线程并发 allow()，总放行数不超过限额（窗口足够大避免过期干扰）。"""
        import threading

        # 用真实时间但把 window 设大，确保测试期间无时间戳过期
        rl = RateLimiter(max_connections_per_second=50, window_size=60)
        granted = []
        lock = threading.Lock()

        def worker() -> None:
            result = rl.allow()
            with lock:
                granted.append(result)

        threads = [threading.Thread(target=worker) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for g in granted if g) == 50  # 恰好放行 50 个

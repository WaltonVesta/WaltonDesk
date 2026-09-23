"""transport.backpressure 测试：DropOldestQueue 满丢最旧 + 计数。"""

from __future__ import annotations

import asyncio

import pytest

from rdc.transport.backpressure import DEFAULT_QUEUE_SIZE, DropOldestQueue, QueueStats


class TestPushPop:
    async def test_fifo_order(self) -> None:
        q: DropOldestQueue[int] = DropOldestQueue(3)
        assert q.push(1) and q.push(2) and q.push(3)
        assert await q.pop() == 1
        assert await q.pop() == 2
        assert await q.pop() == 3

    async def test_full_drops_oldest_keeps_newest(self) -> None:
        """满队列丢的是**最旧**（叮嘱 a：不是新帧）。"""
        q: DropOldestQueue[int] = DropOldestQueue(2)
        q.push(1)
        q.push(2)
        assert q.push(3) is False  # 满 → 返回 False（发生丢弃）
        assert await q.pop() == 2  # 1 被丢，2 是当时最旧
        assert await q.pop() == 3  # 新帧 3 保留

    def test_dropped_counter_exact(self) -> None:
        q: DropOldestQueue[int] = DropOldestQueue(2)
        for i in range(10):
            q.push(i)
        assert q.dropped == 8  # 10 push, 2 留存
        stats = q.stats()
        assert stats == QueueStats(enqueued=10, dequeued=0, dropped=8, maxsize=2)
        assert stats.drop_ratio == 0.8

    def test_drop_ratio_zero_when_empty(self) -> None:
        assert QueueStats().drop_ratio == 0.0

    async def test_size_tracks(self) -> None:
        q: DropOldestQueue[str] = DropOldestQueue(5)
        assert q.size == 0
        q.push("a")
        assert q.size == 1
        await q.pop()
        assert q.size == 0

    def test_default_maxsize(self) -> None:
        assert DropOldestQueue().maxsize == DEFAULT_QUEUE_SIZE

    async def test_pop_waits_for_push(self) -> None:
        q: DropOldestQueue[int] = DropOldestQueue(1)
        task = asyncio.create_task(q.pop())
        await asyncio.sleep(0.01)
        assert not task.done()
        q.push(42)
        assert await task == 42

    def test_rejects_nonpositive_maxsize(self) -> None:
        with pytest.raises(ValueError, match="maxsize"):
            DropOldestQueue(0)
        with pytest.raises(ValueError):
            DropOldestQueue(-1)

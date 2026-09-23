"""发送背压：有界队列 + 满时丢最旧帧（阶段 4，4.4）。

语义与 v4 ``screen_channel`` 的 ``deque(maxlen) + popleft`` 相同，但改为
asyncio 原生实现并补齐可观测性：

- :meth:`DropOldestQueue.push` 非阻塞；队列满时**丢弃最旧**帧为新帧腾位
  （画面帧只有"最新"有意义，丢新帧会让用户看到更旧的画面），
  并累加 :attr:`dropped` 计数供上层上报/诊断。
- :meth:`pop` 阻塞等待（消费者侧），配合每客户端独立发送协程使用
  （见 :mod:`rdc.transport.broadcaster`）。
- :func:`enqueue_frame` 是"尽力入队"包装（``Queue.full`` → 丢旧 + 计数），
  供 broadcaster 统一使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Final

__all__ = ["DEFAULT_QUEUE_SIZE", "DropOldestQueue", "QueueStats"]

#: 画面发送队列默认深度（约 2 秒 @ 30fps；v4 为 10，语义对齐）。
DEFAULT_QUEUE_SIZE: Final[int] = 10


@dataclass(frozen=True)
class QueueStats:
    """队列运行统计（入队/出队/丢弃计数）。"""

    enqueued: int = 0
    dequeued: int = 0
    dropped: int = 0
    maxsize: int = field(default=DEFAULT_QUEUE_SIZE)

    @property
    def drop_ratio(self) -> float:
        """丢弃占比（0.0~1.0）；无入队时为 0.0。"""
        if self.enqueued == 0:
            return 0.0
        return self.dropped / self.enqueued


class DropOldestQueue[T]:
    """有界 asyncio 队列：满时 :meth:`push` 丢最旧项并计数。

    Args:
        maxsize: 队列深度上限（必须为正）。

    Raises:
        ValueError: ``maxsize <= 0``。
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize 必须为正整数，收到 {maxsize}")
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._enqueued = 0
        self._dequeued = 0
        self._dropped = 0

    # -- 生产者侧 -------------------------------------------------------------

    def push(self, item: T) -> bool:
        """尽力入队（非阻塞）。

        Returns:
            ``True`` 正常入队；``False`` 表示队列满、已丢弃最旧帧腾位。
            两种情况下**新帧一定在队列里**（丢旧保新）。
        """
        self._enqueued += 1
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # 满队列必有项；get 不会抛
            except asyncio.QueueEmpty:  # pragma: no cover - 理论竞态防线（消费者恰在间隙取走）
                self._queue.put_nowait(item)
                return True
            self._dropped += 1
            self._queue.put_nowait(item)
            return False

    # -- 消费者侧 -------------------------------------------------------------

    async def pop(self) -> T:
        """阻塞等待下一项。"""
        item = await self._queue.get()
        self._dequeued += 1
        return item

    # -- 观测 -----------------------------------------------------------------

    @property
    def maxsize(self) -> int:
        """队列深度上限。"""
        return self._maxsize

    @property
    def size(self) -> int:
        """当前排队项数。"""
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        """累计丢弃计数。"""
        return self._dropped

    def stats(self) -> QueueStats:
        """当前统计快照。"""
        return QueueStats(
            enqueued=self._enqueued,
            dequeued=self._dequeued,
            dropped=self._dropped,
            maxsize=self._maxsize,
        )

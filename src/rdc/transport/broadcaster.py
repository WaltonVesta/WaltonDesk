"""画面广播器（阶段 4，4.9 / B3 修复核心）。

v4 病灶（ASSESSMENT §5.2-B3）：``daemon._broadcast_loop`` 在**单个广播线程**内
对全部订阅者串行 ``conn.sendall(frame_bytes)``——任一慢客户端（弱网/后台挂起）
阻塞 ``sendall``，整条广播循环停摆，**所有客户端**画面一起冻结
（head-of-line blocking）。

v5 修复（用户确认方案：独立 asyncio 层，阶段 9 接入 service）：

- 每订阅者一条 :class:`~rdc.transport.backpressure.DropOldestQueue` +
  一个独立发送 task；慢客户端只堆积/丢弃**自己**队列里的旧帧，
  广播协程 ``publish`` 全程非阻塞（入队即返回）。
- **每订阅者单独签名**：v5 每连接有独立 HMAC 会话密钥（F1 方案 B），
  帧 trailer 依赖密钥，无法像 v4 那样字节级 zero-copy 共享同一
  ``frame_bytes``。代价是每帧每客户端一次 protobuf 序列化 + HMAC-SHA256
  （SHA-256 吞吐 ~GB/s，4K 帧 ~百 KB 时单客户端 <1ms）；换来的是
  连接级完整性与队头阻塞消除，架构权衡记录于 STAGE4_REPORT。
- 断开回收：发送 task 捕获 ``OSError`` → 注销订阅者（回调通知），
  队列/任务无残留（验收：压测后 fd 与 RSS 回落基线）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from rdc.logging import get_logger
from rdc.protocol.codec import encode_frame
from rdc.protocol.messages import ScreenFrame
from rdc.transport.backpressure import DEFAULT_QUEUE_SIZE, DropOldestQueue, QueueStats
from rdc.transport.base import FrameStream

__all__ = ["BroadcasterStats", "ScreenBroadcaster", "SubscriberInfo"]

_LOG = get_logger("transport.broadcaster")

#: 订阅者注销回调类型：``fn(subscriber_id, reason)``。
RemoveHook = Callable[[str, str], None]

#: 发送异常原因标签。
REASON_SEND_ERROR: Final[str] = "send_error"
REASON_SHUTDOWN: Final[str] = "shutdown"
REASON_REMOVED: Final[str] = "removed"


@dataclass(frozen=True)
class SubscriberInfo:
    """订阅者标识（注册时提供）。"""

    subscriber_id: str
    stream: FrameStream
    hmac_key: bytes


@dataclass(frozen=True)
class BroadcasterStats:
    """广播器聚合统计快照。"""

    subscribers: int
    published: int
    total_dropped: int
    per_subscriber: dict[str, QueueStats] = field(default_factory=dict)


class ScreenBroadcaster:
    """asyncio 画面广播器：编码一次、每订阅者独立队列与发送 task。

    Args:
        queue_size: 每订阅者队列深度（满则丢最旧帧）。

    用法::

        bc = ScreenBroadcaster()
        bc.add(SubscriberInfo("c1", stream, key))
        bc.publish(screen_frame)     # 非阻塞，广播协程内调用
        await bc.remove("c1")        # 注销（幂等）
        await bc.shutdown()          # 停全部（幂等）
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE, *, on_remove: RemoveHook | None = None) -> None:
        self._queue_size = queue_size
        self._on_remove = on_remove
        self._subs: dict[str, ScreenBroadcaster._Entry] = {}
        self._published = 0
        self._stopped = False

    # -- 内部条目 -------------------------------------------------------------

    @dataclass
    class _Entry:
        info: SubscriberInfo
        queue: DropOldestQueue[bytes]
        task: asyncio.Task[None] | None = None

    # -- 订阅管理 -------------------------------------------------------------

    def add(self, info: SubscriberInfo) -> None:
        """注册订阅者并启动其发送 task。

        Raises:
            RuntimeError: 广播器已 shutdown，或 ``subscriber_id`` 重复。
        """
        if self._stopped:
            raise RuntimeError("ScreenBroadcaster 已停止，拒绝新订阅者")
        if info.subscriber_id in self._subs:
            raise RuntimeError(f"订阅者重复注册: {info.subscriber_id}")
        if not info.hmac_key:
            raise ValueError(f"订阅者 {info.subscriber_id} 的 HMAC 密钥为空，拒绝注册")
        entry = self._Entry(info=info, queue=DropOldestQueue(self._queue_size))
        self._subs[info.subscriber_id] = entry
        entry.task = asyncio.get_running_loop().create_task(
            self._send_loop(info.subscriber_id), name=f"screen-send-{info.subscriber_id}"
        )
        _LOG.info("broadcaster_subscriber_added", subscriber=info.subscriber_id, count=len(self._subs))

    async def remove(self, subscriber_id: str, reason: str = REASON_REMOVED) -> None:
        """注销订阅者（幂等）：取消其发送 task。"""
        entry = self._subs.pop(subscriber_id, None)
        if entry is None:
            return
        task = entry.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        entry.info.stream.close()
        await entry.info.stream.wait_closed()
        _LOG.info("broadcaster_subscriber_removed", subscriber=subscriber_id, reason=reason, count=len(self._subs))
        if self._on_remove is not None:
            try:
                self._on_remove(subscriber_id, reason)
            except Exception as exc:  # 回调异常隔离：不得炸掉注销路径（记录，约束 6）
                _LOG.error("broadcaster_on_remove_callback_failed", error=repr(exc))

    # -- 广播 -----------------------------------------------------------------

    def publish(self, frame: ScreenFrame) -> int:
        """向全部订阅者广播一帧（非阻塞）。

        每订阅者用其会话密钥单独签名编码后入队；队列满丢该订阅者最旧帧。

        Returns:
            成功入队的订阅者数。
        """
        self._published += 1
        delivered = 0
        for sid, entry in list(self._subs.items()):
            data = encode_frame(frame, hmac_key=entry.info.hmac_key)
            if not entry.queue.push(data):
                _LOG.debug("broadcaster_frame_dropped", subscriber=sid, dropped=entry.queue.dropped)
            delivered += 1
        return delivered

    # -- 观测 -----------------------------------------------------------------

    def stats(self) -> BroadcasterStats:
        """聚合统计快照。"""
        return BroadcasterStats(
            subscribers=len(self._subs),
            published=self._published,
            total_dropped=sum(e.queue.dropped for e in self._subs.values()),
            per_subscriber={sid: e.queue.stats() for sid, e in self._subs.items()},
        )

    def subscriber_ids(self) -> list[str]:
        """当前订阅者 id 列表（诊断/测试）。"""
        return list(self._subs)

    async def shutdown(self) -> None:
        """注销全部订阅者（幂等；之后 :meth:`add` 拒绝）。"""
        self._stopped = True
        for sid in list(self._subs):
            await self.remove(sid, REASON_SHUTDOWN)

    # -- 每订阅者发送 task ------------------------------------------------------

    async def _send_loop(self, subscriber_id: str) -> None:
        """独立发送循环：从专属队列取帧写流；写失败注销订阅者。"""
        entry = self._subs.get(subscriber_id)
        if entry is None:  # 注册与 task 启动间隙被移除
            return
        queue = entry.queue
        stream = entry.info.stream
        try:
            while True:
                data = await queue.pop()
                try:
                    await stream.send_raw(data)
                except OSError as exc:  # 含 ConnectionResetError：慢/死客户端只影响自己
                    _LOG.warning("broadcaster_send_failed", subscriber=subscriber_id, error=repr(exc))
                    # 不能在当前 task 内 await remove（会 cancel/join 自身）→ 稍后调度
                    asyncio.get_running_loop().create_task(self.remove(subscriber_id, REASON_SEND_ERROR))
                    return
        except asyncio.CancelledError:  # remove()/shutdown() 取消：正常退出
            raise

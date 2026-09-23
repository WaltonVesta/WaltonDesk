"""transport.broadcaster 测试：B3 修复行为锁定。

覆盖（用户叮嘱 a）：
1. 1 个慢客户端不阻塞其他客户端；
2. 队列满时丢**旧**帧（不是新帧）；
3. 计数正确（dropped/published/per-subscriber）；
4. 客户端断开时发送 task 正确清理（无泄漏）。

方向约定：broadcaster 通过 ``SubscriberInfo.stream``（订阅者侧）写帧 →
测试从对端 ``peer``（启用同一会话密钥的消费者）读出。``_Sub`` 内置后台
泵任务把 ``own_writer`` 收集的字节持续搬运给 ``peer`` 读端（模拟网络）。
"""

from __future__ import annotations

import asyncio

import pytest

from rdc.protocol.codec import decode_frame
from rdc.protocol.messages import Codec, ScreenFrame
from rdc.transport.broadcaster import (
    REASON_SEND_ERROR,
    REASON_SHUTDOWN,
    BroadcasterStats,
    ScreenBroadcaster,
    SubscriberInfo,
)
from rdc.transport.hmac_nonce import new_nonce
from tests._transport_support import make_stream_pair


def _frame(seq: int = 1, data: bytes = b"jpegbytes") -> ScreenFrame:
    return ScreenFrame(
        seq=seq, timestamp=0.0, width=8, height=8, codec=Codec.JPEG, data=data, keyframe=True
    )


class _Sub:
    """一个订阅者：broadcaster 写 stream，测试从 peer（同密钥消费者）读。"""

    def __init__(self, sid: str, *, pump: bool = True) -> None:
        self.stream, self.peer, self.own_writer, _ = make_stream_pair()
        self.key = new_nonce()
        self.peer.enable_hmac(self.key)  # 消费者持同一会话密钥
        self.sid = sid
        self.pump_task: asyncio.Task[None] | None = None
        if pump:
            self.pump_task = asyncio.create_task(self._pump(), name=f"pump-{sid}")

    async def _pump(self) -> None:
        """后台搬运 own_writer 收集字节 → peer 读端（模拟网络到达）。"""
        try:
            while True:
                await asyncio.sleep(0)
                data = bytes(self.own_writer.sent)
                self.own_writer.sent.clear()
                if data:
                    self.peer._reader.feed_data(data)  # noqa: SLF001
        except asyncio.CancelledError:
            raise

    def info(self) -> SubscriberInfo:
        return SubscriberInfo(self.sid, self.stream, self.key)

    def stop_pump(self) -> None:
        if self.pump_task is not None and not self.pump_task.done():
            self.pump_task.cancel()
        self.pump_task = None


async def _collect(sub: _Sub, n: int) -> list[int]:
    """从消费者读 n 帧 ScreenFrame，返回 seq 列表（2s/帧超时兜底）。"""
    seqs: list[int] = []
    for _ in range(n):
        msg = await asyncio.wait_for(sub.peer.recv_message(), timeout=2.0)
        assert isinstance(msg, ScreenFrame)
        seqs.append(msg.seq)
    return seqs


class TestFanout:
    async def test_frame_reaches_all_subscribers(self) -> None:
        bc = ScreenBroadcaster()
        subs = [_Sub(f"c{i}") for i in range(3)]
        for s in subs:
            bc.add(s.info())
        assert bc.publish(_frame(7)) == 3
        for s in subs:
            assert await _collect(s, 1) == [7]
        for s in subs:
            s.stop_pump()
        await bc.shutdown()

    async def test_per_subscriber_key_isolation(self) -> None:
        """每订阅者用自己密钥签名：跨密钥验不过（连接级完整性）。"""
        bc = ScreenBroadcaster()
        a, b = _Sub("a"), _Sub("b")
        bc.add(a.info())
        bc.add(b.info())
        frame = _frame(1)
        bc.publish(frame)
        await asyncio.sleep(0)  # 让两个发送 task 各写一帧
        a_bytes = bytes(a.own_writer.sent)
        b_bytes = bytes(b.own_writer.sent)
        assert a_bytes and b_bytes and a_bytes != b_bytes  # 各自独立签名
        assert decode_frame(a_bytes, hmac_key=a.key, require_hmac=True) == frame
        assert decode_frame(b_bytes, hmac_key=b.key, require_hmac=True) == frame
        assert decode_frame(a_bytes, hmac_key=b.key, require_hmac=True) is None
        for s in (a, b):
            s.stop_pump()
        await bc.shutdown()

    async def test_slow_client_does_not_block_others(self) -> None:
        """叮嘱 a-1：慢客户端（drain 永久阻塞）不拖住广播与快客户端。"""
        hold = asyncio.Event()
        bc = ScreenBroadcaster()
        slow = _Sub("slow", pump=False)
        fast = _Sub("fast")
        slow.own_writer.hold_drain = hold  # 慢客户端：首次写即卡住
        bc.add(slow.info())
        bc.add(fast.info())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(1, 21):
            assert bc.publish(_frame(i)) == 2  # publish 全程非阻塞
        assert loop.time() - t0 < 1.0  # v4 在此会串行 sendall 冻结全员
        # 快客户端收到最新 10 帧（1..10 被自己队列丢旧，绝不饿死）
        got = await _collect(fast, 10)
        assert got == list(range(11, 21))
        assert bc.stats().per_subscriber["slow"].dropped == 10
        assert bc.stats().per_subscriber["fast"].dropped == 10
        hold.set()
        fast.stop_pump()
        await bc.shutdown()

    async def test_publish_zero_subscribers(self) -> None:
        bc = ScreenBroadcaster()
        assert bc.publish(_frame(1)) == 0
        await bc.shutdown()


class TestDropOldest:
    async def test_full_queue_drops_oldest_keeps_latest(self) -> None:
        """叮嘱 a-2/3：队列 4 深、10 帧同步 publish → 丢最旧 1..6 共 6 帧，
        恢复后消费者收到留存的最新 4 帧 [7,8,9,10]。"""
        hold = asyncio.Event()
        bc = ScreenBroadcaster(queue_size=4)
        slow = _Sub("slow", pump=False)
        slow.own_writer.hold_drain = hold
        bc.add(slow.info())
        for i in range(1, 11):
            bc.publish(_frame(i))
        st = bc.stats()
        assert st.total_dropped == 6  # 1..6 被丢（publish 期间无人消费）
        assert st.per_subscriber["slow"].dropped == 6
        assert st.per_subscriber["slow"].enqueued == 10
        await asyncio.sleep(0)  # 发送 task 取走 7（write 入缓冲）、卡在 drain
        hold.set()
        await asyncio.sleep(0.05)  # 放行 drain：发送 task 写完队列留存帧
        data = bytes(slow.own_writer.sent)
        slow.own_writer.sent.clear()
        assert data  # [7,8,9,10] 都已写出
        slow.peer._reader.feed_data(data)  # noqa: SLF001
        received: list[int] = []
        for _ in range(4):
            msg = await asyncio.wait_for(slow.peer.recv_message(), timeout=2.0)
            assert isinstance(msg, ScreenFrame)
            received.append(msg.seq)
        assert received == [7, 8, 9, 10]  # 丢旧保新
        await bc.shutdown()

    async def test_dropped_counter_zero_when_draining(self) -> None:
        bc = ScreenBroadcaster(queue_size=5)
        s = _Sub("x")
        bc.add(s.info())
        for i in range(5):
            bc.publish(_frame(i + 1))
            assert await _collect(s, 1) == [i + 1]
        assert bc.stats().total_dropped == 0
        s.stop_pump()
        await bc.shutdown()


class TestRegistration:
    async def test_duplicate_id_rejected(self) -> None:
        bc = ScreenBroadcaster()
        s1, s2 = _Sub("dup"), _Sub("dup")
        s1.stop_pump()
        s2.stop_pump()
        bc.add(s1.info())
        with pytest.raises(RuntimeError, match="重复"):
            bc.add(s2.info())
        await bc.shutdown()

    async def test_empty_key_rejected(self) -> None:
        bc = ScreenBroadcaster()
        stream, _, _, _ = make_stream_pair()
        with pytest.raises(ValueError, match="密钥为空"):
            bc.add(SubscriberInfo("bad", stream, b""))

    async def test_add_after_shutdown_rejected(self) -> None:
        bc = ScreenBroadcaster()
        await bc.shutdown()
        s = _Sub("late")
        s.stop_pump()
        with pytest.raises(RuntimeError, match="已停止"):
            bc.add(s.info())


class TestCleanup:
    async def test_send_error_removes_subscriber(self) -> None:
        """叮嘱 a-4：死客户端 → 发送 task 捕获 OSError → 自动注销 + 回调通知。"""
        removed: list[tuple[str, str]] = []
        bc = ScreenBroadcaster(on_remove=lambda sid, why: removed.append((sid, why)))
        dead = _Sub("dead", pump=False)
        alive = _Sub("alive")
        bc.add(dead.info())
        bc.add(alive.info())
        dead.own_writer.fail_on_write = True
        bc.publish(_frame(1))
        for _ in range(50):
            await asyncio.sleep(0)
            if "dead" not in bc.subscriber_ids():
                break
        assert "dead" not in bc.subscriber_ids()
        assert "alive" in bc.subscriber_ids()
        assert removed == [("dead", REASON_SEND_ERROR)]
        assert await _collect(alive, 1) == [1]  # 其他客户端不受影响
        alive.stop_pump()
        await bc.shutdown()

    async def test_no_task_leak_after_shutdown(self) -> None:
        """注销后事件循环无残留发送 task（无泄漏）。"""
        bc = ScreenBroadcaster()
        subs = [_Sub(f"c{i}") for i in range(5)]
        for s in subs:
            bc.add(s.info())
        bc.publish(_frame(1))
        await bc.shutdown()
        assert bc.subscriber_ids() == []
        leftover = [
            t
            for t in asyncio.all_tasks()
            if (t.get_name().startswith("screen-send-")) and not t.done()
        ]
        assert leftover == []
        for s in subs:
            s.stop_pump()
        await bc.shutdown()  # 二次幂等

    async def test_dead_stream_closed_after_send_error(self) -> None:
        bc = ScreenBroadcaster()
        dead = _Sub("dead", pump=False)
        bc.add(dead.info())
        dead.own_writer.fail_on_write = True
        bc.publish(_frame(1))
        for _ in range(50):
            await asyncio.sleep(0)
            if not bc.subscriber_ids():
                break
        assert dead.own_writer.closed  # 订阅者侧流被回收
        assert dead.stream._reader.at_eof()  # noqa: SLF001  # close 投 EOF 唤醒消费方
        await bc.shutdown()

    async def test_shutdown_reason_propagated(self) -> None:
        seen: list[str] = []
        bc = ScreenBroadcaster(on_remove=lambda sid, why: seen.append(why))
        s = _Sub("a")
        s.stop_pump()
        bc.add(s.info())
        await bc.shutdown()
        assert seen == [REASON_SHUTDOWN]

    async def test_remove_unknown_id_noop(self) -> None:
        bc = ScreenBroadcaster()
        await bc.remove("ghost")  # 幂等不炸

    async def test_on_remove_callback_exception_contained(self) -> None:
        def boom(sid: str, why: str) -> None:
            raise RuntimeError("hook crash")

        bc = ScreenBroadcaster(on_remove=boom)
        s = _Sub("s")
        s.stop_pump()
        bc.add(s.info())
        await bc.remove("s")  # 回调异常被记录，remove 本身完成
        assert bc.subscriber_ids() == []


class TestStats:
    async def test_stats_snapshot(self) -> None:
        bc = ScreenBroadcaster(queue_size=2)
        s = _Sub("s", pump=False)
        bc.add(s.info())
        bc.publish(_frame(1))
        bc.publish(_frame(2))
        st = bc.stats()
        assert isinstance(st, BroadcasterStats)
        assert st.subscribers == 1
        assert st.published == 2
        assert st.per_subscriber["s"].enqueued == 2
        await bc.shutdown()

"""server/broadcaster SubscriptionRegistry 单元测试（阶段 9 批 3）。

内存帧流（FakeWriter 可注入写失败/挂起 drain）构造真实 ScreenSession，
覆盖：重复加入拒绝、主动注销幂等、发送失败自动回收、关键帧 hook、
慢客户端不阻塞 publish（B3 语义）、shutdown 收尾零残留。
broadcaster 所有权在 registry（D23 兑现）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from rdc.protocol.messages import Codec, ScreenFrame
from rdc.server.broadcaster import SubscriptionRegistry
from rdc.transport.server import ScreenSession
from tests._transport_support import make_stream_pair

_KEY = b"k" * 32


def make_session(*, fail_on_write: bool = False, hold: asyncio.Event | None = None) -> ScreenSession:
    """一个内存"画面会话"：真实 FrameStream + FakeWriter 收端。"""
    _, session_stream, _, session_writer = make_stream_pair()
    session_writer.fail_on_write = fail_on_write
    session_writer.hold_drain = hold
    session_stream.enable_hmac(_KEY)
    return ScreenSession(session_stream, "1.2.3.4", _KEY)


def frame(seq: int) -> ScreenFrame:
    return ScreenFrame(
        seq=seq, timestamp=1.0, width=8, height=8, codec=Codec.H264, data=b"\x00" * 16
    )


class TestRegister:
    async def test_register_assigns_id_and_hooks_keyframe(self) -> None:
        keyframes: list[int] = []
        reg = SubscriptionRegistry(on_new_subscriber=lambda: keyframes.append(1))
        sid = reg.register(make_session())
        assert sid == "sub-1"
        assert reg.count() == 1
        assert keyframes == [1]  # 新订阅立即请求关键帧
        await reg.shutdown()

    async def test_same_session_rejected(self) -> None:
        reg = SubscriptionRegistry()
        session = make_session()
        reg.register(session)
        with pytest.raises(ValueError, match="重复登记"):
            reg.register(session)
        assert reg.count() == 1
        await reg.shutdown()

    async def test_same_ip_different_sessions_coexist(self) -> None:
        reg = SubscriptionRegistry()
        a, b = reg.register(make_session()), reg.register(make_session())
        assert a != b
        assert reg.count() == 2
        await reg.shutdown()

    async def test_disconnected_session_rejected(self) -> None:
        session = make_session()
        await session.close()
        reg = SubscriptionRegistry()
        with pytest.raises(RuntimeError, match="未绑定/已断开"):
            reg.register(session)
        assert reg.count() == 0
        await reg.shutdown()

    async def test_ids_monotonic(self) -> None:
        reg = SubscriptionRegistry()
        ids = [reg.register(make_session()) for _ in range(3)]
        assert ids == ["sub-1", "sub-2", "sub-3"]
        await reg.shutdown()


class TestFrameDelivery:
    async def test_published_frame_reaches_writer(self) -> None:
        session = make_session()
        reg = SubscriptionRegistry()
        reg.register(session)
        assert reg.broadcaster.publish(frame(1)) == 1
        await asyncio.sleep(0.05)
        writer = session.stream._writer  # noqa: SLF001 - 测试观测 FakeWriter
        assert len(writer.sent) > 0
        await reg.shutdown()

    async def test_frame_verifies_with_session_key(self) -> None:
        from rdc.protocol.codec import decode_frame

        session = make_session()
        reg = SubscriptionRegistry()
        reg.register(session)
        reg.broadcaster.publish(frame(7))
        await asyncio.sleep(0.05)
        writer = session.stream._writer  # noqa: SLF001
        msg = decode_frame(bytes(writer.sent), hmac_key=_KEY, require_hmac=True)
        assert isinstance(msg, ScreenFrame) and msg.seq == 7
        await reg.shutdown()

    async def test_unregister_stops_delivery(self) -> None:
        session = make_session()
        reg = SubscriptionRegistry()
        sid = reg.register(session)
        await reg.unregister(sid)
        assert reg.broadcaster.publish(frame(1)) == 0
        await reg.await_close_tasks()
        await reg.shutdown()


class TestUnregister:
    async def test_unregister_idempotent(self) -> None:
        reg = SubscriptionRegistry()
        sid = reg.register(make_session())
        await reg.unregister(sid)
        await reg.unregister(sid)  # 未知 id 静默
        await reg.unregister("nope")
        await reg.shutdown()

    async def test_removed_listener_notified_once(self) -> None:
        events: list[tuple[str, str]] = []
        reg = SubscriptionRegistry(on_removed=lambda sid, r: events.append((sid, r)))
        sid = reg.register(make_session())
        await reg.unregister(sid, "bye")
        await reg.await_close_tasks()
        assert events == [(sid, "bye")]
        await reg.shutdown()
        assert len(events) == 1  # shutdown 二次注销不重复通知

    async def test_listener_exception_isolated(self) -> None:
        def boom(_sid: str, _reason: str) -> None:
            raise RuntimeError("listener 异常不得外逸")

        reg = SubscriptionRegistry(on_removed=boom)
        sid = reg.register(make_session())
        await reg.unregister(sid)
        assert reg.count() == 0
        await reg.await_close_tasks()
        await reg.shutdown()


class TestSendFailureRecovery:
    async def test_send_error_auto_removes_subscriber(self) -> None:
        reg = SubscriptionRegistry()
        sid = reg.register(make_session(fail_on_write=True))
        reg.broadcaster.publish(frame(1))  # 发送 task 写失败 → 调度 remove
        for _ in range(100):
            if reg.count() == 0:
                break
            await asyncio.sleep(0.01)
        assert reg.count() == 0
        await reg.await_close_tasks()
        assert sid == "sub-1"
        await reg.shutdown()

    async def test_slow_client_does_not_block_publish(self) -> None:
        hold = asyncio.Event()
        reg = SubscriptionRegistry()
        reg.register(make_session(hold=hold))  # 慢端 drain 永久挂起
        reg.register(make_session())  # 快端
        t0 = time.perf_counter()
        for i in range(20):
            reg.broadcaster.publish(frame(i))
        assert time.perf_counter() - t0 < 1.0  # v4 此处全员冻结
        hold.set()
        await reg.shutdown()


class TestShutdown:
    async def test_shutdown_clears_all_and_rejects_new(self) -> None:
        reg = SubscriptionRegistry()
        reg.register(make_session())
        reg.register(make_session())
        await reg.shutdown()
        assert reg.count() == 0
        assert reg.subscriber_ids() == []
        with pytest.raises(RuntimeError):
            reg.register(make_session())
        await reg.shutdown()  # 幂等

    async def test_publish_after_shutdown_zero_delivered(self) -> None:
        reg = SubscriptionRegistry()
        await reg.shutdown()
        assert reg.broadcaster.publish(frame(1)) == 0

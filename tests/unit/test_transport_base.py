"""transport.base 测试：FrameStream 粘包/半包、HMAC 强制、StreamChannel 生命周期。"""

from __future__ import annotations

import asyncio

import pytest

from rdc.exceptions import ProtocolError
from rdc.protocol.codec import encode_frame
from rdc.protocol.messages import Heartbeat, HeartbeatKind, MouseEvent
from rdc.transport.base import FrameStream, StreamChannel
from rdc.transport.hmac_nonce import new_nonce

from .._transport_support import FakeWriter, make_stream_pair, pump

_PING = Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0)


def _mouse(x: int = 1, y: int = 2) -> MouseEvent:
    from rdc.protocol.messages import MouseAction

    return MouseEvent(x=x, y=y, action=MouseAction.MOVE)


# ---------------------------------------------------------------------------
# FrameStream
# ---------------------------------------------------------------------------


def _pair() -> tuple[FrameStream, FrameStream, FakeWriter]:
    """旧 3 元组适配：返回 (client, server, client_writer)。"""
    client, server, cw, _ = make_stream_pair()
    return client, server, cw


class TestFrameStreamRecv:
    async def test_single_message_round_trip(self) -> None:
        client, server, cw = _pair()
        await client.send_message(_PING)
        pump(cw, server)
        msg = await server.recv_message()
        assert msg == _PING

    async def test_tcp_streaming_no_sticky_loss(self) -> None:
        client, server, cw = _pair()
        await client.send_message(_PING)
        await client.send_message(_mouse(3, 4))
        await client.send_message(_PING)
        pump(cw, server)  # 三帧一次到达（粘包）
        assert await server.recv_message() == _PING
        assert await server.recv_message() == _mouse(3, 4)
        assert await server.recv_message() == _PING

    async def test_half_frame_waits_then_completes(self) -> None:
        client, server, cw = _pair()
        await client.send_message(_PING)
        data = bytes(cw.sent)
        cw.sent.clear()
        # 只送前一半 → recv_message 必须继续等而不是报错
        server._reader.feed_data(data[: len(data) // 2])  # noqa: SLF001
        task = asyncio.create_task(server.recv_message())
        await asyncio.sleep(0)  # 让 task 开跑
        assert not task.done()
        server._reader.feed_data(data[len(data) // 2 :])  # noqa: SLF001
        assert await task == _PING

    async def test_garbage_before_magic_resyncs(self) -> None:
        client, server, cw = _pair()
        server._reader.feed_data(b"JUNK" * 5)  # noqa: SLF001
        await client.send_message(_PING)
        pump(cw, server)
        assert await server.recv_message() == _PING

    async def test_corrupt_crc_frame_skipped_next_ok(self) -> None:
        client, server, cw = _pair()
        await client.send_message(_PING)
        good = bytes(cw.sent)
        cw.sent.clear()
        await client.send_message(_mouse(7, 8))
        second = bytes(cw.sent)
        cw.sent.clear()
        bad = bytearray(good)
        bad[16] ^= 0xFF  # 砸载荷首字节（15B 头之后）→ CRC 不过但不改长度
        server._reader.feed_data(bytes(bad))  # noqa: SLF001
        pump_ok = asyncio.create_task(server.recv_message())
        server._reader.feed_data(second)  # noqa: SLF001
        assert await pump_ok == _mouse(7, 8)  # 坏帧被跳过，好帧到达

    async def test_peer_eof_returns_none(self) -> None:
        client, server, _ = _pair()
        server._reader.feed_eof()  # noqa: SLF001
        assert await server.recv_message() is None


class TestFrameStreamHmac:
    async def test_enable_hmac_requires_nonempty(self) -> None:
        client, _, _ = _pair()
        with pytest.raises(ProtocolError):
            client.enable_hmac(b"")

    async def test_hmac_sign_and_verify_round_trip(self) -> None:
        key = new_nonce()
        client, server, cw = _pair()
        client.enable_hmac(key)
        server.enable_hmac(key)
        await client.send_message(_PING)
        pump(cw, server)
        assert await server.recv_message() == _PING

    async def test_unsigned_frame_rejected_after_enable(self) -> None:
        """剥标志降级攻击：HMAC 开启后收到未签名帧 → 拒绝并继续（不落 None）。"""
        key = new_nonce()
        client, server, cw = _pair()
        server.enable_hmac(key)  # server 只收，不开（模拟裸发）
        await client.send_message(_PING)  # client 无密钥 → 裸帧
        pump(cw, server)
        task = asyncio.create_task(server.recv_message())
        await asyncio.sleep(0)
        assert not task.done()  # 裸帧被丢，等待下一帧而非返回它
        server._reader.feed_eof()  # noqa: SLF001
        assert await task is None  # 收尾：EOF → None，不留悬空任务

    async def test_wrong_key_frame_dropped(self) -> None:
        client, server, cw = _pair()
        client.enable_hmac(new_nonce())
        server.enable_hmac(new_nonce())
        await client.send_message(_PING)
        pump(cw, server)
        task = asyncio.create_task(server.recv_message())
        await asyncio.sleep(0)
        assert not task.done()
        # 再喂一帧用 server 密钥可解的：验证流仍健康
        server_hmac = server._hmac_key  # noqa: SLF001
        assert server_hmac is not None
        server2 = encode_frame(_mouse(9, 9), hmac_key=server_hmac)
        server._reader.feed_data(server2)  # noqa: SLF001
        assert await task == _mouse(9, 9)
        server._reader.feed_eof()  # noqa: SLF001

    async def test_tampered_signed_frame_dropped(self) -> None:
        key = new_nonce()
        client, server, cw = _pair()
        client.enable_hmac(key)
        server.enable_hmac(key)
        await client.send_message(_PING)
        raw = bytearray(bytes(cw.sent))
        cw.sent.clear()
        raw[len(raw) - 5] ^= 0x01  # 砸 HMAC trailer 一 bit
        server._reader.feed_data(bytes(raw))  # noqa: SLF001
        task = asyncio.create_task(server.recv_message())
        await asyncio.sleep(0)
        assert not task.done()
        server._reader.feed_eof()  # noqa: SLF001
        assert await task is None  # 篡改帧被丢弃，EOF 收尾


class TestFrameStreamClose:
    async def test_close_idempotent(self) -> None:
        client, _, cw = _pair()
        client.close()
        client.close()
        assert cw.closed

    async def test_recv_returns_none_after_close(self) -> None:
        client, _, _ = _pair()
        client.close()
        assert await client.recv_message() is None

    async def test_write_failure_propagates_oserror(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(fail_on_write=True)
        stream = FrameStream(reader, writer, name="x")
        with pytest.raises(OSError):
            await stream.send_message(_PING)

    async def test_peer_certificate_der_none_for_plain(self) -> None:
        client, _, _ = _pair()
        assert client.peer_certificate_der is None

    async def test_peer_certificate_der_from_extra(self) -> None:
        from tests._transport_support import FakeSSLObject

        client, _, cw = _pair()
        der = b"0\x82fake-cert"
        cw.extra["ssl_object"] = FakeSSLObject(der)
        assert client.peer_certificate_der == der

    async def test_peer_host_from_extra(self) -> None:
        client, _, _ = _pair()
        assert client.peer_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# StreamChannel
# ---------------------------------------------------------------------------


class TestStreamChannel:
    async def test_unbound_send_raises(self) -> None:
        ch = StreamChannel()
        assert ch.recv_task is None
        with pytest.raises(ConnectionError):
            await ch.send(_PING)

    async def test_bind_stream_once(self) -> None:
        client, _, _ = _pair()
        ch = StreamChannel()
        ch.bind_stream(client)
        with pytest.raises(RuntimeError):
            ch.bind_stream(client)

    async def test_delivers_messages_until_eof(self) -> None:
        client, server, cw = _pair()
        ch = StreamChannel(server)
        ch.start_receiving()
        await client.send_message(_PING)
        pump(cw, server)
        assert await ch.next_message() == _PING
        server._reader.feed_eof()  # noqa: SLF001
        assert await ch.next_message() is None
        await ch.close()

    async def test_start_receiving_twice_no_dup(self) -> None:
        client, _, _ = _pair()
        ch = StreamChannel(client)
        ch.start_receiving()
        first = ch.recv_task
        ch.start_receiving()
        assert ch.recv_task is first
        await ch.close()

    async def test_close_idempotent_and_unblocks(self) -> None:
        _, server, _ = _pair()
        ch = StreamChannel(server)
        ch.start_receiving()
        await ch.close()
        await ch.close()
        assert not ch.is_connected

    async def test_send_after_peer_close_marks_disconnected(self) -> None:
        client, _, _ = _pair()
        ch = StreamChannel(client)
        client.close()  # 先把流关掉（writer.closed → drain 仍 ok? FakeWriter 不模拟关闭态）
        # 直接构造写失败场景
        client._writer.fail_on_write = True  # noqa: SLF001
        with pytest.raises(OSError):
            await ch.send(_PING)
        assert not ch.is_connected
        await ch.close()

    async def test_close_feeds_eof_when_no_reader_loop(self) -> None:
        _, server, _ = _pair()
        ch = StreamChannel(server)
        await ch.close()
        assert await asyncio.wait_for(ch.next_message(), timeout=1) is None

    async def test_channel_protocol_surface(self) -> None:
        """实现必须覆盖 Channel 协议全部成员（mypy 静态已保证，此测运行时锁定）。"""
        from rdc.transport.base import Channel

        client, _, _ = _pair()
        ch = StreamChannel(client)
        for name in ("connect", "send", "next_message", "recv_task", "is_connected", "close"):
            assert hasattr(ch, name)
        assert hasattr(Channel, name)  # 协议本身也定义同名成员

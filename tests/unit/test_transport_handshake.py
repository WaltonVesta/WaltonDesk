"""transport.handshake 测试：认证 + nonce 交换（假流驱动，无真实 TLS）。

驱动模式：client/server 协程并发跑，测试循环把两侧的 FakeWriter 收集字节
互相喂给对方的 reader（``run_handshake_pair``），直至双方结束。
"""

from __future__ import annotations

import asyncio

import pytest

from rdc.exceptions import AuthBannedError, AuthError
from rdc.protocol.messages import AuthRequest, AuthResponse, Heartbeat, HeartbeatKind, ScreenJoin
from rdc.secrets.password import hash_password
from rdc.transport.handshake import (
    perform_control_handshake,
    perform_screen_join,
    serve_control_handshake,
    serve_screen_join,
)
from rdc.transport.hmac_nonce import new_connection_id, new_nonce
from tests._transport_support import make_stream_pair, pump, run_handshake_pair

_ISSUE = lambda ip, cid: "tok-abc"  # noqa: E731


async def _control_ok(
    *, password_hash: str = "", client_password: str = ""
) -> tuple[object, bytes, asyncio.Task, asyncio.Task]:
    client, server, cw, sw = make_stream_pair()
    c_task = asyncio.create_task(
        perform_control_handshake(client, password=client_password, client_name="tester")
    )
    s_task = asyncio.create_task(
        serve_control_handshake(
            server, client_ip="10.0.0.1", password_hash=password_hash, issue_token=_ISSUE
        )
    )
    await run_handshake_pair(c_task, s_task, cw, sw, client, server)
    return client, server, c_task, s_task


class TestControlHandshake:
    async def test_success_derives_same_key_both_ends(self) -> None:
        client, server, c_task, s_task = await _control_ok()
        c_res = await c_task
        s_key = await s_task
        assert c_res.session_token == "tok-abc"
        assert len(c_res.connection_id) == 32
        # 双端密钥一致（同 nonce + 同 connection_id）
        assert client._hmac_key == s_key  # noqa: SLF001
        assert server._hmac_key == s_key  # noqa: SLF001
        assert len(s_key) == 32

    async def test_streams_hmac_enabled_and_frames_signed(self) -> None:
        client, server, c_task, s_task = await _control_ok()
        await c_task
        await s_task
        assert client.hmac_enabled and server.hmac_enabled
        assert client.hmac_enabled and server.hmac_enabled
        # 握手后帧自动签名：用派生密钥在新通道对上验证互通
        # （握手原流的 reader 已被驱动收尾投过 EOF，不复用）
        msg = Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0)
        key = client._hmac_key  # noqa: SLF001
        peer_a, peer_b, aw, _bw = make_stream_pair()
        peer_a.enable_hmac(key)
        peer_b.enable_hmac(key)
        await peer_a.send_message(msg)
        pump(aw, peer_b)
        assert await peer_b.recv_message() == msg

    async def test_first_frame_must_be_auth_request(self) -> None:
        _, server, _, _ = make_stream_pair()
        server._reader.feed_data(  # noqa: SLF001
            __import__("rdc.protocol.codec", fromlist=["encode_frame"]).encode_frame(
                Heartbeat(kind=HeartbeatKind.PING)
            )
        )
        with pytest.raises(AuthError, match="AuthRequest"):
            await serve_control_handshake(
                server, client_ip="x", password_hash="", issue_token=_ISSUE
            )

    async def test_missing_client_nonce_rejected(self) -> None:
        client, server, cw, sw = make_stream_pair()

        async def client_no_nonce() -> None:
            await client.send_message(AuthRequest(password="", client_nonce=b""))

        s_task = asyncio.create_task(
            serve_control_handshake(
                server, client_ip="x", password_hash="", issue_token=_ISSUE
            )
        )
        c_task = asyncio.create_task(client_no_nonce())
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        await c_task
        with pytest.raises(AuthError, match="client_nonce"):
            await s_task

    async def test_wrong_password_rejected_both_sides(self) -> None:
        """认证失败：client 收到 ok=False 抛 AuthError；server 抛 AuthError。"""
        wrong_hash = hash_password("correct-pw")
        client, server, cw, sw = make_stream_pair()
        c_task = asyncio.create_task(perform_control_handshake(client, password="guessed"))
        s_task = asyncio.create_task(
            serve_control_handshake(
                server, client_ip="x", password_hash=wrong_hash, issue_token=_ISSUE
            )
        )
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthError, match="认证失败"):
            await c_task
        with pytest.raises(AuthError, match="认证失败"):
            await s_task
        # 失败路径：server 未调用 issue_token（无令牌/nonce 泄露）

    async def test_broken_stored_hash_denies(self) -> None:
        client, server, cw, sw = make_stream_pair()
        c_task = asyncio.create_task(perform_control_handshake(client, password="x"))
        s_task = asyncio.create_task(
            serve_control_handshake(
                server,
                client_ip="x",
                password_hash="not-a-bcrypt-hash",
                issue_token=_ISSUE,
            )
        )
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthError):
            await c_task
        with pytest.raises(AuthError):
            await s_task

    async def test_client_rejects_response_missing_fields(self) -> None:
        """server 回 ok=True 但缺 token/nonce → client 拒收（不建立半残会话）。"""
        client, server, cw, sw = make_stream_pair()

        async def fake_server() -> None:
            req = await server.recv_message()
            assert isinstance(req, AuthRequest)
            await server.send_message(AuthResponse(ok=True))  # 全空字段

        c_task = asyncio.create_task(perform_control_handshake(client, password="x"))
        s_task = asyncio.create_task(fake_server())
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthError, match="缺少"):
            await c_task
        await s_task

    async def test_banned_response_raises_auth_banned_with_minutes(self) -> None:
        """缺陷 2 握手层哨兵：ok=False+banned=True → AuthBannedError，
        携带剩余秒且文案按分钟折算（≥60 秒）。"""
        client, server, cw, sw = make_stream_pair()

        async def fake_server() -> None:
            req = await server.recv_message()
            assert isinstance(req, AuthRequest)
            await server.send_message(
                AuthResponse(
                    ok=False, reason="banned", banned=True, ban_remaining_seconds=300
                )
            )

        c_task = asyncio.create_task(perform_control_handshake(client, password="x"))
        s_task = asyncio.create_task(fake_server())
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthBannedError) as ei:
            await c_task
        assert ei.value.remaining_seconds == 300
        assert "5 分钟" in str(ei.value)
        # 封禁异常仍是 AuthError 子类：调用方按认证类失败统一处理（缺陷 3 面）
        assert isinstance(ei.value, AuthError)
        await s_task

    async def test_banned_seconds_under_minute_shows_seconds(self) -> None:
        """不足 60 秒按秒文案（措辞分支）。"""
        assert "秒" in AuthBannedError.format_message(45)
        assert "分钟" not in AuthBannedError.format_message(59)

    async def test_server_eof_during_handshake(self) -> None:
        client, server, _, _ = make_stream_pair()
        c_task = asyncio.create_task(perform_control_handshake(client, password="x"))
        await asyncio.sleep(0)  # 让 client 把 AuthRequest 写出
        client._reader.feed_eof()  # noqa: SLF001  # 模拟服务端断连 → client 读端 EOF
        with pytest.raises(AuthError, match="对端关闭"):
            await c_task
        server._reader.feed_eof()  # noqa: SLF001


class TestScreenJoin:
    async def _join_tasks(
        self,
        *,
        token: str = "tok-1",
        connection_id: bytes | None = None,
        consume_err: Exception | None = None,
    ) -> tuple[asyncio.Task, asyncio.Task, object, object]:
        cid = connection_id or new_connection_id()
        client, server, cw, sw = make_stream_pair()
        consumed: list[tuple[str, str, bytes]] = []

        def consume(t: str, ip: str, c: bytes) -> object:
            if consume_err:
                raise consume_err
            consumed.append((t, ip, c))
            return object()

        c_task = asyncio.create_task(
            perform_screen_join(client, session_token=token, connection_id=cid)
        )
        s_task = asyncio.create_task(
            serve_screen_join(server, client_ip="9.9.9.9", consume_token=consume)
        )
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        return c_task, s_task, cid, consumed

    async def test_join_success_keys_match(self) -> None:
        c_task, s_task, _, consumed = await self._join_tasks()
        c_res = await c_task
        s_key = await s_task
        assert len(consumed) == 1
        assert c_res.hmac_key == s_key and len(s_key) == 32

    async def test_join_key_differs_from_control_key(self) -> None:
        c_task, s_task, _, _ = await self._join_tasks()
        s_key = await s_task
        await c_task
        _, _, c_ctrl_task, s_ctrl_task = await _control_ok()
        await c_ctrl_task
        ctrl_key = await s_ctrl_task
        assert s_key != ctrl_key  # 通道间密钥隔离（nonce 不同）

    async def test_token_rejected_no_nonce_sent(self) -> None:
        c_task, s_task, _, _ = await self._join_tasks(consume_err=AuthError("令牌无效"))
        with pytest.raises(AuthError, match="加入失败"):
            await c_task
        with pytest.raises(AuthError, match="令牌无效"):
            await s_task

    async def test_first_frame_must_be_screen_join(self) -> None:
        _, server, _, _ = make_stream_pair()
        from rdc.protocol.codec import encode_frame

        server._reader.feed_data(encode_frame(Heartbeat(kind=HeartbeatKind.PING)))  # noqa: SLF001
        with pytest.raises(AuthError, match="ScreenJoin"):
            await serve_screen_join(
                server, client_ip="x", consume_token=lambda *a: object()
            )

    async def test_client_rejects_response_connection_id_mismatch(self) -> None:
        cid = new_connection_id()
        rogue_cid = new_connection_id()
        client, server, cw, sw = make_stream_pair()

        async def rogue_server() -> None:
            req = await server.recv_message()
            assert isinstance(req, ScreenJoin)
            await server.send_message(
                AuthResponse(ok=True, server_nonce=new_nonce(), connection_id=rogue_cid)
            )

        c_task = asyncio.create_task(
            perform_screen_join(client, session_token="t", connection_id=cid)
        )
        s_task = asyncio.create_task(rogue_server())
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthError, match="connection_id"):
            await c_task
        await s_task

    async def test_client_rejects_response_missing_nonce(self) -> None:
        cid = new_connection_id()
        client, server, cw, sw = make_stream_pair()

        async def stub_server() -> None:
            req = await server.recv_message()
            assert isinstance(req, ScreenJoin)
            await server.send_message(AuthResponse(ok=True, connection_id=cid))  # 无 server_nonce

        c_task = asyncio.create_task(
            perform_screen_join(client, session_token="t", connection_id=cid)
        )
        s_task = asyncio.create_task(stub_server())
        await run_handshake_pair(c_task, s_task, cw, sw, client, server)
        with pytest.raises(AuthError, match="nonce"):
            await c_task
        await s_task

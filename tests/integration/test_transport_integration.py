"""transport 端到端集成测试：真实 loopback TCP + 真实 TLS + 认证 + HMAC 帧流。

用同一事件循环内 server(TaskTransportServer) + client(ControlChannel/ScreenChannel)
跑真实 socket 与真实 TLS 握手，覆盖传输栈全链路（TLS 1.2+ 强制、自签名 TOFU 降级、
AuthRequest/AuthResponse 握手、nonce→HKDF→HMAC 强制、画面通道一次性令牌加入、
broadcaster 帧下发、令牌重放拒绝、错误口令拒绝）。

"双进程"字面要求以同进程双协程 + 真实 socket 等价替代：跨进程对传输栈正确性的
覆盖与本用例相同，却额外引入 venv 冷启动与 pb 生成物路径复杂度（额度考量）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rdc.exceptions import AuthError
from rdc.protocol.messages import (
    ClipboardEvent,
    Codec,
    Heartbeat,
    HeartbeatKind,
    MouseAction,
    MouseButton,
    MouseEvent,
    ScreenFrame,
)
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod
from rdc.transport.control_channel import ControlChannel
from rdc.transport.screen_channel import ScreenChannel
from rdc.transport.server import ControlSession, TransportServer
from tests._transport_support import gen_cert

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 TOFU 信任列表重定向到 tmp 并重置全局单例（隔离测试、不污染用户目录）。"""
    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_path / "known_fp.json")
    monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)


@pytest.fixture
def certs(tmp_path: Path) -> tuple[Path, Path]:
    return gen_cert(tmp_path)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _confirm(_fp: str, _host: str) -> bool:
    """测试用 TOFU 确认：一律信任自签名证书。"""
    return True


def _control(host: str, port: int, password: str = "") -> ControlChannel:
    return ControlChannel(
        host, port, password_provider=lambda: password, fingerprint_confirm=_confirm
    )


def _screen(host: str, port: int, token: str, cid: bytes) -> ScreenChannel:
    return ScreenChannel(
        host, port, session_token=token, connection_id=cid, fingerprint_confirm=_confirm
    )


async def _run_server(certs, **kw):  # noqa: ANN003
    """同事件循环内启动真实 TLS 服务端，返回 (server, 控制端口, 画面端口)。"""
    ctrl_port = _free_port()
    screen_port = _free_port()
    server = TransportServer(
        host="127.0.0.1",
        control_port=ctrl_port,
        screen_port=screen_port,
        cert_path=certs[0],
        key_path=certs[1],
        **kw,
    )
    await server.start()
    return server, ctrl_port, screen_port


class TestControlEndToEnd:
    async def test_handshake_bidirectional_frames(self, certs) -> None:  # noqa: ANN001
        got: list[object] = []
        ready = asyncio.Event()

        async def on_control(session: ControlSession) -> None:
            first = await session.next_message()
            got.append(first)
            ready.set()
            # 回一条剪贴板事件给客户端
            await session.send(ClipboardEvent(text="from-server"))
            # 等到客户端关闭 → next_message 返回 None → 会话结束
            await session.next_message()

        server, ctrl_port, _scr = await _run_server(certs, on_control_session=on_control)
        try:
            ch = _control("127.0.0.1", ctrl_port, "")
            await ch.connect()
            assert ch.is_connected
            assert ch.session_token  # 令牌已发放
            assert len(ch.connection_id or b"") == 32  # 连接标识 32 字节
            assert ch.stream.hmac_enabled  # 握手后 HMAC 强制
            await ch.send(Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0))
            await asyncio.wait_for(ready.wait(), timeout=5)
            assert isinstance(got[0], Heartbeat)
            back = await asyncio.wait_for(ch.next_message(), timeout=5)
            assert isinstance(back, ClipboardEvent) and back.text == "from-server"
            await ch.close()
        finally:
            await server.stop()

    async def test_wrong_password_rejected(self, certs) -> None:  # noqa: ANN001
        server, ctrl_port, _ = await _run_server(
            certs, password_hash=hash_password("right-pw"), on_control_session=None
        )
        try:
            ch = _control("127.0.0.1", ctrl_port, "wrong-pw")
            with pytest.raises(AuthError, match="认证失败"):
                await ch.connect()
            assert not ch.is_connected
        finally:
            await server.stop()

    async def test_input_events_flow(self, certs) -> None:  # noqa: ANN001
        received: list[object] = []
        done = asyncio.Event()

        async def on_control(session: ControlSession) -> None:
            for _ in range(2):
                msg = await session.next_message()
                if msg is None:
                    break
                received.append(msg)
            done.set()
            await session.next_message()

        server, ctrl_port, _ = await _run_server(certs, on_control_session=on_control)
        try:
            ch = _control("127.0.0.1", ctrl_port, "")
            await ch.connect()
            await ch.send(MouseEvent(x=5, y=6, action=MouseAction.CLICK, button=MouseButton.LEFT))
            await ch.send(ClipboardEvent(text="hi"))
            await asyncio.wait_for(done.wait(), timeout=5)
            assert isinstance(received[0], MouseEvent) and received[0].x == 5
            assert isinstance(received[1], ClipboardEvent)
            await ch.close()
        finally:
            await server.stop()


class TestScreenEndToEnd:
    async def test_join_and_frame_fanout(self, certs) -> None:  # noqa: ANN001
        from rdc.transport.broadcaster import ScreenBroadcaster, SubscriberInfo

        bc = ScreenBroadcaster()
        joined = asyncio.Event()

        async def on_control(session: ControlSession) -> None:
            await session.next_message()  # 直到客户端关闭

        async def on_screen(session) -> None:  # noqa: ANN001
            bc.add(SubscriberInfo("s1", session.stream, session.hmac_key))
            joined.set()
            await session.next_message()  # 直到关闭

        server, ctrl_port, scr_port = await _run_server(
            certs, on_control_session=on_control, on_screen_session=on_screen
        )
        try:
            ch = _control("127.0.0.1", ctrl_port, "")
            await ch.connect()
            sc = _screen("127.0.0.1", scr_port, ch.session_token or "", ch.connection_id or b"")
            await sc.connect()
            assert sc.is_connected
            assert sc.stream.hmac_enabled
            await asyncio.wait_for(joined.wait(), timeout=5)
            bc.publish(
                ScreenFrame(
                    seq=1, timestamp=0.0, width=8, height=8, codec=Codec.JPEG, data=b"x", keyframe=True
                )
            )
            frame = await asyncio.wait_for(sc.next_message(), timeout=5)
            assert isinstance(frame, ScreenFrame) and frame.seq == 1
            await sc.close()
            await ch.close()
            await bc.shutdown()
        finally:
            await server.stop()

    async def test_token_replay_second_join_fails(self, certs) -> None:  # noqa: ANN001
        """令牌单次使用：第二次用同一令牌加入画面通道必失败。"""

        async def on_control(session: ControlSession) -> None:
            await session.next_message()

        async def on_screen(session) -> None:  # noqa: ANN001
            await session.next_message()

        server, ctrl_port, scr_port = await _run_server(
            certs, on_control_session=on_control, on_screen_session=on_screen
        )
        try:
            ch = _control("127.0.0.1", ctrl_port, "")
            await ch.connect()
            token = ch.session_token or ""
            cid = ch.connection_id or b""
            sc1 = _screen("127.0.0.1", scr_port, token, cid)
            await sc1.connect()  # 首次成功
            await sc1.close()
            sc2 = _screen("127.0.0.1", scr_port, token, cid)
            with pytest.raises(AuthError):
                await sc2.connect()  # 重放：令牌已被消费
            await sc2.close()
            await ch.close()
        finally:
            await server.stop()


class TestStopOrdering:
    """走查第十三批 Bug 1：``stop()`` 收尾顺序——不得被在途 handler 拖住。

    病灶（旧序）：``close listener → wait_closed → 关会话``。Python 3.12+ 起
    ``Server.wait_closed()`` 会等**全部在途 handler 任务**返回，而 handler 阻塞
    在会话 ``next_message()``（EOF 尚未投递，因关会话排在 wait_closed 之后）→
    ``stop()`` 被拖到 handler 空闲超时（HostService 场景实测 ~9.9s），期间
    ``HostService.pause()`` 全程阻塞、``is_paused`` 恒 False、托盘文案不刷新。
    修复后"关会话投 EOF 唤醒 handler"排在 wait_closed 之前，stop 毫秒级。
    """

    async def test_stop_fast_with_inflight_blocking_handler(self, certs) -> None:  # noqa: ANN001
        started = asyncio.Event()

        async def on_control(session: ControlSession) -> None:
            # 模拟真实 _serve_control：交付后长期驻留等消息（无超时兜底形态）
            started.set()
            await session.next_message()

        server, ctrl_port, _scr = await _run_server(certs, on_control_session=on_control)
        ch = _control("127.0.0.1", ctrl_port, "")
        try:
            await ch.connect()
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.sleep(0.1)  # handler 已阻塞在 next_message
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(server.stop(), timeout=3.0)
            dt = asyncio.get_running_loop().time() - t0
            assert dt < 2.0, f"stop() 耗时 {dt:.2f}s，wait_closed 顺序疑似回归"
            assert not server.listening
        finally:
            await ch.close()
            if server.listening:
                await server.stop()


def _free_port6() -> int:
    import socket

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.bind(("::", 0))
        return int(s.getsockname()[1])


class TestDualstackListening:
    """走查第十三批兼容性：全接口绑定（0.0.0.0）此前仅监听 IPv4。

    真机现象：主控端输被控端 IPv6 地址（如 ``2409:8a55:...``）报"连接超时"
    ——对端 socket 根本没在 v6 上监听。修复：``0.0.0.0`` / ``::`` / ``*``
    一律优先建 ``AF_INET6 + IPV6_V6ONLY=0`` 双栈监听，v4 连接经 v4-mapped
    一并接收；平台不支持双栈时回退旧 IPv4 路径（可观测）。
    """

    @staticmethod
    def _dualstack_supported() -> bool:
        import socket

        try:
            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            return False
        try:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            probe.bind(("::", 0))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    async def _serve(self, certs) -> tuple:  # noqa: ANN001, ANN202
        ctrl_port = _free_port6()
        scr_port = _free_port6()
        seen_ips: list[str] = []

        async def on_control(session: ControlSession) -> None:
            seen_ips.append(session.client_ip)
            await session.next_message()

        server = TransportServer(
            host="0.0.0.0",
            control_port=ctrl_port,
            screen_port=scr_port,
            cert_path=certs[0],
            key_path=certs[1],
            on_control_session=on_control,
        )
        await server.start()
        return server, ctrl_port, seen_ips

    async def test_v4_and_v6_both_connect(self, certs) -> None:  # noqa: ANN001
        """同一双栈监听：127.0.0.1、::1、[::1] 三种写法都能建 TLS 会话。"""
        if not self._dualstack_supported():
            pytest.skip("本机不支持 IPv6 双栈监听（纯 v6 栈/禁用 v6）")
        server, ctrl_port, seen_ips = await self._serve(certs)
        try:
            for target in ("127.0.0.1", "::1", "[::1]"):
                ch = _control(target, ctrl_port, "")
                await asyncio.wait_for(ch.connect(), timeout=5)
                assert ch.is_connected, f"经 {target} 连接失败"
                await ch.close()
            assert server.listening
        finally:
            await server.stop()

    async def test_v4_mapped_peer_normalized_to_v4(self, certs) -> None:
        """v4 连接落到双栈监听：服务端看到的 client_ip 必须是裸 127.0.0.1。

        v4-mapped 形态（``::ffff:127.0.0.1``）若不归一，黑白名单/封禁/限流
        按 v4 配置的规则全部失配（同一台机器以两种身份记账），审计日志也
        与用户预期对不上。
        """
        if not self._dualstack_supported():
            pytest.skip("本机不支持 IPv6 双栈监听")
        server, ctrl_port, seen_ips = await self._serve(certs)
        try:
            ch = _control("127.0.0.1", ctrl_port, "")
            await asyncio.wait_for(ch.connect(), timeout=5)
            await asyncio.sleep(0.2)  # 等 hook 记录
            await ch.close()
            assert seen_ips == ["127.0.0.1"], f"peer IP 未归一：{seen_ips}"
        finally:
            await server.stop()

    async def test_explicit_v4_bind_stays_v4_only(self, certs) -> None:
        """显式 bind_host=127.0.0.1 必须保持纯 v4（用户收紧语义不得被扩大）。"""
        import socket

        ctrl_port = _free_port()
        scr_port = _free_port()
        server = TransportServer(
            host="127.0.0.1",
            control_port=ctrl_port,
            screen_port=scr_port,
            cert_path=certs[0],
            key_path=certs[1],
            on_control_session=None,
        )
        await server.start()
        try:
            families = {s.family for lst in server._servers for s in lst.sockets}  # noqa: SLF001
            assert families == {socket.AF_INET}, f"v4 绑定被扩为 {families}"
        finally:
            await server.stop()


class TestNormalizePeerIp:
    """_normalize_peer_ip 纯函数边界（双栈伴生逻辑，独立可测）。"""

    def test_v4_mapped_forms(self) -> None:
        from rdc.transport.server import _normalize_peer_ip

        assert _normalize_peer_ip("::ffff:127.0.0.1") == "127.0.0.1"
        assert _normalize_peer_ip("::ffff:192.168.1.50") == "192.168.1.50"
        assert _normalize_peer_ip("::FFFF:1.2.3.4") == "1.2.3.4"  # 大小写不敏感

    def test_real_v6_untouched(self) -> None:
        from rdc.transport.server import _normalize_peer_ip

        assert _normalize_peer_ip("::1") == "::1"
        assert _normalize_peer_ip("2409:8a55:1234::5678") == "2409:8a55:1234::5678"
        # 前缀形似但尾段非点分：原样返回（不误伤 ::ffff:1 这类压缩 v6 形态）
        assert _normalize_peer_ip("::ffff:0:1") == "::ffff:0:1"

    def test_empty_and_odd(self) -> None:
        from rdc.transport.server import _normalize_peer_ip

        assert _normalize_peer_ip("") == ""
        assert _normalize_peer_ip("?") == "?"

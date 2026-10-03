"""TransportServer 错误路径与生命周期竞态补测（批 5 覆盖率收口）。

专项锁定 ``transport/server.py`` 的异常/竞态分支——常规集成测试只走成功路径：

- 并发上限：信号量满额 → 新连接**立即拒绝**（v4 acquire(blocking=False) 语义）；
- 握手非 AuthError 失败（坏帧/TlsError/OSError）→ 拒绝且**不上报**认证失败
  （窗口语义保护，与超时同侧）；
- ``check_client`` 返回 awaitable（异步误接线）→ fail-closed 且销毁协程；
- ``on_auth_failed`` 回调抛异常 → 拒绝路径照常、异常隔离；
- 会话 hook 抛异常 → 记录后继续统一清理（会话被关闭、服务可停）；
- stop 竞态：会话 close 抛 OSError / 连接任务收尾抛错 → 幂等吞竞态、不卡死；
- peername 缺失 → client_ip 记 "?"（诊断分支）。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from rdc.exceptions import AuthError
from rdc.protocol.constants import HEADER_FORMAT, PROTOCOL_MAGIC, PROTOCOL_VERSION
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod
from rdc.transport.server import TransportServer
from tests._transport_support import gen_cert

pytestmark = pytest.mark.integration

GARBAGE_FRAME = struct.pack(HEADER_FORMAT, PROTOCOL_MAGIC, PROTOCOL_VERSION, 99, 0, 4, 0) + b"junk"


@pytest.fixture(autouse=True)
def _isolate_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def _raw_connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """裸 TCP + CERT_NONE TLS 客户端（绕开通道类，直接向端口投任意字节）。"""
    import ssl

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    await writer.start_tls(ctx, server_hostname="127.0.0.1")
    return reader, writer


class TestConnectionLimit:
    async def test_over_limit_rejected_immediately(self, certs: tuple[Path, Path]) -> None:
        """max_connections=1：第一条占住会话（hook 挂起），第二条直接被拒。"""
        hold = asyncio.Event()

        async def hang(session) -> None:  # noqa: ANN001
            await hold.wait()

        port = _free_port()
        server = TransportServer(
            host="127.0.0.1",
            control_port=port,
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            max_connections=1,
            on_control_session=hang,
        )
        await server.start()
        try:
            from rdc.transport.control_channel import ControlChannel

            ch = ControlChannel(
                "127.0.0.1",
                port,
                password_provider=lambda: "",
                fingerprint_confirm=lambda _f, _h: True,
            )
            await ch.connect()  # 占满 1 个并发（hook 挂起期间信号量被持有）
            r2, w2 = await _raw_connect(port)
            # 第二条：TLS 成功后应立即被关（sem.locked 拒绝路径）
            data = await asyncio.wait_for(r2.read(4096), timeout=5)
            assert data == b""  # EOF = 服务端拒绝即关
            w2.close()
            hold.set()
            await ch.close()
        finally:
            await server.stop()


class TestHandshakeErrorPaths:
    async def test_unknown_payload_frame_skipped_until_timeout(
        self, certs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """载荷类型未知的帧被 FrameStream 跳过（坏帧不断流），服务端等合法首帧
        直至握手超时 → 拒绝且**不上报**认证失败（与超时同侧语义）。"""
        import rdc.transport.server as srv_mod

        monkeypatch.setattr(srv_mod, "HANDSHAKE_TIMEOUT", 0.5)
        events: list[tuple[str, str]] = []
        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(cp)
            writer.write(GARBAGE_FRAME)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""
            assert events == []  # 超时不计数（窗口语义保护）
        finally:
            await server.stop()

    async def test_wrong_first_message_type_rejected_and_counted(
        self, certs: tuple[Path, Path]
    ) -> None:
        """控制端口首帧是**合法编码但类型错误**的消息（Heartbeat）→ AuthError
        拒绝并上报 control（口令尝试语义缺失仍属认证阶段失败）。"""
        from rdc.protocol.codec import encode_frame
        from rdc.protocol.messages import Heartbeat, HeartbeatKind

        events: list[tuple[str, str]] = []
        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(cp)
            writer.write(encode_frame(Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0)))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""
            await asyncio.sleep(0.05)
            assert ("127.0.0.1", "control") in events
        finally:
            await server.stop()

    async def test_screen_wrong_first_message_rejected_and_counted(
        self, certs: tuple[Path, Path]
    ) -> None:
        """画面端口合法但类型错误的首帧（非 ScreenJoin）→ 拒绝并上报 screen。"""
        from rdc.protocol.codec import encode_frame
        from rdc.protocol.messages import Heartbeat, HeartbeatKind

        events: list[tuple[str, str]] = []
        server = TransportServer(
            host="127.0.0.1",
            control_port=_free_port(),
            screen_port=(sp := _free_port()),
            cert_path=certs[0],
            key_path=certs[1],
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(sp)
            writer.write(encode_frame(Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0)))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""
            await asyncio.sleep(0.05)
            assert ("127.0.0.1", "screen") in events
        finally:
            await server.stop()

    async def test_screen_admit_rejected_before_handshake(self, certs: tuple[Path, Path]) -> None:
        """画面端口准入拒绝（check_client=False）→ TLS 后立关、不计认证失败。"""
        events: list[tuple[str, str]] = []
        server = TransportServer(
            host="127.0.0.1",
            control_port=_free_port(),
            screen_port=(sp := _free_port()),
            cert_path=certs[0],
            key_path=certs[1],
            check_client=lambda _ip: False,
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(sp)
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""
            await asyncio.sleep(0.05)
            assert events == []  # 准入拒绝非认证失败
        finally:
            await server.stop()

    async def test_screen_handshake_timeout_rejected_not_counted(
        self, certs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """画面端口 TLS 后沉默 → 超时拒绝（不上报认证失败，窗口语义保护）。"""
        import rdc.transport.server as srv_mod

        monkeypatch.setattr(srv_mod, "HANDSHAKE_TIMEOUT", 0.5)
        events: list[tuple[str, str]] = []
        server = TransportServer(
            host="127.0.0.1",
            control_port=_free_port(),
            screen_port=(sp := _free_port()),
            cert_path=certs[0],
            key_path=certs[1],
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(sp)
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""
            assert events == []
        finally:
            await server.stop()


class TestAdmitAndHookGuards:
    async def test_async_check_client_fail_closed_and_coro_closed(
        self, certs: tuple[Path, Path]
    ) -> None:
        """check_client 误返回协程 → 拒绝 + 协程销毁（bool(协程) 恒真漏洞封堵）。"""
        started: list[bool] = []
        finished: list[bool] = []

        async def async_check(_ip: str) -> bool:
            started.append(True)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            finally:
                finished.append(True)
            return True

        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            check_client=async_check,  # 契约违例：异步回调
        )
        await server.start()
        try:
            reader, writer = await _raw_connect(cp)
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data == b""  # fail-closed：立即关流
            await asyncio.sleep(0.05)
            assert started == [] and finished == []  # 协程未运行即被 close() 销毁
        finally:
            await server.stop()

    async def test_auth_failed_hook_exception_isolated(self, certs: tuple[Path, Path]) -> None:
        """on_auth_failed 抛错 → 记账失败被隔离（仅记录），拒绝路径照常：
        客户端仍以 AuthError（ok=False 响应）收场、连接关闭。"""

        def boom(_ip: str, _ch: str) -> None:
            raise RuntimeError("记账炸了")

        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            password_hash=hash_password("right-pw"),
            on_auth_failed=boom,
        )
        await server.start()
        try:
            from rdc.transport.control_channel import ControlChannel

            ch = ControlChannel(
                "127.0.0.1",
                cp,
                password_provider=lambda: "wrong-pw",
                fingerprint_confirm=lambda _f, _h: True,
            )
            with pytest.raises(AuthError):
                await asyncio.wait_for(ch.connect(), timeout=10)
            await ch.close()
        finally:
            await server.stop()

    async def test_session_hook_crash_cleaned_up(self, certs: tuple[Path, Path]) -> None:
        """会话 hook 抛异常 → 隔离记录，会话关闭、服务可正常 stop（不泄漏会话）。"""
        connected = asyncio.Event()

        async def crash(session) -> None:  # noqa: ANN001
            connected.set()
            raise RuntimeError("hook 崩了")

        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            on_control_session=crash,
        )
        await server.start()
        try:
            from rdc.transport.control_channel import ControlChannel

            ch = ControlChannel(
                "127.0.0.1",
                cp,
                password_provider=lambda: "",
                fingerprint_confirm=lambda _f, _h: True,
            )
            await ch.connect()
            await asyncio.wait_for(connected.wait(), timeout=5)
            # hook 崩溃后服务端应关闭会话 → 客户端读到 EOF
            msg = await asyncio.wait_for(ch.next_message(), timeout=5)
            assert msg is None
            await ch.close()
        finally:
            await asyncio.wait_for(server.stop(), timeout=5)


class TestStopRacePaths:
    async def test_stop_with_close_failure_and_pending_task(self, certs: tuple[Path, Path]) -> None:
        """stop 竞态：会话 close 抛 OSError + 任务收尾抛错 → 吞掉并记录，幂等完成。"""
        server = TransportServer(
            host="127.0.0.1",
            control_port=(cp := _free_port()),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
        )
        await server.start()

        from rdc.transport.base import StreamChannel
        from rdc.transport.control_channel import ControlChannel

        ch = ControlChannel(
            "127.0.0.1",
            cp,
            password_provider=lambda: "",
            fingerprint_confirm=lambda _f, _h: True,
        )
        await ch.connect()

        # 服务侧注入一个"关不掉的会话"与一个"收尾抛 OSError 的任务"（竞态现场）
        fake_session = StreamChannel(ch.stream)

        async def bad_close() -> None:
            raise OSError("close 竞态")

        fake_session.close = bad_close  # type: ignore[method-assign]
        server._sessions.add(fake_session)  # noqa: SLF001

        async def crashing_task() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise OSError("收尾失败") from None

        t = asyncio.get_running_loop().create_task(crashing_task())
        server._tasks.add(t)  # noqa: SLF001

        await asyncio.wait_for(server.stop(), timeout=5)
        assert server.listening is False
        await asyncio.sleep(0)  # 让任务取消落地
        assert t.done()
        await ch.close()

    async def test_peername_missing_maps_to_question(self, certs: tuple[Path, Path]) -> None:
        """peername 取不到 → client_ip 记 "?"（拒绝路径仍走通，诊断不崩）。"""
        server = TransportServer(
            host="127.0.0.1",
            control_port=_free_port(),
            screen_port=_free_port(),
            cert_path=certs[0],
            key_path=certs[1],
            check_client=lambda ip: ip != "?",
        )
        await server.start()
        try:
            # 直接调内部 handler，喂一个无 peername 的 writer 替身
            from tests._transport_support import FakeWriter

            reader = asyncio.StreamReader()
            writer = FakeWriter()
            writer.extra["peername"] = None
            await server._handle_control(reader, writer)  # noqa: SLF001
            assert writer.closed  # "?" 被 check_client 拒绝 → 关流
        finally:
            await server.stop()

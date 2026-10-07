"""TransportServer 批 3 扩展回调集成测试：check_client / on_auth_failed。

阶段 4 基线（test_transport_integration.py）不动；本文件专项锁定新 API 语义：

- ``check_client``：TLS 后、握手**前**拒绝（被拒 IP 不会收到 AuthResponse，
  会话 hook 不触发）；判定异常 fail-closed；
- ``on_auth_failed``：控制通道口令错误计数 1 次（channel="control"）；
  画面通道令牌无效计数 1 次（channel="screen"）；
  TLS/网络类失败与握手**超时**不上报（窗口语义保护）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rdc.exceptions import AuthBannedError, AuthError
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod
from rdc.transport.control_channel import ControlChannel
from rdc.transport.screen_channel import ScreenChannel
from rdc.transport.server import TransportServer
from tests._transport_support import gen_cert

pytestmark = pytest.mark.integration


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


def _confirm(_fp: str, _host: str) -> bool:
    return True


async def _run_server(certs, **kw):  # noqa: ANN003
    ctrl_port, scr_port = _free_port(), _free_port()
    server = TransportServer(
        host="127.0.0.1",
        control_port=ctrl_port,
        screen_port=scr_port,
        cert_path=certs[0],
        key_path=certs[1],
        **kw,
    )
    await server.start()
    return server, ctrl_port, scr_port


def _control(port: int, password: str = "") -> ControlChannel:
    return ControlChannel(
        "127.0.0.1", port, password_provider=lambda: password, fingerprint_confirm=_confirm
    )


class TestCheckClient:
    async def test_rejected_ip_never_reaches_handshake(self, certs) -> None:
        seen: list[str] = []
        sessions: list[object] = []

        def deny_banned(ip: str) -> bool:
            seen.append(ip)
            return ip != "127.0.0.1"

        async def on_control(session) -> None:  # noqa: ANN001
            sessions.append(session)

        server, ctrl_port, _ = await _run_server(
            certs, check_client=deny_banned, on_control_session=on_control
        )
        try:
            ch = _control(ctrl_port, "")
            with pytest.raises((AuthError, OSError)):
                await asyncio.wait_for(ch.connect(), timeout=5)
            await ch.close()
            assert seen  # 判定被调用（TLS 后、握手前）
            await asyncio.sleep(0.1)
            assert sessions == []  # 会话 hook 未触发
        finally:
            await server.stop()

    async def test_check_exception_fail_closed(self, certs) -> None:
        def boom(_ip: str) -> bool:
            raise RuntimeError("准入逻辑炸了")

        server, ctrl_port, _ = await _run_server(certs, check_client=boom)
        try:
            ch = _control(ctrl_port, "")
            with pytest.raises((AuthError, OSError)):
                await asyncio.wait_for(ch.connect(), timeout=5)
            await ch.close()
        finally:
            await server.stop()

    async def test_allowed_ip_unaffected(self, certs) -> None:
        server, ctrl_port, _ = await _run_server(certs, check_client=lambda ip: True)
        try:
            ch = _control(ctrl_port, "")
            await ch.connect()
            assert ch.is_connected
            await ch.close()
        finally:
            await server.stop()


class TestOnAuthFailed:
    async def test_wrong_password_reported_once_control(self, certs) -> None:
        events: list[tuple[str, str]] = []
        server, ctrl_port, _ = await _run_server(
            certs,
            password_hash=hash_password("right-pw"),
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        try:
            ch = _control(ctrl_port, "wrong-pw")
            with pytest.raises(AuthError):
                await ch.connect()
            await ch.close()
            assert events and events[-1][1] == "control"
            assert events[-1][0] == "127.0.0.1"
        finally:
            await server.stop()

    async def test_success_not_reported(self, certs) -> None:
        events: list[tuple[str, str]] = []

        async def on_control(session) -> None:  # noqa: ANN001
            await session.next_message()

        server, ctrl_port, _ = await _run_server(
            certs,
            password_hash=hash_password("right-pw"),
            on_control_session=on_control,
            on_auth_failed=lambda ip, ch: events.append((ip, ch)),
        )
        try:
            ch = _control(ctrl_port, "right-pw")
            await ch.connect()
            await ch.close()
            assert events == []
        finally:
            await server.stop()

    async def test_bad_token_reported_screen(self, certs) -> None:
        events: list[tuple[str, str]] = []
        server, ctrl_port, scr_port = await _run_server(
            certs, on_auth_failed=lambda ip, ch: events.append((ip, ch))
        )
        try:
            sc = ScreenChannel(
                "127.0.0.1", scr_port, session_token="forged", connection_id=b"c" * 32,
                fingerprint_confirm=_confirm,
            )
            with pytest.raises(AuthError):
                await sc.connect()
            await sc.close()
            assert ("127.0.0.1", "screen") in events
        finally:
            await server.stop()

    async def test_handshake_timeout_not_reported(
        self, certs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TLS 成功后客户端沉默（不发首帧）：属超时而非认证失败，不计数。"""
        import rdc.transport.server as srv_mod

        monkeypatch.setattr(srv_mod, "HANDSHAKE_TIMEOUT", 0.5)
        events: list[tuple[str, str]] = []
        server, ctrl_port, _ = await _run_server(
            certs, on_auth_failed=lambda ip, ch: events.append((ip, ch))
        )
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", ctrl_port)
            import ssl as _ssl

            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
            await writer.start_tls(ctx, server_hostname="127.0.0.1")
            # 保持沉默越过服务端握手超时 → 服务端应拒绝关闭但不上报认证失败
            await asyncio.sleep(1.5)
            writer.close()
            assert events == []
        finally:
            await server.stop()


class TestBanNotice:
    """缺陷 2 哨兵：准入拒绝命中封禁时回一帧 banned 认证响应。"""

    async def test_banned_ip_gets_notice_with_remaining(self, certs) -> None:
        server, ctrl_port, _ = await _run_server(
            certs,
            check_client=lambda ip: False,
            ban_query=lambda ip: 121.0,  # 命中封禁，剩 121 秒
        )
        try:
            ch = _control(ctrl_port, "whatever")
            with pytest.raises(AuthBannedError) as ei:
                await asyncio.wait_for(ch.connect(), timeout=5)
            assert ei.value.remaining_seconds == 121
            await ch.close()
        finally:
            await server.stop()

    async def test_non_ban_rejection_stays_silent(self, certs) -> None:
        """ban_query 返回 0（黑白名单/限流类拒绝）→ 裸关，不发任何响应帧。"""
        server, ctrl_port, _ = await _run_server(
            certs,
            check_client=lambda ip: False,
            ban_query=lambda ip: 0.0,
        )
        try:
            ch = _control(ctrl_port, "whatever")
            with pytest.raises((AuthError, OSError)) as ei:
                await asyncio.wait_for(ch.connect(), timeout=5)
            assert not isinstance(ei.value, AuthBannedError)
            await ch.close()
        finally:
            await server.stop()

    async def test_unwired_baseline_kept(self, certs) -> None:
        """未接线 ban_query（既有全部装配/测试路径）→ 拒绝语义与基线一致。"""
        server, ctrl_port, _ = await _run_server(certs, check_client=lambda ip: False)
        try:
            ch = _control(ctrl_port, "whatever")
            with pytest.raises((AuthError, OSError)) as ei:
                await asyncio.wait_for(ch.connect(), timeout=5)
            assert not isinstance(ei.value, AuthBannedError)
            await ch.close()
        finally:
            await server.stop()

"""v5 握手流程（阶段 4）：认证 + nonce 交换，客户端/服务端共享实现。

v5 握手全部走 **15 字节分帧协议**（协议自描述，无 v4 裸 4B 长度前缀），
nonce 交换时机：**TLS 握手成功之后、任何业务帧之前**（用户叮嘱 c），
且仅当 TLS 会话建立成功才会进入本模块（TLS 失败时连接已被上层中止）。

控制通道（::func:`perform_control_handshake` / :func:`serve_control_handshake`）::

    C→S  AuthRequest{password, client_nonce}          （无签名，TLS 保护）
    S→C  AuthResponse{ok, token, server_nonce, connection_id}
    双方 key = derive_hmac_key(client_nonce, server_nonce, connection_id)
    → 之后该流所有帧 HMAC 签名 + 强制验签

画面通道（::func:`perform_screen_join` / :func:`serve_screen_join`）::

    C→S  ScreenJoin{token, client_nonce, connection_id}（回显控制通道连接标识）
    S→C  AuthResponse{ok, server_nonce, connection_id}
    key = derive_hmac_key(...)  → 与控制通道**不同密钥**（nonce 不同），
    但同绑定 connection_id（令牌单次使用 + IP/connection 绑定）

安全要点：

- 每连接一次 nonce（``connection_id``、双端 nonce 均 32B ``secrets`` 随机）；
- 密钥建立后固定不重派生（用户叮嘱 c）；
- 认证失败**不发 nonce**：服务端直接回 ``ok=False`` 并断连，
  nonce 交换在 TLS 会话建立失败时根本不会启动（叮嘱 c 的落地路径）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rdc.exceptions import AuthError, SecretStoreError
from rdc.logging import get_logger
from rdc.protocol.messages import AuthRequest, AuthResponse, ScreenJoin
from rdc.transport.base import FrameStream
from rdc.transport.hmac_nonce import (
    derive_hmac_key,
    new_connection_id,
    new_nonce,
)

__all__ = [
    "ControlHandshakeResult",
    "ScreenHandshakeResult",
    "perform_control_handshake",
    "perform_screen_join",
    "serve_control_handshake",
    "serve_screen_join",
]

_LOGGER = get_logger("transport.handshake")

#: 握手帧读取超时（秒）。超时视为对端不可信/不可达，直接失败。
HANDSHAKE_TIMEOUT = 10.0


@dataclass(frozen=True)
class ControlHandshakeResult:
    """控制通道握手结果（客户端视角）。"""

    session_token: str
    hmac_key: bytes
    connection_id: bytes


@dataclass(frozen=True)
class ScreenHandshakeResult:
    """画面通道加入结果（客户端视角）。"""

    hmac_key: bytes


async def _recv_frame(stream: FrameStream) -> object:
    """读取一帧并要求特定握手消息类型；EOF/坏帧抛 AuthError。"""
    msg = await stream.recv_message()
    if msg is None:
        raise AuthError("握手期间对端关闭连接")
    return msg


async def perform_control_handshake(
    stream: FrameStream,
    *,
    password: str,
    client_name: str = "",
) -> ControlHandshakeResult:
    """客户端：控制通道认证 + nonce 交换。

    Args:
        stream: 已完成 TLS 握手、尚未开启 HMAC 的控制通道帧流。
        password: 明文口令（TLS 通道内传输；服务端 bcrypt 校验）。
        client_name: 客户端自我标识（审计用）。

    Returns:
        会话令牌（画面通道加入用）与已启用于 ``stream`` 的 HMAC 密钥。

    Raises:
        AuthError: 认证被拒 / 握手响应缺字段 / 对端中途断连。
        asyncio.TimeoutError: 对端 :data:`HANDSHAKE_TIMEOUT` 秒内无响应。
    """
    client_nonce = new_nonce()
    await stream.send_message(
        AuthRequest(password=password, client_name=client_name, client_nonce=client_nonce)
    )
    resp = await _recv_frame(stream)
    if not isinstance(resp, AuthResponse):
        raise AuthError(f"握手期望 AuthResponse，收到 {type(resp).__name__}")
    if not resp.ok:
        raise AuthError(f"认证失败: {resp.reason or '未提供原因'}")
    if not resp.server_nonce or not resp.connection_id or not resp.token:
        raise AuthError("AuthResponse 缺少 nonce/connection_id/token，拒绝建立会话")
    key = derive_hmac_key(client_nonce, resp.server_nonce, resp.connection_id)
    stream.enable_hmac(key)
    _LOGGER.info("control_handshake_ok", host=stream.peer_host)
    return ControlHandshakeResult(session_token=resp.token, hmac_key=key, connection_id=resp.connection_id)


async def serve_control_handshake(
    stream: FrameStream,
    *,
    client_ip: str,
    password_hash: str,
    issue_token: Callable[[str, bytes], str],
) -> bytes:
    """服务端：处理控制通道认证握手。

    Args:
        stream: 已 TLS 的控制通道帧流。
        client_ip: 对端 IP（令牌记录）。
        password_hash: 存储的 bcrypt 哈希（空串 = 未设密码，任意口令通过，
            v4 语义由 ``secrets.password.verify_password`` 实现）。
        issue_token: ``(client_ip, connection_id) → token`` 签发回调
            （:class:`~rdc.transport.session_token.SessionTokenStore.issue`）。

    Returns:
        已启用于 ``stream`` 的 HMAC 密钥。

    Raises:
        AuthError: 首帧不是 AuthRequest / 未携带 nonce / 口令错误 / 对端断连。
    """
    from rdc.secrets.password import verify_password as _bcrypt_check

    request = await _recv_frame(stream)
    if not isinstance(request, AuthRequest):
        raise AuthError(f"握手首帧期望 AuthRequest，收到 {type(request).__name__}")
    if not request.client_nonce:
        raise AuthError("AuthRequest 未携带 client_nonce，拒绝（协议协商失败）")
    connection_id = new_connection_id()
    try:
        password_ok = _bcrypt_check(request.password, password_hash)
    except SecretStoreError:  # 存储哈希损坏：拒绝认证并记录（约束 6，不静默放行）
        password_ok = False
        _LOGGER.error("control_auth_hash_broken", client_ip=client_ip)
    if not password_ok:
        # 认证失败：不发 nonce 不发密钥（叮嘱 c：TLS 内 nonce 仅发给已认证对端）
        await stream.send_message(AuthResponse(ok=False, reason="口令校验失败"))
        raise AuthError(f"控制通道认证失败: {client_ip}")
    server_nonce = new_nonce()
    token = issue_token(client_ip, connection_id)
    await stream.send_message(
        AuthResponse(ok=True, token=token, server_nonce=server_nonce, connection_id=connection_id)
    )
    key = derive_hmac_key(request.client_nonce, server_nonce, connection_id)
    stream.enable_hmac(key)
    _LOGGER.info("control_session_authenticated", client_ip=client_ip)
    return key


async def perform_screen_join(
    stream: FrameStream,
    *,
    session_token: str,
    connection_id: bytes,
) -> ScreenHandshakeResult:
    """客户端：画面通道加入（一次性令牌 + 独立 nonce）。

    Args:
        stream: 已 TLS、未开 HMAC 的画面通道帧流。
        session_token: 控制通道 ``AuthResponse`` 发放的一次性令牌。
        connection_id: 控制通道下发的连接标识（回显绑定证明）。

    Returns:
        已启用于 ``stream`` 的 HMAC 密钥（与控制通道密钥不同）。

    Raises:
        AuthError: 令牌被拒 / 响应异常 / 对端断连。
    """
    client_nonce = new_nonce()
    await stream.send_message(
        ScreenJoin(token=session_token, client_nonce=client_nonce, connection_id=connection_id)
    )
    resp = await _recv_frame(stream)
    if not isinstance(resp, AuthResponse):
        raise AuthError(f"加入期望 AuthResponse，收到 {type(resp).__name__}")
    if not resp.ok:
        raise AuthError(f"画面通道加入失败: {resp.reason or '未提供原因'}")
    if not resp.server_nonce or resp.connection_id != connection_id:
        raise AuthError("ScreenJoin 响应的 nonce/connection_id 异常，拒绝建立会话")
    key = derive_hmac_key(client_nonce, resp.server_nonce, connection_id)
    stream.enable_hmac(key)
    _LOGGER.info("screen_join_ok", host=stream.peer_host)
    return ScreenHandshakeResult(hmac_key=key)


async def serve_screen_join(
    stream: FrameStream,
    *,
    client_ip: str,
    consume_token: Callable[[str, str, bytes], object],
) -> bytes:
    """服务端：处理画面通道加入。

    Args:
        stream: 已 TLS 的画面通道帧流。
        client_ip: 对端 IP（令牌绑定校验）。
        consume_token: ``(token, client_ip, connection_id) → record`` 消费回调
            （:class:`~rdc.transport.session_token.SessionTokenStore.consume`；
            校验失败抛 :class:`~rdc.exceptions.AuthError`）。

    Returns:
        已启用于 ``stream`` 的 HMAC 密钥。

    Raises:
        AuthError: 首帧非法 / 令牌无效（含重放）/ 绑定不符。
    """
    request = await _recv_frame(stream)
    if not isinstance(request, ScreenJoin):
        raise AuthError(f"画面端口首帧期望 ScreenJoin，收到 {type(request).__name__}")
    try:
        consume_token(request.token, client_ip, request.connection_id)
    except AuthError:
        # 令牌重放/过期/绑定不符：回拒绝但**不发 nonce**（叮嘱 c）
        await stream.send_message(AuthResponse(ok=False, reason="令牌无效"))
        raise
    if not request.client_nonce:
        raise AuthError("ScreenJoin 未携带 client_nonce，拒绝")
    server_nonce = new_nonce()
    await stream.send_message(
        AuthResponse(ok=True, server_nonce=server_nonce, connection_id=request.connection_id)
    )
    key = derive_hmac_key(request.client_nonce, server_nonce, request.connection_id)
    stream.enable_hmac(key)
    _LOGGER.info("screen_session_established", client_ip=client_ip)
    return key

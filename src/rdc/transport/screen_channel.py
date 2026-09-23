"""画面传输通道（阶段 4，4.3）：asyncio + TLS + 一次性令牌加入 + 独立 nonce。

与 v4 的差异：

- 线程收发对（`_recv_loop`/`_send_loop` + `deque(maxlen)` + Event）→ 协程 +
  :class:`~rdc.transport.base.StreamChannel`；
- 裸 4B 长度前缀令牌握手 → ``ScreenJoin`` 普通帧（协议自描述）；
- 令牌认证从"服务端 dict[uuid, 过期时间]"升级为
  :class:`~rdc.transport.session_token.SessionTokenStore`
  （secrets 令牌 + IP/connection_id 绑定 + 单次使用）；
- 画面通道 HMAC 密钥与控制通道**不同源**（各自独立 nonce 交换），
  同绑定 ``connection_id`` —— 一条通道密钥泄露不波及另一条。

v4 的发送侧流控在客户端通道里（画面流本是下行通道，v4 发送队列服务
的是"客户端向服务端请求"的旧模式）；v5 画面数据单向 S→C，客户端侧无
发送队列，背压与丢帧位于服务端 :mod:`rdc.transport.broadcaster`（4.4/4.9）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Callable

from rdc.exceptions import TlsError
from rdc.logging import get_logger
from rdc.transport.base import FrameStream, StreamChannel
from rdc.transport.control_channel import CONNECT_TIMEOUT
from rdc.transport.handshake import perform_screen_join
from rdc.transport.tls import (
    build_client_ssl_context,
    is_self_signed_error,
    tls_hostname,
    verify_peer_trust,
)

__all__ = ["ScreenChannel"]

_LOG = get_logger("transport.screen")

FingerprintConfirm = Callable[[str, str], bool]


class ScreenChannel(StreamChannel):
    """主控端接收画面帧的独立通道（S→C 单向数据 + 心跳双向）。

    用法::

        sc = ScreenChannel("192.168.1.5", 9999,
                           session_token=ctrl.session_token,
                           connection_id=ctrl.connection_id)
        await sc.connect()
        msg = await sc.next_message()   # ScreenFrame / Heartbeat…
        await sc.close()
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        session_token: str,
        connection_id: bytes,
        fingerprint_confirm: FingerprintConfirm | None = None,
    ) -> None:
        """
        Args:
            host: 被控端地址。
            port: 画面端口。
            session_token: 控制通道 :class:`AuthResponse` 发放的一次性令牌。
            connection_id: 控制通道下发的连接标识（回显证明持有）。
            fingerprint_confirm: 自签名 TOFU 首连确认回调（画面端口的证书
                与控制端口同一套，通常已在信任列表）。
        """
        super().__init__()
        self._host = host
        self._port = port
        self._session_token = session_token
        self._connection_id = connection_id
        self._fingerprint_confirm = fingerprint_confirm

    async def connect(self) -> None:
        """建连并加入：TCP → TLS → ``ScreenJoin``（消费令牌）→ nonce → HMAC 开启。

        Raises:
            RuntimeError: 重复调用。
            TlsError: TLS/TOFU 失败。
            AuthError: 令牌无效（重放/过期/绑定不符，**第二次使用必失败**）。
            OSError: TCP 失败。
            asyncio.TimeoutError: 超 :data:`CONNECT_TIMEOUT` 秒。
        """
        if self._stream is not None:
            raise RuntimeError("ScreenChannel.connect 只能调用一次")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            stream = await self._upgrade_tls(reader, writer)
            await asyncio.wait_for(
                perform_screen_join(
                    stream,
                    session_token=self._session_token,
                    connection_id=self._connection_id,
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except BaseException:
            writer.close()
            with contextlib.suppress(OSError):  # 含 ssl.SSLError；被拒连接关闭竞态噪音
                await writer.wait_closed()
            raise
        self.bind_stream(stream)
        self.start_receiving()
        _LOG.info("screen_channel_ready", host=self._host, port=self._port)

    async def _upgrade_tls(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> FrameStream:
        """TLS 客户端握手（与控制通道同一策略：自签名才允许 CERT_NONE + TOFU）。"""
        hostname = tls_hostname(self._host)
        ssl_ctx = build_client_ssl_context(verify=True)
        try:
            await writer.start_tls(ssl_ctx, server_hostname=hostname)
        except ssl.SSLError as exc:
            if not is_self_signed_error(exc):
                writer.close()
                raise TlsError(f"画面通道 TLS 握手失败（非自签名原因，拒绝降级）: {exc}") from exc
            # 降级重建前先关闭旧 socket（v4.0.2 同款修复：握手失败的套接字不可复用）
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=CONNECT_TIMEOUT,
            )
            insecure_ctx = build_client_ssl_context(verify=False)
            try:
                await writer.start_tls(insecure_ctx, server_hostname=hostname)
            except (ssl.SSLError, OSError) as exc:
                writer.close()
                raise TlsError(f"画面通道 TLS 降级重握手失败: {exc}") from exc
            try:
                verify_peer_trust(
                    writer.get_extra_info("ssl_object"),
                    self._host,
                    confirm=self._fingerprint_confirm,
                )
            except TlsError:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                raise
        return FrameStream(reader, writer, name="screen")

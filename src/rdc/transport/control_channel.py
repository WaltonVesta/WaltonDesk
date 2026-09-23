"""控制指令通道（阶段 4，4.2）：asyncio + TLS + 认证/nonce 握手。

v4 病灶全部消除：

- 线程模型（接收线程 + lock + 跨线程 socket 置 None 竞态）→ 单事件循环协程；
- 裸 4B 长度前缀握手（与业务流两套编解码、两端配置错位地雷）→ 握手即
  ``AuthRequest``/``AuthResponse`` 普通帧，协议自描述（阶段 3 设计，本层落地）；
- 密码弹窗 ``tk.Tk()`` 在工作线程创建（B2 同类问题）→ 口令经回调传入，
  UI 弹框属阶段 6 职责，本层零 GUI 依赖；
- TLS 降级逻辑迁入 :mod:`rdc.transport.tls`（TOFU + 指纹确认回调注入）。

指纹确认回调契约（对齐 v4 ``fingerprint_callback``）：
``confirm(fingerprint, host) -> bool``；拒绝时 :class:`~rdc.exceptions.TlsError`。
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Callable

from rdc.exceptions import TlsError
from rdc.logging import get_logger
from rdc.transport.base import FrameStream, StreamChannel
from rdc.transport.handshake import perform_control_handshake
from rdc.transport.tls import (
    build_client_ssl_context,
    is_self_signed_error,
    tls_hostname,
    verify_peer_trust,
)

__all__ = ["ControlChannel"]

_LOG = get_logger("transport.control")

#: TCP 建连 + TLS + 认证握手的整体超时（秒，v4 单步 5s×N 收敛为一个总闸）。
CONNECT_TIMEOUT = 10.0

FingerprintConfirm = Callable[[str, str], bool]


class ControlChannel(StreamChannel):
    """主控端 → 被控端的控制通道（指令上行；心跳/剪贴板复用本通道）。

    用法::

        ch = ControlChannel("192.168.1.5", 9998, password_provider=lambda: "pw")
        await ch.connect()               # TCP+TLS+认证+nonce → HMAC 已开启
        await ch.send(MouseEvent(...))   # 自动签名
        msg = await ch.next_message()    # welcome/心跳 pong/剪贴板…
        await ch.close()
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        password_provider: Callable[[], str],
        fingerprint_confirm: FingerprintConfirm | None = None,
        client_name: str = "",
    ) -> None:
        """
        Args:
            host: 被控端地址（IPv4/IPv6/主机名）。
            port: 控制端口。
            password_provider: 口令提供回调（阶段 6 前为同步回调，保持传输层
                零 GUI 依赖；调用发生在 :meth:`connect` 内）。
            fingerprint_confirm: 自签名证书 TOFU 首连确认回调；``None`` 时
                首连拒绝（v4"默认信任"降级在 v5 视为不安全，禁止静默放行）。
            client_name: 客户端自我标识（审计/日志）。
        """
        super().__init__()
        self._host = host
        self._port = port
        self._password_provider = password_provider
        self._fingerprint_confirm = fingerprint_confirm
        self._client_name = client_name
        self.session_token: str | None = None
        self.connection_id: bytes | None = None

    async def connect(self) -> None:
        """建连：TCP → TLS（强制 1.2+，自签名走 TOFU）→ 认证 → nonce 交换 → HMAC 开启。

        成功后本通道满足 :class:`~rdc.transport.base.Channel` 全部约定：
        ``is_connected=True``、后台接收任务运行、帧收发均强制 HMAC。

        Raises:
            RuntimeError: 重复调用。
            TlsError: TLS 握手失败 / 证书校验被拒 / 用户拒绝指纹。
            AuthError: 认证失败或握手帧非法。
            OSError: TCP 连接失败。
            asyncio.TimeoutError: 整体超 :data:`CONNECT_TIMEOUT` 秒。
        """
        if self.stream_available:
            raise RuntimeError("ControlChannel.connect 只能调用一次")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            stream = await self._upgrade_tls(reader, writer)
            result = await asyncio.wait_for(
                perform_control_handshake(
                    stream,
                    password=self._password_provider(),
                    client_name=self._client_name,
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except BaseException:
            writer.close()
            with contextlib.suppress(OSError):  # ssl.SSLError 派生自 OSError；被拒连接关闭竞态噪音
                await writer.wait_closed()
            raise
        self.bind_stream(stream)
        self.session_token = result.session_token
        self.connection_id = result.connection_id
        self.start_receiving()
        _LOG.info("control_channel_ready", host=self._host, port=self._port)

    async def _upgrade_tls(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> FrameStream:
        """在既有 TCP 连接上完成 TLS 客户端握手（含自签名 TOFU 降级路径）。

        v4 降级需要重建 socket（Windows 下握手失败的 SSLSocket 不可复用），
        asyncio 同理：系统 CA 校验失败且**确认为自签名**时关闭重建、
        以 CERT_NONE 重握手并交给 :func:`verify_peer_trust` 做指纹 TOFU。
        其他证书错误（过期/主机名不符）直接上抛，**绝不降级**
        （v4.0.2 修复的降级滥用问题以函数契约固化）。
        """
        hostname = tls_hostname(self._host)
        ssl_ctx = build_client_ssl_context(verify=True)
        try:
            await writer.start_tls(ssl_ctx, server_hostname=hostname)
        except ssl.SSLError as exc:
            if not is_self_signed_error(exc):
                writer.close()
                raise TlsError(f"TLS 握手失败（非自签名原因，拒绝降级）: {exc}") from exc
            _LOG.info("tls_self_signed_fallback", host=self._host)
            writer.close()
            with contextlib.suppress(OSError):  # 被拒连接的本端关闭竞态噪音
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
                raise TlsError(f"TLS 降级重握手失败: {exc}") from exc
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
        return FrameStream(reader, writer, name="control")

    @property
    def stream_available(self) -> bool:
        """帧流是否已绑定（connect 成功后为 ``True``）。"""
        return self._stream is not None

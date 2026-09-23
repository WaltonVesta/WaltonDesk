"""传输层抽象（阶段 4，4.1）：asyncio Channel Protocol + 帧流基座。

设计：

- :class:`Channel` 是上层（client/server/UI）可见的通道契约：
  ``connect / send / next_message / recv_task / close / is_connected``。
- :class:`FrameStream` 把 asyncio ``StreamReader/Writer`` 与协议层
  :class:`~rdc.protocol.framing.PacketBuffer` 粘合：TCP 粘包/半包、CRC、
  zlib、HMAC 验签全部收敛在协议层，本类只做"字节进、领域消息出"。
- 会话安全状态（HMAC 密钥 / 强制验签）通过 :meth:`FrameStream.enable_hmac`
  在握手完成后开启；开启前收到的帧不带签名（握手帧本身在 TLS 通道内），
  开启后 ``require_hmac=True`` 封堵剥标志降级攻击（阶段 3 遗留设计）。

错误契约：

- :meth:`FrameStream.recv_message` 返回 ``None`` **仅表示流终止**（EOF/读失败/
  本地关闭）；载荷语义错误（:class:`~rdc.exceptions.ProtocolError`）记日志后
  跳过该帧继续读——单帧损坏不断流（帧完整性由 CRC/HMAC 把关）。
- 写侧 IO 异常原样上抛 ``OSError``，由调用方决定关闭策略，不静默。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol, runtime_checkable

from rdc.exceptions import ProtocolError
from rdc.logging import get_logger
from rdc.protocol.codec import decode_packet, encode_frame
from rdc.protocol.framing import PacketBuffer
from rdc.protocol.messages import Message

__all__ = ["Channel", "FrameStream", "StreamChannel"]

#: 单次 ``reader.read`` 的块大小（64KB，与 v4 对齐）。
READ_CHUNK_SIZE = 65536


@runtime_checkable
class Channel(Protocol):
    """asyncio 通道契约（4.1）。

    实现者约定：

    - :meth:`connect` 完成"建连 + TLS 握手 + 认证/加入 + nonce 交换"全部前置，
      成功返回即代表通道可用且 HMAC 强制完整性已生效；
    - :meth:`send` 发送一条领域消息（自动 HMAC 签名）；
    - 后台 ``recv_task`` 持续接收并投递到 :meth:`next_message` 队列，
      取出 ``None`` 表示对端关闭；
    - :meth:`close` 幂等：取消接收任务并关闭底层连接。
    """

    async def connect(self) -> None: ...

    async def send(self, msg: Message) -> None: ...

    async def next_message(self) -> Message | None: ...

    @property
    def recv_task(self) -> asyncio.Task[None] | None: ...

    @property
    def is_connected(self) -> bool: ...

    async def close(self) -> None: ...


class FrameStream:
    """一条已建立（通常已 TLS 包装）的双工流上的协议帧收发。

    Args:
        reader: asyncio 流读取端。
        writer: asyncio 流写入端。
        name: 日志标识（如 ``"control"`` / ``"screen"``）。
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        name: str = "stream",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._buffer = PacketBuffer()
        self._name = name
        self._log = get_logger(f"transport.{name}")
        self._closed = False
        self._hmac_key: bytes | None = None
        self._require_hmac = False

    # -- 会话安全状态 ---------------------------------------------------------

    @property
    def hmac_enabled(self) -> bool:
        """握手是否已完成（帧签名/验签已启用）。"""
        return self._require_hmac

    def enable_hmac(self, key: bytes) -> None:
        """握手完成后开启 HMAC：本流发送自动签名、接收强制验签。

        Raises:
            ProtocolError: ``key`` 为空（拒绝"开启但无密钥"的配置错误）。
        """
        if not key:
            raise ProtocolError("enable_hmac: 密钥不能为空")
        self._hmac_key = key
        self._require_hmac = True

    # -- 接收 ---------------------------------------------------------------

    async def recv_message(self) -> Message | None:
        """阻塞等待下一条领域消息（按会话安全状态自动验签）。

        Returns:
            领域消息；流终止时返回 ``None``。
        """
        while True:
            before = len(self._buffer.buffer)
            packet = self._buffer.pop_packet(hmac_key=self._hmac_key, require_hmac=self._require_hmac)
            if packet is not None:
                try:
                    return decode_packet(packet)
                except ProtocolError as exc:
                    self._log.warning("frame_payload_decode_failed", error=str(exc))
                    continue
            if len(self._buffer.buffer) != before:
                # pop 消费了字节但帧无效（CRC/HMAC/魔数重同步丢帧）：缓冲里
                # 可能已有下一条完整帧，必须继续 pop 而非 await read —— 否则
                # "坏帧+好帧同包到达"会让好帧永远滞留（真实停摆缺陷，测试锁定）。
                continue
            if self._closed:
                return None
            try:
                data = await self._reader.read(READ_CHUNK_SIZE)
            except OSError as exc:  # ConnectionResetError 等均派生自 OSError
                self._log.debug("stream_read_failed", error=repr(exc))
                return None
            if not data:
                return None
            self._buffer.append(data)

    # -- 发送 ---------------------------------------------------------------

    async def send_message(self, msg: Message, *, compress: bool = False) -> None:
        """编码并发送一条领域消息（会话开启 HMAC 后自动签名）。

        Raises:
            OSError: 连接断开/重置（上抛，由调用方处理关闭）。
        """
        data = encode_frame(msg, hmac_key=self._hmac_key, compress=compress)
        self._writer.write(data)
        await self._writer.drain()

    async def send_raw(self, data: bytes) -> None:
        """写入已编码帧字节并等待缓冲排空。

        Raises:
            OSError: 连接断开/重置。
        """
        self._writer.write(data)
        await self._writer.drain()

    # -- 元数据与关闭 ---------------------------------------------------------

    @property
    def peer_certificate_der(self) -> bytes | None:
        """TLS 对端证书 DER（非 TLS 连接或未握手时为 ``None``）。"""
        ssl_object = self._writer.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        cert: bytes | None = ssl_object.getpeercert(binary_form=True)
        return cert

    @property
    def peer_host(self) -> str | None:
        """对端地址（诊断/审计用）。"""
        peer = self._writer.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        return None

    def close(self) -> None:
        """关闭连接（幂等）：关闭写端并通知读端 EOF，唤醒挂起的接收协程。"""
        if self._closed:
            return
        self._closed = True
        # asyncio 真实 socket 关闭后 reader 会收到 EOF；FakeWriter 场景需显式投，
        # 因此两种实现统一在此 feed_eof（对已 EOF 的 reader 重复投无害）。
        self._reader.feed_eof()
        try:
            self._writer.close()
        except OSError as exc:  # 关闭失败不阻断——句柄随即丢弃，记录以便排查泄漏
            self._log.debug("stream_close_failed", error=repr(exc))

    async def wait_closed(self) -> None:
        """等待关闭完成（:meth:`close` 之后调用）。"""
        try:
            await self._writer.wait_closed()
        except OSError as exc:  # ssl.SSLError 派生自 OSError；close_notify 竞态噪音仅记日志
            self._log.debug("stream_wait_closed_failed", error=repr(exc))


class StreamChannel:
    """:class:`FrameStream` + 后台接收任务 + 消息队列（:class:`Channel` 实现基座）。

    控制/画面通道的客户端侧与服务端会话共用本类：

    - 客户端子类先以无流形式构造，:meth:`connect` 建立并握手后调用
      :meth:`bind_stream` 绑定；服务端会话直接带流构造。
    - :meth:`start_receiving` 启动后台循环，把收到的消息投递到队列；
    - :meth:`next_message` 从队列取（``None`` = 对端关闭/本端关闭）；
    - :meth:`send` 直接经流发送（自动 HMAC 签名）；写失败上抛 ``OSError``
      并置断开状态（不静默）。

    Args:
        stream: 已建立并完成握手的帧流；``None`` 表示待子类 connect 后绑定。
    """

    def __init__(self, stream: FrameStream | None = None) -> None:
        self._stream: FrameStream | None = stream
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None
        self._connected = stream is not None

    def bind_stream(self, stream: FrameStream) -> None:
        """绑定真实帧流（仅允许一次；:meth:`connect` 内调用）。

        Raises:
            RuntimeError: 已绑定过。
        """
        if self._stream is not None:
            raise RuntimeError("StreamChannel 已绑定帧流")
        self._stream = stream
        self._connected = True

    @property
    def stream(self) -> FrameStream:
        """底层帧流；未连接时抛 :class:`ConnectionError`。

        Raises:
            ConnectionError: 帧流尚未绑定/已释放。
        """
        if self._stream is None:
            raise ConnectionError("通道尚未连接")
        return self._stream

    async def connect(self) -> None:
        """确认通道就绪（实现 :class:`Channel` 协议）。

        服务端会话（构造时已带流）为幂等 no-op；客户端通道子类覆写本方法
        完成"建连 + TLS + 握手"。基类无流可绑定时抛错提示用法错误。

        Raises:
            ConnectionError: 帧流尚未绑定且子类未覆写。
        """
        if self._stream is None:
            raise ConnectionError("基类 StreamChannel 无建连能力：请使用客户端通道子类或服务端会话")

    @property
    def recv_task(self) -> asyncio.Task[None] | None:
        """后台接收任务。"""
        return self._recv_task

    @property
    def is_connected(self) -> bool:
        """通道是否可用（未收到 EOF 且未主动关闭）。"""
        return self._connected

    def start_receiving(self) -> None:
        """启动后台接收循环（重复调用无效果）。

        Raises:
            ConnectionError: 帧流尚未绑定。
        """
        stream = self.stream
        if self._recv_task is not None and not self._recv_task.done():
            return
        self._recv_task = asyncio.get_running_loop().create_task(
            self._recv_loop(stream), name="channel-recv"
        )

    async def _recv_loop(self, stream: FrameStream) -> None:
        try:
            while True:
                msg = await stream.recv_message()
                if msg is None:
                    break
                await self._queue.put(msg)
        except asyncio.CancelledError:  # close() 取消：正常退出
            raise
        finally:
            self._connected = False
            await self._queue.put(None)  # EOF 哨兵：next_message 得到 None

    async def next_message(self) -> Message | None:
        """取下一条消息；``None`` 表示流已终止。"""
        return await self._queue.get()

    async def send(self, msg: Message) -> None:
        """发送一条领域消息（自动签名）。

        Raises:
            ConnectionError: 帧流尚未绑定。
            OSError: 写失败（通道随即标记断开）。
        """
        stream = self.stream
        try:
            await stream.send_message(msg)
        except OSError:
            self._connected = False
            raise

    async def close(self) -> None:
        """关闭通道（幂等）：取消接收任务、关闭流、投 EOF 哨兵。"""
        if not self._connected and self._stream is None:
            return
        self._connected = False
        task = self._recv_task
        self._recv_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._stream is not None:
            self._stream.close()
            await self._stream.wait_closed()
            self._stream = None
        # 若接收协程未投递过 EOF 哨兵（被 cancel 路径），补一个避免消费者挂死
        if self._queue.empty():
            await self._queue.put(None)

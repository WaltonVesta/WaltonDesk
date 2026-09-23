"""transport 测试共享支撑：假流、内存帧通道、证书夹具辅助。

不命名为 test_*.py，pytest 不会收集本文件；由 tests/ 目录 conftest 保证
本模块可被 tests/unit/* 导入。
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

from rdc.transport.base import FrameStream
from rdc.transport.tls import build_client_ssl_context, ensure_server_cert

PEER_CERT_NOT_SET = object()  # sentinel: get_extra_info("ssl_object") 返回 None 之外的占位


class FakeWriter:
    """满足 FrameStream 写侧契约的最小实现（内存收集 + 可编程失败/阻塞）。

    Args:
        fail_on_write: write() 抛 ConnectionResetError（模拟链路断）。
        hold_drain: 提供 Event 时 drain() 阻塞至其 set（模拟慢消费/TCP 背压）。
    """

    def __init__(
        self,
        *,
        fail_on_write: bool = False,
        hold_drain: asyncio.Event | None = None,
    ) -> None:
        self.sent = bytearray()
        self.closed = False
        self.fail_on_write = fail_on_write
        self.hold_drain = hold_drain
        self.extra: dict[str, object] = {"peername": ("127.0.0.1", 54321)}
        self.drain_count = 0

    def write(self, data: bytes) -> None:
        if self.fail_on_write:
            raise ConnectionResetError("模拟写失败")
        self.sent.extend(data)

    async def drain(self) -> None:
        self.drain_count += 1
        if self.hold_drain is not None:
            await self.hold_drain.wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default=None):  # noqa: ANN001, ANN002
        return self.extra.get(name, default)


class FakeSSLObject:
    """getpeercert(binary_form=True) 返回固定 DER 的会话对象替身。"""

    def __init__(self, der: bytes | None) -> None:
        self._der = der

    def getpeercert(self, *, binary_form: bool = False):  # noqa: ANN001
        return self._der


def make_stream_pair() -> tuple[FrameStream, FrameStream, FakeWriter, FakeWriter]:
    """构造一对逻辑互通的帧流（双向均可用 :func:`pump` 手动搬运字节）。

    Returns:
        (client_stream, server_stream, client_writer, server_writer)。
    """
    client_reader = asyncio.StreamReader()
    client_writer = FakeWriter()
    server_reader = asyncio.StreamReader()
    server_writer = FakeWriter()
    client = FrameStream(client_reader, client_writer, name="t-client")
    server = FrameStream(server_reader, server_writer, name="t-server")
    return client, server, client_writer, server_writer


def pump(client_writer: FakeWriter, target: FrameStream) -> int:
    """把 client_writer 已收集字节喂入 target 流并清空（模拟网络到达）。

    Returns:
        搬运字节数。
    """
    data = bytes(client_writer.sent)
    client_writer.sent.clear()
    if data:
        target._reader.feed_data(data)  # noqa: SLF001
    return len(data)


async def run_handshake_pair(
    client_task: asyncio.Task,
    server_task: asyncio.Task,
    cw: FakeWriter,
    sw: FakeWriter,
    client: FrameStream,
    server: FrameStream,
    *,
    rounds: int = 50,
) -> None:
    """交叉搬运两侧字节直到两个任务都结束（握手驱动）。

    结束后向两个 reader 投 EOF：真实场景里服务端会话处理协程在握手抛
    ``AuthError`` 后会关闭连接，客户端据此得到"对端关闭"而非永久等待；
    测试若省略这步，失败路径上仍在等响应的任务会挂死事件循环。
    """
    for _ in range(rounds):
        await asyncio.sleep(0)
        pump(cw, server)
        pump(sw, client)
        if client_task.done() and server_task.done():
            break
    # 收尾一程，让最后的响应送达
    pump(cw, server)
    pump(sw, client)
    client._reader.feed_eof()  # noqa: SLF001
    server._reader.feed_eof()  # noqa: SLF001


def gen_cert(tmp: Path) -> tuple[Path, Path]:
    """在临时目录生成服务端自签名证书，返回 (cert_path, key_path)。"""
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    ensure_server_cert(cert_path, key_path)
    return cert_path, key_path


def client_tls_context() -> ssl.SSLContext:
    """客户端不校验证书但保留 TLS 1.2 下限（集成测试 TOFU 用）。"""
    return build_client_ssl_context(verify=False)

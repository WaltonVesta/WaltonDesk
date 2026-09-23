"""被控端接入（阶段 4）：asyncio TLS 服务器 + 会话建立。

本模块只提供**传输层会话建立**：监听、TLS 服务端握手、认证/加入、HMAC 开启，
产出 :class:`ControlSession` / :class:`ScreenSession`（均为
:class:`~rdc.transport.base.StreamChannel` 实现）。业务分发（输入执行、
画面订阅注册等）属于 server 层职责（阶段 9 拆分时接入 broadcaster）。

与 v4 ``service._accept_loop`` 的对应：

- ``socket.accept`` 线程 + 线程池 → :func:`asyncio.start_server` 回调；
- 每客户端连接即一个协程（无池、无锁、无线程数上限配置）；
- 连接数上限由 :class:`asyncio.Semaphore` 实现（等价 v4 Semaphore，
  满额直接拒绝新连接）。

口令为空（v4 未设密码语义：``verify_password`` 空哈希 → 任意口令通过）时，
v5 仍然走完整握手：令牌照常签发、nonce 照常交换、HMAC 照常强制——
"不认证"只影响口令校验，不降级传输安全。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from rdc.exceptions import AuthError, TlsError
from rdc.logging import get_logger
from rdc.transport.base import FrameStream, StreamChannel
from rdc.transport.handshake import HANDSHAKE_TIMEOUT, serve_control_handshake, serve_screen_join
from rdc.transport.session_token import SessionTokenStore
from rdc.transport.tls import build_server_ssl_context

__all__ = ["ControlSession", "ScreenSession", "TransportServer"]

_LOG = get_logger("transport.server")

#: 会话建立回调：``async fn(session) -> None``；``session.next_message()`` 收到
#: ``None``（对端关闭）时应尽快返回，:meth:`TransportServer.stop` 会兜底取消。
SessionHook = Callable[[StreamChannel], "Awaitable[None]"]


class ControlSession(StreamChannel):
    """被控端视角的控制通道会话（握手完成后交付业务层）。"""

    def __init__(self, stream: FrameStream, client_ip: str, hmac_key: bytes) -> None:
        super().__init__(stream)
        self.client_ip = client_ip
        self.hmac_key = hmac_key


class ScreenSession(StreamChannel):
    """被控端视角的画面通道会话（注册 broadcaster 后持续 publish）。"""

    def __init__(self, stream: FrameStream, client_ip: str, hmac_key: bytes) -> None:
        super().__init__(stream)
        self.client_ip = client_ip
        self.hmac_key = hmac_key


class TransportServer:
    """TLS 接入服务器：控制端口 + 画面端口各一（对齐 v4 双端口模型）。

    Args:
        host: 绑定地址。
        control_port / screen_port: 两端口。
        cert_path / key_path: 服务端证书与私钥（:func:`ensure_server_cert` 产物）。
        password_hash: 服务端 bcrypt 哈希（空串 = 未设密码，任意口令通过）。
        tokens: 会话令牌存储（画面通道一次性消费）。
        max_connections: 并发会话上限（信号量，v4 语义）。
        on_control_session / on_screen_session: 会话建立后的业务回调。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        control_port: int,
        screen_port: int,
        cert_path: Path,
        key_path: Path,
        password_hash: str = "",
        tokens: SessionTokenStore | None = None,
        max_connections: int = 100,
        on_control_session: SessionHook | None = None,
        on_screen_session: SessionHook | None = None,
    ) -> None:
        self._host = host
        self._control_port = control_port
        self._screen_port = screen_port
        self._ssl_ctx = build_server_ssl_context(cert_path, key_path)
        self._password_hash = password_hash
        self._tokens = tokens or SessionTokenStore()
        self._sem = asyncio.Semaphore(max_connections)
        self._on_control = on_control_session
        self._on_screen = on_screen_session
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._sessions: set[StreamChannel] = set()
        self._log = get_logger("transport.server")

    # -- 生命周期 -------------------------------------------------------------

    async def start(self) -> None:
        """开始监听两个端口（非阻塞；处理协程按连接派生并跟踪）。"""
        ctrl = await asyncio.start_server(
            self._dispatch(self._handle_control), self._host, self._control_port, ssl=self._ssl_ctx
        )
        scr = await asyncio.start_server(
            self._dispatch(self._handle_screen), self._host, self._screen_port, ssl=self._ssl_ctx
        )
        self._servers = [ctrl, scr]
        self._log.info(
            "transport_server_started", control_port=self._control_port, screen_port=self._screen_port
        )

    async def stop(self) -> None:
        """停止监听、关闭全部活动会话并回收连接任务（幂等）。

        顺序：①关监听器（拒绝新连接）→ ②关闭会话（投 EOF 哨兵唤醒 hook）
        → ③取消仍未结束的连接任务并等待收尾。
        """
        for server in self._servers:
            server.close()
        for server in self._servers:
            try:
                await server.wait_closed()
            except OSError as exc:  # 关闭竞态噪音：记录不吞（约束 6）
                self._log.debug("server_wait_closed_failed", error=repr(exc))
        self._servers = []
        sessions = list(self._sessions)
        self._sessions.clear()
        for session in sessions:
            try:
                await session.close()
            except (OSError, TlsError) as exc:  # 会话关闭竞态：记录，继续收尾其他会话
                self._log.debug("session_close_failed", error=repr(exc))
        tasks = [t for t in self._tasks if not t.done()]
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except (OSError, TlsError) as exc:
                self._log.debug("connection_task_cleanup_failed", error=repr(exc))

    @property
    def listening(self) -> bool:
        """是否处于监听状态。"""
        return bool(self._servers)

    # -- 连接处理 -------------------------------------------------------------

    def _dispatch(
        self,
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]:
        """把连接回调包装为**可跟踪任务**（stop 时统一取消收尾）。"""

        def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            async def run_handler() -> None:
                await handler(reader, writer)

            task: asyncio.Task[None] = asyncio.get_running_loop().create_task(run_handler())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return client_connected

    async def _handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """控制端口：TLS 已由 asyncio 完成 → 认证握手 → 会话交付。"""
        peer = writer.get_extra_info("peername")
        client_ip = str(peer[0]) if peer else "?"
        if self._sem.locked():  # 已达并发上限：立即拒绝（v4 acquire(blocking=False) 语义）
            self._log.warning("connection_refused_limit", client=client_ip)
            writer.close()
            return
        async with self._sem:
            stream = FrameStream(reader, writer, name="control")
            try:
                key = await asyncio.wait_for(
                    serve_control_handshake(
                        stream,
                        client_ip=client_ip,
                        password_hash=self._password_hash,
                        issue_token=self._tokens.issue,
                    ),
                    timeout=HANDSHAKE_TIMEOUT,
                )
            except (TimeoutError, AuthError, TlsError, OSError) as exc:
                self._log.warning("control_handshake_rejected", client=client_ip, error=str(exc))
                stream.close()
                await stream.wait_closed()
                return
            session = ControlSession(stream, client_ip, key)
            session.start_receiving()
            self._sessions.add(session)
            try:
                await self._run_hook(self._on_control, session, client_ip, "control_session_hook_failed")
            finally:
                self._sessions.discard(session)
                await session.close()

    async def _handle_screen(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """画面端口：TLS → ``ScreenJoin`` 令牌消费 → 会话交付（broadcaster 注册点）。"""
        peer = writer.get_extra_info("peername")
        client_ip = str(peer[0]) if peer else "?"
        if self._sem.locked():
            self._log.warning("connection_refused_limit", client=client_ip)
            writer.close()
            return
        async with self._sem:
            stream = FrameStream(reader, writer, name="screen")
            try:
                key = await asyncio.wait_for(
                    serve_screen_join(
                        stream,
                        client_ip=client_ip,
                        consume_token=self._tokens.consume,
                    ),
                    timeout=HANDSHAKE_TIMEOUT,
                )
            except (TimeoutError, AuthError, TlsError, OSError) as exc:
                self._log.warning("screen_join_rejected", client=client_ip, error=str(exc))
                stream.close()
                await stream.wait_closed()
                return
            session = ScreenSession(stream, client_ip, key)
            session.start_receiving()
            self._sessions.add(session)
            try:
                await self._run_hook(self._on_screen, session, client_ip, "screen_session_hook_failed")
            finally:
                self._sessions.discard(session)
                await session.close()

    async def _run_hook(
        self,
        hook: SessionHook | None,
        session: StreamChannel,
        client_ip: str,
        event: str,
    ) -> None:
        """执行业务回调：异常隔离（记录不外逸，连接仍走统一清理）。"""
        if hook is None:
            return
        try:
            await hook(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 业务回调异常面不可枚举：记录并继续清理（约束 6 允许带日志的宽捕获）
            self._log.error(event, client=client_ip, error=repr(exc))

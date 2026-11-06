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
import inspect
import socket
from collections.abc import Awaitable, Callable
from math import ceil
from pathlib import Path

from rdc.exceptions import AuthError, TlsError
from rdc.logging import get_logger
from rdc.protocol.messages import AuthResponse
from rdc.transport.base import FrameStream, StreamChannel
from rdc.transport.handshake import HANDSHAKE_TIMEOUT, serve_control_handshake, serve_screen_join
from rdc.transport.session_token import SessionTokenStore
from rdc.transport.tls import build_server_ssl_context

__all__ = ["ControlSession", "ScreenSession", "TransportServer"]

_LOG = get_logger("transport.server")


def _normalize_peer_ip(raw: str) -> str:
    """v4-mapped IPv6 地址（``::ffff:127.0.0.1``）归一为裸 v4 点分。

    走查第十三批 IPv6 兼容的必然配套：全接口改双栈监听后，IPv4 连接的
    ``peername`` 会以 ``::ffff:x.x.x.x`` 形态出现。不归一的话，按 v4 配置的
    黑白名单/封禁/限流全部失配（同一台机器以两种身份记账），审计日志也与
    用户预期对不上。非 v4-mapped 形态（真实 v6 地址）原样返回。
    """
    if raw[:7].lower() == "::ffff:":
        tail = raw[7:]
        if "." in tail:  # v4-mapped 的尾段恒为点分十进制
            return tail
    return raw

#: 会话建立回调：``async fn(session) -> None``；``session.next_message()`` 收到
#: ``None``（对端关闭）时应尽快返回，:meth:`TransportServer.stop` 会兜底取消。
SessionHook = Callable[[StreamChannel], "Awaitable[None]"]

#: 连接准入回调（阶段 9 批 3 最小扩展）：``fn(client_ip) -> bool``。
#: TLS 建立后、握手帧读取**前**调用；返回 ``False`` 立即关流拒绝
#: （封禁/黑白名单/限流判定归 server 层，传输层不引入策略依赖）。
ClientCheck = Callable[[str], bool]

#: 认证失败回调（阶段 9 批 3 最小扩展）：``fn(client_ip, channel) -> None``。
#: 控制通道口令校验失败/握手协议非法、画面通道令牌无效（``AuthError``）时
#: 调用；``channel`` ∈ ``{"control", "screen"}``。**不**调用的失败：
#: TLS/网络类（连接未进入认证阶段）、握手**超时**（对端未发出任何口令尝试，
#: 计入会污染爆破防护窗口语义）。
AuthFailedHook = Callable[[str, str], None]

#: 封禁剩余秒查询（走查第四批缺陷 2）：``fn(client_ip) -> float``，返回 >0
#: 表示准入拒绝命中"封禁"及剩余秒数。接线且命中封禁时，控制端口拒绝**前**
#: 回一帧 ``AuthResponse(ok=False, banned=True, ...)`` 让客户端在弹口令框外
#: 得知被封；未接线 / 非封禁类拒绝（黑白名单、限流）维持握手前立即关流
#: ——基线语义不变（被拒 IP 不消耗 bcrypt、限流拒绝不泄露类别信息）。
BanQuery = Callable[[str], float]

#: 封禁回帧前消费对端 AuthRequest 的读取上限（秒）。
#: 走查第六批 Bug3：控制端 ``connect()`` 是 TLS → **弹口令框等真人输入** →
#: 才发 AuthRequest，故本窗口必须覆盖真人打字时间——对齐合法握手预算
#: :data:`HANDSHAKE_TIMEOUT`（旧固定 5s 短于打字耗时，真人慢输即被服务端超时
#: 裸关、客户端得 ConnectionReset，banned 帧从未送达；集成测试的口令 provider
#: 同步秒回，掩盖了该竞态）。洪水连接不发帧则超时静默关（开销与裸关同量级，
#: 且已被限流器与封禁 IP 时间窗兜住）。
BAN_NOTICE_READ_TIMEOUT = HANDSHAKE_TIMEOUT


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
        check_client: TLS 后、握手前的连接准入判定（阶段 9 批 3 扩展，
            返回 ``False`` 关流拒绝；缺省全通过）。
        on_auth_failed: 认证阶段失败通知（阶段 9 批 3 扩展，供封禁计数；
            缺省不上报）。
        ban_query: 封禁剩余秒查询（走查第四批缺陷 2，接线可选）：仅当
            ``check_client`` 拒绝且本回调返回 >0（命中封禁）时，控制端口
            回一帧 ``banned`` 认证响应告知客户端；未接线维持握手前立即
            关流基线（不消耗 bcrypt、非封禁拒绝不泄露类别）。
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
        check_client: ClientCheck | None = None,
        on_auth_failed: AuthFailedHook | None = None,
        ban_query: BanQuery | None = None,
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
        self._check_client = check_client
        self._on_auth_failed = on_auth_failed
        self._ban_query = ban_query
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._sessions: set[StreamChannel] = set()
        self._log = get_logger("transport.server")

    # -- 生命周期 -------------------------------------------------------------

    #: 视为"全部接口"的绑定值（大小写不敏感匹配 strip 后）。
    _WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*"})

    def _dualstack_sockets(self) -> list[socket.socket]:
        """为全接口绑定构造双栈监听 socket（走查第十三批 IPv6 兼容）。

        ``0.0.0.0`` 在 Windows/Linux 常规栈下**只监听 IPv4**（实测：主控端输
        IPv6 地址连不上，报超时）。本方法建 ``AF_INET6 + IPV6_V6ONLY=0`` 的
        双栈 socket 绑 ``::``——v4 连接经 v4-mapped 地址一并接收，v6 原生接
        收。任一环节不支持（纯 v6 栈机器 / WSL 关 dualstack）返回空列表，
        调用方回退常规 IPv4 路径（旧行为，可观测日志留痕）。
        """
        host = self._host.strip()
        if host.lower() not in self._WILDCARD_HOSTS:
            return []  # 显式单栈绑定（如 127.0.0.1）：尊重用户配置
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError as exc:
            self._log.debug("dualstack_socket_unavailable", error=repr(exc))
            return []
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", 0))  # 端口占位探测双栈可用性（随即换目标端口）
            sock.close()
        except OSError as exc:
            self._log.debug("dualstack_probe_failed", error=repr(exc))
            sock.close()
            return []
        out: list[socket.socket] = []
        try:
            for port in (self._control_port, self._screen_port):
                ls = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ls.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                ls.bind(("::", port))
                ls.listen(100)
                ls.setblocking(False)
                out.append(ls)
        except OSError as exc:
            for ls in out:
                ls.close()
            self._log.warning("dualstack_bind_failed_fallback_ipv4", error=repr(exc))
            return []
        return out

    async def start(self) -> None:
        """开始监听两个端口（非阻塞；处理协程按连接派生并跟踪）。

        全接口绑定优先走双栈（IPv4+IPv6 同时可连，见
        :meth:`_dualstack_sockets`）；其余绑定值走
        :func:`asyncio.start_server` 原路径（host 含 ``:`` 时 asyncio 自动
        按 AF_INET6 解析，IPv6 字面量绑定同样原生支持）。
        """
        dual = self._dualstack_sockets()
        if dual:
            ctrl = await asyncio.start_server(
                self._dispatch(self._handle_control),
                sock=dual[0],
                ssl=self._ssl_ctx,
            )
            scr = await asyncio.start_server(
                self._dispatch(self._handle_screen),
                sock=dual[1],
                ssl=self._ssl_ctx,
            )
            self._log.info("transport_server_dualstack")
        else:
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

        走查第十三批 Bug 1（顺序修正）：旧序 ①close listener+``wait_closed``
        ②关会话 ③收任务——Python 3.12+ 起 ``Server.wait_closed()`` 会等**全部
        在途 handler 任务**跑完，而控制 handler 阻塞在 ``heartbeat_timeout``
        （默认 10s）读空闲窗口里，EOF 哨兵又要等 ② 才投递 → ``stop()`` 实测
        被拖 9.9s（``HostService.pause()`` 同步卡死，期间 ``is_paused`` 恒
        False、托盘文案不动、再点仍发暂停）。新序：①``close()`` 拒新连（不
        等待）→ ②关会话投 EOF 即时唤醒 handler → ③取消并回收残余任务 →
        ④最后 ``wait_closed()``（此时 handler 已自然收尾，毫秒级）。
        """
        for server in self._servers:
            server.close()
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
        for server in self._servers:
            try:
                await server.wait_closed()
            except OSError as exc:  # 关闭竞态噪音：记录不吞（约束 6）
                self._log.debug("server_wait_closed_failed", error=repr(exc))
        self._servers = []

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

    async def _reject_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client_ip: str
    ) -> None:
        """控制端口准入拒绝出口（走查第四批缺陷 2）。

        未接线 ``ban_query`` 或拒绝原因非封禁 → 维持握手前裸关基线（不消耗
        bcrypt、不泄露拒绝类别）。命中封禁 → 握手是请求-响应式，先消费对端
        已发出的认证请求，再回一帧 ``banned`` 响应携带剩余秒，客户端据此在
        弹口令框外即获知封禁。洪水连接（不发请求）限时等待后静默关，
        开销与裸关同量级。
        """
        remaining = self._ban_query(client_ip) if self._ban_query is not None else 0.0
        if remaining <= 0:
            writer.close()
            return
        stream = FrameStream(reader, writer, name="control-reject")
        replied = False
        try:
            got = await asyncio.wait_for(
                stream.recv_message(), timeout=BAN_NOTICE_READ_TIMEOUT
            )
            if got is not None:  # 对端已发起认证，回帧可达
                await stream.send_message(
                    AuthResponse(
                        ok=False,
                        reason="banned",
                        banned=True,
                        ban_remaining_seconds=ceil(remaining),
                    )
                )
                replied = True
        except (OSError, TimeoutError):  # 写失败/请求未到：静默关，等同裸关语义
            pass
        if replied:
            # 回帧已 drain：走 FrameStream 优雅关闭（feed_eof + close + wait_closed
            # 冲刷 TLS close_notify），对齐正常握手拒绝路径，杜绝裸关截断响应。
            stream.close()
            await stream.wait_closed()
        else:
            writer.close()

    async def _handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """控制端口：TLS 已由 asyncio 完成 → 准入 → 认证握手 → 会话交付。"""
        peer = writer.get_extra_info("peername")
        client_ip = _normalize_peer_ip(str(peer[0])) if peer else "?"
        if not self._admit(client_ip):
            await self._reject_control(reader, writer, client_ip)
            return
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
            except AuthError as exc:
                self._log.warning("control_handshake_rejected", client=client_ip, error=str(exc))
                self._notify_auth_failed(client_ip, "control")
                stream.close()
                await stream.wait_closed()
                return
            except (TimeoutError, TlsError, OSError) as exc:
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
        """画面端口：TLS → 准入 → ``ScreenJoin`` 令牌消费 → 会话交付（broadcaster 注册点）。"""
        peer = writer.get_extra_info("peername")
        client_ip = _normalize_peer_ip(str(peer[0])) if peer else "?"
        if not self._admit(client_ip):
            writer.close()
            return
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
            except AuthError as exc:
                self._log.warning("screen_join_rejected", client=client_ip, error=str(exc))
                self._notify_auth_failed(client_ip, "screen")
                stream.close()
                await stream.wait_closed()
                return
            except (TimeoutError, TlsError, OSError) as exc:
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

    # -- 阶段 9 批 3 最小扩展：准入 / 认证失败上报 ----------------------------

    def _admit(self, client_ip: str) -> bool:
        """TLS 后、握手前的准入判定（无回调=全通过）。"""
        if self._check_client is None:
            return True
        try:
            verdict = self._check_client(client_ip)
        except Exception as exc:  # 准入判定异常视为拒绝（fail-closed，S1-S4 口径）
            self._log.error("client_check_failed_fail_closed", client=client_ip, error=repr(exc))
            return False
        if inspect.isawaitable(verdict):
            # 异步回调误接线（契约 ClientCheck 为同步）：拒绝并销毁协程——
            # bool(协程) 恒真是静默放行漏洞，绝不允许。
            self._log.error("client_check_returned_awaitable_fail_closed", client=client_ip)
            close = getattr(verdict, "close", None)
            if callable(close):
                close()
            return False
        return bool(verdict)

    def _notify_auth_failed(self, client_ip: str, channel: str) -> None:
        """认证失败上报（无回调=no-op；回调异常隔离，不破坏拒绝路径）。"""
        if self._on_auth_failed is None:
            return
        try:
            self._on_auth_failed(client_ip, channel)
        except Exception as exc:  # 记账失败不得掩盖原始拒绝（记录，约束 6）
            self._log.error("auth_failed_hook_error", client=client_ip, error=repr(exc))

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

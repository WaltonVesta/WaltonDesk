"""会话令牌（阶段 4，4.6）：一次性画面通道加入凭据。

安全语义（对 v4 ``uuid4 + dict[str, 过期时间]`` 的加固）：

- 生成：``secrets.token_urlsafe(32)``（256-bit 熵，v4 用的 uuid4 版本 4
  同为 122-bit 随机，但 token_urlsafe 更短且明确面向会话凭据）。
- **单次使用**：``consume`` 成功即失效——重放第二次必失败（任务书验收项）。
- **绑定**：令牌签发时绑定 ``(client_ip, connection_id)``。画面通道加入时
  必须携带**同一 connection_id** 且来自**同一 IP**，否则拒绝。
  这防止"控制通道令牌泄露给第三方后被他机使用"的跨连接挪用。
- **TTL 60 秒**：基于 :func:`time.monotonic`（不受系统时钟回拨影响），
  过期即拒。

线程/任务安全：类只被单一 asyncio 事件循环内使用（服务端会话处理协程），
不加锁；``consume`` 的"取出即删除"在同一事件循环内原子。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rdc.exceptions import AuthError

__all__ = ["SESSION_TOKEN_TTL", "SessionToken", "SessionTokenStore"]

#: 令牌有效期（秒，任务书规定 60s）。
SESSION_TOKEN_TTL: Final[float] = 60.0


def _new_token() -> str:
    """生成 256-bit 熵的 URL 安全令牌字符串。"""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class SessionToken:
    """一个已签发的会话令牌（不可变记录）。"""

    value: str
    client_ip: str
    connection_id: bytes
    expires_at: float  # monotonic 时间戳


class SessionTokenStore:
    """会话令牌签发/校验存储（单次使用 + 绑定 + TTL）。

    Args:
        ttl: 有效期秒数，默认 :data:`SESSION_TOKEN_TTL`。
        clock: 单调时钟函数（测试注入假时钟；生产用 :func:`time.monotonic`）。
        token_factory: 令牌生成函数（测试注入固定值；生产用
            ``secrets.token_urlsafe(32)`` 包装）。
    """

    def __init__(
        self,
        *,
        ttl: float = SESSION_TOKEN_TTL,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = _new_token,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"ttl 必须为正，收到 {ttl}")
        self._ttl = ttl
        self._clock = clock
        self._token_factory = token_factory
        self._issued: dict[str, SessionToken] = {}
        self._consumed = 0

    def issue(self, client_ip: str, connection_id: bytes) -> str:
        """为 ``(client_ip, connection_id)`` 签发一次性令牌，返回令牌字符串。

        签发顺带清理已过期记录（惰性回收，无后台任务）。
        """
        now = self._clock()
        for stale in [t.value for t in self._issued.values() if now >= t.expires_at]:
            del self._issued[stale]
        token = SessionToken(
            value=self._token_factory(),
            client_ip=client_ip,
            connection_id=connection_id,
            expires_at=now + self._ttl,
        )
        self._issued[token.value] = token
        return token.value

    def consume(self, token: str, client_ip: str, connection_id: bytes) -> SessionToken:
        """校验并**消费**令牌（成功即永久失效）。

        Args:
            token: 待校验令牌。
            client_ip: 画面通道连接的对端 IP。
            connection_id: 画面通道加入帧携带的连接标识。

        Returns:
            被消费的令牌记录（供审计）。

        Raises:
            AuthError: 令牌未知（含已使用/已过期，**统一文案不泄露**是哪一种）、
                IP 或 connection_id 不匹配、TTL 已过。
        """
        record = self._issued.pop(token, None)
        if record is None:
            raise AuthError("会话令牌无效（未知、已使用或已过期）")
        now = self._clock()
        if now >= record.expires_at:
            raise AuthError("会话令牌无效（未知、已使用或已过期）")
        if client_ip != record.client_ip or connection_id != record.connection_id:
            # 绑定不符：令牌被挪用到其他连接。拒绝并把原记录丢弃（宁可用户重连，
            # 也不给攻击者二次尝试机会）。
            raise AuthError("会话令牌与当前连接绑定不符")
        self._consumed += 1
        return record

    def pending_count(self) -> int:
        """当前未消费且未过期的令牌数（诊断用）。"""
        now = self._clock()
        return sum(1 for t in self._issued.values() if now < t.expires_at)

    @property
    def consumed_count(self) -> int:
        """累计成功消费数（诊断/测试用）。"""
        return self._consumed

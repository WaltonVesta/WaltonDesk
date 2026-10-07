"""被控端认证与会话准入门（阶段 9 批 3 拆分，D14）。

从 v4 ``RemoteService`` 拆出的**纯判定件**（零 socket、零 Qt、可单测），
对应任务书 b 组接线：

- 口令哈希视图：``enable_auth=False`` → 暴露空哈希（v4 "未设密码任意口令
  通过"语义由 ``transport.handshake`` 的空哈希路径天然实现，握手/HMAC/token
  照常——认证开关只影响口令校验，不降级传输安全）；
- 连接准入 :meth:`HostAuth.check_client`：封禁 → 白名单 → 黑名单 → 每秒限流
  （v4 顺序为 限流→并发→黑白名单→认证期封禁检查；v5 把封禁提前到入口，
  被封 IP 连握手帧都不给发，差异记录 STAGE9 报告）；
- 认证结果接线 :meth:`on_auth_success` / :meth:`on_auth_failure`：
  审计事件 + :class:`~rdc.server.banlist.Banlist` 滑动窗口计数（批 1 交付）。

v4 对照消除：
- 白/黑名单硬读 ``Config`` 单例 → 注入 :class:`~rdc.config.settings.Settings`；
- 封禁段内联 service（累计计数、直写文件）→ 委托 ``Banlist``（滑动窗口、原子写）；
- 认证日志裸文本 → structlog 审计事件（批 2 ``AuditLogger``）。

限流复用 :class:`rdc.client.ratelimit.RateLimiter`（deque+lock，单事件循环
内使用无竞争；测试可注入替身）。
"""

from __future__ import annotations

from typing import Final

from rdc.client.ratelimit import RateLimiter
from rdc.config.settings import Settings
from rdc.logging import get_logger
from rdc.server.audit import AuditLogger
from rdc.server.banlist import Banlist

__all__ = ["HostAuth", "REASON_BANNED", "REASON_BLACKLISTED", "REASON_NOT_WHITELISTED", "REASON_RATE_LIMITED"]

_LOG = get_logger("server.auth")

#: 每秒新建连接上限（原 ``rdc.constants.CONNECTION_RATE_LIMIT``，阶段 9 双份
#: 常量清理时迁入唯一消费方；v4 同值）。
CONNECTION_RATE_LIMIT: Final = 5

#: 准入拒绝原因代码串（审计 ``conn.rejected.reason`` 值域，禁止传敏感文本）。
REASON_BANNED: Final = "banned"
REASON_NOT_WHITELISTED: Final = "not_whitelisted"
REASON_BLACKLISTED: Final = "blacklisted"
REASON_RATE_LIMITED: Final = "rate_limited"
REASON_BAD_PASSWORD: Final = "bad_password"


class HostAuth:
    """被控端认证策略（准入 + 封禁 + 审计接线）。

    Args:
        settings: 运行时配置（引用持有：字段热更对判定即时生效；
            ``max_auth_attempts``/``auth_ban_duration`` 为 ``Banlist``
            缺省构造参数，仅在未注入 ``banlist`` 时读取）。
        banlist: 封禁件；缺省按 settings 构造（路径走
            :func:`rdc.config.paths.banlist_file`）。
        audit: 审计发射器（缺省新建，走 structlog 默认管道）。
        rate_limiter: 每秒连接限流（缺省 ``CONNECTION_RATE_LIMIT``，v4 同值）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        banlist: Banlist | None = None,
        audit: AuditLogger | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit if audit is not None else AuditLogger()
        self._banlist: Banlist = banlist if banlist is not None else Banlist(
            max_attempts=settings.max_auth_attempts,
            ban_duration=float(settings.auth_ban_duration),
        )
        self._rate = rate_limiter if rate_limiter is not None else RateLimiter(
            max_connections_per_second=CONNECTION_RATE_LIMIT
        )

    # ------------------------------------------------------------------ 配置视图

    @property
    def settings(self) -> Settings:
        """当前配置对象（服务层读取其余开关用）。"""
        return self._settings

    @property
    def banlist(self) -> Banlist:
        """封禁件（测试观测/管理接口）。"""
        return self._banlist

    @property
    def audit(self) -> AuditLogger:
        """审计发射器（服务层复用同一实例接线控制操作聚合）。"""
        return self._audit

    def rate_connection_count(self) -> int:
        """限流窗口内当前连接数（诊断口径，v4 ``rate_limiter_count`` 延续）。"""
        return self._rate.get_connection_count()

    @property
    def password_hash(self) -> str:
        """交给握手的 bcrypt 哈希。

        ``enable_auth=False`` → 空串（v4 语义：未设密码任意口令通过；
        握手、token、nonce、HMAC 全部照常，传输安全不降级）。
        """
        if not self._settings.enable_auth:
            return ""
        return self._settings.password_hash

    # ------------------------------------------------------------------ 连接准入

    def check_client(self, ip: str) -> bool:
        """TLS 建立后、握手帧前的准入判定（TransportServer ``check_client``）。

        判定顺序：封禁 → 白名单 → 黑名单 → 每秒限流。
        拒绝路径发射 ``conn.rejected`` 审计（带原因代码串）。
        """
        if self._banlist.is_banned(ip):
            self._audit.conn_rejected(ip=ip, reason=REASON_BANNED)
            return False
        s = self._settings
        if s.enable_whitelist and ip not in s.whitelist_ips:
            self._audit.conn_rejected(ip=ip, reason=REASON_NOT_WHITELISTED)
            return False
        if s.enable_blacklist and ip in s.blacklist_ips:
            self._audit.conn_rejected(ip=ip, reason=REASON_BLACKLISTED)
            return False
        if not self._rate.allow():
            self._audit.conn_rejected(ip=ip, reason=REASON_RATE_LIMITED)
            return False
        return True

    def ban_remaining_seconds(self, ip: str) -> float:
        """封禁剩余秒（走查第四批缺陷 2）：供传输层准入拒绝时判定。

        命中封禁返回 >0（传输层据此回一帧 ``banned`` 认证响应）；非封禁类
        拒绝（黑白名单、限流）或未封禁返回 0.0（传输层维持握手前裸关，
        不泄露拒绝类别）。委托 :meth:`Banlist.remaining_seconds`。
        """
        return self._banlist.remaining_seconds(ip)

    # ------------------------------------------------------------------ 认证结果接线

    def on_auth_success(self, ip: str) -> None:
        """口令校验通过：清窗口计数 + 审计（握手成功后由服务调用）。"""
        self._banlist.reset(ip)
        self._audit.auth_success(ip=ip)

    def on_auth_failure(self, ip: str, reason: str = REASON_BAD_PASSWORD) -> None:
        """认证失败：滑动窗口计数，达阈值触发封禁并审计。

        ``TransportServer.on_auth_failed`` 以单参回调接入（默认口令错误）。
        """
        self._audit.auth_failure(ip=ip, reason=reason)
        if self._banlist.record_failure(ip):
            self._audit.auth_banned(
                ip=ip,
                attempts=self._settings.max_auth_attempts,
                duration_s=float(self._settings.auth_ban_duration),
            )

    # ------------------------------------------------------------------ 维护

    def cleanup_expired_bans(self) -> int:
        """到期封禁批清理（服务空闲定时器调用），返回解封数。

        解封数量事件 ``auth.unban_batch`` 由 :class:`~rdc.server.banlist.Banlist`
        自身记录；单 IP 显式解封走 :meth:`Banlist.unban` 后由调用方补
        :meth:`AuditLogger.auth_unbanned`。
        """
        return self._banlist.cleanup_expired()

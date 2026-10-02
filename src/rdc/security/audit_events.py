"""结构化审计事件定义（阶段 9.4）：事件名常量 + 载荷构造。

与 :mod:`rdc.server.audit`（发射器）分离的原因：事件词汇表属于**安全语义**
（渗透审计、事后取证按事件名过滤），必须独立于服务端装配存活——主控端、
CLI、未来 syslog 转发器都只 import 本模块即可对齐字段名，避免"字符串事件名
散落各调用点、查询时拼不出全集"。

事件命名空间（点分小写，structlog ``event`` 字段值）：

============  =============================================  ==========
类别          事件                                           触发方
============  =============================================  ==========
认证          ``auth.success`` / ``auth.failure`` /         被控端
              ``auth.banned`` / ``auth.unbanned``
连接          ``conn.open`` / ``conn.close`` /              被控端
              ``conn.rejected``（白/黑名单、超限）
控制操作      ``ctrl.mouse`` / ``ctrl.key`` /               被控端
              ``ctrl.text`` / ``ctrl.clipboard``
会话(主控)    ``session.connect`` / ``session.disconnect``  主控端
证书          ``cert.expiring`` / ``cert.expired``          双方
============  =============================================  ==========

字段约定（跨事件统一键名，查询友好）：
``ip`` / ``connection_id`` / ``reason`` / ``actor``。
口令明文、私钥材料**永不调入本模块任何函数**（接口从签名上就不接受它们）。
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["EVENTS", "audit_fields"]


class _Auth:
    SUCCESS: Final = "auth.success"
    FAILURE: Final = "auth.failure"
    BANNED: Final = "auth.banned"
    UNBANNED: Final = "auth.unbanned"


class _Conn:
    OPEN: Final = "conn.open"
    CLOSE: Final = "conn.close"
    REJECTED: Final = "conn.rejected"


class _Ctrl:
    MOUSE: Final = "ctrl.mouse"
    KEY: Final = "ctrl.key"
    TEXT: Final = "ctrl.text"
    CLIPBOARD: Final = "ctrl.clipboard"


class _Session:
    CONNECT: Final = "session.connect"
    DISCONNECT: Final = "session.disconnect"


class _Cert:
    EXPIRING: Final = "cert.expiring"
    EXPIRED: Final = "cert.expired"


class EVENTS:
    """事件名常量集合（按类别分组，IDE 可补全）。"""

    Auth = _Auth
    Conn = _Conn
    Ctrl = _Ctrl
    Session = _Session
    Cert = _Cert


#: 全部合法事件名（测试/文档核对用；运行时不强制——但新增事件须登记于此）。
ALL_EVENTS: Final[frozenset[str]] = frozenset(
    [
        _Auth.SUCCESS, _Auth.FAILURE, _Auth.BANNED, _Auth.UNBANNED,
        _Conn.OPEN, _Conn.CLOSE, _Conn.REJECTED,
        _Ctrl.MOUSE, _Ctrl.KEY, _Ctrl.TEXT, _Ctrl.CLIPBOARD,
        _Session.CONNECT, _Session.DISCONNECT,
        _Cert.EXPIRING, _Cert.EXPIRED,
    ]
)


def audit_fields(
    *,
    ip: str | None = None,
    connection_id: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造审计事件的统一字段字典（剔除 ``None``，保持键序稳定）。

    Args:
        ip: 对端 IP。
        connection_id: 会话连接 ID（hex 字符串）。
        reason: 拒绝/失败原因（自由文本，但**不得含口令/私钥**——调用方责任，
            常见值如 ``bad_password`` / ``banned`` / ``over_limit``）。
        actor: 操作者标识（``client`` / ``host`` / ``user``）。
        **extra: 事件特有字段（如 ``key=`` / ``button=``，见各调用点文档）。

    Returns:
        新字典；只含非 None 键，structlog 直接 ``**`` 展开使用。
    """
    base: dict[str, Any] = {}
    for name, value in (
        ("ip", ip),
        ("connection_id", connection_id),
        ("reason", reason),
        ("actor", actor),
    ):
        if value is not None:
            base[name] = value
    base.update(extra)
    return base

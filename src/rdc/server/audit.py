"""结构化审计发射器（阶段 9.4）：认证/连接/控制操作全覆盖审计。

事件词汇表来自 :mod:`rdc.security.audit_events`（键名/事件名单一来源），
本模块只负责**发射策略**：

- **认证/连接/证书事件**：逐条 ``info``/``warning``（低频、安全语义关键，
  渗透取证按条回溯）。
- **控制操作**（鼠标/按键/文本/剪贴板）：高频路径逐条打日志会冲垮轮转
  （30fps 鼠标批 + 每键两事件）。默认**首条 + 计数聚合**：每类事件首次
  到达记一条 ``info``，其后累加计数器；``maybe_flush()``（装配层节拍调用）
  在计数超过 ``flush_count`` 或驻留超过 ``flush_interval_s`` 时输出一条
  ``ctrl.summary``。``verbose=True`` 切逐条模式（调试/短会话用）。

发射器不持有 IO：logger 走 :func:`rdc.logging.get_logger`（structlog，
JSON 文件 + console 双 renderer，阶段 2 已就位），测试注入 capture logger。
时钟可注入（``clock``），聚合窗口全确定性测试。

**红线**：接口签名从不接受口令明文/私钥材料；``reason`` 字段只放
稳定代码串（``bad_password`` 等），调用方不得拼接敏感值。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from rdc.logging import get_logger
from rdc.security.audit_events import EVENTS, audit_fields

__all__ = ["AuditLogger", "CONTROL_EVENTS"]

#: 聚合计数覆盖的控制类事件名。
CONTROL_EVENTS: frozenset[str] = frozenset(
    {EVENTS.Ctrl.MOUSE, EVENTS.Ctrl.KEY, EVENTS.Ctrl.TEXT, EVENTS.Ctrl.CLIPBOARD}
)


class AuditLogger:
    """被控端/主控端共用的审计发射器。

    Args:
        logger: structlog 绑定 logger（默认 ``get_logger("server.audit")``；
            测试注入 capture）。
        verbose_control: ``True`` 时控制操作逐条 ``info``（不聚合）。
        flush_count: 聚合刷出阈值（任一类事件累计条数）。
        flush_interval_s: 聚合刷出时间阈值（秒）。
        clock: 单调时钟注入（测试假时钟）。
    """

    def __init__(
        self,
        *,
        logger: Any | None = None,
        verbose_control: bool = False,
        flush_count: int = 100,
        flush_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if flush_count < 1:
            raise ValueError(f"flush_count 必须 ≥1，收到 {flush_count}")
        if flush_interval_s <= 0:
            raise ValueError(f"flush_interval_s 必须 >0，收到 {flush_interval_s}")
        self._log = logger if logger is not None else get_logger("server.audit")
        self._verbose = verbose_control
        self._flush_count = flush_count
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._counts: Counter[str] = Counter()
        self._first_seen: dict[str, float] = {}

    # ------------------------------------------------------------------ 认证

    def auth_success(self, *, ip: str, connection_id: str | None = None) -> None:
        """口令校验通过。"""
        self._log.info(EVENTS.Auth.SUCCESS, **audit_fields(ip=ip, connection_id=connection_id))

    def auth_failure(self, *, ip: str, reason: str) -> None:
        """认证失败（``reason`` 为稳定代码串：bad_password/timeout/malformed…）。"""
        self._log.warning(EVENTS.Auth.FAILURE, **audit_fields(ip=ip, reason=reason))

    def auth_banned(self, *, ip: str, attempts: int, duration_s: float) -> None:
        """滑动窗口失败达阈值触发封禁。"""
        self._log.warning(
            EVENTS.Auth.BANNED, **audit_fields(ip=ip, attempts=attempts, duration_s=duration_s)
        )

    def auth_unbanned(self, *, ip: str, manual: bool = False) -> None:
        """封禁到期自动解封或管理接口手动解封。"""
        self._log.info(EVENTS.Auth.UNBANNED, **audit_fields(ip=ip, manual=manual))

    # ------------------------------------------------------------------ 连接

    def conn_open(self, *, ip: str, kind: str, connection_id: str | None = None) -> None:
        """通道建立（``kind``: control/screen）。"""
        self._log.info(EVENTS.Conn.OPEN, **audit_fields(ip=ip, connection_id=connection_id, kind=kind))

    def conn_close(self, *, ip: str, kind: str, reason: str | None = None) -> None:
        """通道关闭（主动/异常，``reason`` 代码串）。"""
        self._log.info(EVENTS.Conn.CLOSE, **audit_fields(ip=ip, kind=kind, reason=reason))

    def conn_rejected(self, *, ip: str, reason: str) -> None:
        """连接建立即拒（白/黑名单、超连接数、TLS 失败）。"""
        self._log.warning(EVENTS.Conn.REJECTED, **audit_fields(ip=ip, reason=reason))

    # ------------------------------------------------------------------ 证书

    def cert_expiring(self, *, fingerprint_prefix: str, remaining_days: int) -> None:
        """证书临期告警（只带指纹缩写，不读私钥）。"""
        self._log.warning(
            EVENTS.Cert.EXPIRING,
            **audit_fields(fp=fingerprint_prefix, remaining_days=remaining_days),
        )

    # ------------------------------------------------------------------ 会话（主控端）

    def session_connect(self, *, host: str, port: int) -> None:
        """主控端发起连接。"""
        self._log.info(EVENTS.Session.CONNECT, **audit_fields(host=host, port=port))

    def session_disconnect(self, *, host: str, reason: str | None = None) -> None:
        """主控端断开（``reason`` 代码串：user/timeout/fatal）。"""
        self._log.info(EVENTS.Session.DISCONNECT, **audit_fields(host=host, reason=reason))

    # ------------------------------------------------------------------ 控制操作

    def control(self, event: str, *, ip: str | None = None, **fields: Any) -> None:
        """记录一次控制操作（event ∈ :data:`CONTROL_EVENTS`）。

        非 verbose 模式：每类首条即时 ``info``，后续只计数，由
        :meth:`maybe_flush` 聚合输出——审计"全覆盖"（每条都进入计数），
        日志量受控。
        """
        if event not in CONTROL_EVENTS:
            raise ValueError(f"非控制类事件请走专用方法: {event!r}")
        now = self._clock()
        if self._verbose:
            self._log.info(event, **audit_fields(ip=ip, actor="client", **fields))
            return
        self._counts[event] += 1
        if event not in self._first_seen:
            self._first_seen[event] = now
            self._log.info(
                event, **audit_fields(ip=ip, actor="client", first=True, **fields)
            )

    def pending_total(self) -> int:
        """未刷出的聚合计数总和（观测面/测试）。"""
        return sum(self._counts.values())

    def maybe_flush(self) -> bool:
        """按阈值刷出聚合摘要；刷出返回 ``True``，否则 ``False``。

        装配层在画面节拍（coalesce/pipeline tick）里调用，成本 O(事件类数)。
        """
        if not self._counts:
            return False
        now = self._clock()
        oldest = min(self._first_seen.get(ev, now) for ev in self._counts)
        threshold_hit = max(self._counts.values()) >= self._flush_count
        time_hit = (now - oldest) >= self._flush_interval_s
        if not (threshold_hit or time_hit):
            return False
        self.flush()
        return True

    def flush(self) -> dict[str, int]:
        """强制刷出聚合摘要（会话结束/关停路径），返回本次计数快照。"""
        snapshot = dict(self._counts)
        if snapshot:
            fields = audit_fields(actor="client")
            fields.update(snapshot)
            self._log.info("ctrl.summary", **fields)
        self._counts.clear()
        self._first_seen.clear()
        return snapshot

"""security/audit_events + server/audit 单元测试（阶段 9.4）。"""

from __future__ import annotations

import pytest

from rdc.security.audit_events import ALL_EVENTS, EVENTS, audit_fields
from rdc.server.audit import CONTROL_EVENTS, AuditLogger


class CapturingLogger:
    """structlog 发射面替身：info/warning 记录为 dict 列表。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def _record(self, level: str, event: str, **kw: object) -> None:
        self.entries.append({"event": event, "log_level": level, **kw})

    def info(self, event: str, **kw: object) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: object) -> None:
        self._record("warning", event, **kw)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def capture_logger() -> CapturingLogger:
    return CapturingLogger()


class TestAuditFields:
    def test_none_values_dropped(self) -> None:
        assert audit_fields(ip="1.1.1.1", reason=None) == {"ip": "1.1.1.1"}

    def test_key_order_stable(self) -> None:
        fields = audit_fields(actor="client", ip="1.1.1.1")
        assert list(fields) == ["ip", "actor"]

    def test_extra_merged(self) -> None:
        fields = audit_fields(ip="x", attempts=3)
        assert fields == {"ip": "x", "attempts": 3}

    def test_all_event_names_unique_and_dotted(self) -> None:
        assert len(ALL_EVENTS) == 15
        for ev in ALL_EVENTS:
            assert "." in ev and ev == ev.lower()

    def test_control_events_subset_of_all(self) -> None:
        assert CONTROL_EVENTS <= ALL_EVENTS


class TestEmitSimple:
    """认证/连接/会话/证书事件逐条发射（structlog capture 断言）。"""

    def test_auth_success_fields(self) -> None:
        cap = capture_logger()
        AuditLogger(logger=cap).auth_success(ip="1.2.3.4", connection_id="ab12")
        (ev,) = cap.entries
        assert ev["event"] == EVENTS.Auth.SUCCESS
        assert ev["ip"] == "1.2.3.4" and ev["connection_id"] == "ab12"

    def test_auth_failure_is_warning(self) -> None:
        cap = capture_logger()
        AuditLogger(logger=cap).auth_failure(ip="1.2.3.4", reason="bad_password")
        (ev,) = cap.entries
        assert ev["log_level"] == "warning"
        assert ev["event"] == EVENTS.Auth.FAILURE and ev["reason"] == "bad_password"

    def test_ban_and_unban(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        audit.auth_banned(ip="9.9.9.9", attempts=3, duration_s=300.0)
        audit.auth_unbanned(ip="9.9.9.9")
        audit.auth_unbanned(ip="8.8.8.8", manual=True)
        assert [e["event"] for e in cap.entries] == [
            EVENTS.Auth.BANNED, EVENTS.Auth.UNBANNED, EVENTS.Auth.UNBANNED
        ]
        assert cap.entries[0]["log_level"] == "warning"
        assert cap.entries[2]["manual"] is True

    def test_conn_open_close_reject(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        audit.conn_open(ip="1.1.1.1", kind="control")
        audit.conn_close(ip="1.1.1.1", kind="control", reason="eof")
        audit.conn_rejected(ip="2.2.2.2", reason="over_limit")
        assert [e["event"] for e in cap.entries] == [
            EVENTS.Conn.OPEN, EVENTS.Conn.CLOSE, EVENTS.Conn.REJECTED
        ]
        assert cap.entries[2]["log_level"] == "warning"

    def test_session_events(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        audit.session_connect(host="10.0.0.5", port=9998)
        audit.session_disconnect(host="10.0.0.5", reason="user")
        assert cap.entries[0]["port"] == 9998
        assert cap.entries[1]["reason"] == "user"

    def test_cert_expiring_carries_abbrev_fp(self) -> None:
        cap = capture_logger()
        AuditLogger(logger=cap).cert_expiring(fingerprint_prefix="AB:CD", remaining_days=5)
        (ev,) = cap.entries
        assert ev["event"] == EVENTS.Cert.EXPIRING and ev["remaining_days"] == 5


class TestControlAggregation:
    def test_non_control_event_rejected(self) -> None:
        audit = AuditLogger(logger=capture_logger())
        with pytest.raises(ValueError, match="非控制类"):
            audit.control("auth.success")

    def test_first_emitted_then_counted(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        for _ in range(5):
            audit.control(EVENTS.Ctrl.MOUSE, ip="1.1.1.1")
        assert len(cap.entries) == 1  # 只有首条
        assert cap.entries[0]["first"] is True
        assert audit.pending_total() == 5

    def test_mixed_classes_each_have_one_first(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        audit.control(EVENTS.Ctrl.MOUSE)
        audit.control(EVENTS.Ctrl.KEY, key="a")
        audit.control(EVENTS.Ctrl.MOUSE)
        audit.control(EVENTS.Ctrl.TEXT)
        assert len(cap.entries) == 3
        assert audit.pending_total() == 4

    def test_flush_by_count(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap, flush_count=3)
        audit.control(EVENTS.Ctrl.MOUSE)
        assert audit.maybe_flush() is False
        audit.control(EVENTS.Ctrl.MOUSE)
        audit.control(EVENTS.Ctrl.MOUSE)
        assert audit.maybe_flush() is True
        assert audit.pending_total() == 0

    def test_flush_by_time(self) -> None:
        clock = FakeClock()
        cap = capture_logger()
        audit = AuditLogger(logger=cap, flush_count=1000, flush_interval_s=60.0, clock=clock)
        audit.control(EVENTS.Ctrl.KEY)
        clock.advance(59.9)
        assert audit.maybe_flush() is False
        clock.advance(0.2)
        assert audit.maybe_flush() is True

    def test_flush_summary_shape(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap, flush_count=2)
        audit.control(EVENTS.Ctrl.MOUSE)
        audit.control(EVENTS.Ctrl.MOUSE)
        audit.control(EVENTS.Ctrl.KEY)
        audit.maybe_flush()
        summary = cap.entries[-1]
        assert summary["event"] == "ctrl.summary"
        assert summary[EVENTS.Ctrl.MOUSE] == 2 and summary[EVENTS.Ctrl.KEY] == 1

    def test_flush_empty_noop(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap)
        assert audit.maybe_flush() is False
        assert audit.flush() == {}
        assert cap.entries == []

    def test_explicit_flush_clears_state(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap, flush_count=100)
        audit.control(EVENTS.Ctrl.CLIPBOARD)
        snap = audit.flush()
        assert snap == {EVENTS.Ctrl.CLIPBOARD: 1}
        assert audit.pending_total() == 0
        # 新一批重新计"首条"
        audit.control(EVENTS.Ctrl.CLIPBOARD)
        assert len(cap.entries) == 3

    def test_verbose_mode_per_event(self) -> None:
        cap = capture_logger()
        audit = AuditLogger(logger=cap, verbose_control=True)
        for _ in range(4):
            audit.control(EVENTS.Ctrl.MOUSE, ip="1.1.1.1")
        assert len(cap.entries) == 4
        assert audit.maybe_flush() is False  # verbose 不进计数器


class TestValidation:
    def test_flush_count_below_one(self) -> None:
        with pytest.raises(ValueError, match="flush_count"):
            AuditLogger(flush_count=0)

    def test_flush_interval_non_positive(self) -> None:
        with pytest.raises(ValueError, match="flush_interval_s"):
            AuditLogger(flush_interval_s=0)


class TestNoSecretsInFields:
    """红线：审计接口不存在口令/私钥参数（签名层面固化）。"""

    def test_no_password_kwarg(self) -> None:
        import inspect

        audit = AuditLogger(logger=capture_logger())
        checked = 0
        for name, fn in inspect.getmembers(audit, predicate=inspect.ismethod):
            if name.startswith("_"):
                continue
            checked += 1
            params = inspect.signature(fn).parameters
            assert not any("password" in p or "secret" in p or "key_pem" in p for p in params)
        assert checked >= 10

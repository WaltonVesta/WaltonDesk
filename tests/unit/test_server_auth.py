"""server/auth HostAuth 单元测试（阶段 9 批 3）。

真实 Banlist（persist=False + 假时钟）+ 限流替身 + 审计捕获替身，
覆盖：准入判定顺序、口令视图语义、认证结果接线、密码热更语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rdc.config.settings import Settings
from rdc.server.audit import AuditLogger
from rdc.server.auth import (
    REASON_BAD_PASSWORD,
    REASON_BANNED,
    REASON_BLACKLISTED,
    REASON_NOT_WHITELISTED,
    REASON_RATE_LIMITED,
    HostAuth,
)
from rdc.server.banlist import Banlist


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CapturingLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def _record(self, level: str, event: str, **kw: object) -> None:
        self.entries.append({"event": event, "log_level": level, **kw})

    def info(self, event: str, **kw: object) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: object) -> None:
        self._record("warning", event, **kw)


class AlwaysAllowLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def allow(self) -> bool:
        self.calls += 1
        return True

    def get_connection_count(self) -> int:
        return self.calls


class DenyLimiter:
    def allow(self) -> bool:
        return False

    def get_connection_count(self) -> int:
        return 0


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def caplog_audit() -> CapturingLogger:
    return CapturingLogger()


def make_auth(
    tmp_path: Path,
    clock: FakeClock,
    caplog_audit: CapturingLogger,
    *,
    settings: Settings | None = None,
    limiter: object | None = None,
) -> HostAuth:
    s = settings if settings is not None else Settings(enable_auth=True, password_hash="hash")
    bans = Banlist(
        path=tmp_path / "banlist.json",
        max_attempts=3,
        ban_duration=300.0,
        failure_window_s=60.0,
        clock=clock,
        persist=False,
    )
    return HostAuth(
        s,
        banlist=bans,
        audit=AuditLogger(logger=caplog_audit),
        rate_limiter=limiter if limiter is not None else AlwaysAllowLimiter(),  # type: ignore[arg-type]
    )


class TestPasswordHashView:
    def test_enabled_with_hash_returns_hash(self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        assert auth.password_hash == "hash"

    def test_disabled_returns_empty_v4_semantics(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        # enable_auth=False → 空哈希（任意口令通过，v4 语义；传输安全不降级由握手层保证）
        auth = make_auth(tmp_path, clock, caplog_audit, settings=Settings(enable_auth=False, password_hash="x"))
        assert auth.password_hash == ""

    def test_enabled_empty_hash_stays_empty(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit, settings=Settings(enable_auth=True, password_hash=""))
        assert auth.password_hash == ""

    def test_settings_hot_update_visible(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        # 引用持有：settings 字段变更即时生效（restart 前读取新值的调用点）
        s = Settings(enable_auth=True, password_hash="old")
        auth = make_auth(tmp_path, clock, caplog_audit, settings=s)
        s.password_hash = "new"
        assert auth.password_hash == "new"


class TestCheckClient:
    def test_default_allow(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        assert auth.check_client("1.2.3.4") is True
        assert caplog_audit.entries == []

    def test_banned_first_and_audited(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        for _ in range(3):
            auth.on_auth_failure("9.9.9.9")
        caplog_audit.entries.clear()
        assert auth.check_client("9.9.9.9") is False
        (ev,) = caplog_audit.entries
        assert ev["event"] == "conn.rejected" and ev["reason"] == REASON_BANNED

    def test_banned_beats_whitelist(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        # 判定顺序：封禁 → 白名单——被封 IP 即使在白名单也拒（安全优先）
        s = Settings(enable_whitelist=True, whitelist_ips=["9.9.9.9"])
        auth = make_auth(tmp_path, clock, caplog_audit, settings=s)
        for _ in range(3):
            auth.on_auth_failure("9.9.9.9")
        assert auth.check_client("9.9.9.9") is False

    def test_whitelist_rejects_unknown(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        s = Settings(enable_whitelist=True, whitelist_ips=["10.0.0.1"])
        auth = make_auth(tmp_path, clock, caplog_audit, settings=s)
        assert auth.check_client("10.0.0.2") is False
        assert caplog_audit.entries[-1]["reason"] == REASON_NOT_WHITELISTED
        assert auth.check_client("10.0.0.1") is True

    def test_blacklist_rejects_known(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        s = Settings(enable_blacklist=True, blacklist_ips=["6.6.6.6"])
        auth = make_auth(tmp_path, clock, caplog_audit, settings=s)
        assert auth.check_client("6.6.6.6") is False
        assert caplog_audit.entries[-1]["reason"] == REASON_BLACKLISTED
        assert auth.check_client("7.7.7.7") is True

    def test_rate_limited(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit, limiter=DenyLimiter())
        assert auth.check_client("1.1.1.1") is False
        assert caplog_audit.entries[-1]["reason"] == REASON_RATE_LIMITED


class TestAuthResultHooks:
    def test_failure_audits_then_bans_at_threshold(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        auth.on_auth_failure("1.2.3.4")
        auth.on_auth_failure("1.2.3.4")
        assert [e["event"] for e in caplog_audit.entries] == [
            "auth.failure", "auth.failure"
        ]
        assert auth.check_client("1.2.3.4") is True  # 2/3 未达阈值
        auth.on_auth_failure("1.2.3.4")
        names = [e["event"] for e in caplog_audit.entries]
        assert names[-2:] == ["auth.failure", "auth.banned"]
        assert auth.check_client("1.2.3.4") is False

    def test_failure_custom_reason(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        auth.on_auth_failure("1.2.3.4", "token_invalid")
        assert caplog_audit.entries[0]["reason"] == "token_invalid"

    def test_success_resets_window_and_audits(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        auth.on_auth_failure("1.2.3.4")
        auth.on_auth_failure("1.2.3.4")
        auth.on_auth_success("1.2.3.4")
        assert auth.banlist.failure_count("1.2.3.4") == 0
        assert caplog_audit.entries[-1]["event"] == "auth.success"
        # 计数清零后重新累计：再失败 2 次仍不触发封禁
        auth.on_auth_failure("1.2.3.4")
        auth.on_auth_failure("1.2.3.4")
        assert auth.check_client("1.2.3.4") is True

    def test_ban_expiry_unbans_via_check(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        for _ in range(3):
            auth.on_auth_failure("1.2.3.4")
        assert auth.check_client("1.2.3.4") is False
        clock.advance(301)
        assert auth.check_client("1.2.3.4") is True

    def test_cleanup_expired_bans(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        auth = make_auth(tmp_path, clock, caplog_audit)
        for _ in range(3):
            auth.on_auth_failure("1.2.3.4")
        clock.advance(301)
        assert auth.cleanup_expired_bans() == 1


class TestRateObservation:
    def test_rate_connection_count_delegates(
        self, tmp_path: Path, clock: FakeClock, caplog_audit: CapturingLogger
    ) -> None:
        limiter = AlwaysAllowLimiter()
        auth = make_auth(tmp_path, clock, caplog_audit, limiter=limiter)
        auth.check_client("1.1.1.1")
        assert auth.rate_connection_count() == 1


class TestDefaultConstruction:
    def test_default_banlist_from_settings(self, tmp_path: Path) -> None:
        # 缺省 Banlist 走 paths.banlist_file()——重定向到 tmp 避免污染真实目录
        from rdc.config import paths

        paths.set_override_root(tmp_path)
        try:
            auth = HostAuth(Settings(max_auth_attempts=5, auth_ban_duration=60))
            snap = auth.banlist.snapshot()
            assert snap["max_attempts"] == 5 and snap["ban_duration"] == 60.0
        finally:
            paths.set_override_root(None)

    def test_reason_constants_distinct(self) -> None:
        reasons = {REASON_BANNED, REASON_NOT_WHITELISTED, REASON_BLACKLISTED, REASON_RATE_LIMITED, REASON_BAD_PASSWORD}
        assert len(reasons) == 5

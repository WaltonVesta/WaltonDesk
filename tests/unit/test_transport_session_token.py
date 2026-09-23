"""transport.session_token 测试：一次性 + 绑定 + TTL（含令牌重放）。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from rdc.exceptions import AuthError
from rdc.transport.session_token import SESSION_TOKEN_TTL, SessionTokenStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> SessionTokenStore:
    return SessionTokenStore(clock=clock, token_factory=lambda: "tok-fixed")


class TestIssueAndConsume:
    def test_token_length_and_entropy(self) -> None:
        st = SessionTokenStore()
        tok = st.issue("1.2.3.4", b"c" * 32)
        assert len(tok) >= 43  # urlsafe(32) 至少 43 字符
        # 默认工厂两次签发不同值（真实随机）
        assert tok != st.issue("1.2.3.4", b"c" * 32)

    def test_valid_consume(self, store: SessionTokenStore) -> None:
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        rec = store.consume(tok, "10.0.0.1", cid)
        assert rec.client_ip == "10.0.0.1"
        assert store.consumed_count == 1

    def test_replay_second_use_fails(self, store: SessionTokenStore) -> None:
        """验收项：令牌第二次使用必失败。"""
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        store.consume(tok, "10.0.0.1", cid)
        with pytest.raises(AuthError):
            store.consume(tok, "10.0.0.1", cid)

    def test_wrong_connection_id_rejected_and_burns_token(
        self, store: SessionTokenStore
    ) -> None:
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        with pytest.raises(AuthError):
            store.consume(tok, "10.0.0.1", b"x" * 32)
        # 绑定失败后原令牌也被消费掉，不给攻击者第二次机会
        with pytest.raises(AuthError):
            store.consume(tok, "10.0.0.1", cid)

    def test_wrong_ip_rejected(self, store: SessionTokenStore) -> None:
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        with pytest.raises(AuthError):
            store.consume(tok, "10.9.9.9", cid)

    def test_expired_token_rejected(self, store: SessionTokenStore, clock: FakeClock) -> None:
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        clock.advance(SESSION_TOKEN_TTL + 1)
        with pytest.raises(AuthError):
            store.consume(tok, "10.0.0.1", cid)

    def test_boundary_ttl(self, store: SessionTokenStore, clock: FakeClock) -> None:
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        clock.advance(SESSION_TOKEN_TTL - 0.001)
        assert store.consume(tok, "10.0.0.1", cid) is not None

    def test_expired_message_uniform_no_oracle(
        self, store: SessionTokenStore, clock: FakeClock
    ) -> None:
        """未知 / 已用 / 过期三类失败必须同一文案，不给重放者区分信号。"""
        cid = b"c" * 32
        tok = store.issue("10.0.0.1", cid)
        store.consume(tok, "10.0.0.1", cid)
        msg_used = _err_text(lambda: store.consume(tok, "10.0.0.1", cid))
        msg_unknown = _err_text(lambda: store.consume("nope", "10.0.0.1", cid))
        tok2 = store.issue("10.0.0.1", cid)
        clock.advance(SESSION_TOKEN_TTL + 1)
        msg_expired = _err_text(lambda: store.consume(tok2, "10.0.0.1", cid))
        assert msg_used == msg_unknown == msg_expired

    def test_issue_sweeps_expired(self, clock: FakeClock) -> None:
        store = SessionTokenStore(clock=clock)
        store.issue("a", b"1" * 32)
        clock.advance(SESSION_TOKEN_TTL + 1)
        store.issue("b", b"2" * 32)
        assert store.pending_count() == 1  # 旧令牌已被回收

    def test_invalid_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl"):
            SessionTokenStore(ttl=0)


def _err_text(fn: Callable[[], object]) -> str:
    try:
        fn()
    except AuthError as exc:
        return str(exc)
    raise AssertionError("expected AuthError")

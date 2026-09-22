"""``rdc.secrets.factory`` 平台探测与降级链测试（阶段 2，2.3/2.9）。

工厂契约：
1. 原生后端可用 → 直接选中，``degraded=False``；
2. 原生探测 False 或抛错 → 降级 Fernet，``degraded=True`` 且 ``reason`` 非空；
3. 降级链顺序由 sys.platform 决定（win32→dpapi / darwin→keychain / 其余→libsecret）；
4. Fernet 候选必须携带口令提供器（历史 bug 回归：曾漏传导致兜底必崩）。
"""

from __future__ import annotations

import sys

import pytest

from rdc.exceptions import SecretStoreError
from rdc.secrets import factory
from rdc.secrets.factory import StoreSelection, get_secret_store
from rdc.secrets.fernet_fallback import FernetFallbackStore


class _FakeStore:
    def __init__(self, name: str, *, available: bool = True, raises: bool = False) -> None:
        self._name = name
        self._available = available
        self._raises = raises

    @property
    def name(self) -> str:
        return self._name

    def available(self) -> bool:
        if self._raises:
            raise SecretStoreError(f"{self._name}: 探测炸了")
        return self._available

    def save(self, key: str, value: str) -> None: ...
    def load(self, key: str) -> str | None: return None
    def delete(self, key: str) -> None: ...


@pytest.fixture
def win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


class TestCandidates:
    def test_candidates_order_windows(self, win32) -> None:
        names = [s.name for s in factory._candidates(lambda: "pw")]
        assert names == ["dpapi", "fernet"]

    def test_candidates_order_macos(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        names = [s.name for s in factory._candidates(lambda: "pw")]
        assert names == ["keychain", "fernet"]

    def test_candidates_order_linux(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        names = [s.name for s in factory._candidates(lambda: "pw")]
        assert names == ["libsecret", "fernet"]

    def test_candidates_fernet_receives_passphrase(self, win32) -> None:
        """回归：兜底候选必须带上口令提供器，否则降级后 save/load 必炸。"""
        provider = lambda: "pw"  # noqa: E731
        fb = factory._candidates(provider)[1]
        assert isinstance(fb, FernetFallbackStore)
        assert fb._provider is provider


class TestSelection:
    def test_native_available_selected(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        fake = _FakeStore("dpapi", available=True)
        monkeypatch.setattr(factory, "_candidates", lambda _p: [fake, _FakeStore("fernet")])
        sel = get_secret_store(lambda: "pw")
        assert sel.store is fake
        assert sel.degraded is False

    def test_native_unavailable_degrades_to_fernet(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        dead = _FakeStore("dpapi", available=False)
        fb = _FakeStore("fernet", available=True)
        monkeypatch.setattr(factory, "_candidates", lambda _p: [dead, fb])
        sel = get_secret_store(lambda: "pw")
        assert sel.store is fb
        assert sel.degraded is True
        assert "dpapi" in sel.reason

    def test_native_raises_still_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        boom = _FakeStore("libsecret", raises=True)
        fb = _FakeStore("fernet", available=True)
        monkeypatch.setattr(factory, "_candidates", lambda _p: [boom, fb])
        sel = get_secret_store(lambda: "pw")
        assert sel.store is fb
        assert "探测炸了" in sel.reason

    def test_all_unavailable_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        both = [_FakeStore("dpapi", available=False), _FakeStore("fernet", available=False)]
        monkeypatch.setattr(factory, "_candidates", lambda _p: both)
        with pytest.raises(SecretStoreError, match="无任何可用后端"):
            get_secret_store(lambda: "pw")

    def test_selection_is_frozen_dataclass(self) -> None:
        import dataclasses

        sel = StoreSelection(store=_FakeStore("x"), degraded=False, reason="")
        with pytest.raises(dataclasses.FrozenInstanceError):
            sel.degraded = True  # type: ignore[misc]


class TestWindowsRealChain:
    """Windows 本机真实链路：DpapiStore 应被直接选中（非降级）。"""

    def test_get_secret_store_real_windows_picks_dpapi(self, win32, tmp_app_dir) -> None:
        sel = get_secret_store(lambda: "pw")
        assert sel.store.name == "dpapi"
        assert sel.degraded is False

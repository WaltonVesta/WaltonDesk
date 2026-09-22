"""``rdc.secrets.dpapi`` 测试（阶段 2，2.4/2.9）。

Windows 本机跑**真实** crypt32（这正是"本机可验证部分"）；
非 Windows 平台（CI ubuntu/mac）自动 skip 真实往返，但仍测平台拒绝分支。
"""

from __future__ import annotations

import sys

import pytest

from rdc.exceptions import SecretStoreError
from rdc.secrets import dpapi as d

_is_windows = sys.platform == "win32"
win32_only = pytest.mark.skipif(not _is_windows, reason="DPAPI 仅 Windows 真实可测")


class TestEntropy:
    def test_dpapi_entropy_matches_v4_literal(self) -> None:
        """熵字符串是 v4 存量兼容的生命线，锁死为历史值（结尾 v3）。"""
        assert d.DPAPI_ENTROPY == b"RemoteDesktop-Control-v3"

    def test_dpapi_protect_rejects_non_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SecretStoreError, match="非 Windows"):
            d.protect(b"x")

    def test_dpapi_unprotect_rejects_non_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(SecretStoreError, match="非 Windows"):
            d.unprotect(b"x")

    def test_dpapi_available_false_off_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert d.dpapi_available() is False


@win32_only
class TestRealRoundtrip:
    """真实 crypt32 往返（Windows 本机）。"""

    def test_dpapi_protect_unprotect_roundtrip(self) -> None:
        secret = "p@ss-桌面-中文-emoji🎯".encode()
        assert d.unprotect(d.protect(secret)) == secret

    def test_dpapi_ciphertext_differs_each_call(self) -> None:
        """DPAPI 含随机 session key，同一明文两次密文不同、都能解回。"""
        ct1 = d.protect(b"same")
        ct2 = d.protect(b"same")
        assert ct1 != ct2
        assert d.unprotect(ct1) == d.unprotect(ct2) == b"same"

    def test_dpapi_unprotect_garbage_raises(self) -> None:
        with pytest.raises(SecretStoreError, match="解密失败"):
            d.unprotect(b"\x00\x01not-a-dpapi-blob")


@win32_only
class TestStore:
    """DpapiStore 文件 vault（tmp_app_dir 隔离）。"""

    def test_store_roundtrip(self, tmp_app_dir) -> None:
        store = d.DpapiStore()
        store.save("saved_password", "桌面密码")
        assert store.load("saved_password") == "桌面密码"

    def test_store_name_and_available(self, tmp_app_dir) -> None:
        store = d.DpapiStore()
        assert store.name == "dpapi"
        assert store.available() is True

    def test_store_load_missing_returns_none(self, tmp_app_dir) -> None:
        assert d.DpapiStore().load("nope") is None

    def test_store_delete(self, tmp_app_dir) -> None:
        store = d.DpapiStore()
        store.save("k", "v")
        store.delete("k")
        assert store.load("k") is None
        store.delete("k")  # 幂等

    def test_store_overwrites_existing(self, tmp_app_dir) -> None:
        store = d.DpapiStore()
        store.save("k", "v1")
        store.save("k", "v2")
        assert store.load("k") == "v2"

    def test_store_ciphertext_never_in_plaintext(self, tmp_app_dir) -> None:
        store = d.DpapiStore()
        store.save("k", "top-secret")
        raw = tmp_app_dir.joinpath("secrets.dpapi.json").read_text(encoding="utf-8")
        assert "top-secret" not in raw

    def test_store_corrupt_file_raises(self, tmp_app_dir) -> None:
        tmp_app_dir.joinpath("secrets.dpapi.json").write_text("[1]", encoding="utf-8")
        with pytest.raises(SecretStoreError, match="格式损坏"):
            d.DpapiStore().load("k")

    def test_store_invalid_base64_raises(self, tmp_app_dir) -> None:
        tmp_app_dir.joinpath("secrets.dpapi.json").write_text('{"k": "!!!"}', encoding="utf-8")
        with pytest.raises(SecretStoreError, match="base64"):
            d.DpapiStore().load("k")

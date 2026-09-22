"""``rdc.secrets.fernet_fallback`` 测试（阶段 2，2.6/2.9）。

覆盖：PBKDF2 派生确定性、vault 往返、错误口令、salt 缺失自动生成、
vault 损坏、未配置口令。派生迭代数在测试里调低（1000）避免拖慢套件，
生产默认 480k 仅作参数透传。
"""

from __future__ import annotations

import pytest

from rdc.config import paths
from rdc.exceptions import SecretStoreError
from rdc.secrets import fernet_fallback as ff


class TestDeriveKey:
    def test_derive_key_is_deterministic(self) -> None:
        key1 = ff.derive_key("pw", b"0" * 16, iterations=1000)
        key2 = ff.derive_key("pw", b"0" * 16, iterations=1000)
        assert key1 == key2

    def test_derive_key_differs_on_passphrase(self) -> None:
        base = ff.derive_key("pw-a", b"0" * 16, iterations=1000)
        other = ff.derive_key("pw-b", b"0" * 16, iterations=1000)
        assert base != other

    def test_derive_key_differs_on_salt(self) -> None:
        base = ff.derive_key("pw", b"0" * 16, iterations=1000)
        other = ff.derive_key("pw", b"1" * 16, iterations=1000)
        assert base != other

    def test_derive_key_produces_fernet_usable_key(self) -> None:
        from cryptography.fernet import Fernet

        token = Fernet(ff.derive_key("pw", b"salt" * 4, iterations=1000)).encrypt(b"hi")
        assert token


class TestVault:
    def test_fernet_store_roundtrip(self, tmp_app_dir) -> None:
        store = ff.FernetFallbackStore(lambda: "hunter2-桌面")
        store.save("saved_password", "p@ss")
        assert store.load("saved_password") == "p@ss"

    def test_fernet_store_load_missing_returns_none(self, tmp_app_dir) -> None:
        store = ff.FernetFallbackStore(lambda: "pw")
        assert store.load("nope") is None

    def test_fernet_store_delete(self, tmp_app_dir) -> None:
        store = ff.FernetFallbackStore(lambda: "pw")
        store.save("k", "v")
        store.delete("k")
        assert store.load("k") is None
        store.delete("k")  # 不存在再删：幂等

    def test_fernet_store_wrong_passphrase_raises(self, tmp_app_dir) -> None:
        ff.FernetFallbackStore(lambda: "right").save("k", "v")
        with pytest.raises(SecretStoreError, match="口令变更|解密"):
            ff.FernetFallbackStore(lambda: "wrong").load("k")

    def test_fernet_store_no_provider_raises(self, tmp_app_dir) -> None:
        store = ff.FernetFallbackStore(None)
        with pytest.raises(SecretStoreError, match="未配置口令"):
            store.save("k", "v")

    def test_fernet_store_empty_passphrase_raises(self, tmp_app_dir) -> None:
        store = ff.FernetFallbackStore(lambda: "")
        with pytest.raises(SecretStoreError, match="口令为空"):
            store.save("k", "v")

    def test_fernet_salt_created_once_and_reused(self, tmp_app_dir) -> None:
        ff.FernetFallbackStore(lambda: "pw").save("k", "v")
        salt_file = tmp_app_dir / "secrets.salt"
        assert salt_file.exists()
        before = salt_file.read_bytes()
        ff.FernetFallbackStore(lambda: "pw").load("k")
        assert salt_file.read_bytes() == before

    def test_fernet_corrupt_vault_raises(self, tmp_app_dir) -> None:
        paths.secret_store_file("fernet").write_text("{bad json", encoding="utf-8")
        store = ff.FernetFallbackStore(lambda: "pw")
        with pytest.raises(SecretStoreError, match="vault|损坏"):
            store.load("k")

    def test_fernet_corrupt_salt_raises(self, tmp_app_dir) -> None:
        (tmp_app_dir / "secrets.salt").write_text("xyz-not-hex", encoding="ascii")
        with pytest.raises(SecretStoreError, match="salt"):
            ff.FernetFallbackStore(lambda: "pw").save("k", "v")

    def test_fernet_available_always_true(self, tmp_app_dir) -> None:
        assert ff.FernetFallbackStore(lambda: "pw").available() is True

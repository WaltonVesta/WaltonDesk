"""``rdc.secrets.password`` bcrypt 往返测试（阶段 2，2.9）。"""

from __future__ import annotations

import pytest

from rdc.exceptions import SecretStoreError
from rdc.secrets import password as pw


class TestHashPassword:
    def test_hash_password_roundtrip_matches(self) -> None:
        hashed = pw.hash_password("S3cret!桌面")
        assert pw.verify_password("S3cret!桌面", hashed)

    def test_hash_password_wrong_password_fails(self) -> None:
        hashed = pw.hash_password("right-pw")
        assert not pw.verify_password("wrong-pw", hashed)

    def test_hash_password_empty_returns_empty(self) -> None:
        assert pw.hash_password("") == ""

    def test_hash_password_uses_bcrypt_prefix(self) -> None:
        assert pw.hash_password("x").startswith("$2b$")

    def test_hash_password_rounds_12_embedded(self) -> None:
        assert "$12$" in pw.hash_password("x")


class TestVerifyPassword:
    def test_verify_password_no_hash_means_unset(self) -> None:
        assert pw.verify_password("anything", "") is True

    def test_verify_password_corrupt_hash_raises(self) -> None:
        """哈希格式损坏必须抛错拒绝认证，绝不静默放行（约束 6）。"""
        with pytest.raises(SecretStoreError, match="格式无效"):
            pw.verify_password("x", "not-a-bcrypt-hash")

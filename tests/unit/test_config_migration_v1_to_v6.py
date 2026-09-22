"""``rdc.config.migration.migrate`` 的 v1→v6 全链路测试（阶段 2，2.9）。

覆盖：逐级字段新增、v4 SHA256 清空、v5→v6 个人版清理（saved_password /
password_hash 直接删除）、幂等性、未来版本拒绝、非 dict 输入拒绝。
"""

from __future__ import annotations

import pytest

from rdc.config import migration
from rdc.exceptions import ConfigError


def _v1() -> dict:
    return {"version": 1, "port": 9999}


class TestFullChain:
    """v1 一路升到 v6。"""

    def test_migrate_v1_reaches_config_version(self) -> None:
        result = migration.migrate(_v1())
        assert result["version"] == migration.CONFIG_VERSION

    def test_migrate_v1_gains_network_and_auth_fields(self) -> None:
        result = migration.migrate(_v1())
        assert result["bandwidth_limit"] == 0
        assert result["max_connections"] == 10
        assert result["auth_timeout"] == 10
        assert result["whitelist_ips"] == []

    def test_migrate_v3_forces_auth_on(self) -> None:
        v3 = {"version": 3, "enable_auth": False}
        result = migration.migrate(v3)
        assert result["enable_auth"] is True

    def test_migrate_v4_clears_sha256_hash_and_legacy_tls(self) -> None:
        v4 = {
            "version": 4,
            "password_hash": "sha256$abcdef1234567890",
            "enable_encryption": False,
            "tls_cert": "x",
            "tls_key": "y",
        }
        result = migration.migrate(v4)
        assert "enable_encryption" not in result
        assert "tls_cert" not in result
        assert "tls_key" not in result
        assert result["tls_cert_generated"] == 0


class TestV5ToV6:
    """个人版清理：v5→v6 直接清空旧密码字段。"""

    def test_migrate_v5_to_v6_deletes_saved_password(self) -> None:
        v5 = {"version": 5, "saved_password": "DPAPI1:abcd", "remember_password": True}
        result = migration.migrate(v5)
        assert "saved_password" not in result
        assert result["remember_password"] is True

    def test_migrate_v5_to_v6_clears_password_hash(self) -> None:
        v5 = {"version": 5, "password_hash": "bcrypt$xyz"}
        result = migration.migrate(v5)
        assert "password_hash" not in result

    def test_migrate_v5_to_v6_sets_defaults_when_absent(self) -> None:
        result = migration.migrate({"version": 5})
        assert result["remember_password"] is False
        assert result["enable_auth"] is True


class TestIdempotentAndGuards:
    """幂等与输入防护。"""

    def test_migrate_is_idempotent_on_current_version(self) -> None:
        current = {"version": 6, "port": 5000, "jpeg_quality": 70}
        snapshot = dict(current)
        assert migration.migrate(current) == snapshot

    def test_migrate_twice_equals_once(self) -> None:
        once = migration.migrate(_v1())
        twice = migration.migrate(once)
        assert once == twice

    def test_migrate_rejects_future_version(self) -> None:
        with pytest.raises(ConfigError, match="高于当前支持"):
            migration.migrate({"version": 999})

    def test_migrate_rejects_non_dict(self) -> None:
        with pytest.raises(ConfigError, match="期望 JSON 对象"):
            migration.migrate(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_migrate_does_not_mutate_input(self) -> None:
        raw = _v1()
        migration.migrate(raw)
        assert raw == {"version": 1, "port": 9999}

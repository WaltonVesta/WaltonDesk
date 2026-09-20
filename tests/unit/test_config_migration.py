"""配置版本迁移回归测试（阶段 1）。

覆盖 ``rdc.config._v4_config.Config._migrate_config`` 的 v1→v5 完整迁移链。
这是 REFACTOR_PLAN 阶段 2 的前置安全网：阶段 2 用 Pydantic Settings 重写配置层
时，必须保证 v4 存量用户的 config.json 仍能无损迁移——本测试锁定该行为。

``_migrate_config`` 不读写实例状态（v4→v5 分支调用模块级 ``get_logger()``），
故用 ``uninitialized_config`` 裸实例即可测试，**不触碰真实 ~/.remote_desktop**。
"""

from __future__ import annotations

import pytest

from rdc.config._v4_config import CONFIG_VERSION, Config


def _v1_config() -> dict:
    """构造一个 v1 时代的最小配置（缺后续版本新增的全部字段）。"""
    return {"version": 1, "port": 9999}


class TestMigrationChain:
    """v1→v5 逐级迁移。"""

    def test_v1_migrates_to_current_version(self, uninitialized_config: Config) -> None:
        """v1 配置经迁移链后 version 升到当前版本。"""
        result = uninitialized_config._migrate_config(_v1_config(), 1)
        assert result["version"] == CONFIG_VERSION

    def test_v1_gains_v2_fields(self, uninitialized_config: Config) -> None:
        """v1→v2 新增字段全部就位（迁移后旧加密字段最终会被 v4→v5 清除）。"""
        result = uninitialized_config._migrate_config(_v1_config(), 1)
        # v2 引入的字段中，bandwidth_limit / enable_compression / max_connections 留存
        assert result["bandwidth_limit"] == 0
        assert result["enable_compression"] is True
        assert result["max_connections"] == 10

    def test_v1_gains_v3_auth_fields(self, uninitialized_config: Config) -> None:
        """v2→v3 引入认证与黑白名单字段。"""
        result = uninitialized_config._migrate_config(_v1_config(), 1)
        assert result["auth_timeout"] == 10
        assert result["max_auth_attempts"] == 3
        assert result["auth_ban_duration"] == 300
        assert result["whitelist_ips"] == []
        assert result["blacklist_ips"] == []

    def test_v3_to_v4_forces_auth_on(self, uninitialized_config: Config) -> None:
        """v3→v4 强制开启认证（消除无密码暴露风险）。"""
        v3 = {
            "version": 3, "port": 9999, "enable_auth": False,
            "password_hash": "", "auth_timeout": 10, "max_auth_attempts": 3,
            "auth_ban_duration": 300, "enable_whitelist": False, "whitelist_ips": [],
            "enable_blacklist": False, "blacklist_ips": [],
        }
        result = uninitialized_config._migrate_config(v3, 3)
        assert result["enable_auth"] is True


class TestV4ToV5:
    """v4→v5：SHA256 哈希失效 + 旧加密字段清除。"""

    def _v4_config(self, **overrides) -> dict:
        base = {
            "version": 4, "port": 9999, "enable_auth": True,
            "password_hash": "", "auth_timeout": 10, "max_auth_attempts": 3,
            "auth_ban_duration": 300, "enable_whitelist": False, "whitelist_ips": [],
            "enable_blacklist": False, "blacklist_ips": [],
            # v2 引入、v5 应清除的旧加密字段
            "enable_encryption": False, "tls_cert": "", "tls_key": "",
        }
        base.update(overrides)
        return base

    def test_old_sha256_hash_cleared(self, uninitialized_config: Config) -> None:
        """v4 的旧 SHA256 password_hash 在迁移到 v5 时被清空（强制重设）。"""
        v4 = self._v4_config(password_hash="sha256$abcdef1234567890")
        result = uninitialized_config._migrate_config(v4, 4)
        assert result["password_hash"] == ""

    def test_legacy_encryption_fields_removed(self, uninitialized_config: Config) -> None:
        """v5 移除 enable_encryption / tls_cert / tls_key 三个旧字段。"""
        result = uninitialized_config._migrate_config(self._v4_config(), 4)
        assert "enable_encryption" not in result
        assert "tls_cert" not in result
        assert "tls_key" not in result

    def test_tls_cert_generated_added(self, uninitialized_config: Config) -> None:
        """v5 新增 tls_cert_generated 字段，初值 0（未生成）。"""
        result = uninitialized_config._migrate_config(self._v4_config(), 4)
        assert result["tls_cert_generated"] == 0

    def test_empty_hash_stays_empty(self, uninitialized_config: Config) -> None:
        """原本就无密码（空哈希）时，迁移后仍为空，不误设。"""
        result = uninitialized_config._migrate_config(self._v4_config(password_hash=""), 4)
        assert result["password_hash"] == ""


class TestMigrationIdempotent:
    """迁移幂等性：已是当前版本的配置不再被改动。"""

    def test_current_version_unchanged(self, uninitialized_config: Config) -> None:
        """传入已是 CONFIG_VERSION 的配置，迁移不改变内容。"""
        current = {
            "version": CONFIG_VERSION, "port": 9999, "enable_auth": True,
            "password_hash": "bcrypt$xyz", "tls_cert_generated": 1700000000,
        }
        snapshot = dict(current)
        result = uninitialized_config._migrate_config(current, CONFIG_VERSION)
        assert result == snapshot

    def test_migration_does_not_raise_on_minimal_v1(self, uninitialized_config: Config) -> None:
        """极简 v1 配置（仅 version）走完整链不抛异常。"""
        result = uninitialized_config._migrate_config({"version": 1}, 1)
        assert result["version"] == CONFIG_VERSION

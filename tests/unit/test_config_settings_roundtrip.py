"""``rdc.config.settings`` 的模型校验、加载、原子保存测试（阶段 2，2.9）。

重点锁定三条验收相关行为：
1. v4 手写 ``_validate`` 的范围规则完整迁移为 Pydantic 校验器；
2. 磁盘坏值"重置+告警"的修复语义（apply_defaults）；
3. 旧 v4/v5 config.json 经 load_settings 无损迁移到 v6 并落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rdc.config import paths
from rdc.config import settings as s
from rdc.exceptions import ConfigError


class TestValidators:
    """字段级校验器（迁移自 v4 config.py:329-355）。"""

    def test_settings_validator_rejects_invalid_port(self) -> None:
        with pytest.raises(ValidationError, match="端口"):
            s.Settings(port=80)

    def test_settings_validator_rejects_port_above_65535(self) -> None:
        with pytest.raises(ValidationError, match="端口"):
            s.Settings(control_port=70000)

    def test_settings_validator_rejects_invalid_quality(self) -> None:
        with pytest.raises(ValidationError, match="画质"):
            s.Settings(jpeg_quality=101)

    def test_settings_validator_rejects_invalid_fps(self) -> None:
        with pytest.raises(ValidationError, match="帧率"):
            s.Settings(max_fps=200)

    def test_settings_validator_rejects_invalid_max_connections(self) -> None:
        with pytest.raises(ValidationError, match="最大连接数"):
            s.Settings(max_connections=0)

    def test_settings_validate_assignment_catches_runtime_bad_set(self) -> None:
        st = s.Settings()
        with pytest.raises(ValidationError):
            st.jpeg_quality = -5

    def test_settings_ranges_reject_inverted_quality_window(self) -> None:
        st = s.Settings(min_quality=90, max_quality=30)
        with pytest.raises(ConfigError, match="画质范围无效"):
            st.validate_ranges()

    def test_settings_ranges_reject_inverted_fps_window(self) -> None:
        st = s.Settings(min_fps=60, max_fps=10)
        with pytest.raises(ConfigError, match="帧率范围无效"):
            st.validate_ranges()

    def test_settings_ranges_reject_heartbeat_timeout_below_interval(self) -> None:
        st = s.Settings(heartbeat_interval=5.0, heartbeat_timeout=2.0)
        with pytest.raises(ConfigError, match="心跳超时"):
            st.validate_ranges()


class TestLoadSave:
    """读写与迁移。"""

    def test_load_settings_creates_default_file_when_missing(self, config_file: Path) -> None:
        st = s.load_settings(config_file)
        assert st.version == s.CONFIG_VERSION
        assert config_file.exists()
        on_disk = json.loads(config_file.read_text(encoding="utf-8"))
        assert on_disk["version"] == s.CONFIG_VERSION

    def test_save_then_load_roundtrip_preserves_values(self, config_file: Path) -> None:
        st = s.load_settings(config_file)
        st.port = 12345
        st.jpeg_quality = 88
        st.recent_hosts = ["192.168.1.5"]
        s.save_settings(st, config_file)
        reloaded = s.load_settings(config_file)
        assert reloaded.port == 12345
        assert reloaded.jpeg_quality == 88
        assert reloaded.recent_hosts == ["192.168.1.5"]

    def test_load_settings_migrates_v4_file_to_v6(self, config_file: Path) -> None:
        """真实形态的 v4 config.json（含旧字段）无损升级：业务值保留、旧密码清空。"""
        v4_data = {
            "version": 4,
            "port": 8888,
            "jpeg_quality": 72,
            "password_hash": "sha256$old",
            "saved_password": "DPAPI1:secret",
            "enable_encryption": False,
            "tls_cert": "/x.pem",
            "tls_key": "/x.key",
            "recent_hosts": ["10.0.0.1"],
        }
        config_file.write_text(json.dumps(v4_data), encoding="utf-8")
        st = s.load_settings(config_file)
        assert st.version == s.CONFIG_VERSION
        assert st.port == 8888
        assert st.jpeg_quality == 72
        assert st.recent_hosts == ["10.0.0.1"]
        # v6 清空旧哈希；阶段 9 方案 A 把 password_hash 加回 Settings（默认空串），
        # "旧密码不迁移"的语义 = 迁移后值为空，而非字段缺席。
        assert st.password_hash == ""
        assert not hasattr(st, "saved_password")

    def test_save_atomic_leaves_no_temp_files(self, tmp_app_dir: Path, config_file: Path) -> None:
        s.save_settings(s.Settings(), config_file)
        leftovers = [p.name for p in tmp_app_dir.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_load_corrupt_json_backs_up_and_returns_defaults(self, config_file: Path) -> None:
        config_file.write_text("{ not json !!", encoding="utf-8")
        st = s.load_settings(config_file)
        assert st == s.Settings()
        assert config_file.with_suffix(".json.corrupt").exists()

    def test_load_non_dict_json_raises(self, config_file: Path) -> None:
        config_file.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ConfigError, match="期望 JSON 对象"):
            s.load_settings(config_file)

    def test_load_future_version_raises(self, config_file: Path) -> None:
        config_file.write_text(json.dumps({"version": 99}), encoding="utf-8")
        with pytest.raises(ConfigError, match="高于当前支持"):
            s.load_settings(config_file)

    def test_load_repair_mode_resets_invalid_field(self, config_file: Path) -> None:
        config_file.write_text(json.dumps({"version": 6, "port": 80}), encoding="utf-8")
        st = s.load_settings(config_file)
        assert st.port == 9999  # 重置为默认

    def test_load_strict_mode_raises_on_invalid_field(self, config_file: Path) -> None:
        config_file.write_text(json.dumps({"version": 6, "port": 80}), encoding="utf-8")
        with pytest.raises(ConfigError, match="字段校验失败"):
            s.load_settings(config_file, apply_defaults=False)

    def test_load_repairs_inverted_quality_range(self, config_file: Path) -> None:
        config_file.write_text(
            json.dumps({"version": 6, "min_quality": 90, "max_quality": 10}), encoding="utf-8"
        )
        st = s.load_settings(config_file)
        assert st.min_quality <= st.max_quality

    def test_load_ignores_unknown_future_keys(self, config_file: Path) -> None:
        config_file.write_text(
            json.dumps({"version": 6, "some_unknown_key": 1}), encoding="utf-8"
        )
        st = s.load_settings(config_file)
        assert st.version == s.CONFIG_VERSION


class TestSingleton:
    """进程级单例。"""

    def test_current_settings_auto_loads_once(self, config_file: Path) -> None:
        first = s.current_settings()
        second = s.current_settings()
        assert first is second

    def test_set_current_replaces_singleton(self) -> None:
        custom = s.Settings(port=13000)
        s.set_current(custom)
        assert s.current_settings() is custom
        s.reset_for_test()


class TestPaths:
    """config/paths.py 的路径解析。"""

    def test_paths_default_root_is_home_dot_dir(self) -> None:
        paths.set_override_root(None)
        assert paths.config_dir() == Path.home() / paths.APP_DIR_NAME

    def test_paths_override_redirects_all_entries(self, tmp_app_dir: Path) -> None:
        assert paths.config_file() == tmp_app_dir / "config.json"
        assert paths.history_file() == tmp_app_dir / "history.json"
        assert paths.log_dir() == tmp_app_dir / "logs"
        assert paths.cert_dir() == tmp_app_dir / "certs"
        assert paths.secret_store_file("x") == tmp_app_dir / "secrets.x.json"

    def test_paths_ensure_dirs_creates_three_directories(self, tmp_app_dir: Path) -> None:
        paths.ensure_dirs()
        assert paths.log_dir().is_dir()
        assert paths.cert_dir().is_dir()

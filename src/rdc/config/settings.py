"""``Settings``：v5 配置模型（阶段 2，2.1）。

Pydantic v2 模型 + 带名子校验器（端口/画质/帧率/连接数范围从 v4 手写
``_validate`` 迁移而来，见 REFACTOR_PLAN §2.4）。

设计决定（个人版策略，2026-09-22）：

- **扁平 schema、v6 键名**：与 v4 ``config.json`` 共用同一文件与键名，
  旧 ``Config`` 与新 ``Settings`` 读写同一份磁盘数据不会互相覆盖；
  阶段 4/6 把调用点逐步切到本模型后再引入嵌套分组。
- **不保留兼容门面**：``_v4_config.Config`` 原样保留供旧代码使用，
  本模块不为它做任何适配。
- **旧密码不迁移**：v1-v5 配置经 :func:`rdc.config.migration.migrate`
  升 v6 时 ``password_hash`` / ``saved_password`` 直接清空（v4 无存量用户）。

用法::

    settings = load_settings()          # 读磁盘 → 迁移 → 校验 → 应用校验器
    settings.video.jpeg_quality = 75
    settings.save()                     # 原子写回 config.json
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pydantic
from pydantic import BaseModel, Field, field_validator

from rdc.config import migration, paths
from rdc.exceptions import ConfigError

__all__ = [
    "CONFIG_VERSION",
    "HostEntry",
    "Settings",
    "apply_validators",
    "current_settings",
    "load_settings",
    "reset_for_test",
]

CONFIG_VERSION = migration.CONFIG_VERSION

_PORT_RANGE = (1024, 65535)
_QUALITY_RANGE = (1, 100)
_FPS_RANGE = (1, 120)
_MAX_CONN_RANGE = (1, 100)


class HostEntry(BaseModel):
    """``hosts`` 映射表中单台被控端（沿用 v4 字段，值域宽松）。"""

    model_config = {"extra": "allow"}

    name: str = ""
    host: str = ""
    port: int | None = None


class Settings(BaseModel):
    """v6 全量配置字段（与 config.json 扁平键一一对应）。"""

    model_config = {"validate_assignment": True, "extra": "ignore"}

    version: int = CONFIG_VERSION
    # --- 网络 ---
    port: int = 9999
    control_port: int = 9998
    max_connections: int = 10
    bandwidth_limit: int = 0  # KB/s，0=不限
    enable_compression: bool = True
    # --- 画面/编码 ---
    jpeg_quality: int = 60
    min_quality: int = 30
    max_quality: int = 95
    min_fps: int = 5
    max_fps: int = 30
    use_diff: bool = True
    # --- 传输行为 ---
    heartbeat_interval: float = 3.0
    heartbeat_timeout: float = 10.0
    auto_reconnect: bool = True
    reconnect_delay: float = 2.0
    max_reconnect_attempts: int = 10
    # --- 功能开关 ---
    enable_clipboard: bool = True
    enable_file_transfer: bool = True
    language: str = "zh_CN"
    # --- 主机列表 ---
    hosts: dict[str, HostEntry] = Field(default_factory=dict)
    recent_hosts: list[str] = Field(default_factory=list)
    max_recent_hosts: int = 10
    # --- 认证（v6：password_hash 仅内存态，由 secrets.password 管理） ---
    enable_auth: bool = True
    auth_timeout: int = 10
    max_auth_attempts: int = 3
    auth_ban_duration: int = 300
    # --- TLS ---
    tls_cert_generated: int = 0
    # 用户证书（阶段 9.3）：两者均非空且校验通过 → 服务端用用户证书替代自签名；
    # 任一路径为空视为未启用（自签名路径，ensure_server_cert 兜底）。
    tls_cert_path: str = ""
    tls_key_path: str = ""
    # --- IP 过滤 ---
    enable_whitelist: bool = False
    whitelist_ips: list[str] = Field(default_factory=list)
    enable_blacklist: bool = False
    blacklist_ips: list[str] = Field(default_factory=list)
    # --- 记住密码（仅 UI 偏好；真实值在 SecretStore，不落 config.json） ---
    remember_password: bool = False

    # ------------------------------------------------------------------ #
    # 字段校验器（迁移自 v4 config.py:329-355 手写 _validate）
    # ------------------------------------------------------------------ #

    @field_validator("port", "control_port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        lo, hi = _PORT_RANGE
        if not (lo <= v <= hi):
            raise ValueError(f"端口 {v} 超出有效范围 ({lo}-{hi})")
        return v

    @field_validator("jpeg_quality")
    @classmethod
    def _check_quality(cls, v: int) -> int:
        lo, hi = _QUALITY_RANGE
        if not (lo <= v <= hi):
            raise ValueError(f"画质 {v} 超出有效范围 ({lo}-{hi})")
        return v

    @field_validator("min_fps", "max_fps")
    @classmethod
    def _check_fps(cls, v: int) -> int:
        lo, hi = _FPS_RANGE
        if not (lo <= v <= hi):
            raise ValueError(f"帧率 {v} 超出有效范围 ({lo}-{hi})")
        return v

    @field_validator("max_connections")
    @classmethod
    def _check_max_conn(cls, v: int) -> int:
        lo, hi = _MAX_CONN_RANGE
        if not (lo <= v <= hi):
            raise ValueError(f"最大连接数 {v} 超出有效范围 ({lo}-{hi})")
        return v

    def validate_ranges(self) -> None:
        """跨字段区间校验（min<=max 类关系无法用单字段校验器表达）。

        Raises:
            ConfigError: 任一区间关系无效。
        """
        checks: list[tuple[str, bool]] = [
            (f"画质范围无效: min={self.min_quality}, max={self.max_quality}",
             1 <= self.min_quality <= self.max_quality <= 100),
            (f"帧率范围无效: min={self.min_fps}, max={self.max_fps}",
             1 <= self.min_fps <= self.max_fps <= 120),
            (f"心跳超时必须大于间隔: {self.heartbeat_timeout} <= {self.heartbeat_interval}",
             self.heartbeat_timeout > self.heartbeat_interval > 0),
        ]
        invalid = [msg for msg, ok in checks if not ok]
        if invalid:
            raise ConfigError("; ".join(invalid))


def apply_validators(settings: Settings) -> list[str]:
    """逐字段应用 :class:`Settings` 的校验器，把违规值重置为默认。

    v4 对越界值只报错不修复；v5 个人版选择"重置+告警"，避免坏值反复触发。
    ``validate_ranges``（跨字段区间）的失败也计入返回的告警。

    Returns:
        人类可读的修复告警列表（空列表表示一切合法）。
    """
    warnings: list[str] = []
    defaults = Settings()
    for field in Settings.model_fields:
        if field == "version":
            continue
        current = getattr(settings, field)
        if current == getattr(defaults, field):
            continue
        try:
            Settings.model_validate({field: current})
        except pydantic.ValidationError:
            new_value = getattr(defaults, field)
            warnings.append(f"字段 {field} 值 {current!r} 无效，重置为默认 {new_value!r}")
            setattr(settings, field, new_value)
    try:
        settings.validate_ranges()
    except ConfigError as exc:
        warnings.append(f"区间校验失败，恢复默认: {exc}")
        settings.min_quality = defaults.min_quality
        settings.max_quality = defaults.max_quality
        settings.min_fps = defaults.min_fps
        settings.max_fps = defaults.max_fps
        settings.heartbeat_interval = defaults.heartbeat_interval
        settings.heartbeat_timeout = defaults.heartbeat_timeout
    return warnings


def _read_raw(config_path: Path) -> dict[str, Any] | None:
    """读取并解析 config.json；返回 None 表示需要新建默认配置。"""
    try:
        raw: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        backup = config_path.with_suffix(".json.corrupt")
        with contextlib.suppress(OSError):
            os.replace(config_path, backup)
        logging.getLogger("rdc.config").error("配置文件损坏已备份到 %s: %s", backup, exc)
        return None
    except OSError as exc:
        raise ConfigError(f"读取配置失败 {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置格式无效：期望 JSON 对象，得到 {type(raw).__name__}")
    return raw


def load_settings(
    config_path: Path | None = None,
    *,
    apply_defaults: bool = True,
) -> Settings:
    """加载配置：读盘 → v1→v6 迁移链 → Pydantic 校验 → （可选）应用校验器。

    Args:
        config_path: 覆盖配置文件路径（默认 ``paths.config_file()``，测试注入用）。
        apply_defaults: 对无效字段执行"重置+告警"（默认 True；False 时坏值直接
            抛 ``ConfigError``，供需要严格失败的场景）。

    Raises:
        ConfigError: 版本号高于当前支持版本，或（``apply_defaults=False`` 时）字段值无效。
    """
    path = config_path if config_path is not None else paths.config_file()
    raw = _read_raw(path)
    if raw is None:
        settings = Settings()
    else:
        migrated = migration.migrate(raw)
        try:
            settings = Settings.model_validate(migrated)
        except pydantic.ValidationError as exc:
            if not apply_defaults:
                raise ConfigError(f"配置字段校验失败: {exc}") from exc
            # 修复模式：把磁盘上的非法字段逐个重置为默认值后重试。
            logging.getLogger("rdc.config").warning("配置含非法字段，将重置为默认: %s", exc)
            defaults = Settings()
            fixed: dict[str, Any] = dict(migrated)
            for err in exc.errors():
                if not err["loc"]:
                    continue
                field = str(err["loc"][0])
                if field in Settings.model_fields:
                    fixed[field] = getattr(defaults, field)
            try:
                settings = Settings.model_validate(fixed)
            except pydantic.ValidationError as exc2:
                raise ConfigError(f"配置字段校验失败（修复后仍无效）: {exc2}") from exc2
    if apply_defaults:
        for msg in apply_validators(settings):
            logging.getLogger("rdc.config").warning(msg)
    else:
        try:
            settings.validate_ranges()
        except ConfigError as exc:
            raise ConfigError(f"配置无效且未启用修复模式: {exc}") from exc
    if raw is None:
        save_settings(settings, path)
    return settings


def save_settings(settings: Settings, config_path: Path | None = None) -> None:
    """原子写回磁盘（先写临时文件再 os.replace，避免中途崩溃留半截文件）。"""
    path = config_path if config_path is not None else paths.config_file()
    data = settings.model_dump(mode="json")
    data["version"] = CONFIG_VERSION
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        raise ConfigError(f"保存配置失败 {path}: {exc}") from exc


# ---------------------------------------------------------------------- #
# 进程级单例（阶段 4/6 迁移调用点时使用）
# ---------------------------------------------------------------------- #

_current: Settings | None = None


def current_settings() -> Settings:
    """返回进程级 Settings；未加载时自动 :func:`load_settings`。"""
    global _current
    if _current is None:
        _current = load_settings()
    return _current


def set_current(settings: Settings) -> None:
    """替换进程级 Settings（测试注入/配置热更新用）。"""
    global _current
    _current = settings


def reset_for_test() -> None:
    """清空单例（仅测试用）。"""
    global _current
    _current = None

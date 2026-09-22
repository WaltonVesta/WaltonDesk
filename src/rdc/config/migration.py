"""配置版本迁移链 v1→v6（阶段 2，2.2）。

纯函数 + 注册表设计（无单例、无实例状态），便于测试与逐版本推理。

与 v4 ``Config._migrate_config`` 的关系：
- v1→v5 逻辑逐条保留（字段增删、SHA256 哈希清空）；
- **v5→v6（个人版策略，2026-09-22 决定）**：``saved_password`` 字段直接删除、
  ``password_hash`` 直接清空 —— 不做三轨制存量迁移（v1-v4 从未稳定可用，
  不存在需要保密的存量用户）。用户通过 ``--set-password`` 重设即可。

版本号常量在本模块定义，``settings.py`` 与调用方从这里取，避免多处漂移。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rdc.exceptions import ConfigError
from rdc.logging import get_logger

__all__ = ["CONFIG_VERSION", "migrate"]

#: v5 配置 schema 版本（v6 删除 saved_password / password_hash）。
CONFIG_VERSION = 6


def _to_v2(c: dict[str, Any]) -> None:
    """v1→v2：新增 TLS/带宽/压缩/连接数字段（沿用 v4 语义）。"""
    c.setdefault("enable_encryption", False)
    c.setdefault("tls_cert", "")
    c.setdefault("tls_key", "")
    c.setdefault("bandwidth_limit", 0)
    c.setdefault("enable_compression", True)
    c.setdefault("max_connections", 10)


def _to_v3(c: dict[str, Any]) -> None:
    """v2→v3：新增认证与黑白名单。"""
    c.setdefault("enable_auth", False)
    c.setdefault("password_hash", "")
    c.setdefault("auth_timeout", 10)
    c.setdefault("max_auth_attempts", 3)
    c.setdefault("auth_ban_duration", 300)
    c.setdefault("enable_whitelist", False)
    c.setdefault("whitelist_ips", [])
    c.setdefault("enable_blacklist", False)
    c.setdefault("blacklist_ips", [])


def _to_v4(c: dict[str, Any]) -> None:
    """v3→v4：默认开启认证，消除无密码暴露风险。"""
    c["enable_auth"] = True


def _to_v5(c: dict[str, Any]) -> None:
    """v4→v5：SHA256 哈希失效清空 + 移除旧 TLS 手工配置 + 自动生成标记。"""
    if c.get("password_hash"):
        c["password_hash"] = ""
        get_logger("config.migration").warning(
            "检测到旧版 SHA256 密码哈希已清除，请用 --set-password 重设"
        )
    for legacy in ("enable_encryption", "tls_cert", "tls_key"):
        c.pop(legacy, None)
    c.setdefault("tls_cert_generated", 0)


def _to_v6(c: dict[str, Any]) -> None:
    """v5→v6：个人版清理 —— 删除三轨制残留字段。

    ``remember_password`` 保留（UI 偏好），其真实值在阶段 2 起只存在于
    SecretStore，不再写入 config.json。
    """
    if c.pop("saved_password", None):
        get_logger("config.migration").info("v6 清理：已删除旧 saved_password 字段")
    c.setdefault("remember_password", False)
    if c.pop("password_hash", None):
        get_logger("config.migration").info(
            "v6 清理：已清空 password_hash（个人版不迁移存量密码），请用 --set-password 重设"
        )
    c.setdefault("enable_auth", True)


#: 目标版本 → 迁移步骤（就地修改 dict）。
_STEPS: dict[int, Callable[[dict[str, Any]], None]] = {
    2: _to_v2,
    3: _to_v3,
    4: _to_v4,
    5: _to_v5,
    6: _to_v6,
}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意已知旧版本的配置 dict 迁移到 :data:`CONFIG_VERSION`。

    Args:
        raw: 从 config.json 读出的原始字典（不会被修改）。

    Returns:
        迁移后的新字典（``version`` 字段已更新为当前版本）。

    Raises:
        ConfigError: 版本号高于当前版本（配置来自未来的损坏/降级安装）。
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"配置格式无效：期望 JSON 对象，得到 {type(raw).__name__}")
    version = int(raw.get("version", 1) or 1)
    if version > CONFIG_VERSION:
        raise ConfigError(f"配置版本 {version} 高于当前支持的 {CONFIG_VERSION}，拒绝加载")
    result = dict(raw)
    for target in range(version + 1, CONFIG_VERSION + 1):
        _STEPS[target](result)
        result["version"] = target
    result["version"] = CONFIG_VERSION
    return result

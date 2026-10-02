"""配置与数据目录解析（阶段 2 新增，2.1 支撑）。

v4 在 ``config.py``、``logger.py``、``utils/security.py`` 三处各自拼接
``Path.home() / ".remote_desktop"``，路径变更后容易互相不一致。本模块是
唯一的路径来源，测试可用 :func:`set_override_root` 重定向到临时目录，
彻底不触碰真实 HOME。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "APP_DIR_NAME",
    "banlist_file",
    "cert_dir",
    "config_dir",
    "config_file",
    "ensure_dirs",
    "history_file",
    "log_dir",
    "override_root",
    "secret_store_file",
    "set_override_root",
]

APP_DIR_NAME = ".remote_desktop"

_override_root: Path | None = None


def set_override_root(path: Path | str | None) -> None:
    """把应用数据根目录重定向到 ``path``（None 恢复默认 HOME 下路径）。

    仅供测试与便携模式使用；生产代码不应调用。
    """
    global _override_root
    _override_root = Path(path) if path is not None else None


def override_root() -> Path | None:
    """返回当前重定向根目录（未重定向时为 None）。"""
    return _override_root


def config_dir() -> Path:
    """应用数据根目录（v4 语义：``~/.remote_desktop``）。"""
    if _override_root is not None:
        return _override_root
    return Path.home() / APP_DIR_NAME


def config_file() -> Path:
    """主配置文件路径 ``config.json``。"""
    return config_dir() / "config.json"


def history_file() -> Path:
    """连接历史文件路径 ``history.json``。"""
    return config_dir() / "history.json"


def banlist_file() -> Path:
    """认证封禁持久化文件路径 ``banlist.json``（阶段 9 ``server/banlist`` 落盘处）。"""
    return config_dir() / "banlist.json"


def log_dir() -> Path:
    """日志目录 ``logs/``。"""
    return config_dir() / "logs"


def cert_dir() -> Path:
    """证书目录 ``certs/``（阶段 9 的自签/用户证书落盘处）。"""
    return config_dir() / "certs"


def secret_store_file(name: str) -> Path:
    """文件落盘型 SecretStore 的存储文件（如 DPAPI/Fernet 密文索引）。"""
    return config_dir() / f"secrets.{name}.json"


def ensure_dirs() -> None:
    """创建配置/日志/证书目录（幂等）。"""
    for d in (config_dir(), log_dir(), cert_dir()):
        d.mkdir(parents=True, exist_ok=True)

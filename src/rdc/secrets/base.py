"""``SecretStore`` 协议定义（阶段 2，2.3）。

所有平台密钥存储（Windows DPAPI / macOS Keychain / Linux libsecret /
Fernet 兜底）实现同一协议，业务层只依赖本协议，不感知具体后端 ——
这是达成 ``secrets/`` 覆盖率 ≥85% 的依赖注入前提（ASSESSMENT §5.7）。

错误处理约定：实现**不得静默吞错**。任何失败必须抛出
:class:`~rdc.exceptions.SecretStoreError` 并在消息中带平台名。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["STORED_PASSWORD_KEY", "SecretStore"]

#: v4 遗留单密码迁移与新写入统一使用的键名。
STORED_PASSWORD_KEY = "saved_password"


@runtime_checkable
class SecretStore(Protocol):
    """平台安全密钥存储抽象。"""

    @property
    def name(self) -> str:
        """后端标识（``dpapi`` / ``keychain`` / ``libsecret`` / ``fernet``）。"""
        ...

    def available(self) -> bool:
        """当前环境是否可用（探测失败返回 False，不抛异常）。"""
        ...

    def save(self, key: str, value: str) -> None:
        """保存 ``value``；失败抛 ``SecretStoreError``。"""
        ...

    def load(self, key: str) -> str | None:
        """读取 ``key`` 的值；不存在返回 None；读取失败抛 ``SecretStoreError``。"""
        ...

    def delete(self, key: str) -> None:
        """删除 ``key``；键不存在视为成功；其他失败抛 ``SecretStoreError``。"""
        ...

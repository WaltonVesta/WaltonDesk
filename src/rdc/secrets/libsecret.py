"""Linux libsecret 密钥存储（阶段 2，2.5）。

真实 API 调用面（与 ``secretstorage`` 库一致，mock 测试必须镜像这套签名）：

- ``secretstorage.dbus_init() -> Connection``
- ``secretstorage.get_default_collection(conn) -> Collection``
- ``collection.is_locked() -> bool`` / ``collection.unlock()``
- ``collection.create_item(label, attributes, secret: bytes, replace=True)``
- ``collection.search_items(attributes) -> Iterable[Item]``
- ``item.get_secret() -> bytes`` / ``item.delete()``

本机为 Windows，**无法真机验证**（R6：Linux 部分靠 CI ubuntu runner）。
"""

from __future__ import annotations

import sys
from typing import Any

from rdc.exceptions import SecretStoreError

__all__ = ["LibsecretStore", "libsecret_available"]

APP_NAME = "RemoteDesktop"


def _is_linux() -> bool:
    """运行时平台判定（测试可 monkeypatch sys.platform）。"""
    return sys.platform.startswith("linux")


def _open_collection() -> Any:  # noqa: ANN401
    """连接会话总线并打开默认 collection（锁定时尝试解锁）。"""
    try:
        import secretstorage
    except ImportError as exc:
        raise SecretStoreError(f"libsecret: secretstorage 不可用: {exc}") from exc
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            collection.unlock()
        return collection
    except SecretStoreError:
        raise
    except Exception as exc:  # dbus/环境类异常统一收窄为带平台名的 SecretStoreError
        raise SecretStoreError(f"libsecret: 连接密钥环失败: {type(exc).__name__}: {exc}") from exc


def libsecret_available() -> bool:
    """Linux 且 secretstorage 与默认 collection 可用时为 True。"""
    if not _is_linux():
        return False
    try:
        _open_collection()
        return True
    except SecretStoreError:
        return False


class LibsecretStore:
    """基于 freedesktop.org 密钥环（libsecret/secretstorage）的 SecretStore。

    每个 key 对应一个 item，attributes 固定
    ``{"application": APP_NAME, "rdc_key": <key>}``。
    """

    @property
    def name(self) -> str:
        return "libsecret"

    def available(self) -> bool:
        return libsecret_available()

    @staticmethod
    def _attributes(key: str) -> dict[str, str]:
        return {"application": APP_NAME, "rdc_key": key}

    def save(self, key: str, value: str) -> None:
        collection = _open_collection()
        try:
            collection.create_item(
                f"{APP_NAME}:{key}",
                self._attributes(key),
                value.encode("utf-8"),
                replace=True,
            )
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"libsecret: 保存键 {key!r} 失败: {type(exc).__name__}: {exc}") from exc

    def load(self, key: str) -> str | None:
        collection = _open_collection()
        try:
            for item in collection.search_items(self._attributes(key)):
                secret: bytes = item.get_secret()
                return secret.decode("utf-8")
            return None
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"libsecret: 读取键 {key!r} 失败: {type(exc).__name__}: {exc}") from exc

    def delete(self, key: str) -> None:
        collection = _open_collection()
        try:
            for item in collection.search_items(self._attributes(key)):
                item.delete()
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"libsecret: 删除键 {key!r} 失败: {type(exc).__name__}: {exc}") from exc

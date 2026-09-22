"""macOS Keychain 密钥存储（阶段 2，2.5）。

真实 API 调用面（pyobjc ``Security`` 框架约定：**C 函数带出参时，pyobjc 返回
``(status, *outs)`` 元组**，与裸 C 的标量返回不同）：

- ``SecKeychainAddGenericPassword(db, sLen, service, aLen, account, pLen,
  password, itemOut) -> (OSStatus, item)``
- ``SecKeychainFindGenericPassword(db, sLen, service, aLen, account,
  pLenOut, pDataOut, itemOut) -> (OSStatus, passwordLength, passwordData, item)``
- ``SecKeychainDeleteGenericPassword(db, sLen, service, aLen, account,
  foundClassOut, itemOut) -> (OSStatus, foundClass, item)``
- ``SecKeychainItemFreeContent(attrList, data) -> None``

``passwordData`` 为 NSData，可用 ``bytes()`` 取内容。实现用 ``_status`` 统一
兼容标量与元组两种形态，mock 测试镜像**元组**形态（=真机 pyobjc 行为）。

本机为 Windows，**无法真机验证**（R6：CI macos runner + 交付后用户验证）。
"""

from __future__ import annotations

import sys
from typing import Any

from rdc.exceptions import SecretStoreError

__all__ = ["KeychainStore", "keychain_available"]

SERVICE = "RemoteDesktop"
#: errSecItemNotFound —— Find/Delete 返回它表示"无此条目"。
_ERR_ITEM_NOT_FOUND = -25300

# v4 语义：service 固定；每个逻辑 key 占一条 generic password（account=key）。


def _is_macos() -> bool:
    """运行时平台判定（测试可 monkeypatch sys.platform）。"""
    return sys.platform == "darwin"


def _status(result: Any) -> int:  # noqa: ANN401
    """从 pyobjc 返回统一提取 OSStatus（兼容标量与元组）。"""
    if isinstance(result, tuple):
        return int(result[0])
    return int(result)


def _security_module() -> Any:  # noqa: ANN401
    """导入 pyobjc Security 框架（不存在时抛 SecretStoreError）。"""
    try:
        import Security
    except ImportError as exc:
        raise SecretStoreError(f"keychain: pyobjc Security 框架不可用: {exc}") from exc
    return Security


def keychain_available() -> bool:
    """macOS 且 pyobjc Security 可导入时为 True。"""
    if not _is_macos():
        return False
    try:
        import Security

        return bool(Security)
    except ImportError:
        return False


class KeychainStore:
    """基于 macOS Keychain 的 :class:`~rdc.secrets.base.SecretStore` 实现。

    每个 key 映射为一条 generic password（service=SERVICE, account=key）。
    """

    @property
    def name(self) -> str:
        return "keychain"

    def available(self) -> bool:
        return keychain_available()

    def save(self, key: str, value: str) -> None:
        sec = _security_module()
        service = SERVICE.encode("utf-8")
        account = key.encode("utf-8")
        password = value.encode("utf-8")
        # 先删同名旧项保证幂等；not found 不算失败。
        del_res = sec.SecKeychainDeleteGenericPassword(
            None, len(service), service, len(account), account, None, None
        )
        del_status = _status(del_res)
        if del_status != 0 and del_status != _ERR_ITEM_NOT_FOUND:
            raise SecretStoreError(f"keychain: 覆盖键 {key!r} 前删除失败，OSStatus={del_status}")
        add_res = sec.SecKeychainAddGenericPassword(
            None, len(service), service, len(account), account, len(password), password, None
        )
        status = _status(add_res)
        if status != 0:
            raise SecretStoreError(f"keychain: 保存键 {key!r} 失败，OSStatus={status}")

    def load(self, key: str) -> str | None:
        sec = _security_module()
        service = SERVICE.encode("utf-8")
        account = key.encode("utf-8")
        result = sec.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account, None, None, None
        )
        status = _status(result)
        if status == _ERR_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise SecretStoreError(f"keychain: 读取键 {key!r} 失败，OSStatus={status}")
        # pyobjc: (status, passwordLength, passwordData, item)
        _, _length, data, _item = result
        try:
            return bytes(data).decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise SecretStoreError(f"keychain: 键 {key!r} 数据解码失败: {exc}") from exc
        finally:
            sec.SecKeychainItemFreeContent(None, data)

    def delete(self, key: str) -> None:
        sec = _security_module()
        service = SERVICE.encode("utf-8")
        account = key.encode("utf-8")
        status = _status(
            sec.SecKeychainDeleteGenericPassword(
                None, len(service), service, len(account), account, None, None
            )
        )
        if status != 0 and status != _ERR_ITEM_NOT_FOUND:
            raise SecretStoreError(f"keychain: 删除键 {key!r} 失败，OSStatus={status}")

"""SecretStore 工厂与平台探测降级链（阶段 2，2.3）。

探测顺序（v4 三轨制的替代）：

    win32  → DpapiStore（crypt32 加载失败则降级）
    darwin → KeychainStore（pyobjc 不可用则降级）
    linux  → LibsecretStore（dbus/collection 不可用则降级）
    兜底   → FernetFallbackStore（机器绑定口令加密，永远 available）

降级不是静默的：调用方可用 ``explain`` 字段取到降级原因并写日志。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from rdc.exceptions import SecretStoreError
from rdc.secrets.base import SecretStore
from rdc.secrets.dpapi import DpapiStore
from rdc.secrets.fernet_fallback import FernetFallbackStore
from rdc.secrets.keychain import KeychainStore
from rdc.secrets.libsecret import LibsecretStore

__all__ = ["StoreSelection", "get_secret_store"]


@dataclass(frozen=True)
class StoreSelection:
    """一次工厂选择的结果：选中的后端 + 是否发生降级及原因。"""

    store: SecretStore
    degraded: bool
    reason: str


def _candidates(
    fernet_passphrase: Callable[[], str] | None = None,
) -> list[SecretStore]:
    """按当前平台返回候选后端（首选在前，Fernet 兜底在最后）。"""
    fallback = FernetFallbackStore(fernet_passphrase)
    if sys.platform == "win32":
        return [DpapiStore(), fallback]
    if sys.platform == "darwin":
        return [KeychainStore(), fallback]
    return [LibsecretStore(), fallback]


def get_secret_store(
    fernet_passphrase: Callable[[], str] | None = None,
) -> StoreSelection:
    """返回当前平台可用的 SecretStore；原生后端不可用时降级到 Fernet 兜底。

    Args:
        fernet_passphrase: Fernet 兜底所需口令提供器；仅在可能走到兜底时需要。

    Returns:
        :class:`StoreSelection`；``degraded=True`` 时 ``reason`` 说明降级原因，
        调用方应记入日志（约束 6：不静默）。
    """
    candidates = _candidates(fernet_passphrase)
    reasons: list[str] = []
    for store in candidates:
        try:
            ok = store.available()
        except SecretStoreError as exc:
            ok = False
            reasons.append(f"{store.name}: {exc}")
        if ok:
            return StoreSelection(store=store, degraded=bool(reasons), reason="; ".join(reasons))
        if store.name != "fernet":
            reasons.append(f"{store.name}: 平台探测返回不可用")
    raise SecretStoreError("secrets: 无任何可用后端（不应到达）: " + "; ".join(reasons))

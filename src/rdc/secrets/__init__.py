"""密钥存储包（阶段 2）。

平台原生后端（Windows DPAPI / macOS Keychain / Linux libsecret）+
机器绑定 Fernet 兜底，统一 :class:`~rdc.secrets.base.SecretStore` 协议。
新代码经 :func:`get_secret_store` 获取实例，不直接 import 具体后端。
"""

from __future__ import annotations

from rdc.secrets.base import STORED_PASSWORD_KEY, SecretStore
from rdc.secrets.dpapi import DpapiStore
from rdc.secrets.factory import StoreSelection, get_secret_store
from rdc.secrets.fernet_fallback import FernetFallbackStore
from rdc.secrets.keychain import KeychainStore
from rdc.secrets.libsecret import LibsecretStore
from rdc.secrets.password import hash_password, verify_password

__all__ = [
    "STORED_PASSWORD_KEY",
    "DpapiStore",
    "FernetFallbackStore",
    "KeychainStore",
    "LibsecretStore",
    "SecretStore",
    "StoreSelection",
    "get_secret_store",
    "hash_password",
    "verify_password",
]

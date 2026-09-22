"""Fernet 兜底密钥存储（阶段 2，2.6）。

平台原生密钥环（DPAPI/Keychain/libsecret）不可用时使用。密钥派生链：

    salt（首次随机生成并落盘） + 机器 ID（uuid.getnode） + 用户口令
        → PBKDF2-HMAC-SHA256 → 32B → urlsafe_b64 → Fernet 密钥

密文 vault 落盘于 ``~/.remote_desktop/secrets.fernet.json``。
换机器（机器 ID 变）或换口令都会导致解密失败 —— 失败抛
``SecretStoreError``，绝不静默返回空值。
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from rdc.config import paths
from rdc.exceptions import SecretStoreError

__all__ = ["FernetFallbackStore", "derive_key"]

PBKDF2_ITERATIONS = 480_000
_SALT_FILE = "secrets.salt"


def derive_key(passphrase: str, salt: bytes, *, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """由口令 + salt + 机器 ID 派生 urlsafe-base64 的 32 字节 Fernet 密钥。

    同一输入（同机器、同 salt、同口令、同迭代数）必然得到同一密钥。
    """
    machine = str(uuid.getnode()).encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt + machine,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _load_or_create_salt() -> bytes:
    """读取持久化 salt；不存在则随机生成并写入（0600 语义尽力而为）。"""
    salt_path = paths.config_dir() / _SALT_FILE
    try:
        return bytes.fromhex(salt_path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        salt = os.urandom(16)
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        salt_path.write_text(salt.hex(), encoding="ascii")
        return salt
    except (OSError, ValueError) as exc:
        raise SecretStoreError(f"fernet: salt 文件损坏或不可读 {salt_path}: {exc}") from exc


class FernetFallbackStore:
    """机器绑定的 Fernet 加密文件 vault（:class:`~rdc.secrets.base.SecretStore`）。

    ``passphrase_provider`` 是一个无参可调用对象，返回口令字符串
    （便于测试注入与未来 UI 弹窗接入）。
    """

    def __init__(self, passphrase_provider: Callable[[], str] | None = None) -> None:
        self._provider = passphrase_provider
        self._path = paths.secret_store_file("fernet")

    @property
    def name(self) -> str:
        return "fernet"

    def available(self) -> bool:
        return True

    def _fernet(self) -> Fernet:
        if self._provider is None:
            raise SecretStoreError("fernet: 未配置口令提供器")
        passphrase = self._provider()
        if not passphrase:
            raise SecretStoreError("fernet: 口令为空")
        return Fernet(derive_key(passphrase, _load_or_create_salt()))

    def _read_map(self) -> dict[str, str]:
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"fernet: 读取 vault 失败 {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SecretStoreError(f"fernet: vault 格式损坏（期望 JSON 对象）: {self._path}")
        return {str(k): str(v) for k, v in raw.items()}

    def _write_map(self, data: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            raise SecretStoreError(f"fernet: 写入 vault 失败 {self._path}: {exc}") from exc

    def save(self, key: str, value: str) -> None:
        token = self._fernet().encrypt(value.encode("utf-8")).decode("ascii")
        data = self._read_map()
        data[key] = token
        self._write_map(data)

    def load(self, key: str) -> str | None:
        token = self._read_map().get(key)
        if token is None:
            return None
        try:
            plain = self._fernet().decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise SecretStoreError(
                f"fernet: 解密键 {key!r} 失败（口令变更或 vault 损坏），拒绝返回空值"
            ) from exc
        return plain.decode("utf-8")

    def delete(self, key: str) -> None:
        data = self._read_map()
        if key in data:
            del data[key]
            self._write_map(data)

"""Windows DPAPI 密钥存储（阶段 2，2.4）。

v4 在 ``_dpapi_protect`` 与 ``_dpapi_unprotect`` 里**逐字重复**定义了
``DATA_BLOB`` 结构体（config.py:80-81 与 103-104，见 ASSESSMENT §5.4），
本模块收敛为单一 ``_DataBlob`` + ``_crypt_call`` 辅助函数。

密文落盘位置：``~/.remote_desktop/secrets.dpapi.json``（键 → base64 密文）。
DPAPI 本身只加解密不落盘；密文受当前 Windows 用户 + 附加熵绑定，
换用户/换机器无法解密 —— 与 v4 的 ``DPAPI1:`` 字段语义等价。

**熵字符串必须保持 v4 原值** ``b"RemoteDesktop-Control-v3"``（注意结尾是
v3 的历史事实），否则存量密文全部解不开。
"""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import wintypes
from typing import Any

from rdc.config import paths
from rdc.exceptions import SecretStoreError

__all__ = ["DPAPI_ENTROPY", "DpapiStore", "dpapi_available", "protect", "unprotect"]

#: DPAPI 附加熵 —— 与 v4 逐字节一致，改动即破坏存量密文兼容性。
DPAPI_ENTROPY = b"RemoteDesktop-Control-v3"


def _is_windows() -> bool:
    """运行时平台判定（不用 import 期常量，保证测试可 monkeypatch sys.platform）。"""
    return sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    """Win32 ``DATA_BLOB``（crypt32.h）。单一实现，替代 v4 的两份重复定义。"""

    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt_call(fn: Any, blob_in: _DataBlob, blob_ent: _DataBlob) -> bytes:  # noqa: ANN401
    """调用 CryptProtectData / CryptUnprotectData 并取出结果缓冲区。

    两个 API 的调用形态完全一致（仅函数指针与描述串不同），收敛到一处。
    """
    blob_out = _DataBlob()
    desc = ctypes.c_wchar_p("RemoteDesktop secrets")
    ok = fn(ctypes.byref(blob_in), desc, ctypes.byref(blob_ent), None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()  # WindowsOnly：本函数仅在 win32 分支被调用
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes) -> bytes:
    """DPAPI 加密（当前用户 + ``DPAPI_ENTROPY`` 绑定）。"""
    if not _is_windows():
        raise SecretStoreError("dpapi: 非 Windows 平台不可用")
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    return _crypt_call(ctypes.windll.crypt32.CryptProtectData, _DataBlob(len(data), buf), _DataBlob(len(DPAPI_ENTROPY), ent))


def unprotect(data: bytes) -> bytes:
    """DPAPI 解密。密文与当前用户/熵不匹配时抛 ``SecretStoreError``。"""
    if not _is_windows():
        raise SecretStoreError("dpapi: 非 Windows 平台不可用")
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    try:
        return _crypt_call(ctypes.windll.crypt32.CryptUnprotectData, _DataBlob(len(data), buf), _DataBlob(len(DPAPI_ENTROPY), ent))
    except OSError as exc:
        raise SecretStoreError(f"dpapi: 解密失败（用户或熵不匹配）: {exc}") from exc


def dpapi_available() -> bool:
    """Windows 且 crypt32 可加载时为 True。"""
    if not _is_windows():
        return False
    try:
        return bool(ctypes.windll.crypt32)
    except OSError:
        return False


class DpapiStore:
    """基于 DPAPI 加密 + JSON 文件落盘的 :class:`SecretStore` 实现。"""

    def __init__(self) -> None:
        self._path = paths.secret_store_file("dpapi")

    @property
    def name(self) -> str:
        return "dpapi"

    def available(self) -> bool:
        return dpapi_available()

    def _read_map(self) -> dict[str, str]:
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"dpapi: 读取存储文件失败 {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SecretStoreError(f"dpapi: 存储文件格式损坏（期望 JSON 对象）: {self._path}")
        return {str(k): str(v) for k, v in raw.items()}

    def _write_map(self, data: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            raise SecretStoreError(f"dpapi: 写入存储文件失败 {self._path}: {exc}") from exc

    def save(self, key: str, value: str) -> None:
        try:
            token = base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")
        except SecretStoreError:
            raise
        except OSError as exc:
            raise SecretStoreError(f"dpapi: 加密键 {key!r} 失败: {exc}") from exc
        data = self._read_map()
        data[key] = token
        self._write_map(data)

    def load(self, key: str) -> str | None:
        token = self._read_map().get(key)
        if token is None:
            return None
        try:
            cipher = base64.b64decode(token.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretStoreError(f"dpapi: 存储值不是合法 base64（文件损坏？）: {exc}") from exc
        plain = unprotect(cipher)
        return plain.decode("utf-8")

    def delete(self, key: str) -> None:
        data = self._read_map()
        if key in data:
            del data[key]
            self._write_map(data)

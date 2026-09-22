"""帧完整性保护：HMAC-SHA256 + TLS exporter 密钥派生（阶段 3，3.6）。

为什么需要它：CRC32 是**非密码学**校验，攻击者可构造保持 CRC 不变的篡改；
且 CRC 只覆盖载荷，包头（Type/Flags/Length/Version）可被无感改写。
HMAC-SHA256 覆盖 ``头部 + 线上载荷`` 并以后置 32B trailer 传输，
在 :class:`~rdc.protocol.framing.Packet.from_bytes` 里先于 zlib 解压验证，
使篡改（含 zip bomb 构造）在任何解压副作用之前被拒。

密钥来源（按优先级）：

1. **TLS exporter**（:func:`derive_key_from_tls`）—— RFC 5705
   ``export_keying_material``，把会话密钥派生委托给 TLS 握手本身：
   MITM 无法在不破坏 TLS 的情况下推出同一密钥，天然会话绑定 + 抗重放。
   label/context 固定（两端一致即可独立派生）。
   ⚠️ **API 现状（2026-09-23 本机 Python 3.14.3 实测）**：stdlib
   ``ssl.SSLSocket`` / ``ssl.SSLObject`` / ``ssl.SSLSession`` 均**没有**
   ``export_keying_material``（CPython 长期未暴露 OpenSSL 的
   ``SSL_export_keying_material``）。本函数按 **Protocol 适配器**设计：
   传入任何提供该方法签名的对象即可（如 pyOpenSSL ``Connection``，
   自 17.4.0 起支持 RFC 5705）。"用哪个 TLS 栈拿 exporter" 是阶段 4
   tls.py 的架构决策（见 PROGRESS.md 阶段 4 前置项）。
2. **HKDF 兜底**（:func:`derive_key_from_secret`）—— 无 TLS 导出能力时
   （单元测试、握手消息内交换的会话随机数）从共享主密钥经 HKDF-SHA256 派生。
   ⚠️ 主密钥必须来自 :mod:`rdc.secrets`（阶段 2）或 TLS 通道内协商的随机数，
   禁止硬编码。

签名用 :func:`compute_hmac`，验签用 :func:`verify_hmac`（恒时比较，失败抛
:class:`HmacVerificationError`）。

典型握手后流程（阶段 4 接入）::

    key = derive_key_from_tls(tls_conn)              # 两端各自派生
    frame = Packet(...).to_bytes(hmac_key=key)       # 发送端签名
    Packet.from_bytes(frame, hmac_key=key, require_hmac=True)  # 接收端验签
"""

from __future__ import annotations

import hmac as _hmac
from hashlib import sha256
from typing import Final, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from rdc.exceptions import HmacVerificationError
from rdc.protocol.constants import HMAC_SIZE, TLS_EXPORTER_CONTEXT

__all__ = [
    "KeyingMaterialExporter",
    "compute_hmac",
    "derive_key_from_secret",
    "derive_key_from_tls",
    "verify_hmac",
]

# RFC 5705 keying material exporter 的 label（TLS 规范要求前缀 "EXM-"）
TLS_EXPORTER_LABEL: Final[bytes] = b"EXM-rdc-v5-hmac"


class KeyingMaterialExporter(Protocol):
    """具备 RFC 5705 导出能力的会话句柄（适配器契约）。

    ⚠️ 实测（2026-09-23，Python 3.14.3）：stdlib ``ssl.SSLSocket`` /
    ``ssl.SSLObject`` / ``ssl.SSLSession`` **均无** ``export_keying_material``
    （CPython 至今未暴露 OpenSSL 的 ``SSL_export_keying_material``，任务书
    REFACTOR_PLAN §3.6 引用的 API 不存在）。本 Protocol 只约定方法签名，
    真实实现由阶段 4 ``transport/tls.py`` 选定（候选：pyOpenSSL
    ``Connection.export_keying_material``，Apache-2.0，自 17.4.0 起支持）。
    """

    def export_keying_material(
        self,
        length: int,
        label: bytes,
        context: bytes | None = ...,
        use_context_prefix: bool = ...,
    ) -> bytes: ...


def derive_key_from_tls(
    exporter: KeyingMaterialExporter,
    *,
    length: int = HMAC_SIZE,
) -> bytes:
    """从 TLS 会话句柄导出 HMAC 密钥（推荐路径，需具备 exporter 能力）。

    Args:
        exporter: 提供 :meth:`KeyingMaterialExporter.export_keying_material`
            的会话对象（阶段 4 接入真实 TLS 栈；本层不关心其具体来源）。
        length: 导出密钥字节数，默认 32（SHA-256 全长）。

    Returns:
        两端从同一 TLS 会话独立派生出的同一会话密钥。

    Raises:
        ValueError: ``length`` 非正。
        ssl.SSLError / OSError: TLS 层导出失败（由调用方处理，不静默降级）。
    """
    if length <= 0:
        raise ValueError("导出密钥长度必须为正")
    return exporter.export_keying_material(length, TLS_EXPORTER_LABEL, TLS_EXPORTER_CONTEXT)


def derive_key_from_secret(master_secret: bytes, *, length: int = HMAC_SIZE) -> bytes:
    """从共享主密钥经 HKDF-SHA256 派生会话 HMAC 密钥（兜底路径）。

    Args:
        master_secret: 高熵主密钥（≥32 字节，来自 SecretStore / 密钥协商）。
        length: 派生输出字节数。

    Returns:
        HKDF 派生密钥。

    Raises:
        ValueError: ``master_secret`` 过短（<16 字节）或 ``length`` 非正。
    """
    if length <= 0:
        raise ValueError("派生密钥长度必须为正")
    if len(master_secret) < 16:
        raise ValueError("master_secret 过短（至少 16 字节），拒绝弱密钥")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=TLS_EXPORTER_CONTEXT)
    return hkdf.derive(master_secret)


def _canonical_message(header: bytes, payload: bytes) -> bytes:
    """拼接 HMAC 覆盖范围：头部 + 线上载荷（严格按顺序，不留歧义字节）。"""
    return header + payload


def compute_hmac(key: bytes, header: bytes, payload: bytes) -> bytes:
    """计算帧的 HMAC-SHA256 trailer。

    Args:
        key: 会话密钥（:func:`derive_key_from_tls` / :func:`derive_key_from_secret` 产物）。
        header: 15 字节包头。
        payload: 线上载荷字节（压缩后）。

    Returns:
        32 字节摘要。

    Raises:
        ValueError: 密钥为空。
    """
    if not key:
        raise ValueError("HMAC 密钥不能为空")
    return _hmac.new(key, _canonical_message(header, payload), sha256).digest()


def verify_hmac(key: bytes, header: bytes, payload: bytes, tag: bytes) -> None:
    """恒时校验 HMAC trailer。

    Args:
        key: 会话密钥。
        header: 15 字节包头。
        payload: 线上载荷字节。
        tag: 收到的 32 字节 trailer。

    Raises:
        HmacVerificationError: trailer 长度错误或摘要不匹配（含截断/篡改）。
    """
    if len(tag) != HMAC_SIZE:
        raise HmacVerificationError(f"HMAC trailer 长度 {len(tag)} != {HMAC_SIZE}")
    expected = compute_hmac(key, header, payload)
    if not _hmac.compare_digest(expected, tag):
        raise HmacVerificationError("HMAC 校验失败：帧被篡改或密钥不匹配")

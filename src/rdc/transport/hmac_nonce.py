"""会话 HMAC 密钥派生（阶段 4，4.7 / F1 方案 B 落地）。

F1 决策（2026-09-23 用户确认）：stdlib ``ssl`` 无 ``export_keying_material``，
v5 改用 **TLS 通道内交换 nonce + HKDF** 派生帧完整性密钥：

- 零新依赖（不引入 pyOpenSSL）；自己用场景（点对点、非公网多租户）安全足够；
- 协议适配器面保留（``hmac_integrity.KeyingMaterialExporter`` 仍在，
  未来若上真实 exporter 可无痛换回，见 STAGE3_REPORT §3 与本文档"扩展点"）。

派生（``derive_hmac_key``）::

    ikm  = client_nonce || server_nonce        # 32B + 32B，固定拼接顺序
    salt = connection_id                        # 服务端每连接随机 32B
    info = b"rdc-hmac-v1"
    key  = HKDF-SHA256(ikm, salt, info, length=32)

- ``connection_id`` 由控制通道 ``AuthResponse`` 下发，画面通道复用同一
  ``connection_id``，配合各自通道独立交换的 nonce → **两条通道派生出不同密钥**，
  且都绑定到这一次连接（MITM 无法在不被 TLS/HMAC 检出下伪造）。
- nonce 交换发生在 TLS 握手的**通道内**（AuthRequest/AuthResponse/ScreenJoin 帧），
  每连接一次；密钥建立后**固定不重派生**（用户叮嘱 c）。

扩展点：若后续引入具备 RFC 5705 的 TLS 栈，替换本模块的
:func:`derive_hmac_key` 为 ``hmac_integrity.derive_key_from_tls`` 即可，
上层（tls/handshake/channel）签名/验签面不变。
"""

from __future__ import annotations

import secrets
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from rdc.exceptions import ProtocolError

__all__ = [
    "CONNECTION_ID_SIZE",
    "HKDF_INFO",
    "HMAC_KEY_SIZE",
    "NONCE_SIZE",
    "derive_hmac_key",
    "new_connection_id",
    "new_nonce",
]

#: nonce 字节数（256-bit 熵，对齐 SHA-256 输出）。
NONCE_SIZE: Final[int] = 32
#: 连接标识字节数（HKDF salt）。
CONNECTION_ID_SIZE: Final[int] = 32
#: 派生出的 HMAC 会话密钥长度（SHA-256 全长）。
HMAC_KEY_SIZE: Final[int] = 32
#: HKDF info 域分隔常量（两端必须一致）。
HKDF_INFO: Final[bytes] = b"rdc-hmac-v1"


def new_nonce() -> bytes:
    """生成一条连接的 HMAC nonce（32 字节密码学随机，每连接一次）。"""
    return secrets.token_bytes(NONCE_SIZE)


def new_connection_id() -> bytes:
    """生成连接标识（32 字节随机，用作 HKDF salt）。"""
    return secrets.token_bytes(CONNECTION_ID_SIZE)


def _check_bytes(value: bytes, name: str, size: int) -> None:
    """长度校验：非预期长度即拒绝（防止截断/填充攻击削弱熵）。"""
    if len(value) != size:
        raise ProtocolError(f"{name} 必须为 {size} 字节，实际 {len(value)}")


def derive_hmac_key(
    client_nonce: bytes,
    server_nonce: bytes,
    connection_id: bytes,
) -> bytes:
    """双端各自从交换到的 nonce + connection_id 派生同一 HMAC 会话密钥。

    Args:
        client_nonce: 客户端随机（:func:`new_nonce`，32B）。
        server_nonce: 服务端随机（:func:`new_nonce`，32B）。
        connection_id: 服务端下发的连接标识（32B，HKDF salt）。

    Returns:
        32 字节 HMAC-SHA256 会话密钥；两端以相同入参独立调用得到同一结果。

    Raises:
        ProtocolError: 任一输入长度非预期（熵不足，拒绝派生弱密钥）。
    """
    _check_bytes(client_nonce, "client_nonce", NONCE_SIZE)
    _check_bytes(server_nonce, "server_nonce", NONCE_SIZE)
    _check_bytes(connection_id, "connection_id", CONNECTION_ID_SIZE)
    ikm = client_nonce + server_nonce
    hkdf = HKDF(algorithm=hashes.SHA256(), length=HMAC_KEY_SIZE, salt=connection_id, info=HKDF_INFO)
    return hkdf.derive(ikm)

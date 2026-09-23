"""TLS 上下文构建与证书指纹信任（阶段 4，4.5）。

职责：

- :func:`ensure_server_cert` —— 自签名证书生成/续期（迁移自 v4
  ``service._ensure_tls_cert`` / ``_generate_self_signed_cert``，逻辑不变：
  2048-bit RSA、CN=RemoteDesktop、365 天、剩余 <7 天重新生成）。
- :func:`build_server_ssl_context` / :func:`build_client_ssl_context` —— 构建
  **强制 TLS 1.2+** 的 SSLContext（硬性约束 3），供 asyncio ``start_server`` /
  ``open_connection`` 复用。
- TOFU（Trust On First Use）：自签名场景无 CA 可校验，改为对端证书 SHA-256
  指纹首次确认、之后自动信任（迁移自 v4 ``control_channel._wrap_tls`` 降级逻辑）。

安全边界（与 v4 对齐并收紧）：

- ``build_client_ssl_context(verify=True)`` 走系统 CA + 主机名校验（防 MITM）。
- 仅当握手失败**确认为自签名证书**时，调用方才应降级到 ``verify=False`` 重连，
  随后用 :func:`verify_peer_trust` 做指纹 TOFU；其他证书错误（过期/主机名不符）
  必须直接上抛，不得降级（v4.0.2 修复的降级滥用问题在此以函数契约固化）。
"""

from __future__ import annotations

import datetime
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from rdc.exceptions import TlsError
from rdc.logging import get_logger
from rdc.security.fingerprint import FingerprintManager, get_fingerprint_manager

__all__ = [
    "build_client_ssl_context",
    "build_server_ssl_context",
    "ensure_server_cert",
    "fingerprint_of_ssl_object",
    "is_self_signed_error",
    "tls_hostname",
    "verify_peer_trust",
]

_LOGGER = get_logger("transport.tls")


def tls_hostname(host: str) -> str:
    """TLS SNI / 主机名校验用名称（IPv6 字面量去掉方括号）。"""
    return host.removeprefix("[").removesuffix("]")

#: 证书剩余有效期低于该天数即重新生成（与 v4 一致）。
CERT_RENEW_THRESHOLD_DAYS: Final[int] = 7
#: 自签名证书有效天数。
CERT_VALID_DAYS: Final[int] = 365

# 触发"降级到自签名 TOFU"的 ssl 错误特征（仅此情形允许关闭证书校验）。
# ⚠️ v5 收紧：不匹配 v4 用的 "certificate_verify_failed"——那是证书校验失败的
# **公共**错误串（过期/未知 CA/主机名不符都会产生），匹配它等于对一切校验
# 失败降级（v4.0.2 声称"仅自签名才降级"但实际条件过宽）。仅保留自签名专有关键词。
_SELF_SIGNED_MARKERS: Final[tuple[str, ...]] = (
    "self signed certificate",
    "self-signed certificate",
)


def is_self_signed_error(exc: BaseException) -> bool:
    """判断 SSL 握手异常是否由"对端自签名证书"引起。

    只有返回 ``True`` 时调用方才可安全降级到 ``verify=False`` + 指纹 TOFU；
    过期、主机名不符、吊销等其他证书错误一律返回 ``False``，应直接拒绝连接。
    """
    text = str(exc).lower()
    return any(marker in text for marker in _SELF_SIGNED_MARKERS)


# ---------------------------------------------------------------------------
# 服务端：证书生成 + SSLContext
# ---------------------------------------------------------------------------


def _load_not_after(cert_path: Path) -> datetime.datetime | None:
    """读取 PEM 证书到期时间（aware UTC）；解析失败返回 ``None``。"""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    try:
        return cert.not_valid_after_utc
    except AttributeError:  # 极旧 cryptography：无 tz 后缀属性
        naive = cert.not_valid_after
        return naive.replace(tzinfo=datetime.UTC)


def _generate_self_signed_cert(cert_path: Path, key_path: Path, *, common_name: str) -> None:
    """生成 2048-bit RSA 自签名证书（PEM）到指定路径。"""
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RemoteDesktop Control"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_VALID_DAYS))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_server_cert(
    cert_path: Path,
    key_path: Path,
    *,
    common_name: str = "RemoteDesktop",
) -> None:
    """确保服务端自签名证书存在且未临期；否则生成/续期。

    Args:
        cert_path: 证书 PEM 路径（不存在则创建父目录）。
        key_path: 私钥 PEM 路径。
        common_name: 证书 CN（默认 ``RemoteDesktop``，与 v4 一致）。

    行为：两者齐全且剩余有效期 > :data:`CERT_RENEW_THRESHOLD_DAYS` 天时跳过；
    否则重新生成（含解析损坏的兜底重生成）。
    """
    if cert_path.exists() and key_path.exists():
        not_after = _load_not_after(cert_path)
        if not_after is not None:
            remaining = (not_after - datetime.datetime.now(datetime.UTC)).days
            if remaining > CERT_RENEW_THRESHOLD_DAYS:
                return
            _LOGGER.info("tls_cert_expiring_soon", remaining_days=remaining)
        else:
            _LOGGER.warning("tls_cert_unreadable_regenerating", path=str(cert_path))
    _generate_self_signed_cert(cert_path, key_path, common_name=common_name)
    _LOGGER.info("tls_cert_generated", path=str(cert_path))


def build_server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """构建服务端 SSLContext（强制 TLS 1.2+）。

    Raises:
        TlsError: 证书链加载失败（文件缺失/损坏）。
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    except (OSError, ssl.SSLError) as exc:
        raise TlsError(f"服务端证书加载失败: {exc}") from exc
    return context


# ---------------------------------------------------------------------------
# 客户端：SSLContext + TOFU 指纹信任
# ---------------------------------------------------------------------------


def build_client_ssl_context(*, verify: bool = True) -> ssl.SSLContext:
    """构建客户端 SSLContext（强制 TLS 1.2+）。

    Args:
        verify: ``True`` 走系统 CA + 主机名校验（首选路径）；
            ``False`` 关闭校验（仅自签名降级 + TOFU 时由调用方使用）。

    ⚠️ ``verify=False`` 会禁用一切证书信任，必须由 :func:`verify_peer_trust`
    补上指纹 TOFU，否则等于对 MITM 敞开——调用方负责成对使用。
    """
    context = ssl.create_default_context() if verify else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def fingerprint_of_ssl_object(ssl_object: ssl.SSLObject | None) -> str | None:
    """从 TLS 会话取对端证书并算 SHA-256 指纹；无证书返回 ``None``。"""
    if ssl_object is None:
        return None
    der: bytes | None = ssl_object.getpeercert(binary_form=True)
    if not der:
        return None
    return FingerprintManager.compute_fingerprint(der)


def verify_peer_trust(
    ssl_object: ssl.SSLObject | None,
    host: str,
    *,
    confirm: Callable[[str, str], bool] | None = None,
    manager: FingerprintManager | None = None,
) -> str:
    """对自签名证书执行 TOFU 指纹校验。

    流程（迁移 v4 ``_wrap_tls`` 第二步，收紧默认信任）：

    1. 取对端证书指纹，无证书 → 拒绝（:class:`TlsError`）。
    2. 指纹已在信任列表 → 通过。
    3. 否则调用 ``confirm(fingerprint, host)`` 请用户确认；确认则加入信任列表，
       拒绝则 :class:`TlsError`。
    4. **无 confirm 回调**：v4 会默认信任（存在 MITM 风险）；v5 视为配置错误直接
       拒绝——首连确认是 TOFU 的安全前提，不允许静默放行。

    Args:
        ssl_object: 已握手的 TLS 会话对象（``writer.get_extra_info("ssl_object")``）。
        host: 对端主机（记录关联 + 供确认框展示）。
        confirm: 用户确认回调，返回 ``True`` 表示信任。
        manager: 指纹管理器（默认取全局单例；测试可注入）。

    Returns:
        通过的指纹（大写十六进制冒号分隔）。

    Raises:
        TlsError: 无证书 / 无确认回调 / 用户拒绝指纹。
    """
    fp_manager = manager or get_fingerprint_manager()
    fingerprint = fingerprint_of_ssl_object(ssl_object)
    if fingerprint is None:
        raise TlsError("无法获取对端证书，拒绝连接")
    if fp_manager.is_trusted(fingerprint, host):
        _LOGGER.info("tls_fingerprint_trusted", host=host)
        return fingerprint
    if confirm is None:
        raise TlsError(f"首次连接需确认证书指纹但未提供确认回调：{fingerprint} ({host})")
    _LOGGER.info("tls_fingerprint_first_use", fingerprint=fingerprint, host=host)
    if confirm(fingerprint, host):
        fp_manager.trust(fingerprint, host)
        return fingerprint
    raise TlsError("用户拒绝证书指纹，连接中止")

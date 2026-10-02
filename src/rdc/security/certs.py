"""用户证书管理（阶段 9.3）：非自签名证书加载、过期检测、轮换建议。

v4 只有"自动生成自签名 + 365 天临期重生成"一条路径（``service._ensure_tls_cert``，
已迁 :func:`rdc.transport.tls.ensure_server_cert`）。个人/企业用户持有**真实 CA
签发证书**（域名 + SAN）时，v5 支持配置加载，握手走系统 CA 校验、**关闭 TOFU
降级**——这正是 MITM 渗透面（阶段 9 渗透清单 S2）的正解。

本模块职责（纯函数 + 一个不可变结果对象，零 socket/零 Qt）：

- :func:`load_user_cert_bundle` —— 加载并校验 PEM 证书 + 私钥：可解析、
  私钥与证书公钥匹配、未过期；
- :func:`check_expiry` —— 过期/临期状态分类（供审计与轮换提示）；
- :func:`rotation_advice` —— 统一轮换决策文案；
- :func:`describe_cert` —— 一行证书摘要（日志/UI 展示，不含私钥材料）。

轮换口径：用户证书**永不自动重签**（私钥来自外部 CA，应用无权签发），
"轮换"= 到期前提示用户替换磁盘文件 + 重启服务；自签名路径的自动续期
仍由 ``transport.tls.ensure_server_cert`` 承担。

安全红线：
- 私钥文件内容**绝不进入日志/异常文案**（异常只带路径与错误类型）；
- Windows 私钥 ACL/权限位校验超出重构范围，文档化为部署须知；
- 加载失败一律 :class:`rdc.exceptions.TlsError`，不静默回落自签名
  （回落 = 降级 MITM 面，违反 S1 口径）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import types as asym_types
from cryptography.x509.oid import ExtensionOID, NameOID

from rdc.exceptions import TlsError

__all__ = [
    "EXPIRY_WARN_DAYS",
    "ExpiryStatus",
    "UserCertBundle",
    "check_expiry",
    "describe_cert",
    "load_user_cert_bundle",
    "rotation_advice",
]

#: 剩余有效期低于该天数视为临期（与 transport.tls 自签续期阈值同口径）。
EXPIRY_WARN_DAYS: Final[int] = 7


class ExpiryStatus(StrEnum):
    """证书有效期分类。"""

    VALID = "valid"
    """剩余 > 告警阈值。"""
    EXPIRING = "expiring"
    """0 < 剩余 ≤ 告警阈值：应立即轮换。"""
    EXPIRED = "expired"
    """已过期：拒绝加载。"""


@dataclass(frozen=True, slots=True)
class UserCertBundle:
    """一次成功加载的用户证书材料（仅元数据，不含私钥）。

    Attributes:
        cert_path: 证书 PEM 路径。
        key_path: 私钥 PEM 路径。
        not_before: 证书生效时间（aware UTC）。
        not_after: 证书到期时间（aware UTC）。
        subject_cn: 主题 CN（可能为空串）。
        dns_names: SAN 中的 DNS 名列表。
        fingerprint: 证书 DER 的 SHA-256 指纹（大写冒号分隔）。
    """

    cert_path: Path
    key_path: Path
    not_before: datetime.datetime
    not_after: datetime.datetime
    subject_cn: str
    dns_names: tuple[str, ...]
    fingerprint: str


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _load_certificate(cert_path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(cert_path.read_bytes())
    except FileNotFoundError as exc:
        raise TlsError(f"证书文件不存在: {cert_path}") from exc
    except (OSError, ValueError) as exc:
        raise TlsError(f"证书解析失败（需 PEM 格式）: {cert_path}") from exc


def _public_key_der(pub: asym_types.PublicKeyTypes) -> bytes:
    """公钥的规范化 DER 序列化（跨算法可比较，无需逐个提取公钥数值）。"""
    return pub.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _check_key_matches(cert: x509.Certificate, key_path: Path) -> None:
    """私钥可读、未加密、且与证书公钥匹配。

    私钥材料只用于内存中比较公钥，不进入任何异常文案（异常只带路径）。
    """
    try:
        raw = key_path.read_bytes()
    except FileNotFoundError as exc:
        raise TlsError(f"私钥文件不存在: {key_path}") from exc
    except OSError as exc:
        raise TlsError(f"私钥文件读取失败: {key_path}") from exc
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError, OSError) as exc:
        raise TlsError(f"私钥解析失败（需未加密 PEM 私钥）: {key_path}") from exc
    if _public_key_der(cert.public_key()) != _public_key_der(key.public_key()):
        raise TlsError(f"私钥与证书不匹配: {key_path}")


def _fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _validity(cert: x509.Certificate) -> tuple[datetime.datetime, datetime.datetime]:
    try:
        return cert.not_valid_before_utc, cert.not_valid_after_utc
    except AttributeError:  # 极旧 cryptography 兼容分支
        naive_b = cert.not_valid_before.replace(tzinfo=datetime.UTC)
        naive_a = cert.not_valid_after.replace(tzinfo=datetime.UTC)
        return naive_b, naive_a


def _cn(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else ""


def _dns_names(cert: x509.Certificate) -> tuple[str, ...]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return ()
    san = ext.value
    if not isinstance(san, x509.SubjectAlternativeName):  # pragma: no cover - 防御
        return ()
    return tuple(san.get_values_for_type(x509.DNSName))


def load_user_cert_bundle(cert_path: Path, key_path: Path) -> UserCertBundle:
    """加载并校验用户证书 + 私钥，返回元数据 bundle。

    校验链：PEM 可解析 → 私钥可读且与证书公钥匹配 → 未过期。
    任一失败抛 :class:`TlsError`（**不回落自签名**）。

    Args:
        cert_path: 证书 PEM 路径（全链文件按 cryptography 语义取首段叶证书）。
        key_path: 未加密 PEM 私钥路径。

    Returns:
        :class:`UserCertBundle`（仅元数据，不含私钥）。
    """
    cert = _load_certificate(cert_path)
    _check_key_matches(cert, key_path)
    not_before, not_after = _validity(cert)
    status = check_expiry(not_before, not_after)
    if status is ExpiryStatus.EXPIRED:
        raise TlsError(
            f"用户证书已过期（{not_after:%Y-%m-%d %H:%M} UTC）: {cert_path}"
            " —— 请更换证书，v5 不会回落自签名"
        )
    return UserCertBundle(
        cert_path=cert_path,
        key_path=key_path,
        not_before=not_before,
        not_after=not_after,
        subject_cn=_cn(cert),
        dns_names=_dns_names(cert),
        fingerprint=_fingerprint(cert),
    )


# ---------------------------------------------------------------------------
# 有效期 / 轮换
# ---------------------------------------------------------------------------


def check_expiry(
    not_before: datetime.datetime,
    not_after: datetime.datetime,
    *,
    now: datetime.datetime | None = None,
    warn_days: int = EXPIRY_WARN_DAYS,
) -> ExpiryStatus:
    """证书有效期分类（时钟可注入，测试全确定性）。

    Args:
        not_before: 生效时间（aware UTC）。
        not_after: 到期时间（aware UTC）。
        now: 判定时钟；``None`` 取当前 UTC。
        warn_days: 临期阈值天数。
    """
    current = now if now is not None else datetime.datetime.now(datetime.UTC)
    if current >= not_after:
        return ExpiryStatus.EXPIRED
    if (not_after - current) <= datetime.timedelta(days=warn_days):
        return ExpiryStatus.EXPIRING
    return ExpiryStatus.VALID


def rotation_advice(
    bundle: UserCertBundle, *, now: datetime.datetime | None = None
) -> str | None:
    """给运维/审计的轮换建议文案；无需动作返回 ``None``。"""
    status = check_expiry(bundle.not_before, bundle.not_after, now=now)
    if status is ExpiryStatus.EXPIRED:
        return (
            f"证书已过期（{bundle.not_after:%Y-%m-%d}）: 更换 {bundle.cert_path} 后重启服务"
        )
    if status is ExpiryStatus.EXPIRING:
        current = now if now is not None else datetime.datetime.now(datetime.UTC)
        left = max(0, (bundle.not_after - current).days)
        return (
            f"证书剩余 {left} 天（{bundle.not_after:%Y-%m-%d} 到期）: "
            "请尽快向 CA 申请新证书替换"
        )
    return None


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def describe_cert(bundle: UserCertBundle) -> str:
    """一行摘要（不含私钥任何信息）。"""
    san = ",".join(bundle.dns_names) if bundle.dns_names else "(无SAN)"
    return (
        f"subject_cn={bundle.subject_cn or '(无CN)'} san={san} "
        f"valid_until={bundle.not_after:%Y-%m-%d %H:%M}UTC "
        f"fp={_sha16(bundle.fingerprint)}…"
    )


def _sha16(fingerprint: str) -> str:
    """指纹前 16 字符（展示缩写，避免整串刷日志）。"""
    return fingerprint[:16]

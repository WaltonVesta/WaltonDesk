"""security/certs 单元测试（阶段 9.3）：真实 cryptography 往返，零网络。"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from rdc.exceptions import TlsError
from rdc.security.certs import (
    EXPIRY_WARN_DAYS,
    ExpiryStatus,
    UserCertBundle,
    check_expiry,
    describe_cert,
    load_user_cert_bundle,
    rotation_advice,
)

UTC = datetime.UTC


def make_cert(
    tmp_path: Path,
    *,
    name: str = "user",
    days_valid: int = 365,
    days_past: int = 0,
    san: tuple[str, ...] = ("rdc.example.com",),
    cn: str = "rdc.example.com",
    key: str = "rsa",
) -> tuple[Path, Path]:
    """生成一对 PEM 证书/私钥（自签，仅测试材料）。"""
    if key == "rsa":
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        priv = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(UTC) - datetime.timedelta(days=days_past)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in san]), critical=False
        )
    cert = builder.sign(priv, hashes.SHA256())
    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class TestLoadUserCertBundle:
    def test_valid_bundle_metadata(self, tmp_path: Path) -> None:
        cert_path, key_path = make_cert(tmp_path)
        bundle = load_user_cert_bundle(cert_path, key_path)
        assert bundle.subject_cn == "rdc.example.com"
        assert bundle.dns_names == ("rdc.example.com",)
        assert bundle.not_after > bundle.not_before
        assert len(bundle.fingerprint.split(":")) == 32  # SHA-256 hex 分组

    def test_ec_key_accepted(self, tmp_path: Path) -> None:
        cert_path, key_path = make_cert(tmp_path, name="ec", key="ec")
        bundle = load_user_cert_bundle(cert_path, key_path)
        assert bundle.dns_names == ("rdc.example.com",)

    def test_missing_cert_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(TlsError, match="证书文件不存在"):
            load_user_cert_bundle(tmp_path / "nope.crt", tmp_path / "nope.key")

    def test_garbage_cert_rejected(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "garbage.crt"
        cert_path.write_bytes(b"not a pem")
        _, key_path = make_cert(tmp_path, name="g")
        with pytest.raises(TlsError, match="PEM"):
            load_user_cert_bundle(cert_path, key_path)

    def test_mismatched_key_rejected(self, tmp_path: Path) -> None:
        cert_a, _ = make_cert(tmp_path, name="a")
        _, key_b = make_cert(tmp_path, name="b")
        with pytest.raises(TlsError, match="不匹配"):
            load_user_cert_bundle(cert_a, key_b)

    def test_encrypted_key_rejected(self, tmp_path: Path) -> None:
        cert_path, _ = make_cert(tmp_path, name="enc")
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = tmp_path / "enc.key"
        key_path.write_bytes(
            priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.BestAvailableEncryption(b"hunter2"),
            )
        )
        with pytest.raises(TlsError, match="未加密"):
            load_user_cert_bundle(cert_path, key_path)

    def test_expired_cert_rejected(self, tmp_path: Path) -> None:
        cert_path, key_path = make_cert(
            tmp_path, name="old", days_valid=30, days_past=40
        )
        with pytest.raises(TlsError, match="已过期"):
            load_user_cert_bundle(cert_path, key_path)

    def test_error_text_never_contains_key_bytes(self, tmp_path: Path) -> None:
        cert_path, _ = make_cert(tmp_path, name="leak")
        _, key_b = make_cert(tmp_path, name="leak2")  # 另一把私钥 → 不匹配
        with pytest.raises(TlsError) as ei:
            load_user_cert_bundle(cert_path, key_b)
        msg = str(ei.value)
        assert "PRIVATE KEY" not in msg
        assert "BEGIN" not in msg

    def test_no_san_yields_empty_tuple(self, tmp_path: Path) -> None:
        cert_path, key_path = make_cert(tmp_path, name="nosan", san=())
        bundle = load_user_cert_bundle(cert_path, key_path)
        assert bundle.dns_names == ()


class TestCheckExpiry:
    def _window(self) -> tuple[datetime.datetime, datetime.datetime]:
        now = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        return now, now + datetime.timedelta(days=30)

    def test_valid_far_from_expiry(self) -> None:
        nb, na = self._window()
        assert check_expiry(nb, na, now=nb + datetime.timedelta(days=1)) is ExpiryStatus.VALID

    def test_expiring_within_threshold(self) -> None:
        nb, na = self._window()
        now = na - datetime.timedelta(days=EXPIRY_WARN_DAYS)
        assert check_expiry(nb, na, now=now) is ExpiryStatus.EXPIRING

    def test_boundary_exactly_threshold_is_expiring(self) -> None:
        nb, na = self._window()
        now = na - datetime.timedelta(days=EXPIRY_WARN_DAYS)
        assert check_expiry(nb, na, now=now, warn_days=EXPIRY_WARN_DAYS) is ExpiryStatus.EXPIRING
        later = na - datetime.timedelta(days=EXPIRY_WARN_DAYS, seconds=-1)
        assert check_expiry(nb, na, now=later) is ExpiryStatus.EXPIRING

    def test_expired_at_not_after(self) -> None:
        nb, na = self._window()
        assert check_expiry(nb, na, now=na) is ExpiryStatus.EXPIRED

    def test_uses_real_clock_by_default(self) -> None:
        past = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
        assert check_expiry(past - datetime.timedelta(days=10), past) is ExpiryStatus.EXPIRED


class TestRotationAdvice:
    def bundle(self, tmp_path: Path, **kw: object) -> UserCertBundle:
        cp, kp = make_cert(tmp_path, name="rot", **kw)  # type: ignore[arg-type]
        return load_user_cert_bundle(cp, kp)

    def test_valid_cert_no_advice(self, tmp_path: Path) -> None:
        b = self.bundle(tmp_path)
        assert rotation_advice(b) is None

    def test_expiring_cert_advice_mentions_days(self, tmp_path: Path) -> None:
        b = self.bundle(tmp_path, days_valid=30, days_past=0)
        # 用未来时钟把它变成临期
        now = b.not_after - datetime.timedelta(days=3)
        advice = rotation_advice(b, now=now)
        assert advice is not None
        assert "剩余 3 天" in advice or "剩余 2 天" in advice

    def test_expired_cert_advice(self, tmp_path: Path) -> None:
        nb = datetime.datetime(2025, 1, 1, tzinfo=UTC)
        b = UserCertBundle(
            cert_path=Path("a.crt"), key_path=Path("a.key"),
            not_before=nb, not_after=nb + datetime.timedelta(days=1),
            subject_cn="", dns_names=(), fingerprint="AA",
        )
        advice = rotation_advice(b, now=nb + datetime.timedelta(days=10))
        assert advice is not None
        assert "已过期" in advice


class TestDescribeCert:
    def test_summary_contains_cn_san_date(self, tmp_path: Path) -> None:
        cp, kp = make_cert(tmp_path, name="desc")
        bundle = load_user_cert_bundle(cp, kp)
        text = describe_cert(bundle)
        assert "rdc.example.com" in text
        assert "valid_until=" in text
        assert "PRIVATE KEY" not in text


class TestNoSocketNoQt:
    def test_pure_security_layer(self) -> None:
        import inspect

        import rdc.security.certs as mod

        src = inspect.getsource(mod)
        for banned in ("PySide6", "import socket", "asyncio"):
            assert banned not in src

"""transport.tls 测试：SSLContext 构建、证书生命周期、自签名判定、TOFU。"""

from __future__ import annotations

import datetime
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from rdc.exceptions import TlsError
from rdc.security.fingerprint import FingerprintManager
from rdc.transport.tls import (
    build_client_ssl_context,
    build_server_ssl_context,
    ensure_server_cert,
    fingerprint_of_ssl_object,
    is_self_signed_error,
    tls_hostname,
    verify_peer_trust,
)
from tests._transport_support import FakeSSLObject

_DER = b"0\x82\x01\x02fake-der-cert"
_FP = FingerprintManager.compute_fingerprint(_DER)


class TestCertLifecycle:
    def test_generates_when_absent(self, tmp_path: Path) -> None:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        ensure_server_cert(cert, key)
        assert cert.exists() and key.exists()
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        assert loaded.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "RemoteDesktop"
        assert (loaded.not_valid_after_utc - datetime.datetime.now(datetime.UTC)).days > 360

    def test_reuses_valid_cert(self, tmp_path: Path) -> None:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        ensure_server_cert(cert, key)
        before = cert.read_bytes()
        ensure_server_cert(cert, key)
        assert cert.read_bytes() == before  # 未临期 → 不重生成

    def test_regenerates_expiring_cert(self, tmp_path: Path) -> None:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        _write_cert_valid_days(cert, key, days=3)  # <7 天阈值 → 触发续期
        ensure_server_cert(cert, key)
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        assert (loaded.not_valid_after_utc - datetime.datetime.now(datetime.UTC)).days > 300

    def test_regenerates_corrupt_cert(self, tmp_path: Path) -> None:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_bytes(b"garbage")
        key.write_bytes(b"garbage")
        ensure_server_cert(cert, key)
        x509.load_pem_x509_certificate(cert.read_bytes())  # 可解析 = 已重生成

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        cert = tmp_path / "sub" / "dir" / "cert.pem"
        ensure_server_cert(cert, tmp_path / "sub" / "dir" / "key.pem")
        assert cert.exists()


def _write_cert_valid_days(cert: Path, key: Path, days: int) -> None:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    c = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RemoteDesktop")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RemoteDesktop")]))
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(k, hashes.SHA256())
    )
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )


class TestSslContexts:
    def test_server_context_forces_tls12(self, tmp_path: Path) -> None:
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        ensure_server_cert(cert, key)
        ctx = build_server_ssl_context(cert, key)
        assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2

    def test_server_context_missing_files_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TlsError, match="证书加载失败"):
            build_server_ssl_context(tmp_path / "no.pem", tmp_path / "nokey.pem")

    def test_client_verify_true_default(self) -> None:
        ctx = build_client_ssl_context()
        assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
        assert ctx.verify_mode is ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_client_verify_false_insecure(self) -> None:
        ctx = build_client_ssl_context(verify=False)
        assert ctx.verify_mode is ssl.CERT_NONE
        assert ctx.check_hostname is False
        assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2


class TestSelfSignedDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate (_ssl.c:1010)",
            "[SSL: CERTIFICATE_VERIFY_FAILED] ... self-signed certificate",
        ],
    )
    def test_true_for_self_signed(self, text: str) -> None:
        assert is_self_signed_error(ssl.SSLError(text))

    @pytest.mark.parametrize(
        "text",
        [
            "certificate has expired",
            "hostname mismatch, certificate is not valid for 'evil.com'",
            "[SSL: UNKNOWN_CA] unknown ca",
            # v5 收紧：裸公共错误串不再触发降级（v4 过宽匹配的行为差异，锁定）
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate",
        ],
    )
    def test_false_for_other_cert_errors(self, text: str) -> None:
        assert not is_self_signed_error(ssl.SSLError(text))


class TestFingerprintOfSsl:
    def test_none_for_plain_or_no_cert(self) -> None:
        assert fingerprint_of_ssl_object(None) is None
        assert fingerprint_of_ssl_object(FakeSSLObject(None)) is None

    def test_format(self) -> None:
        fp = fingerprint_of_ssl_object(FakeSSLObject(_DER))
        assert fp == _FP
        groups = fp.split(":")
        assert len(groups) == 32 and all(len(g) == 2 for g in groups)
        assert fp == fp.upper()


@pytest.fixture
def fp_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FingerprintManager:
    """文件路径重定向到 tmp 的真实 FingerprintManager（不污染用户目录）。"""
    import rdc.security.fingerprint as fp_mod

    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_path / "known.json")
    return FingerprintManager()


class TestVerifyPeerTrust:
    def test_first_confirm_then_auto_trust(self, fp_manager: FingerprintManager) -> None:
        ssl_obj = FakeSSLObject(_DER)
        asked: list[tuple[str, str]] = []

        def confirm(fp: str, host: str) -> bool:
            asked.append((fp, host))
            return True

        assert verify_peer_trust(ssl_obj, "10.0.0.9", confirm=confirm, manager=fp_manager) == _FP
        assert asked == [(_FP, "10.0.0.9")]
        # 第二次：已在信任列表 → 不再弹确认
        assert verify_peer_trust(ssl_obj, "10.0.0.9", confirm=confirm, manager=fp_manager) == _FP
        assert len(asked) == 1

    def test_user_rejects(self, fp_manager: FingerprintManager) -> None:
        with pytest.raises(TlsError, match="拒绝"):
            verify_peer_trust(
                FakeSSLObject(_DER), "h", confirm=lambda fp, host: False, manager=fp_manager
            )
        # 拒绝后不留信任记录
        assert not fp_manager.is_trusted(_FP)

    def test_no_confirm_callback_rejected_v5(self, fp_manager: FingerprintManager) -> None:
        """v5 收紧：无确认回调不再默认信任（v4 静默放行行为禁止）。"""
        with pytest.raises(TlsError, match="确认回调"):
            verify_peer_trust(FakeSSLObject(_DER), "h", confirm=None, manager=fp_manager)

    def test_no_cert_rejected(self, fp_manager: FingerprintManager) -> None:
        with pytest.raises(TlsError, match="无法获取对端证书"):
            verify_peer_trust(None, "h", confirm=lambda fp, host: True, manager=fp_manager)

    def test_trust_persisted_to_disk(self, tmp_path: Path, fp_manager: FingerprintManager) -> None:
        fp_manager.trust(_FP, "h")
        assert (tmp_path / "known.json").exists()


def test_tls_hostname_strips_v6_brackets() -> None:
    assert tls_hostname("[::1]") == "::1"
    assert tls_hostname("host.local") == "host.local"

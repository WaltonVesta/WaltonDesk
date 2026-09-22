"""HMAC-SHA256 完整性模块测试（阶段 3，3.6）。

覆盖三条密钥来源路径与签名/验签核心：

1. TLS exporter（Protocol 适配器契约）—— 2026-09-23 实测：stdlib
   ``ssl`` 全链**没有** ``export_keying_material``（任务书引用的 API 在
   Python 中不存在），本层只按 Protocol 契约测试；真实 TLS 栈接入
   （候选 pyOpenSSL ``Connection``，Apache-2.0）是阶段 4 ``transport/tls.py``
   的架构决策，届时补端到端"双端派生同钥"集成测试。
2. HKDF 兜底（真实 cryptography 实现）。
3. compute/verify（篡改必拒：载荷/头部/密钥/trailer 任一变化）。
"""

from __future__ import annotations

import struct

import pytest
from cryptography.hazmat.primitives import hashes

from rdc.exceptions import HmacVerificationError, ProtocolError
from rdc.protocol import constants as C
from rdc.protocol.hmac_integrity import (
    TLS_EXPORTER_LABEL,
    compute_hmac,
    derive_key_from_secret,
    derive_key_from_tls,
    verify_hmac,
)


class _StubExporter:
    """镜像 RFC 5705 exporter 真实调用面（记录参数；seed 模拟会话主密钥）。"""

    def __init__(self, seed: bytes = b"\x00" * 32) -> None:
        self.calls: list[tuple[int, bytes, bytes | None]] = []
        self._seed = seed

    def export_keying_material(
        self, length: int, label: bytes, context: bytes | None = ..., use_context_prefix: bool = ...
    ) -> bytes:
        self.calls.append((length, label, context if context is not ... else None))
        digest = hashes.Hash(hashes.SHA256())
        digest.update(self._seed + label + (context or b""))
        raw = digest.finalize()
        return (raw * ((length + 31) // 32 + 1))[:length]


class TestTlsDeriveContract:
    """Protocol 契约：参数、长度、双端对称性（同会话→同密钥）。"""

    def test_derive_calls_exporter_with_label_and_context(self) -> None:
        exp = _StubExporter()
        key = derive_key_from_tls(exp)
        assert len(exp.calls) == 1
        length, label, context = exp.calls[0]
        assert length == C.HMAC_SIZE
        assert label == TLS_EXPORTER_LABEL
        assert context == C.TLS_EXPORTER_CONTEXT
        assert len(key) == C.HMAC_SIZE

    def test_derive_custom_length(self) -> None:
        exp = _StubExporter()
        assert len(derive_key_from_tls(exp, length=64)) == 64

    def test_derive_rejects_nonpositive_length(self) -> None:
        with pytest.raises(ValueError, match="必须为正"):
            derive_key_from_tls(_StubExporter(), length=0)

    def test_label_prefix_follows_rfc5705(self) -> None:
        assert TLS_EXPORTER_LABEL.startswith(b"EXM-")

    def test_same_session_both_ends_derive_same_key(self) -> None:
        """核心性质：同一会话（同 seed）两端各自调用派生，结果必须一致。"""
        srv_side = _StubExporter(seed=b"shared-session-master" + b"\x11" * 9)
        cli_side = _StubExporter(seed=b"shared-session-master" + b"\x11" * 9)
        assert derive_key_from_tls(srv_side) == derive_key_from_tls(cli_side)

    def test_different_sessions_different_keys(self) -> None:
        """会话绑定：不同会话材料 → 不同密钥（MITM 无法跨会话伪造）。"""
        k1 = derive_key_from_tls(_StubExporter(seed=b"\x01" * 32))
        k2 = derive_key_from_tls(_StubExporter(seed=b"\x02" * 32))
        assert k1 != k2

    def test_stdlib_ssl_lacks_exporter_documented_assumption(self) -> None:
        """锁定 2026-09-23 实测事实：stdlib ssl 无 export_keying_material。

        若未来 CPython 补上该 API（ssl 对象真实可用），本测试会失败——
        届时阶段 4 应切换真实 stdlib 路径并删除本锁定测试。
        """
        import ssl

        for owner in (ssl.SSLSocket, ssl.SSLObject, ssl.SSLSession):
            assert not hasattr(owner, "export_keying_material"), owner.__name__

    def test_stub_satisfies_protocol_duck_typing(self) -> None:
        """结构匹配即可传入（Protocol 松耦合验证）。"""
        key = derive_key_from_tls(_StubExporter())
        assert len(key) == C.HMAC_SIZE


class TestHkdfFallback:
    def test_determinism_same_secret_same_key(self) -> None:
        assert derive_key_from_secret(b"m" * 32) == derive_key_from_secret(b"m" * 32)

    def test_different_secret_different_key(self) -> None:
        assert derive_key_from_secret(b"a" * 32) != derive_key_from_secret(b"b" * 32)

    def test_rejects_short_master_secret(self) -> None:
        with pytest.raises(ValueError, match="过短"):
            derive_key_from_secret(b"x" * 15)

    def test_accepts_16_byte_boundary(self) -> None:
        assert len(derive_key_from_secret(b"x" * 16)) == C.HMAC_SIZE

    def test_rejects_nonpositive_length(self) -> None:
        with pytest.raises(ValueError, match="必须为正"):
            derive_key_from_secret(b"x" * 32, length=0)


class TestComputeVerify:
    KEY = b"session-key-32bytes-long!!"

    def test_compute_returns_32_bytes(self) -> None:
        assert len(compute_hmac(self.KEY, b"h", b"p")) == C.HMAC_SIZE

    def test_verify_accepts_valid_tag(self) -> None:
        header = b"RDPC" + struct.pack(">BBB I", 2, 3, 4, 7)
        verify_hmac(self.KEY, header, b"1234567", compute_hmac(self.KEY, header, b"1234567"))

    def test_verify_rejects_tampered_payload(self) -> None:
        tag = compute_hmac(self.KEY, b"RDPC" + bytes(11), b"orig")
        with pytest.raises(HmacVerificationError):
            verify_hmac(self.KEY, b"RDPC" + bytes(11), b"tamp", tag)

    def test_verify_rejects_tampered_header(self) -> None:
        header = b"RDPC" + struct.pack(">BBB I", 2, 3, 0, 7)
        tag = compute_hmac(self.KEY, header, b"1234567")
        bad = b"RDPC" + struct.pack(">BBB I", 2, 9, 0, 7)
        with pytest.raises(HmacVerificationError):
            verify_hmac(self.KEY, bad, b"1234567", tag)

    def test_verify_rejects_wrong_key(self) -> None:
        tag = compute_hmac(b"key-a" * 8, b"h", b"p")
        with pytest.raises(HmacVerificationError):
            verify_hmac(b"key-b" * 8, b"h", b"p", tag)

    def test_verify_rejects_truncated_tag(self) -> None:
        tag = compute_hmac(self.KEY, b"h", b"p")
        with pytest.raises(HmacVerificationError, match="长度"):
            verify_hmac(self.KEY, b"h", b"p", tag[:31])

    def test_compute_rejects_empty_key(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            compute_hmac(b"", b"h", b"p")

    def test_hmac_error_is_protocol_error_subclass(self) -> None:
        assert issubclass(HmacVerificationError, ProtocolError)

    def test_fixed_header_length_prevents_ambiguity(self) -> None:
        """签名域 header(固定15B)+payload，定长头部天然消歧（无需长度前缀）。"""
        header = b"RDPC" + bytes(11)
        t1 = compute_hmac(self.KEY, header, b"CD")
        t2 = compute_hmac(self.KEY, header, b"CE")
        assert t1 != t2

    def test_header_length_change_flips_digest(self) -> None:
        """头部字节数变化（Length 字段被改写）必然改变摘要。"""
        payload = b"same"
        t1 = compute_hmac(self.KEY, b"RDPC" + bytes(11), payload)
        t2 = compute_hmac(self.KEY, b"RDPC" + bytes(10), payload)
        assert t1 != t2

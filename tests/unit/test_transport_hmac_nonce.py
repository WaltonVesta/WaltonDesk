"""transport.hmac_nonce 测试：HKDF 派生（F1 方案 B 落地验证）。"""

from __future__ import annotations

import pytest

from rdc.exceptions import ProtocolError
from rdc.transport.hmac_nonce import (
    CONNECTION_ID_SIZE,
    HKDF_INFO,
    HMAC_KEY_SIZE,
    NONCE_SIZE,
    derive_hmac_key,
    new_connection_id,
    new_nonce,
)


class TestGenerators:
    def test_nonce_sizes(self) -> None:
        assert len(new_nonce()) == NONCE_SIZE == 32
        assert len(new_connection_id()) == CONNECTION_ID_SIZE == 32

    def test_values_are_random_per_call(self) -> None:
        assert new_nonce() != new_nonce()
        assert new_connection_id() != new_connection_id()


class TestDerive:
    def test_both_ends_derive_same_key(self) -> None:
        """假 TLS 会话模拟：双端各自以相同入参独立派生得到同一 32B 密钥。"""
        c_nonce, s_nonce, cid = new_nonce(), new_nonce(), new_connection_id()
        client_key = derive_hmac_key(c_nonce, s_nonce, cid)
        server_key = derive_hmac_key(c_nonce, s_nonce, cid)
        assert client_key == server_key
        assert len(client_key) == HMAC_KEY_SIZE == 32

    def test_nonce_concat_order_matters(self) -> None:
        c_nonce, s_nonce, cid = new_nonce(), new_nonce(), new_connection_id()
        assert derive_hmac_key(c_nonce, s_nonce, cid) != derive_hmac_key(s_nonce, c_nonce, cid)

    def test_connection_id_binds_key(self) -> None:
        c_nonce, s_nonce = new_nonce(), new_nonce()
        k1 = derive_hmac_key(c_nonce, s_nonce, new_connection_id())
        k2 = derive_hmac_key(c_nonce, s_nonce, new_connection_id())
        assert k1 != k2  # 每连接密钥不同

    def test_different_nonces_different_keys(self) -> None:
        cid = new_connection_id()
        base = derive_hmac_key(new_nonce(), new_nonce(), cid)
        other = derive_hmac_key(new_nonce(), new_nonce(), cid)
        assert base != other

    def test_deterministic_for_fixed_inputs(self) -> None:
        c = b"\x11" * 32
        s = b"\x22" * 32
        cid = b"\x33" * 32
        assert derive_hmac_key(c, s, cid) == derive_hmac_key(c, s, cid)

    @pytest.mark.parametrize(
        ("client", "server", "cid"),
        [
            (b"", b"\x22" * 32, b"\x33" * 32),
            (b"\x11" * 31, b"\x22" * 32, b"\x33" * 32),
            (b"\x11" * 32, b"\x22" * 33, b"\x33" * 32),
            (b"\x11" * 32, b"\x22" * 32, b"\x33" * 16),
        ],
    )
    def test_short_inputs_rejected(self, client: bytes, server: bytes, cid: bytes) -> None:
        """熵不足输入一律拒绝（不悄悄削弱派生强度）。"""
        with pytest.raises(ProtocolError, match="32"):
            derive_hmac_key(client, server, cid)

    def test_info_constant_locked(self) -> None:
        assert HKDF_INFO == b"rdc-hmac-v1"

    def test_derived_key_usable_by_framing(self) -> None:
        """派生密钥可直接喂协议层：签名 → 验签往返。"""
        from rdc.protocol.codec import decode_frame, encode_frame
        from rdc.protocol.messages import Heartbeat, HeartbeatKind

        key = derive_hmac_key(new_nonce(), new_nonce(), new_connection_id())
        msg = Heartbeat(kind=HeartbeatKind.PING, timestamp=1.5)
        frame = encode_frame(msg, hmac_key=key)
        assert decode_frame(frame, hmac_key=key, require_hmac=True) == msg
        assert decode_frame(frame, hmac_key=key, require_hmac=False) == msg
        assert decode_frame(frame, hmac_key=new_nonce(), require_hmac=True) is None

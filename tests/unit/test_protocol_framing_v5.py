"""分帧层 v5 扩展测试（阶段 3，3.5）。

锁定 v5 相对 v4 的新行为：

- Version 真正启用：恒发 2，接收端拒绝其他版本（v4 里该字段是死字节）。
- PKT_FLAG_HMAC (0x08)：签名帧往返；篡改头/载荷任一字节必被拒；
  trailer 缺失（半包）等待不崩。
- 降级攻击防线：``require_hmac=True`` 拒绝无签名帧；带签名但无密钥拒绝。
- PKT_FLAG_PRE_COMPRESSED (0x10)：置位即跳过 zlib，标志互斥校验。
- 恶意/超限长度：PacketBuffer 分配前拒绝（叮嘱 c，v4 MAX_PACKET_SIZE 检查保留）。
- 魔数重同步修复：半截魔数尾部保留而非清空。
"""

from __future__ import annotations

import struct

import pytest

from rdc.exceptions import ProtocolError
from rdc.protocol import constants as C
from rdc.protocol.framing import HEADER_SIZE, Packet, PacketBuffer

KEY = b"hmac-session-key-32-bytes-ok!"


def _mk(payload: bytes = b"hello", flags: int = C.PKT_FLAG_NONE, type_: int = C.PKT_TYPE_CONTROL) -> Packet:
    return Packet(type_, payload, flags)


class TestVersionField:
    """Version 字段真实化（叮嘱 a：v4=1, v5=2）。"""

    def test_default_version_is_2(self) -> None:
        frame = _mk().to_bytes()
        assert frame[4] == C.PROTOCOL_VERSION_V5 == 2

    def test_explicit_version_4_rejected_on_parse(self) -> None:
        pkt = Packet(C.PKT_TYPE_CONTROL, b"x", C.PKT_FLAG_NONE, version=C.PROTOCOL_VERSION_V4)
        assert Packet.from_bytes(pkt.to_bytes()) is None

    def test_buffer_discards_unsupported_version(self) -> None:
        pkt = Packet(C.PKT_TYPE_CONTROL, b"x", C.PKT_FLAG_NONE, version=99)
        buf = PacketBuffer()
        buf.append(pkt.to_bytes())
        assert buf.pop_packet() is None
        assert len(buf.buffer) == 0  # 无法安全消费，整缓冲丢弃

    def test_packet_carries_parsed_version(self) -> None:
        restored = Packet.from_bytes(_mk().to_bytes())
        assert restored is not None and restored.version == 2


class TestHmacFlag:
    """PKT_FLAG_HMAC=0x08 全路径。"""

    def test_flag_value_is_0x08(self) -> None:
        assert C.PKT_FLAG_HMAC == 0x08
        # 与 v4 位不冲突（叮嘱 a）
        assert C.PKT_FLAG_HMAC not in (C.PKT_FLAG_COMPRESSED, C.PKT_FLAG_ENCRYPTED, C.PKT_FLAG_HAS_CRC)

    def test_signed_frame_round_trip(self) -> None:
        pkt = _mk(b"payload-bytes")
        frame = pkt.to_bytes(hmac_key=KEY)
        assert frame[6] & C.PKT_FLAG_HMAC
        assert len(frame) == HEADER_SIZE + len(b"payload-bytes") + C.HMAC_SIZE
        restored = Packet.from_bytes(frame, hmac_key=KEY)
        assert restored is not None
        assert restored.payload == b"payload-bytes"

    def test_unsigned_frame_round_trip_when_key_available(self) -> None:
        """会话有密钥但帧未签名（如首帧握手消息）：require_hmac=False 放行。"""
        frame = _mk().to_bytes()
        assert Packet.from_bytes(frame, hmac_key=KEY) is not None

    def test_tampered_payload_detected(self) -> None:
        frame = bytearray(_mk(b"original").to_bytes(hmac_key=KEY))
        frame[HEADER_SIZE + 2] ^= 0xFF
        assert Packet.from_bytes(bytes(frame), hmac_key=KEY) is None

    def test_tampered_header_detected(self) -> None:
        """CRC 只覆盖载荷；头部改写正是 HMAC 存在的理由（Type 字节篡改）。"""
        frame = bytearray(_mk(b"abc").to_bytes(hmac_key=KEY))
        frame[5] ^= 0x01  # Type 位翻转 → CRC 仍对（载荷未动）但 HMAC 必拒
        assert Packet.from_bytes(bytes(frame), hmac_key=KEY) is None

    def test_tampered_length_detected(self) -> None:
        frame = bytearray(_mk(b"0123456789").to_bytes(hmac_key=KEY))
        # Length 4B 位于 header[7:11]，缩减 1 字节（trailer 会被截进载荷）
        frame[7:11] = struct.pack(">I", 9)
        assert Packet.from_bytes(bytes(frame), hmac_key=KEY) is None

    def test_wrong_key_detected(self) -> None:
        frame = _mk().to_bytes(hmac_key=KEY)
        assert Packet.from_bytes(frame, hmac_key=b"other-key-32-bytes-long!!!!!!") is None

    def test_no_key_refuses_signed_frame(self) -> None:
        frame = _mk().to_bytes(hmac_key=KEY)
        assert Packet.from_bytes(frame) is None  # 无法验证 → 拒绝，而非放行

    def test_require_hmac_blocks_downgrade(self) -> None:
        """剥掉 HMAC 标志的降级帧在 require_hmac=True 下必拒（防降级攻击）。"""
        frame = bytearray(_mk(b"data").to_bytes(hmac_key=KEY))
        # 攻击面模拟：剥标志后 CRC 仍覆盖载荷（头部未校验），单靠 CRC 可被绕过
        frame[6] &= ~C.PKT_FLAG_HMAC
        del frame[-C.HMAC_SIZE:]  # 连 trailer 一起去掉，帧"看起来"未签名
        assert Packet.from_bytes(bytes(frame), hmac_key=KEY, require_hmac=False) is not None  # v4 式宽容
        assert Packet.from_bytes(bytes(frame), hmac_key=KEY, require_hmac=True) is None  # v5 强制

    def test_flags_hmac_without_key_raises_on_encode(self) -> None:
        pkt = Packet(C.PKT_TYPE_CONTROL, b"x", C.PKT_FLAG_HMAC)
        with pytest.raises(ProtocolError, match="拒绝降级发送"):
            pkt.to_bytes()

    def test_truncated_signed_frame_waits(self) -> None:
        frame = _mk(b"payload").to_bytes(hmac_key=KEY)
        assert Packet.from_bytes(frame[:-1], hmac_key=KEY) is None  # 半包：None 等待
        buf = PacketBuffer()
        buf.append(frame[: len(frame) - 8])
        assert buf.pop_packet(hmac_key=KEY) is None
        buf.append(frame[len(frame) - 8 :])
        assert buf.pop_packet(hmac_key=KEY) is not None


class TestPreCompressedFlag:
    """PKT_FLAG_PRE_COMPRESSED=0x10：显式声明"载荷已压缩"（不猜测内容）。"""

    def test_pre_compressed_skips_zlib(self) -> None:
        import zlib

        pkt = Packet(C.PKT_TYPE_SCREEN, b"already-compressed-jpeg", C.PKT_FLAG_PRE_COMPRESSED)
        frame = pkt.to_bytes()
        # 线上字节 == 原载荷（未被 zlib 二次处理）
        assert frame[HEADER_SIZE : HEADER_SIZE + len(pkt.payload)] == b"already-compressed-jpeg"
        restored = Packet.from_bytes(frame)
        assert restored is not None
        assert restored.payload == b"already-compressed-jpeg"
        assert zlib.crc32(b"already-compressed-jpeg") & 0xFFFFFFFF == restored.get_crc()

    def test_conflicting_flags_rejected_on_construct(self) -> None:
        with pytest.raises(ProtocolError, match="互斥"):
            Packet(C.PKT_TYPE_CONTROL, b"x", C.PKT_FLAG_COMPRESSED | C.PKT_FLAG_PRE_COMPRESSED)

    def test_conflicting_flags_rejected_on_parse(self) -> None:
        raw = bytearray(struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_COMPRESSED | C.PKT_FLAG_PRE_COMPRESSED, 1, 0,
        ) + b"x")
        assert Packet.from_bytes(bytes(raw)) is None

    def test_unknown_high_flags_rejected(self) -> None:
        raw = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, 0x20, 0, 0,
        )
        assert Packet.from_bytes(raw) is None


class TestMaliciousLength:
    """恶意长度字段防护（叮嘱 c：对应 v4 MAX_PACKET_SIZE 检查，v5 保留）。"""

    def _fake_header(self, length: int) -> bytes:
        return struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_NONE, length, 0,
        )

    def test_4gb_claim_rejected_no_oom(self) -> None:
        buf = PacketBuffer()
        buf.append(self._fake_header((1 << 32) - 1))  # 4 字节长度字段能声明的最大值（≈4GB）
        assert buf.pop_packet() is None
        assert len(buf.buffer) == 0

    def test_just_over_limit_rejected(self) -> None:
        buf = PacketBuffer()
        buf.append(self._fake_header(C.MAX_PACKET_SIZE + 1))
        assert buf.pop_packet() is None
        assert len(buf.buffer) == 0

    def test_from_bytes_oversized_none(self) -> None:
        raw = self._fake_header(C.MAX_PACKET_SIZE + 1)
        assert Packet.from_bytes(raw) is None

    def test_hmac_plus_oversized_still_rejected(self) -> None:
        frame = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_HMAC, C.MAX_PACKET_SIZE + 1, 0,
        )
        assert Packet.from_bytes(frame, hmac_key=KEY) is None


class TestResyncFix:
    """魔数重同步修复：半截魔数不再被当垃圾清掉。"""

    def test_partial_magic_tail_preserved(self) -> None:
        """缓冲区 ≥ 头部长度但无完整魔数：尾部魔数前缀保留（v4 会误清丢帧）。"""
        pkt = _mk(b"y")
        frame = pkt.to_bytes()
        buf = PacketBuffer()
        # 13 字节垃圾 + 帧的前 2 字节 "RD" → 共 15 字节触发重同步逻辑
        buf.append(b"XXXXXXXXXXXXX" + frame[:2])
        assert buf.pop_packet() is None
        assert bytes(buf.buffer) == b"RD"  # v4 行为是 clear()，v5 保留前缀
        buf.append(frame[2:])  # 补齐 "PC" + 头部其余 + 载荷
        restored = buf.pop_packet()
        assert restored is not None
        assert restored.payload == b"y"

    def test_garbage_still_skipped(self) -> None:
        buf = PacketBuffer()
        buf.append(b"\x00\xffgarbage" + _mk(b"ok").to_bytes())
        restored = buf.pop_packet()
        assert restored is not None and restored.payload == b"ok"


class TestCrcCoverage:
    """CRC 基于线上（压缩后）字节；HMAC 覆盖头+线上载荷。"""

    def test_crc_on_wire_payload_compressed(self) -> None:
        import zlib

        pkt = Packet(C.PKT_TYPE_CONTROL, b"z" * 500, C.PKT_FLAG_COMPRESSED)
        frame = pkt.to_bytes()
        _, _, _, flags, length, crc = struct.unpack(C.HEADER_FORMAT, frame[:HEADER_SIZE])
        assert flags & C.PKT_FLAG_COMPRESSED
        wire = frame[HEADER_SIZE : HEADER_SIZE + length]
        assert crc == zlib.crc32(wire) & 0xFFFFFFFF
        assert zlib.decompress(wire) == b"z" * 500

    def test_crc_tampered_rejected_without_hmac(self) -> None:
        """无 HMAC 会话下，CRC 仍是完整性防线：篡改载荷末字节必拒。"""
        frame = bytearray(_mk(b"payload" * 10).to_bytes())
        frame[-1] ^= 0xFF
        assert Packet.from_bytes(bytes(frame)) is None

    def test_two_frames_concatenated(self) -> None:
        """TCP 粘连两帧：依次弹出，顺序与内容都不错位。"""
        a, b = _mk(b"first-frame"), _mk(b"second-frame")
        buf = PacketBuffer()
        buf.append(a.to_bytes() + b.to_bytes())
        assert buf.pop_packet() == a
        assert buf.pop_packet() == b
        assert buf.pop_packet() is None  # 缓冲区已空

    def test_partial_frame_waits_for_rest(self) -> None:
        """半包：不足一帧时返回 None 不清缓冲，补齐后可完整弹出。"""
        pkt = _mk(b"x" * 200)
        raw = pkt.to_bytes()
        buf = PacketBuffer()
        buf.append(raw[:10])
        assert buf.pop_packet() is None
        buf.append(raw[10:])
        assert buf.pop_packet() == pkt

    def test_value_equality_after_round_trip(self) -> None:
        pkt = _mk(b"abc")
        assert Packet.from_bytes(pkt.to_bytes()) == pkt

    def test_packet_equality_type_sensitive(self) -> None:
        assert _mk(b"a") != Packet(C.PKT_TYPE_HEARTBEAT, b"a", C.PKT_FLAG_NONE)

    def test_packet_eq_non_packet_returns_notimplemented(self) -> None:
        pkt = _mk(b"a")
        assert pkt.__eq__("not-a-packet") is NotImplemented
        assert pkt != "not-a-packet"

    def test_packet_hashable_by_value(self) -> None:
        a, b = _mk(b"same"), _mk(b"same")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1


class TestCompressFallback:
    """zlib 压缩失败（极端输入）降级为不压缩，不静默吞错。"""

    def test_compress_failure_falls_back_to_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import zlib as _zlib

        from rdc.protocol import framing as fr

        def _boom(*args: object, **kwargs: object) -> bytes:
            raise _zlib.error("synthetic failure")

        monkeypatch.setattr(fr.zlib, "compress", _boom)
        pkt = Packet(C.PKT_TYPE_CONTROL, b"data" * 100, C.PKT_FLAG_COMPRESSED)
        frame = pkt.to_bytes()
        assert not pkt.flags & C.PKT_FLAG_COMPRESSED  # 标志已撤销
        restored = Packet.from_bytes(frame)
        assert restored is not None
        assert restored.payload == b"data" * 100

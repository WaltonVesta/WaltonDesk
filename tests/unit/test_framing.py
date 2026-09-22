"""协议分帧往返回归测试（阶段 1）。

覆盖 REFACTOR_PLAN 阶段 1 验收：为搬迁后的 ``rdc.protocol.framing`` 建立
回归基线，确保 v4 分帧逻辑在 src layout 迁移后行为不变。

阶段 3 将在此扩展 Protobuf 载荷、HMAC 完整性、以及 hypothesis property-based
往返测试；本文件先锁定 v4 JSON 分帧的字节级行为（向后兼容的安全网）。
"""

from __future__ import annotations

import zlib

import pytest

from rdc.constants import (
    PKT_FLAG_COMPRESSED,
    PKT_FLAG_HAS_CRC,
    PKT_TYPE_CONTROL,
    PKT_TYPE_HEARTBEAT,
    PKT_TYPE_SCREEN,
    PROTOCOL_MAGIC,
)
from rdc.protocol.framing import HEADER_SIZE, Packet, PacketBuffer


class TestPacketRoundTrip:
    """Packet 序列化 / 反序列化往返。"""

    def test_json_round_trip_uncompressed(self) -> None:
        """不压缩时 JSON 载荷往返后内容一致。"""
        original = {"type": "ping", "timestamp": 1234567.89}
        pkt = Packet.create_json(PKT_TYPE_HEARTBEAT, original, compress=False)
        raw = pkt.to_bytes()
        restored = Packet.from_bytes(raw)
        assert restored is not None
        assert restored.type == PKT_TYPE_HEARTBEAT
        assert restored.to_json() == original

    def test_json_round_trip_compressed(self) -> None:
        """压缩时往返后内容一致，且压缩标志与 CRC 标志都在。"""
        original = {"type": "welcome", "version": "5.0.0", "pad": "x" * 500}
        pkt = Packet.create_json(PKT_TYPE_CONTROL, original, compress=True)
        raw = pkt.to_bytes()
        restored = Packet.from_bytes(raw)
        assert restored is not None
        assert restored.flags & PKT_FLAG_COMPRESSED
        assert restored.flags & PKT_FLAG_HAS_CRC
        assert restored.to_json() == original

    def test_header_layout_is_15_bytes(self) -> None:
        """包头固定 15 字节，魔数在头部（v4 wire 兼容的硬约束）。"""
        assert HEADER_SIZE == 15
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"a": 1}, compress=False)
        raw = pkt.to_bytes()
        assert raw[:4] == PROTOCOL_MAGIC

    def test_unicode_payload_survives(self) -> None:
        """含中文的 JSON 载荷（ensure_ascii=False）往返不丢字符。"""
        original = {"text": "远程桌面控制", "emoji": "🖥️"}
        pkt = Packet.create_json(PKT_TYPE_CONTROL, original, compress=False)
        restored = Packet.from_bytes(pkt.to_bytes())
        assert restored is not None
        assert restored.to_json() == original


class TestPacketScreen:
    """屏幕包的 meta + 图像字节复合结构。"""

    def test_screen_round_trip(self) -> None:
        """create_screen / parse_screen 往返：meta 与图像字节都还原。"""
        img = bytes(range(256)) * 4  # 1KB 伪图像数据
        pkt = Packet.create_screen(
            img, width=1920, height=1080, quality=60,
            is_diff=True, offset_x=32, offset_y=64, compress=False,
        )
        restored = Packet.from_bytes(pkt.to_bytes())
        assert restored is not None
        assert restored.type == PKT_TYPE_SCREEN
        meta, restored_img = restored.parse_screen()
        assert restored_img == img
        assert meta["width"] == 1920
        assert meta["height"] == 1080
        assert meta["is_diff"] is True
        assert meta["offset_x"] == 32
        assert meta["offset_y"] == 64

    def test_parse_screen_rejects_non_screen_type(self) -> None:
        """对非屏幕包调用 parse_screen 抛 ValueError。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"a": 1}, compress=False)
        restored = Packet.from_bytes(pkt.to_bytes())
        assert restored is not None
        with pytest.raises(ValueError):
            restored.parse_screen()


class TestPacketRejects:
    """损坏 / 恶意输入的防御性拒绝。"""

    def test_bad_magic_returns_none(self) -> None:
        """魔数不匹配返回 None，不抛异常。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"a": 1}, compress=False)
        raw = bytearray(pkt.to_bytes())
        raw[0:4] = b"XXXX"
        assert Packet.from_bytes(bytes(raw)) is None

    def test_truncated_returns_none(self) -> None:
        """数据不足一个完整包时返回 None。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"a": 1}, compress=False)
        raw = pkt.to_bytes()
        assert Packet.from_bytes(raw[: HEADER_SIZE + 1]) is None

    def test_crc_mismatch_returns_none(self) -> None:
        """CRC 校验失败返回 None（篡改载荷最后一个字节）。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"a": "b" * 100}, compress=False)
        raw = bytearray(pkt.to_bytes())
        raw[-1] ^= 0xFF  # 翻转末字节，破坏 CRC
        assert Packet.from_bytes(bytes(raw)) is None


class TestPacketBuffer:
    """粘包 / 拆包处理。"""

    def test_two_packets_concatenated(self) -> None:
        """两个包粘连时能依次弹出，且各自内容正确。"""
        p1 = Packet.create_json(PKT_TYPE_HEARTBEAT, {"n": 1}, compress=False)
        p2 = Packet.create_json(PKT_TYPE_CONTROL, {"n": 2}, compress=False)
        buf = PacketBuffer()
        buf.append(p1.to_bytes() + p2.to_bytes())
        r1 = buf.pop_packet()
        r2 = buf.pop_packet()
        assert r1 is not None and r1.to_json() == {"n": 1}
        assert r2 is not None and r2.to_json() == {"n": 2}
        assert buf.pop_packet() is None  # 缓冲区已空

    def test_split_packet_reassembles(self) -> None:
        """一个包分多次到达时，前几次返回 None，凑齐后弹出完整包。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"k": "v" * 50}, compress=False)
        raw = pkt.to_bytes()
        buf = PacketBuffer()
        buf.append(raw[:5])
        assert buf.pop_packet() is None  # 头部都不完整
        buf.append(raw[5:])
        restored = buf.pop_packet()
        assert restored is not None
        assert restored.to_json()["k"] == "v" * 50

    def test_garbage_before_magic_is_skipped(self) -> None:
        """魔数前的垃圾字节被跳过，仍能解出后续有效包。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"ok": True}, compress=False)
        buf = PacketBuffer()
        buf.append(b"\x00\x01\x02garbage" + pkt.to_bytes())
        restored = buf.pop_packet()
        assert restored is not None
        assert restored.to_json() == {"ok": True}

    def test_oversized_length_clears_buffer(self) -> None:
        """长度字段超限（恶意/损坏）时清空缓冲区并返回 None，不 OOM。"""
        buf = PacketBuffer()
        # 构造一个 length 字段为超大值的假头部
        import struct

        from rdc.constants import MAX_PACKET_SIZE
        fake_header = struct.pack(
            ">4sBBB I I", PROTOCOL_MAGIC, 1, PKT_TYPE_CONTROL, 0,
            MAX_PACKET_SIZE + 1, 0,
        )
        buf.append(fake_header)
        assert buf.pop_packet() is None
        assert len(buf.buffer) == 0  # 缓冲区已被清空


class TestCompression:
    """压缩标志的实际效果。"""

    def test_compressed_payload_smaller_for_redundant_data(self) -> None:
        """高冗余数据压缩后 payload 比未压缩小（验证 zlib 确实生效）。"""
        redundant = {"data": "a" * 2000}
        comp = Packet.create_json(PKT_TYPE_CONTROL, redundant, compress=True)
        plain = Packet.create_json(PKT_TYPE_CONTROL, redundant, compress=False)
        # 压缩包的总字节应显著小于未压缩包
        assert len(comp.to_bytes()) < len(plain.to_bytes())
        # 解压后内容仍一致
        restored = Packet.from_bytes(comp.to_bytes())
        assert restored is not None
        assert restored.to_json() == redundant

    def test_crc_computed_on_compressed_payload(self) -> None:
        """CRC 基于压缩后的 payload（与 v4 行为一致）。"""
        pkt = Packet.create_json(PKT_TYPE_CONTROL, {"d": "x" * 1000}, compress=True)
        raw = pkt.to_bytes()
        # 从头部取出 CRC 与 length，验证 CRC == zlib.crc32(压缩后 payload)
        import struct
        _, _, _, flags, length, crc = struct.unpack(">4sBBB I I", raw[:HEADER_SIZE])
        compressed_payload = raw[HEADER_SIZE: HEADER_SIZE + length]
        assert flags & PKT_FLAG_HAS_CRC
        assert crc == zlib.crc32(compressed_payload) & 0xFFFFFFFF

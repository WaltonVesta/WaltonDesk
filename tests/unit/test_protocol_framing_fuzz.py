"""分帧层模糊测试（阶段 3，3.9）。

不变量（验收标准：10,000 例不崩溃）：

1. **不崩溃**：任意字节喂进 PacketBuffer / Packet.from_bytes 不抛非协议异常；
2. **不 OOM**：恶意长度字段（含 4GB 声明）在**分配前**被拒（叮嘱 c）；
3. **不死循环**：pop_packet 单次调用有确定的工作上限（至多一次重同步 + 一次解析）；
4. **缓冲区有界**：无论输入如何，缓冲区大小不无界增长（合法帧被消费、
   超限/非法版本被清空、其余等待但不复制膨胀）。

种子固定（derive 自测试名），失败可复现；hypothesis 侧再叠加随机字节。
"""

from __future__ import annotations

import random
import struct

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rdc.protocol import constants as C
from rdc.protocol.framing import HEADER_SIZE, Packet, PacketBuffer

_KEY = b"fuzz-session-key-32-bytes-!!"


def _valid_frame(payload: bytes = b"fuzz", type_: int = C.PKT_TYPE_CONTROL) -> bytes:
    return Packet(type_, payload, C.PKT_FLAG_NONE).to_bytes()


class TestRandomBytesFuzz:
    """纯随机字节流：10,000 例（seed=阶段3固定）不崩溃、不越界。"""

    def test_10k_random_chunks(self) -> None:
        rng = random.Random(20260922)  # 固定种子，失败可复现
        for _ in range(10_000):
            buf = PacketBuffer()
            mode = rng.randrange(4)
            if mode == 0:
                data = bytes(rng.randbytes(rng.randrange(0, 64)))
            elif mode == 1:  # 完整合法帧 + 随机尾巴
                data = _valid_frame(bytes(rng.randbytes(rng.randrange(0, 16)))) + bytes(rng.randbytes(rng.randrange(0, 8)))
            elif mode == 2:  # 随机切分的合法帧（半包/粘包交错）
                frame = _valid_frame(bytes(rng.randbytes(rng.randrange(0, 16))))
                cut = rng.randrange(1, max(2, len(frame)))
                buf.append(frame[:cut])
                buf.pop_packet()
                data = frame[cut:]
            else:  # 随机魔数头 + 随机声明长度
                length = rng.choice([0, 1, 4096, C.MAX_PACKET_SIZE - 1, C.MAX_PACKET_SIZE, C.MAX_PACKET_SIZE + 1, 2**32 - 1])
                flags = rng.randrange(256)
                data = struct.pack(C.HEADER_FORMAT, C.PROTOCOL_MAGIC, rng.randrange(256),
                                   rng.randrange(256), flags, length, rng.randrange(2**32))
                data += bytes(rng.randbytes(rng.randrange(0, 32)))
            buf.append(data)
            # 至多弹 3 次防无界循环（正常也弹不出更多）
            for _ in range(3):
                pkt = buf.pop_packet()
                if pkt is None:
                    break
                assert isinstance(pkt, Packet)
            assert len(buf.buffer) <= max(C.MAX_PACKET_SIZE * 2, len(data))  # 宽松上界：不复制膨胀

    def test_10k_from_bytes_garbage(self) -> None:
        rng = random.Random(20260923)
        for _ in range(10_000):
            n = rng.randrange(0, 80)
            data = bytes(rng.randbytes(n))
            out = Packet.from_bytes(data, hmac_key=_KEY)
            assert out is None or isinstance(out, Packet)
            out2 = Packet.from_bytes(data)  # 无密钥路径
            assert out2 is None or isinstance(out2, Packet)


class TestMaliciousLengthNeverAllocates:
    """恶意长度声明（叮嘱 c）：无论怎么声明，缓冲区不增长到超上限。"""

    @pytest.mark.parametrize("length", [
        C.MAX_PACKET_SIZE + 1,
        1 << 31,
        (1 << 32) - 1,  # 4GB 级声明
    ])
    def test_huge_length_rejected_before_alloc(self, length: int) -> None:
        buf = PacketBuffer()
        header = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_NONE, length, 0,
        )
        buf.append(header + b"tail")
        assert buf.pop_packet() is None
        assert len(buf.buffer) == 0  # 整缓冲已清空，未等待凑 4GB

    def test_huge_length_with_hmac_flag_also_rejected(self) -> None:
        buf = PacketBuffer()
        header = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_HMAC, (1 << 32) - 1, 0,
        )
        buf.append(header)
        assert buf.pop_packet(hmac_key=_KEY, require_hmac=True) is None
        assert len(buf.buffer) == 0

    def test_just_under_limit_waits_without_consuming_memory(self) -> None:
        """合法上限内的声明只是等待凑帧（不 OOM：本测试只喂头部，缓冲区 = 头部大小）。"""
        buf = PacketBuffer()
        header = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_NONE, 1024 * 1024, 0,
        )
        buf.append(header)
        assert buf.pop_packet() is None
        assert len(buf.buffer) == HEADER_SIZE  # 等待中，未分配 1MB


class TestZipBombDefense:
    """压缩解压展开防护：解压产物超限 / 尾部多余数据都要被拒。

    必须**手工构帧**：若走 ``Packet(COMPRESSED).to_bytes`` 会先压一层，
    解一层后得到的是内层流（小体积），测不到展开防护。
    """

    @staticmethod
    def _raw_compressed_frame(wire: bytes) -> bytes:
        import zlib

        header = struct.pack(
            C.HEADER_FORMAT, C.PROTOCOL_MAGIC, C.PROTOCOL_VERSION_V5,
            C.PKT_TYPE_CONTROL, C.PKT_FLAG_COMPRESSED, len(wire),
            zlib.crc32(wire) & 0xFFFFFFFF,
        )
        return header + wire

    def test_bomb_decompression_rejected(self) -> None:
        import zlib

        bomb = zlib.compress(b"\x00" * (C.MAX_PACKET_SIZE + 1024), level=9)  # 32MB+ 零 → 极小
        frame = self._raw_compressed_frame(bomb)
        assert Packet.from_bytes(frame) is None  # 解压超 MAX_PACKET_SIZE → 拒

    def test_trailing_data_in_zlib_rejected(self) -> None:
        import zlib

        wire = zlib.compress(b"legit", level=3) + b"EXTRA"
        frame = self._raw_compressed_frame(wire)
        assert Packet.from_bytes(frame) is None  # eof 后有未消费字节 → 拒


class TestHypothesisByteFuzz:
    """hypothesis 兜底：任意 bytes 喂 from_bytes / pop_packet 不崩溃。"""

    @settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=256))
    def test_from_bytes_never_raises(self, data: bytes) -> None:
        out = Packet.from_bytes(data, hmac_key=_KEY, require_hmac=True)
        assert out is None or isinstance(out, Packet)

    @settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(st.binary(max_size=128), max_size=20))
    def test_buffer_never_raises(self, chunks: list[bytes]) -> None:
        buf = PacketBuffer()
        for chunk in chunks:
            buf.append(chunk)
            pkt = buf.pop_packet(hmac_key=_KEY)
            assert pkt is None or isinstance(pkt, Packet)
        # 收尾再弹两次确保幂等无异常
        assert buf.pop_packet() is None or isinstance(buf.pop_packet(), Packet)

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        version=st.integers(0, 255),
        pkt_type=st.integers(0, 255),
        flags=st.integers(0, 255),
        length=st.integers(0, 2**32 - 1),
        crc=st.integers(0, 2**32 - 1),
        tail=st.binary(max_size=48),
    )
    def test_arbitrary_headers_safe(
        self, version: int, pkt_type: int, flags: int, length: int, crc: int, tail: bytes
    ) -> None:
        header = struct.pack(C.HEADER_FORMAT, C.PROTOCOL_MAGIC, version, pkt_type, flags, length, crc)
        data = header + tail
        out = Packet.from_bytes(data, hmac_key=_KEY)
        assert out is None or isinstance(out, Packet)
        buf = PacketBuffer()
        buf.append(data)
        pkt = buf.pop_packet(hmac_key=_KEY)
        assert pkt is None or isinstance(pkt, Packet)
        assert len(buf.buffer) <= len(data)

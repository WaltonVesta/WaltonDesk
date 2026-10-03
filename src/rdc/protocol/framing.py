"""网络协议分帧（15 字节包头 + 载荷），阶段 3 v5 重写。

包结构（大端，无对齐填充，实测 15 字节）::

    [Magic "RDPC" 4B][Version 1B][Type 1B][Flags 1B][Length 4B][CRC32 4B][Payload][HMAC?]

v5 相对 v4 的变化（范围调整后无 v4 兼容层，见 PROGRESS.md）：

- **Version 真正启用**：恒发 2（:data:`~rdc.protocol.constants.PROTOCOL_VERSION_V5`），
  接收端拒绝其他版本 —— v4 里该字段写 1 但从不校验，是死字段。
- **PKT_FLAG_HMAC (0x08)**：置位时 Payload 之后追加 32 字节 HMAC-SHA256 trailer。
  HMAC 覆盖 **头部 + 线上载荷（压缩后字节）**，补上 CRC32 保护不到的头部字段
  （Type/Flags/Length/Version 可被无感改写）。
- **PKT_FLAG_PRE_COMPRESSED (0x10)**：载荷已是 JPEG/H.264 等压缩态，
  发送端跳过 zlib（v4 热点：对已压缩数据二次 zlib 纯耗 CPU 还增大体积）。
  判定依据是显式标志位，不猜测载荷内容（2026-09-22 叮嘱 a）。
- **降级攻击防线**：``require_hmac=True`` 时无 HMAC 标志的帧一律拒绝。
  仅靠"有标志才校验"可被剥掉标志位绕过，会话是否强制 HMAC 由接收端
  策略（阶段 4 传输层，密钥协商完成后置 True）决定。
- **恶意长度防护保留并加强**（2026-09-22 叮嘱 c）：Length 超
  :data:`~rdc.protocol.constants.MAX_PACKET_SIZE` 在**分配前**拒绝并清空缓冲。
- 魔数重同步修复：v4 遇"半截魔数"（如 ``RDP`` 先到）会误清整个缓冲区丢数据，
  v5 保留可能是魔数前缀的尾部字节。

载荷编解码一律走 :mod:`rdc.protocol.codec`（Protobuf 领域消息）；本模块
v4 遗留的 JSON/屏幕包辅助方法已随阶段 9 批 4 遗留面清理删除。
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

from rdc.exceptions import HmacVerificationError, ProtocolError
from rdc.logging import get_logger
from rdc.protocol.constants import (
    HEADER_FORMAT,
    HEADER_SIZE,
    HMAC_SIZE,
    MAX_PACKET_SIZE,
    PKT_FLAG_COMPRESSED,
    PKT_FLAG_HAS_CRC,
    PKT_FLAG_HMAC,
    PKT_FLAG_NONE,
    PKT_FLAG_PRE_COMPRESSED,
    PKT_FLAG_UNKNOWN_STRICT_MASK,
    PROTOCOL_MAGIC,
    PROTOCOL_VERSION,
)
from rdc.protocol.hmac_integrity import compute_hmac, verify_hmac

_LOGGER = get_logger("protocol.framing")

__all__ = ["HEADER_SIZE", "Packet", "PacketBuffer"]

_ZLIB_LEVEL: Final[int] = 3  # 与 v4 一致：低级别换 CPU，网络场景足够


class Packet:
    """一个协议数据包：Type + Flags + 载荷字节。

    序列化 (:meth:`to_bytes`) 完成顺序为"压缩 → CRC → HMAC"，
    CRC 与 HMAC 都基于**线上字节**（压缩后），与接收端校验面对齐。

    Args:
        type: 载荷类型字节（见 :mod:`rdc.protocol.constants` 的 ``PKT_TYPE_*``）。
        payload: 载荷原始字节（未压缩态）。
        flags: 标志位按位或（COMPRESSED / PRE_COMPRESSED / HMAC 等）。
        version: 协议版本，默认 v5 (:data:`~rdc.protocol.constants.PROTOCOL_VERSION`)。

    Raises:
        ProtocolError: COMPRESSED 与 PRE_COMPRESSED 同时置位（语义冲突）。
    """

    __slots__ = ("type", "payload", "flags", "version", "_crc")

    def __init__(self, type: int, payload: bytes, flags: int = PKT_FLAG_NONE, version: int = PROTOCOL_VERSION) -> None:
        if (flags & PKT_FLAG_COMPRESSED) and (flags & PKT_FLAG_PRE_COMPRESSED):
            raise ProtocolError("COMPRESSED 与 PRE_COMPRESSED 标志互斥：载荷不能既待 zlib 又已压缩")
        self.type = type
        self.payload = payload
        self.flags = flags
        self.version = version
        self._crc = 0

    # -- 序列化 -------------------------------------------------------------

    def to_bytes(self, hmac_key: bytes | None = None) -> bytes:
        """序列化为帧字节。

        Args:
            hmac_key: 提供时以 HMAC-SHA256 签名并追加 32B trailer
                （自动置 ``PKT_FLAG_HMAC``）。

        Returns:
            完整帧（头部 + 线上载荷 + 可选 HMAC trailer）。

        Raises:
            ProtocolError: ``self.flags`` 已含 HMAC 位但未提供密钥
                （拒绝静默降级为不签名）。
        """
        payload = self.payload
        flags = self.flags

        # 已压缩载荷（JPEG/H.264）跳过 zlib；仅 COMPRESSED 位才做 zlib。
        if flags & PKT_FLAG_COMPRESSED and not flags & PKT_FLAG_PRE_COMPRESSED:
            try:
                payload = zlib.compress(payload, level=_ZLIB_LEVEL)
            except zlib.error as exc:  # 极端输入：降级为不压缩，不静默吞
                _LOGGER.warning("zlib_compress_failed_fallback_raw", error=str(exc))
                flags &= ~PKT_FLAG_COMPRESSED
                payload = self.payload

        if flags & PKT_FLAG_HMAC:
            if hmac_key is None:
                raise ProtocolError("flags 含 PKT_FLAG_HMAC 但未提供 hmac_key，拒绝降级发送")
        elif hmac_key is not None:
            flags |= PKT_FLAG_HMAC

        crc = zlib.crc32(payload) & 0xFFFFFFFF
        self._crc = crc
        flags |= PKT_FLAG_HAS_CRC
        self.flags = flags

        header = struct.pack(
            HEADER_FORMAT, PROTOCOL_MAGIC, self.version, self.type, flags, len(payload), crc
        )
        frame = header + payload
        if flags & PKT_FLAG_HMAC and hmac_key is not None:
            frame += compute_hmac(hmac_key, header, payload)
        return frame

    # -- 反序列化 -----------------------------------------------------------

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        hmac_key: bytes | None = None,
        require_hmac: bool = False,
    ) -> Packet | None:
        """从帧字节解析数据包。

        校验顺序刻意把**成本与危险**排在前面：魔数/版本/标志 → 长度上限
        （分配前）→ CRC → HMAC → zlib（有展开上限）。HMAC 校验发生在解压前，
        篡改在任何解压副作用（zip bomb）之前被拒。

        Args:
            data: 完整帧字节。
            hmac_key: 会话 HMAC 密钥；帧带 HMAC 位但密钥缺失时**拒绝**（无法验证）。
            require_hmac: 会话策略要求强制完整性保护；帧未带 HMAC 位时拒绝，
                封堵"剥标志降级"攻击。

        Returns:
            解析出的 :class:`Packet`；任何校验失败返回 ``None``（并记日志）。
        """
        if len(data) < HEADER_SIZE:
            return None

        magic, version, pkt_type, flags, length, crc = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])

        if magic != PROTOCOL_MAGIC:
            _LOGGER.warning("frame_bad_magic")
            return None
        if version != PROTOCOL_VERSION:
            _LOGGER.warning("frame_unsupported_version", got=version, want=PROTOCOL_VERSION)
            return None
        if flags & PKT_FLAG_UNKNOWN_STRICT_MASK:
            _LOGGER.warning("frame_unknown_flags", flags=flags)
            return None
        if flags & PKT_FLAG_COMPRESSED and flags & PKT_FLAG_PRE_COMPRESSED:
            _LOGGER.warning("frame_conflicting_flags", flags=flags)
            return None

        # 恶意/损坏长度防护：在信任任何长度派生前拒绝（叮嘱 c）。
        if length > MAX_PACKET_SIZE:
            _LOGGER.warning("frame_length_exceeded", length=length, limit=MAX_PACKET_SIZE)
            return None

        needs_hmac = bool(flags & PKT_FLAG_HMAC)
        if require_hmac and not needs_hmac:
            _LOGGER.warning("frame_hmac_required_but_absent")
            return None

        trailer = HMAC_SIZE if needs_hmac else 0
        if len(data) < HEADER_SIZE + length + trailer:
            return None  # 未收全

        wire_payload = data[HEADER_SIZE : HEADER_SIZE + length]

        if (flags & PKT_FLAG_HAS_CRC) and (zlib.crc32(wire_payload) & 0xFFFFFFFF) != crc:
            _LOGGER.warning("frame_crc_mismatch")
            return None

        if needs_hmac:
            tag = data[HEADER_SIZE + length : HEADER_SIZE + length + HMAC_SIZE]
            if hmac_key is None:
                _LOGGER.warning("frame_hmac_no_key")
                return None
            header = data[:HEADER_SIZE]
            try:
                verify_hmac(hmac_key, header, wire_payload, tag)
            except HmacVerificationError:
                _LOGGER.warning("frame_hmac_verify_failed")
                return None

        payload = wire_payload
        if flags & PKT_FLAG_COMPRESSED and not flags & PKT_FLAG_PRE_COMPRESSED:
            try:
                decompressor = zlib.decompressobj()
                payload = decompressor.decompress(wire_payload, max_length=MAX_PACKET_SIZE + 1)
                if decompressor.eof and decompressor.unused_data:
                    raise zlib.error("压缩数据后存在多余数据")
                if len(payload) > MAX_PACKET_SIZE:
                    raise zlib.error("解压后数据超限")
            except zlib.error as exc:  # zip bomb / 损坏流
                _LOGGER.warning("frame_decompress_failed", error=str(exc))
                return None

        # 0x02（v4 加密占位）与 0x08 的签名验证互不重叠；TLS 已提供机密性，忽略即可。
        packet = cls(pkt_type, payload, flags & ~PKT_FLAG_HMAC, version=version)
        packet._crc = crc
        return packet

    def get_crc(self) -> int:
        """返回上次序列化/解析时记录的 CRC32。"""
        return self._crc

    def __eq__(self, other: object) -> bool:
        """值语义相等：type + payload + version（flags 由载荷派生，不参与）。"""
        if not isinstance(other, Packet):
            return NotImplemented
        return self.type == other.type and self.payload == other.payload and self.version == other.version

    def __hash__(self) -> int:
        return hash((self.type, self.payload, self.version))


class PacketBuffer:
    """TCP 流接收缓冲区：处理粘包 / 半包 / 垃圾字节重同步。

    用法：``append(chunk)`` 后反复 ``pop_packet()`` 直到返回 ``None``。
    """

    __slots__ = ("buffer",)

    def __init__(self) -> None:
        self.buffer = bytearray()

    def append(self, data: bytes) -> None:
        """追加网络到达的字节。"""
        self.buffer.extend(data)

    def pop_packet(
        self,
        hmac_key: bytes | None = None,
        require_hmac: bool = False,
    ) -> Packet | None:
        """弹出一个完整且校验通过的包。

        Args:
            hmac_key: 透传给 :meth:`Packet.from_bytes`。
            require_hmac: 透传给 :meth:`Packet.from_bytes`（会话强制完整性）。

        Returns:
            下一个有效 :class:`Packet`；缓冲不足一帧或帧无效待更多字节时 ``None``。

        行为保证（模糊测试锁定）：
        - 恶意/超限长度：分配前拒绝，清空缓冲区，不 OOM；
        - 任何输入不抛异常、不无界循环（每次调用至多一次解析尝试 + 有限重同步）；
        - 魔数前的垃圾字节被丢弃，可能的半个魔数前缀保留。
        """
        if len(self.buffer) < HEADER_SIZE:
            return None

        if self.buffer[:4] != PROTOCOL_MAGIC:
            pos = self.buffer.find(PROTOCOL_MAGIC)
            if pos < 0:
                # 无完整魔数：保留最长的"可能是魔数前缀"尾部（≤3 字节），
                # 修复 v4 把半截魔数当垃圾清掉导致丢帧的问题。
                keep = 0
                for k in (3, 2, 1):
                    if PROTOCOL_MAGIC.startswith(bytes(self.buffer[-k:])):
                        keep = k
                        break
                del self.buffer[: len(self.buffer) - keep]
                return None
            del self.buffer[:pos]
            if len(self.buffer) < HEADER_SIZE:
                return None

        magic, version, pkt_type, flags, length, _crc = struct.unpack(
            HEADER_FORMAT, bytes(self.buffer[:HEADER_SIZE])
        )

        # 长度上限与非法版本必须在等待凑帧**之前**判定，否则恶意声明会挂死缓冲。
        if length > MAX_PACKET_SIZE:
            _LOGGER.warning("buffer_length_exceeded_discarded", length=length, limit=MAX_PACKET_SIZE)
            self.buffer.clear()
            return None
        if version != PROTOCOL_VERSION:
            # 版本不兼容无法按本帧长度安全消费，整缓冲丢弃（v5 不做跨版本协商）。
            _LOGGER.warning("buffer_unsupported_version_discarded", got=version)
            self.buffer.clear()
            return None

        frame_end = HEADER_SIZE + length + (HMAC_SIZE if flags & PKT_FLAG_HMAC else 0)
        if frame_end > len(self.buffer):
            return None  # 未收全

        packet_data = bytes(self.buffer[:frame_end])
        del self.buffer[:frame_end]
        return Packet.from_bytes(packet_data, hmac_key=hmac_key, require_hmac=require_hmac)

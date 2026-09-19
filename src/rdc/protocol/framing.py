"""
网络协议处理
分包/粘包，数据序列化，压缩，CRC校验
"""

import hashlib
import json
import struct
import zlib
from typing import Optional

from rdc.constants import (
    MAX_PACKET_SIZE,
    PKT_FLAG_COMPRESSED,
    PKT_FLAG_ENCRYPTED,
    PKT_FLAG_HAS_CRC,
    PKT_FLAG_NONE,
    PROTOCOL_MAGIC,
    PROTOCOL_VERSION,
)

# 包结构: [Magic 4B][Version 1B][Type 1B][Flags 1B][Length 4B][CRC 4B][Payload]
HEADER_FORMAT = ">4sBBB I I"  # Magic(4) + Version(1) + Type(1) + Flags(1) + Length(4) + CRC(4)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class Packet:
    """数据包"""
    
    __slots__ = ('type', 'payload', 'flags', '_crc')
    
    def __init__(self, type: int, payload: bytes, flags: int = 0):
        self.type = type
        self.payload = payload
        self.flags = flags
        self._crc = 0
    
    def to_bytes(self) -> bytes:
        """序列化为字节（支持压缩和CRC）"""
        payload = self.payload
        
        # 先压缩再计算CRC（CRC基于压缩后的数据）
        if self.flags & PKT_FLAG_COMPRESSED:
            try:
                payload = zlib.compress(payload, level=3)
            except Exception as e:
                from rdc.logger import get_logger
                get_logger().warning(f"压缩失败: {e}")
                # 压缩失败时取消压缩标志
                self.flags &= ~PKT_FLAG_COMPRESSED
                payload = self.payload
        
        # 计算CRC（基于处理后的payload）
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        self._crc = crc
        self.flags |= PKT_FLAG_HAS_CRC
        
        # 加密（预留）
        if self.flags & PKT_FLAG_ENCRYPTED:
            # TODO: 实现加密
            pass
        
        header = struct.pack(
            HEADER_FORMAT,
            PROTOCOL_MAGIC,
            PROTOCOL_VERSION,
            self.type,
            self.flags,
            len(payload),
            crc
        )
        return header + payload
    
    @classmethod
    def from_bytes(cls, data: bytes) -> Optional['Packet']:
        """从字节反序列化"""
        if len(data) < HEADER_SIZE:
            return None
        
        magic, version, pkt_type, flags, length, crc = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )
        
        if magic != PROTOCOL_MAGIC:
            return None
        
        # 长度上限校验：防止恶意/损坏的长度字段耗尽内存
        if length > MAX_PACKET_SIZE:
            from rdc.logger import get_logger
            get_logger().warning(f"包长度超限: {length}，丢弃")
            return None
        
        if len(data) < HEADER_SIZE + length:
            return None
        
        payload = data[HEADER_SIZE:HEADER_SIZE + length]
        actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
        
        # 验证CRC
        if (flags & PKT_FLAG_HAS_CRC) and actual_crc != crc:
            from rdc.logger import get_logger
            get_logger().warning(f"CRC校验失败: {actual_crc} != {crc}")
            return None
        
        # 解压缩
        if flags & PKT_FLAG_COMPRESSED:
            try:
                decompressor = zlib.decompressobj()
                payload = decompressor.decompress(payload, MAX_PACKET_SIZE + 1)
                if decompressor.eof and decompressor.unused_data:
                    raise zlib.error("压缩数据后存在多余数据")
                if len(payload) > MAX_PACKET_SIZE:
                    raise zlib.error("解压后数据超限")
            except zlib.error as e:
                from rdc.logger import get_logger
                get_logger().error(f"解压缩失败: {e}")
                return None
        
        # 解密（预留）
        if flags & PKT_FLAG_ENCRYPTED:
            # TODO: 实现解密
            pass
        
        packet = cls(pkt_type, payload, flags)
        packet._crc = actual_crc
        return packet
    
    @classmethod
    def create_json(cls, pkt_type: int, data: dict, 
                    compress: bool = True) -> 'Packet':
        """创建JSON数据包"""
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        flags = PKT_FLAG_COMPRESSED if compress else PKT_FLAG_NONE
        return cls(pkt_type, payload, flags)
    
    def to_json(self) -> dict:
        """解析JSON载荷"""
        return json.loads(self.payload.decode('utf-8'))
    
    @classmethod
    def create_screen(cls, img_data: bytes, width: int, height: int, 
                      quality: int, is_diff: bool = False,
                      offset_x: int = 0, offset_y: int = 0,
                      compress: bool = True) -> 'Packet':
        """创建屏幕数据包"""
        meta = {
            'width': width,
            'height': height,
            'quality': quality,
            'is_diff': is_diff,
            'offset_x': offset_x,
            'offset_y': offset_y,
            'timestamp': __import__('time').time(),
            'checksum': hashlib.md5(img_data).hexdigest()[:8]
        }
        meta_bytes = json.dumps(meta).encode('utf-8')
        # 元数据长度 + 元数据 + 图像数据
        payload = struct.pack('>I', len(meta_bytes)) + meta_bytes + img_data
        flags = PKT_FLAG_COMPRESSED if compress else PKT_FLAG_NONE
        return cls(1, payload, flags)  # type=1 for screen
    
    def parse_screen(self) -> tuple[dict, bytes]:
        """解析屏幕数据包"""
        if self.type != 1:
            raise ValueError("Not a screen packet")
        meta_len = struct.unpack('>I', self.payload[:4])[0]
        meta = json.loads(self.payload[4:4+meta_len].decode('utf-8'))
        img_data = self.payload[4+meta_len:]
        return meta, img_data
    
    def get_crc(self) -> int:
        """获取CRC校验值"""
        return self._crc


class PacketBuffer:
    """数据包接收缓冲区（处理粘包）"""
    
    def __init__(self):
        self.buffer = bytearray()
    
    def append(self, data: bytes) -> None:
        """追加数据"""
        self.buffer.extend(data)
    
    def pop_packet(self) -> Packet | None:
        """弹出一个完整包"""
        if len(self.buffer) < HEADER_SIZE:
            return None
        
        # 检查magic
        if self.buffer[:4] != PROTOCOL_MAGIC:
            # 查找下一个magic
            pos = self.buffer.find(PROTOCOL_MAGIC)
            if pos > 0:
                del self.buffer[:pos]
            else:
                # 如果没有找到，丢弃所有数据（防止死循环）
                self.buffer.clear()
                return None
        
        try:
            magic, version, pkt_type, flags, length, crc = struct.unpack(
                HEADER_FORMAT, bytes(self.buffer[:HEADER_SIZE])
            )
        except struct.error:
            # 数据不足
            return None
        
        # 长度上限校验：超限说明流已损坏或遭遇恶意包，丢弃全部缓冲
        if length > MAX_PACKET_SIZE:
            from rdc.logger import get_logger
            get_logger().warning(f"接收包长度超限: {length}，丢弃缓冲区")
            self.buffer.clear()
            return None
        
        if len(self.buffer) < HEADER_SIZE + length:
            return None
        
        # 提取包
        packet_data = bytes(self.buffer[:HEADER_SIZE + length])
        del self.buffer[:HEADER_SIZE + length]
        
        return Packet.from_bytes(packet_data)
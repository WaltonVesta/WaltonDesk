"""协议层常量（阶段 3 拆分自 :mod:`rdc.constants`）。

v5 协议代码一律从本模块导入 wire 相关常量；``rdc.constants`` 中的同名旧值
仅供阶段 4 前尚未重写的 v4 调用方使用（TODO(phase-4): 调用点切换后移除）。

与 :mod:`rdc.protocol.messages` 的 IntEnum 关系：本模块是 **wire 字节层**
常量（包头字段/标志位），IntEnum 是 **载荷语义层** 枚举，数值由测试锁定一致。
"""

from __future__ import annotations

import struct
from typing import Final

# ---------------------------------------------------------------------------
# 包头（15 字节，大端，无对齐填充）
# [Magic 4B][Version 1B][Type 1B][Flags 1B][Length 4B BE][CRC32 4B BE]
# ---------------------------------------------------------------------------
PROTOCOL_MAGIC: Final[bytes] = b"RDPC"  # Remote Desktop Protocol Control
HEADER_FORMAT: Final[str] = ">4sBBB I I"
HEADER_SIZE: Final[int] = struct.calcsize(HEADER_FORMAT)  # == 15（v4 文档曾误写 16）

# 协议版本（Version 字段的真实取值；v4 的占位 1 保留供遗留面，v5 起启用 2）
PROTOCOL_VERSION_V4: Final[int] = 1
PROTOCOL_VERSION_V5: Final[int] = 2
# 本模块实现的当前版本
PROTOCOL_VERSION: Final[int] = PROTOCOL_VERSION_V5

# ---------------------------------------------------------------------------
# 载荷类型（Type 字节）—— 与 rdc.protocol.messages.PayloadType 数值一致
# ---------------------------------------------------------------------------
PKT_TYPE_SCREEN: Final[int] = 0x01
PKT_TYPE_CONTROL: Final[int] = 0x02  # v4 语义：鼠标/键盘共用；v5 拆出 KEY_EVENT
PKT_TYPE_HEARTBEAT: Final[int] = 0x03
PKT_TYPE_FILE: Final[int] = 0x04  # 预留：阶段 8 文件传输
PKT_TYPE_CLIPBOARD: Final[int] = 0x05
PKT_TYPE_AUTH: Final[int] = 0x10
PKT_TYPE_AUTH_RESPONSE: Final[int] = 0x11
# v5 新增：键盘事件从 CONTROL 拆出独立类型（避免与鼠标共用 Type 后无法分派）
PKT_TYPE_KEY_EVENT: Final[int] = 0x12
# v5 阶段 4 新增：画面通道加入帧（一次性令牌 + nonce 握手）
PKT_TYPE_SESSION_JOIN: Final[int] = 0x13
PKT_TYPE_STREAM_CONTROL: Final[int] = 0x20

# ---------------------------------------------------------------------------
# 标志位（Flags 字节，按位或）
#   0x01/0x02/0x04 为 v4 遗留位（0x02 从未实现，v5 不复用、置位即忽略）；
#   0x08 HMAC 与 0x10 预压缩为 v5 新增（范围调整：不存在 v4 客户端，无冲突风险）。
# ---------------------------------------------------------------------------
PKT_FLAG_NONE: Final[int] = 0x00
PKT_FLAG_COMPRESSED: Final[int] = 0x01  # payload 经 zlib(level=3)，接收端解压
PKT_FLAG_ENCRYPTED: Final[int] = 0x02  # ⚠️ v4 占位从未实现；v5 废弃（TLS 提供加密）
PKT_FLAG_HAS_CRC: Final[int] = 0x04  # CRC32 基于**线上字节**（压缩后）的 payload
PKT_FLAG_HMAC: Final[int] = 0x08  # v5：payload 后追加 32B HMAC-SHA256 trailer
PKT_FLAG_PRE_COMPRESSED: Final[int] = 0x10  # v5：载荷已压缩（JPEG/H.264），跳过 zlib

# 显式解码未知标志位策略：0x02 及高位为"应忽略"位，0x01/0x04/0x08/0x10 为行为位。
# 接收端遇 0x20~0x80 置位视为畸形包（前向兼容的扩展位策略由阶段 4 决定是否放宽）。
PKT_FLAG_UNKNOWN_STRICT_MASK: Final[int] = ~0x1F

# HMAC trailer 长度与 TLS exporter 派生参数
HMAC_SIZE: Final[int] = 32  # SHA-256 截断前的完整摘要长度
TLS_EXPORTER_CONTEXT: Final[bytes] = b"rdc-v5-hmac"

# 单包大小上限（防止恶意/损坏的长度字段耗尽内存；v4 语义在 v5 保留并加强）
MAX_PACKET_SIZE: Final[int] = 32 * 1024 * 1024  # 32MB（支持 4K 高画质单帧）

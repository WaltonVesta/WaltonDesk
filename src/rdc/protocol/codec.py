"""codec —— v5 Protobuf 编解码门面（阶段 3，3.4）。

设计（2026-09-22 用户确认口径）：

- 对上层唯一接口是 :mod:`rdc.protocol.messages` 的领域 dataclass；
- ``pb.rdc_pb2`` 生成类只出现在本模块内部（wire 层）；
- 二者靠本模块互转；**无 v4/JSON 分派**（范围调整：双版本已砍）。

公开面：

- :func:`encode_packet` —— 领域消息 → :class:`~rdc.protocol.framing.Packet`
  （载荷为 Protobuf 字节；``ScreenFrame`` 自动置 PRE_COMPRESSED 跳过 zlib）
- :func:`decode_packet` —— Packet → 领域消息（按包头 Type 分派）
- :func:`encode_frame` / :func:`decode_frame` —— 一步到位的
  领域消息 ↔ 帧字节（含可选 HMAC 签名/验签透传）

错误契约：所有"载荷不可信"情形（未知 Type、protobuf 解析失败、枚举越界、
载荷语义非法）统一抛 :class:`~rdc.exceptions.ProtocolError`，不静默返回 ``None``。
"""

from __future__ import annotations

from enum import IntEnum
from typing import cast

from google.protobuf.message import DecodeError

from rdc.exceptions import ProtocolError
from rdc.protocol import constants as C
from rdc.protocol import messages as M
from rdc.protocol.framing import Packet
from rdc.protocol.pb import rdc_pb2 as pb

__all__ = ["decode_frame", "decode_packet", "encode_frame", "encode_packet", "from_payload", "to_proto"]


def _enum[T: IntEnum](enum_cls: type[T], value: int) -> T:
    """把 proto 解出的整数安全转成领域 IntEnum。

    proto3 开放枚举允许 wire 出现未定义值，这里越界即抛 ProtocolError。
    """
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ProtocolError(f"枚举值越界: {enum_cls.__name__}({value})") from exc


def _pb_enum[T](enum_cls: type[T], value: int) -> T:
    """int → protobuf 枚举字段值。

    运行时 protobuf 对枚举字段接受等值 int（零成本）；生成的 .pyi 存根
    却要求枚举类型本身，故用 cast 弥合，不做运行时转换。
    """
    return cast("T", value)


# ---------------------------------------------------------------------------
# 领域 dataclass → protobuf
# ---------------------------------------------------------------------------


def _mouse_to_pb(m: M.MouseEvent) -> pb.MouseEvent:
    return pb.MouseEvent(
        x=m.x,
        y=m.y,
        action=_pb_enum(pb.MouseAction, int(m.action)),
        button=_pb_enum(pb.MouseButton, int(m.button)),
        scroll_delta=m.scroll_delta,
        timestamp=m.timestamp,
    )


def _key_to_pb(m: M.KeyEvent) -> pb.KeyEvent:
    return pb.KeyEvent(
        action=_pb_enum(pb.KeyAction, int(m.action)),
        key=m.key,
        text=m.text,
        modifiers=[_pb_enum(pb.ModifierKey, int(x)) for x in m.modifiers],
        timestamp=m.timestamp,
    )


_ProtoMessage = (
    pb.AuthRequest
    | pb.AuthResponse
    | pb.ScreenJoin
    | pb.ScreenFrame
    | pb.MouseEvent
    | pb.KeyEvent
    | pb.ClipboardEvent
    | pb.StreamControl
    | pb.Heartbeat
)


def to_proto(msg: M.Message) -> _ProtoMessage:
    """领域消息 → protobuf 生成实例（wire 层，仅本模块与测试使用）。

    Raises:
        ProtocolError: 消息类型不受支持。
    """
    match msg:
        case M.AuthRequest():
            return pb.AuthRequest(
                password=msg.password,
                client_name=msg.client_name,
                protocol_version=msg.protocol_version,
                client_nonce=msg.client_nonce,
            )
        case M.AuthResponse():
            return pb.AuthResponse(
                ok=msg.ok,
                token=msg.token,
                reason=msg.reason,
                protocol_version=msg.protocol_version,
                server_nonce=msg.server_nonce,
                connection_id=msg.connection_id,
            )
        case M.ScreenJoin():
            return pb.ScreenJoin(
                token=msg.token,
                client_nonce=msg.client_nonce,
                protocol_version=msg.protocol_version,
                connection_id=msg.connection_id,
            )
        case M.ScreenFrame():
            return pb.ScreenFrame(
                seq=msg.seq,
                timestamp=msg.timestamp,
                width=msg.width,
                height=msg.height,
                codec=_pb_enum(pb.Codec, int(msg.codec)),
                keyframe=msg.keyframe,
                data=msg.data,
                origin_x=msg.origin_x,
                origin_y=msg.origin_y,
            )
        case M.MouseEvent():
            return _mouse_to_pb(msg)
        case M.KeyEvent():
            return _key_to_pb(msg)
        case M.ClipboardEvent():
            return pb.ClipboardEvent(
                text=msg.text,
                chunk_index=msg.chunk_index,
                chunk_total=msg.chunk_total,
                timestamp=msg.timestamp,
            )
        case M.StreamControl():
            cfg = None
            if msg.config is not None:
                cfg = pb.StreamConfig(
                    quality=msg.config.quality,
                    max_fps=msg.config.max_fps,
                    min_fps=msg.config.min_fps,
                    enable_diff=msg.config.enable_diff,
                    enable_compression=msg.config.enable_compression,
                )
            return pb.StreamControl(cmd=_pb_enum(pb.StreamCmd, int(msg.cmd)), config=cfg)
        case M.Heartbeat():
            return pb.Heartbeat(kind=_pb_enum(pb.HeartbeatKind, int(msg.kind)), timestamp=msg.timestamp)
        case _:
            raise ProtocolError(f"不支持的消息类型: {type(msg).__name__}")


# ---------------------------------------------------------------------------
# protobuf → 领域 dataclass
# ---------------------------------------------------------------------------


def _mouse_from_pb(p: pb.MouseEvent) -> M.MouseEvent:
    return M.MouseEvent(
        x=p.x,
        y=p.y,
        action=_enum(M.MouseAction, p.action),
        button=_enum(M.MouseButton, p.button),
        scroll_delta=p.scroll_delta,
        timestamp=p.timestamp,
    )


def _key_from_pb(p: pb.KeyEvent) -> M.KeyEvent:
    return M.KeyEvent(
        action=_enum(M.KeyAction, p.action),
        key=p.key,
        text=p.text,
        modifiers=tuple(_enum(M.ModifierKey, x) for x in p.modifiers),
        timestamp=p.timestamp,
    )


def _stream_from_pb(p: pb.StreamControl) -> M.StreamControl:
    cfg: M.StreamConfig | None = None
    if p.HasField("config"):
        cfg = M.StreamConfig(
            quality=p.config.quality,
            max_fps=p.config.max_fps,
            min_fps=p.config.min_fps,
            enable_diff=p.config.enable_diff,
            enable_compression=p.config.enable_compression,
        )
    return M.StreamControl(cmd=_enum(M.StreamCmd, p.cmd), config=cfg)


def from_payload(pkt_type: int, payload: bytes) -> M.Message:
    """按包头 Type 解析 protobuf 载荷为领域消息。

    Args:
        pkt_type: 包头 Type 字节。
        payload: 解压后的载荷字节（protobuf 序列化）。

    Returns:
        领域消息（构造期校验在 dataclass ``__post_init__`` 中执行）。

    Raises:
        ProtocolError: 未知 Type / protobuf 解析失败 / 枚举越界 / 字段语义非法。
    """
    try:
        match pkt_type:
            case C.PKT_TYPE_AUTH:
                p = pb.AuthRequest()
                p.ParseFromString(payload)
                return M.AuthRequest(
                    password=p.password,
                    client_name=p.client_name,
                    protocol_version=p.protocol_version,
                    client_nonce=p.client_nonce,
                )
            case C.PKT_TYPE_AUTH_RESPONSE:
                p = pb.AuthResponse()
                p.ParseFromString(payload)
                return M.AuthResponse(
                    ok=p.ok,
                    token=p.token,
                    reason=p.reason,
                    protocol_version=p.protocol_version,
                    server_nonce=p.server_nonce,
                    connection_id=p.connection_id,
                )
            case C.PKT_TYPE_SESSION_JOIN:
                p = pb.ScreenJoin()
                p.ParseFromString(payload)
                return M.ScreenJoin(
                    token=p.token,
                    client_nonce=p.client_nonce,
                    protocol_version=p.protocol_version,
                    connection_id=p.connection_id,
                )
            case C.PKT_TYPE_SCREEN:
                p = pb.ScreenFrame()
                p.ParseFromString(payload)
                codec = _enum(M.Codec, p.codec)
                return M.ScreenFrame(
                    seq=p.seq,
                    timestamp=p.timestamp,
                    width=p.width,
                    height=p.height,
                    codec=codec,
                    data=p.data,
                    keyframe=p.keyframe,
                    origin_x=p.origin_x,  # 旧载荷缺字段 → proto3 默认 0
                    origin_y=p.origin_y,
                )
            case C.PKT_TYPE_CONTROL:
                p = pb.MouseEvent()
                p.ParseFromString(payload)
                return _mouse_from_pb(p)
            case C.PKT_TYPE_KEY_EVENT:
                p = pb.KeyEvent()
                p.ParseFromString(payload)
                return _key_from_pb(p)
            case C.PKT_TYPE_CLIPBOARD:
                p = pb.ClipboardEvent()
                p.ParseFromString(payload)
                return M.ClipboardEvent(
                    text=p.text,
                    chunk_index=p.chunk_index,
                    chunk_total=p.chunk_total,
                    timestamp=p.timestamp,
                )
            case C.PKT_TYPE_STREAM_CONTROL:
                p = pb.StreamControl()
                p.ParseFromString(payload)
                return _stream_from_pb(p)
            case C.PKT_TYPE_HEARTBEAT:
                p = pb.Heartbeat()
                p.ParseFromString(payload)
                return M.Heartbeat(kind=_enum(M.HeartbeatKind, p.kind), timestamp=p.timestamp)
            case _:
                raise ProtocolError(f"未知载荷类型: 0x{pkt_type:02x}")
    except DecodeError as exc:
        raise ProtocolError(f"protobuf 载荷解析失败 (type=0x{pkt_type:02x}): {exc}") from exc
    except ProtocolError as exc:  # 领域 dataclass __post_init__ 语义校验
        raise ProtocolError(f"载荷语义非法 (type=0x{pkt_type:02x}): {exc}") from exc


# ---------------------------------------------------------------------------
# Packet 层门面
# ---------------------------------------------------------------------------


def _flags_for(msg: M.Message, compress: bool) -> int:
    """按消息语义决定分帧标志。

    ScreenFrame 的 data 恒为 JPEG/H.264 压缩流 → PRE_COMPRESSED（显式声明，
    不猜测内容）。其余载荷 protobuf 已足够紧凑，默认不压缩；
    ``compress=True`` 仅对非画面消息启用 zlib（大剪贴板文本场景）。
    """
    if isinstance(msg, M.ScreenFrame):
        return C.PKT_FLAG_PRE_COMPRESSED
    return C.PKT_FLAG_COMPRESSED if compress else C.PKT_FLAG_NONE


def encode_packet(msg: M.Message, *, compress: bool = False) -> Packet:
    """领域消息 → :class:`Packet`（载荷为 protobuf 字节）。

    Args:
        msg: 领域消息。
        compress: 对非画面载荷启用 zlib；画面帧自动置 PRE_COMPRESSED。

    Returns:
        可直接 ``to_bytes()`` 的 Packet。
    """
    payload = to_proto(msg).SerializeToString(deterministic=True)
    return Packet(int(msg.payload_type), payload, _flags_for(msg, compress))


def decode_packet(packet: Packet) -> M.Message:
    """Packet → 领域消息（按 ``packet.type`` 分派）。

    Raises:
        ProtocolError: 类型未知或载荷非法。
    """
    return from_payload(packet.type, packet.payload)


def encode_frame(
    msg: M.Message,
    *,
    hmac_key: bytes | None = None,
    compress: bool = False,
) -> bytes:
    """领域消息 → 完整帧字节（可选 HMAC 签名）。"""
    return encode_packet(msg, compress=compress).to_bytes(hmac_key=hmac_key)


def decode_frame(
    data: bytes,
    *,
    hmac_key: bytes | None = None,
    require_hmac: bool = False,
) -> M.Message | None:
    """完整帧字节 → 领域消息。

    Returns:
        领域消息；帧不完整或完整性校验失败时 ``None``（语义同
        :meth:`Packet.from_bytes` —— 这两类失败属于传输层，由
        :class:`~rdc.protocol.framing.PacketBuffer` 记日志；
        载荷语义错误则抛 :class:`~rdc.exceptions.ProtocolError`）。
    """
    packet = Packet.from_bytes(data, hmac_key=hmac_key, require_hmac=require_hmac)
    if packet is None:
        return None
    return decode_packet(packet)

"""codec v5 编解码测试（阶段 3，3.8 hypothesis 往返 + 3.10 字节快照）。

- hypothesis 往返：**全部 8 类消息**，构造消息 → encode → decode → 与原消息相等
  （叮嘱 b：语义等价往返，不做 Protobuf 字节级稳定假设）。
- 边界：最小载荷、大二进制/大文本载荷、中文/emoji/控制字符、全枚举穷举。
- 字节快照：锁定 v5 自身的 wire 输出（防序列化回归，非 v4 兼容目的）。
- 错误契约：未知 Type / 损坏 protobuf / 枚举越界 → ProtocolError，不静默。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rdc.exceptions import ProtocolError
from rdc.protocol import constants as C
from rdc.protocol import messages as M
from rdc.protocol.codec import (
    decode_frame,
    decode_packet,
    encode_frame,
    encode_packet,
    from_payload,
    to_proto,
)
from rdc.protocol.framing import Packet
from rdc.protocol.pb import rdc_pb2 as pb

# ---------------------------------------------------------------------------
# hypothesis 策略（按 dataclass 不变量构造，避免非法组合）
# ---------------------------------------------------------------------------

_floats = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e18, max_value=1e18)
_int32 = st.integers(min_value=-(2**31), max_value=2**31 - 1)
_uint32 = st.integers(min_value=0, max_value=2**32 - 1)
_text = st.text(max_size=200)


@st.composite
def auth_requests(draw: st.DrawFn) -> M.AuthRequest:
    return M.AuthRequest(password=draw(_text), client_name=draw(_text), protocol_version=draw(_uint32))


@st.composite
def auth_responses(draw: st.DrawFn) -> M.AuthResponse:
    return M.AuthResponse(
        ok=draw(st.booleans()), token=draw(_text), reason=draw(_text), protocol_version=draw(_uint32)
    )


@st.composite
def screen_frames(draw: st.DrawFn) -> M.ScreenFrame:
    return M.ScreenFrame(
        seq=draw(_uint32),
        timestamp=draw(_floats),
        width=draw(st.integers(min_value=1, max_value=2**32 - 1)),
        height=draw(st.integers(min_value=1, max_value=2**32 - 1)),
        codec=draw(st.sampled_from([M.Codec.JPEG, M.Codec.H264])),
        data=draw(st.binary(min_size=1, max_size=2048)),
        keyframe=draw(st.booleans()),
    )


@st.composite
def mouse_events(draw: st.DrawFn) -> M.MouseEvent:
    action = draw(st.sampled_from(list(M.MouseAction)))
    if action in (M.MouseAction.PRESS, M.MouseAction.RELEASE, M.MouseAction.CLICK, M.MouseAction.SCROLL):
        button = draw(st.sampled_from([M.MouseButton.LEFT, M.MouseButton.RIGHT, M.MouseButton.MIDDLE]))
    else:
        button = draw(st.sampled_from(list(M.MouseButton)))
    return M.MouseEvent(
        x=draw(_int32), y=draw(_int32), action=action, button=button,
        scroll_delta=draw(_int32), timestamp=draw(_floats),
    )


@st.composite
def key_events(draw: st.DrawFn) -> M.KeyEvent:
    action = draw(st.sampled_from(list(M.KeyAction)))
    key = draw(st.text(min_size=1, max_size=30)) if action in (M.KeyAction.PRESS, M.KeyAction.RELEASE) else draw(_text)
    text = draw(st.text(min_size=1, max_size=50)) if action is M.KeyAction.TYPE else draw(_text)
    modifiers = tuple(draw(st.lists(st.sampled_from(list(M.ModifierKey)), max_size=5)))
    return M.KeyEvent(action=action, key=key, text=text, modifiers=modifiers, timestamp=draw(_floats))


@st.composite
def clipboard_events(draw: st.DrawFn) -> M.ClipboardEvent:
    total = draw(st.integers(min_value=1, max_value=1000))
    return M.ClipboardEvent(
        text=draw(_text), chunk_index=draw(st.integers(min_value=0, max_value=total - 1)),
        chunk_total=total, timestamp=draw(_floats),
    )


@st.composite
def stream_controls(draw: st.DrawFn) -> M.StreamControl:
    cmd = draw(st.sampled_from([M.StreamCmd.START, M.StreamCmd.STOP]))
    if draw(st.booleans()):
        return M.StreamControl(cmd=cmd, config=None)
    cfg = M.StreamConfig(
        quality=draw(_int32), max_fps=draw(_int32), min_fps=draw(_int32),
        enable_diff=draw(st.booleans()), enable_compression=draw(st.booleans()),
    )
    return M.StreamControl(cmd=cmd, config=cfg)


@st.composite
def config_controls(draw: st.DrawFn) -> M.StreamControl:
    cfg = M.StreamConfig(
        quality=draw(_int32), max_fps=draw(_int32), min_fps=draw(_int32),
        enable_diff=draw(st.booleans()), enable_compression=draw(st.booleans()),
    )
    return M.StreamControl(cmd=M.StreamCmd.CONFIG, config=cfg)


@st.composite
def heartbeats(draw: st.DrawFn) -> M.Heartbeat:
    return M.Heartbeat(kind=draw(st.sampled_from(list(M.HeartbeatKind))), timestamp=draw(_floats))


_message_strategies = st.one_of(
    auth_requests(), auth_responses(), screen_frames(), mouse_events(), key_events(),
    clipboard_events(), stream_controls(), config_controls(), heartbeats(),
)

_HYPOTHESIS_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ---------------------------------------------------------------------------
# 3.8 hypothesis 往返
# ---------------------------------------------------------------------------


class TestHypothesisRoundTrip:
    """任意合法消息 → 编码 → 解码 → 与原消息相等（frozen dataclass 逐字段）。"""

    @_HYPOTHESIS_SETTINGS
    @given(_message_strategies)
    def test_packet_round_trip(self, msg: M.Message) -> None:
        """消息 → Packet → 消息（codec 门面层往返）。"""
        pkt = encode_packet(msg)
        assert decode_packet(pkt) == msg

    @_HYPOTHESIS_SETTINGS
    @given(_message_strategies)
    def test_frame_round_trip(self, msg: M.Message) -> None:
        """消息 → 帧字节 → 消息（含分帧序列化路径）。"""
        frame = encode_frame(msg)
        assert decode_frame(frame) == msg

    @_HYPOTHESIS_SETTINGS
    @given(_message_strategies)
    def test_payload_type_preserved(self, msg: M.Message) -> None:
        pkt = encode_packet(msg)
        assert pkt.type == int(msg.payload_type)

    @_HYPOTHESIS_SETTINGS
    @given(_message_strategies)
    def test_screen_frames_skip_zlib_others_optional(self, msg: M.Message) -> None:
        """画面帧恒置 PRE_COMPRESSED；其余载荷默认无 zlib。"""
        pkt = encode_packet(msg)
        if isinstance(msg, M.ScreenFrame):
            assert pkt.flags & C.PKT_FLAG_PRE_COMPRESSED
            assert not pkt.flags & C.PKT_FLAG_COMPRESSED
        else:
            assert not pkt.flags & C.PKT_FLAG_PRE_COMPRESSED


class TestRoundTripCompressed:
    """compress=True 路径（非画面大载荷走 zlib）同样往返无损。"""

    @_HYPOTHESIS_SETTINGS
    @given(clipboard_events())
    def test_compressed_clipboard_round_trip(self, msg: M.ClipboardEvent) -> None:
        pkt = encode_packet(msg, compress=True)
        if not isinstance(msg, M.ScreenFrame) and msg.text:
            assert pkt.flags & C.PKT_FLAG_COMPRESSED
        assert decode_packet(pkt) == msg


# ---------------------------------------------------------------------------
# 边界显式用例（叮嘱 b：空/最大/中文/二进制）
# ---------------------------------------------------------------------------


class TestBoundaries:
    def test_minimal_heartbeat(self) -> None:
        msg = M.Heartbeat(kind=M.HeartbeatKind.UNSPECIFIED, timestamp=0.0)
        assert decode_frame(encode_frame(msg)) == msg

    def test_large_binary_screen_frame(self) -> None:
        data = bytes(range(256)) * 512  # 128KB 伪 H.264 流
        msg = M.ScreenFrame(seq=1, timestamp=0.0, width=1, height=1, codec=M.Codec.H264, data=data)
        restored = decode_frame(encode_frame(msg))
        assert isinstance(restored, M.ScreenFrame)
        assert restored.data == data

    def test_large_text_clipboard(self) -> None:
        msg = M.ClipboardEvent(text="远程桌面" * 20000, chunk_total=1)  # 8 万字符
        assert decode_frame(encode_frame(msg)) == msg

    def test_unicode_control_chars_key_text(self) -> None:
        msg = M.KeyEvent(action=M.KeyAction.TYPE, text="中\n文\u0000🖥️\u200d")
        assert decode_frame(encode_frame(msg)) == msg

    def test_empty_string_fields(self) -> None:
        msg = M.AuthRequest(password="", client_name="")
        assert decode_frame(encode_frame(msg)) == msg

    def test_int32_extremes_mouse(self) -> None:
        msg = M.MouseEvent(
            x=-(2**31), y=2**31 - 1, action=M.MouseAction.SCROLL,
            button=M.MouseButton.MIDDLE, scroll_delta=-(2**31),
        )
        assert decode_frame(encode_frame(msg)) == msg

    def test_uint32_extremes_screen(self) -> None:
        msg = M.ScreenFrame(seq=2**32 - 1, timestamp=0.0, width=2**32 - 1, height=2**32 - 1,
                            codec=M.Codec.JPEG, data=b"\x00")
        assert decode_frame(encode_frame(msg)) == msg

    def test_all_enum_values_exhaustive(self) -> None:
        for kind in M.HeartbeatKind:
            assert decode_frame(encode_frame(M.Heartbeat(kind=kind))) == M.Heartbeat(kind=kind)
        for btn in (M.MouseButton.LEFT, M.MouseButton.RIGHT, M.MouseButton.MIDDLE):
            for act in (M.MouseAction.PRESS, M.MouseAction.RELEASE, M.MouseAction.CLICK):
                m = M.MouseEvent(x=1, y=1, action=act, button=btn)
                assert decode_frame(encode_frame(m)) == m
        for mod in M.ModifierKey:
            k = M.KeyEvent(action=M.KeyAction.PRESS, key="a", modifiers=(mod,))
            assert decode_frame(encode_frame(k)) == k


# ---------------------------------------------------------------------------
# 3.10 v5 字节快照（锁序列化回归；范围调整：非 v4 golden bytes）
# ---------------------------------------------------------------------------


class TestByteSnapshots:
    """锁定 v5 自身 wire 输出字节（proto 载荷 + 完整帧）。"""

    SNAPSHOT_PROTO: dict[str, tuple[M.Message, str]] = {
        "heartbeat_ping": (
            M.Heartbeat(kind=M.HeartbeatKind.PING, timestamp=1727000000.5),
            "08011100002070fbbbd941",
        ),
        "mouse_move": (
            M.MouseEvent(x=100, y=-50, action=M.MouseAction.MOVE, timestamp=1727000000.25),
            "086410ceffffffffffffffff0118013100001070fbbbd941",
        ),
        "key_type_cjk": (
            M.KeyEvent(action=M.KeyAction.TYPE, text="你好🖥",
                       modifiers=(M.ModifierKey.CTRL, M.ModifierKey.SHIFT)),
            "08031a0ae4bda0e5a5bdf09f96a522020102",
        ),
        "auth_request": (
            M.AuthRequest(password="s3cr3t-帕斯沃德", client_name="walton-pc"),
            "0a137333637233742de5b895e696afe6b283e5beb7120977616c746f6e2d70631802",
        ),
        "screen_jpeg": (
            M.ScreenFrame(seq=7, timestamp=1727000000.0, width=3840, height=2160,
                          codec=M.Codec.JPEG, data=b"\xff\xd8\xff\xe0jpegbytes", keyframe=True),
            "08071100000070fbbbd94118801e20f010280130013a0dffd8ffe06a7065676279746573",
        ),
        "stream_config": (
            M.StreamControl(cmd=M.StreamCmd.CONFIG, config=M.StreamConfig(
                quality=80, max_fps=60, min_fps=10, enable_diff=False, enable_compression=True)),
            "080312080850103c180a2801",
        ),
        "auth_response_fail": (
            M.AuthResponse(ok=False, reason="密码错误", protocol_version=2),
            "1a0ce5af86e7a081e99499e8afaf2002",
        ),
    }

    SNAPSHOT_FRAME: dict[str, tuple[M.Message, str]] = {
        "heartbeat_ping": (SNAPSHOT_PROTO["heartbeat_ping"][0],
                           "524450430203040000000bb20e075b08011100002070fbbbd941"),
        "mouse_move": (SNAPSHOT_PROTO["mouse_move"][0],
                       "524450430202040000001843eabfc3086410ceffffffffffffffff0118013100001070fbbbd941"),
        "key_type_cjk": (SNAPSHOT_PROTO["key_type_cjk"][0],
                         "52445043021204000000121301cdc408031a0ae4bda0e5a5bdf09f96a522020102"),
        "auth_request": (SNAPSHOT_PROTO["auth_request"][0],
                         "5244504302100400000022b3cbfd670a137333637233742de5b895e696afe6b283e5beb7120977616c746f6e2d70631802"),
        "screen_jpeg": (SNAPSHOT_PROTO["screen_jpeg"][0],
                        "52445043020114000000243c94e76708071100000070fbbbd94118801e20f010280130013a0dffd8ffe06a7065676279746573"),
        "stream_config": (SNAPSHOT_PROTO["stream_config"][0],
                          "524450430220040000000c59efe4d9080312080850103c180a2801"),
    }

    @pytest.mark.parametrize("name", list(SNAPSHOT_PROTO))
    def test_proto_payload_snapshot(self, name: str) -> None:
        msg, hex_expect = self.SNAPSHOT_PROTO[name]
        assert to_proto(msg).SerializeToString(deterministic=True).hex() == hex_expect

    @pytest.mark.parametrize("name", list(SNAPSHOT_FRAME))
    def test_frame_snapshot(self, name: str) -> None:
        msg, hex_expect = self.SNAPSHOT_FRAME[name]
        assert encode_frame(msg).hex() == hex_expect

    def test_frame_header_fields_snapshot(self) -> None:
        """帧头逐字段：Magic RDPC + Version=2 + Type + Flags(HAS_CRC)。"""
        frame = encode_frame(M.Heartbeat(kind=M.HeartbeatKind.PING, timestamp=0.0))
        assert frame[:4] == b"RDPC"
        assert frame[4] == C.PROTOCOL_VERSION_V5
        assert frame[5] == C.PKT_TYPE_HEARTBEAT
        assert frame[6] & C.PKT_FLAG_HAS_CRC


# ---------------------------------------------------------------------------
# 错误契约（拒绝一切静默吞错）
# ---------------------------------------------------------------------------


class TestCodecErrorContract:
    def test_unknown_packet_type_raises(self) -> None:
        pkt = Packet(0x7E, b"\x08\x01", C.PKT_FLAG_NONE)
        with pytest.raises(ProtocolError, match="未知载荷类型"):
            decode_packet(pkt)

    def test_garbage_protobuf_raises(self) -> None:
        """截断 varint 不是合法 protobuf → ProtocolError（非裸 DecodeError）。"""
        pkt = Packet(C.PKT_TYPE_HEARTBEAT, b"\x08", C.PKT_FLAG_NONE)
        with pytest.raises(ProtocolError, match="protobuf 载荷解析失败"):
            decode_packet(pkt)

    def test_unknown_enum_value_raises(self) -> None:
        p = pb.MouseEvent(action=99, x=0, y=0)
        payload = p.SerializeToString()
        with pytest.raises(ProtocolError, match="枚举值越界"):
            from_payload(C.PKT_TYPE_CONTROL, payload)

    def test_semantic_violation_raises(self) -> None:
        """wire 合法但语义非法（chunk_index >= total）→ ProtocolError。"""
        p = pb.ClipboardEvent(text="x", chunk_index=5, chunk_total=2)
        with pytest.raises(ProtocolError, match="chunk_index"):
            from_payload(C.PKT_TYPE_CLIPBOARD, p.SerializeToString())

    def test_screen_frame_empty_data_raises(self) -> None:
        p = pb.ScreenFrame(seq=1, timestamp=0.0, width=10, height=10, codec=pb.CODEC_JPEG, data=b"")
        with pytest.raises(ProtocolError):
            from_payload(C.PKT_TYPE_SCREEN, p.SerializeToString())

    def test_unsupported_message_type_to_proto(self) -> None:
        class Odd:
            pass

        with pytest.raises(ProtocolError, match="不支持的消息类型"):
            to_proto(Odd())  # type: ignore[arg-type]


class TestDecodeFrameTransportNone:
    """decode_frame 对"传输层失败"返回 None（语义同 Packet.from_bytes）。"""

    def test_truncated_frame_returns_none(self) -> None:
        frame = encode_frame(M.Heartbeat(kind=M.HeartbeatKind.PING))
        assert decode_frame(frame[:-1]) is None  # 半包：等待更多字节

    def test_bad_magic_returns_none(self) -> None:
        frame = bytearray(encode_frame(M.Heartbeat(kind=M.HeartbeatKind.PING)))
        frame[0:4] = b"XXXX"
        assert decode_frame(bytes(frame)) is None

    def test_garbage_bytes_returns_none(self) -> None:
        assert decode_frame(b"not-a-frame-at-all") is None

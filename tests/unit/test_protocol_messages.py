"""领域 dataclass 约束 + 枚举与 proto 对齐（阶段 3，3.3）。"""

from __future__ import annotations

import dataclasses

import pytest

from rdc.exceptions import ProtocolError
from rdc.protocol import constants as C
from rdc.protocol import messages as M
from rdc.protocol.pb import rdc_pb2 as pb


class TestPayloadTypeAlignment:
    """PayloadType 与 wire Type 常量、proto enum 数值一致。"""

    def test_payload_type_matches_wire_constants(self) -> None:
        assert M.PayloadType.SCREEN_FRAME == C.PKT_TYPE_SCREEN
        assert M.PayloadType.CONTROL == C.PKT_TYPE_CONTROL
        assert M.PayloadType.HEARTBEAT == C.PKT_TYPE_HEARTBEAT
        assert M.PayloadType.CLIPBOARD_EVENT == C.PKT_TYPE_CLIPBOARD
        assert M.PayloadType.AUTH_REQUEST == C.PKT_TYPE_AUTH
        assert M.PayloadType.AUTH_RESPONSE == C.PKT_TYPE_AUTH_RESPONSE
        assert M.PayloadType.KEY_EVENT == C.PKT_TYPE_KEY_EVENT
        assert M.PayloadType.STREAM_CONTROL == C.PKT_TYPE_STREAM_CONTROL

    def test_payload_type_matches_proto_enum(self) -> None:
        assert int(M.PayloadType.SCREEN_FRAME) == pb.PAYLOAD_SCREEN_FRAME
        assert int(M.PayloadType.HEARTBEAT) == pb.PAYLOAD_HEARTBEAT
        assert int(M.PayloadType.CLIPBOARD_EVENT) == pb.PAYLOAD_CLIPBOARD_EVENT
        assert int(M.PayloadType.AUTH_REQUEST) == pb.PAYLOAD_AUTH_REQUEST
        assert int(M.PayloadType.AUTH_RESPONSE) == pb.PAYLOAD_AUTH_RESPONSE
        assert int(M.PayloadType.KEY_EVENT) == pb.PAYLOAD_KEY_EVENT
        assert int(M.PayloadType.STREAM_CONTROL) == pb.PAYLOAD_STREAM_CONTROL


class TestEnumAlignment:
    """6 组语义枚举与 proto enum 逐值对齐（叮嘱：数值锁定）。"""

    _MOUSE = {"UNSPECIFIED": "MOUSE_ACTION_UNSPECIFIED", "MOVE": "MOUSE_MOVE", "PRESS": "MOUSE_PRESS",
              "RELEASE": "MOUSE_RELEASE", "CLICK": "MOUSE_CLICK", "SCROLL": "MOUSE_SCROLL", "DRAG": "MOUSE_DRAG"}

    def test_mouse_action_matches_proto(self) -> None:
        for name, pb_name in self._MOUSE.items():
            assert int(M.MouseAction[name]) == getattr(pb, pb_name), name

    def test_mouse_button_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "MOUSE_BUTTON_UNSPECIFIED", "LEFT": "MOUSE_LEFT",
                              "RIGHT": "MOUSE_RIGHT", "MIDDLE": "MOUSE_MIDDLE"}.items():
            assert int(M.MouseButton[name]) == getattr(pb, pb_name), name

    def test_key_action_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "KEY_ACTION_UNSPECIFIED", "PRESS": "KEY_PRESS",
                              "RELEASE": "KEY_RELEASE", "TYPE": "KEY_TYPE"}.items():
            assert int(M.KeyAction[name]) == getattr(pb, pb_name), name

    def test_modifier_key_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "MODIFIER_UNSPECIFIED", "CTRL": "MODIFIER_CTRL",
                              "SHIFT": "MODIFIER_SHIFT", "ALT": "MODIFIER_ALT",
                              "WIN": "MODIFIER_WIN", "ALTGR": "MODIFIER_ALTGR"}.items():
            assert int(M.ModifierKey[name]) == getattr(pb, pb_name), name

    def test_codec_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "CODEC_UNSPECIFIED", "JPEG": "CODEC_JPEG",
                              "H264": "CODEC_H264"}.items():
            assert int(M.Codec[name]) == getattr(pb, pb_name), name

    def test_stream_cmd_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "STREAM_CMD_UNSPECIFIED", "START": "STREAM_START",
                              "STOP": "STREAM_STOP", "CONFIG": "STREAM_CONFIG"}.items():
            assert int(M.StreamCmd[name]) == getattr(pb, pb_name), name

    def test_heartbeat_kind_matches_proto(self) -> None:
        for name, pb_name in {"UNSPECIFIED": "HEARTBEAT_UNSPECIFIED", "PING": "HEARTBEAT_PING",
                              "PONG": "HEARTBEAT_PONG"}.items():
            assert int(M.HeartbeatKind[name]) == getattr(pb, pb_name), name


class TestMessageInvariants:
    """__post_init__ 语义校验（构造期即拒非法组合）。"""

    def test_screen_frame_rejects_zero_size(self) -> None:
        with pytest.raises(ProtocolError):
            M.ScreenFrame(seq=0, timestamp=0.0, width=0, height=100, codec=M.Codec.JPEG, data=b"x")

    def test_screen_frame_rejects_empty_data(self) -> None:
        with pytest.raises(ProtocolError):
            M.ScreenFrame(seq=0, timestamp=0.0, width=10, height=10, codec=M.Codec.JPEG, data=b"")

    def test_screen_frame_rejects_negative_seq(self) -> None:
        with pytest.raises(ProtocolError):
            M.ScreenFrame(seq=-1, timestamp=0.0, width=10, height=10, codec=M.Codec.JPEG, data=b"x")

    def test_mouse_press_without_button_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.MouseEvent(x=0, y=0, action=M.MouseAction.PRESS)

    def test_mouse_move_without_button_ok(self) -> None:
        m = M.MouseEvent(x=1, y=2, action=M.MouseAction.MOVE)
        assert m.button is M.MouseButton.UNSPECIFIED

    def test_mouse_scroll_without_button_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.MouseEvent(x=0, y=0, action=M.MouseAction.SCROLL)

    def test_key_press_without_key_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.KeyEvent(action=M.KeyAction.PRESS)

    def test_key_type_without_text_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.KeyEvent(action=M.KeyAction.TYPE)

    def test_clipboard_chunk_out_of_range_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.ClipboardEvent(text="x", chunk_index=2, chunk_total=2)

    def test_clipboard_zero_total_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            M.ClipboardEvent(text="x", chunk_total=0)

    def test_stream_config_requires_config_for_cmd(self) -> None:
        with pytest.raises(ProtocolError):
            M.StreamControl(cmd=M.StreamCmd.CONFIG)

    def test_stream_start_without_config_ok(self) -> None:
        assert M.StreamControl(cmd=M.StreamCmd.START).config is None


class TestFrozenAndDispatch:
    """frozen 语义与 payload_type 派发。"""

    def test_messages_are_frozen(self) -> None:
        m = M.Heartbeat(kind=M.HeartbeatKind.PING)
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.kind = M.HeartbeatKind.PONG  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("msg", "ptype"),
        [
            (M.AuthRequest(password="p"), M.PayloadType.AUTH_REQUEST),
            (M.AuthResponse(ok=True), M.PayloadType.AUTH_RESPONSE),
            (M.ScreenFrame(seq=1, timestamp=0.0, width=2, height=2, codec=M.Codec.JPEG, data=b"x"),
             M.PayloadType.SCREEN_FRAME),
            (M.MouseEvent(x=0, y=0, action=M.MouseAction.MOVE), M.PayloadType.CONTROL),
            (M.KeyEvent(action=M.KeyAction.PRESS, key="a"), M.PayloadType.KEY_EVENT),
            (M.ClipboardEvent(text="t"), M.PayloadType.CLIPBOARD_EVENT),
            (M.StreamControl(cmd=M.StreamCmd.START), M.PayloadType.STREAM_CONTROL),
            (M.Heartbeat(kind=M.HeartbeatKind.PING), M.PayloadType.HEARTBEAT),
        ],
    )
    def test_payload_type_property(self, msg: M.Message, ptype: M.PayloadType) -> None:
        assert msg.payload_type is ptype

    def test_default_version_is_v5(self) -> None:
        assert M.AuthRequest(password="p").protocol_version == C.PROTOCOL_VERSION_V5 == 2

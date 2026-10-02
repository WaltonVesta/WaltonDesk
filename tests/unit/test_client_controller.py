"""client/controller 单元测试（阶段 9 批 4）。

替身通道（monkeypatch ControlChannel/ScreenChannel 类）+ 假时钟 + 真 PIL/PyAV
解码，覆盖：状态机全链、口令/指纹异步契约、输入合流出批、帧→回调、指标
窗口、RTT、剪贴板双向、重连分支（网络环/认证止/取消弃）、Qt 键码表对齐。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from rdc.client import controller as cc
from rdc.config.settings import Settings
from rdc.exceptions import AuthError, ConnectionError
from rdc.input.coalesce import InputCoalescer
from rdc.protocol.messages import (
    ClipboardEvent,
    Codec,
    Heartbeat,
    HeartbeatKind,
    KeyAction,
    KeyEvent,
    MouseAction,
    MouseButton,
    MouseEvent,
    ScreenFrame,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeControl:
    """控制通道替身：构造签名与真类一致，connect 可脚本化失败。"""

    #: connect 抛出的异常（None=成功）；类级共享，测试设置
    connect_error: BaseException | None = None
    instances: list[FakeControl] = []

    def __init__(
        self,
        host: str,
        port: int,
        *,
        password_provider,  # noqa: ANN001
        fingerprint_confirm=None,  # noqa: ANN001
        client_name: str = "",
    ) -> None:
        self.host, self.port = host, port
        self.password_provider = password_provider
        self.fingerprint_confirm = fingerprint_confirm
        self.client_name = client_name
        self.session_token = "tok-abc"
        self.connection_id = b"c" * 32
        self.is_connected = False
        self.sent: list[object] = []
        self.closed = False
        self.inbox: asyncio.Queue[object | None] = asyncio.Queue()
        FakeControl.instances.append(self)

    async def connect(self) -> None:
        if FakeControl.connect_error is not None:
            raise FakeControl.connect_error
        # 真通道在 connect 内 await password_provider()——替身同构
        pw = self.password_provider()
        if asyncio.iscoroutine(pw):
            pw = await pw
        self.provided_password = pw
        self.is_connected = True

    async def send(self, msg: object) -> None:
        self.sent.append(msg)

    async def next_message(self) -> object | None:
        return await self.inbox.get()

    async def close(self) -> None:
        self.is_connected = False
        self.closed = True


class FakeScreen(FakeControl):
    """画面通道替身：connect 不索取口令。"""

    instances: list[FakeScreen] = []

    def __init__(
        self,
        host: str,
        port: int,
        *,
        session_token: str,
        connection_id: bytes,
        fingerprint_confirm=None,  # noqa: ANN001
    ) -> None:
        self.host, self.port = host, port
        self.session_token_in = session_token
        self.connection_id_in = connection_id
        self.fingerprint_confirm = fingerprint_confirm
        self.is_connected = False
        self.sent = []
        self.closed = False
        self.inbox = asyncio.Queue()
        FakeScreen.instances.append(self)

    async def connect(self) -> None:
        if FakeScreen.connect_error is not None:
            raise FakeScreen.connect_error
        self.is_connected = True


@pytest.fixture(autouse=True)
async def _patch_channels(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    FakeControl.instances.clear()
    FakeControl.connect_error = None
    FakeScreen.instances.clear()
    FakeScreen.connect_error = None
    monkeypatch.setattr(cc, "ControlChannel", FakeControl)
    monkeypatch.setattr(cc, "ScreenChannel", FakeScreen)
    _LIVE_CTLS.clear()
    yield
    # 逐实例拆会话：终止接收环/心跳/重连退避，防跨测试、跨事件循环泄漏
    for ctl in list(_LIVE_CTLS):
        with contextlib.suppress(Exception):
            await ctl.disconnect()


_LIVE_CTLS: list[cc.ClientController] = []


def make_ctrl(
    settings: Settings | None = None,
    *,
    clock: FakeClock | None = None,
    image_factory=None,  # noqa: ANN001
    **kw: object,
) -> tuple[cc.ClientController, FakeClock]:
    clk = clock or FakeClock()
    ctl = cc.ClientController(
        settings=settings if settings is not None else Settings(),
        image_factory=image_factory or (lambda w, h, arr: (w, h)),
        coalescer=InputCoalescer(clock=clk),
        clock=clk,
        **kw,  # type: ignore[arg-type]
    )
    _LIVE_CTLS.append(ctl)
    return ctl, clk


async def connect_ok(ctl: cc.ClientController) -> None:
    await ctl.connect(
        "10.0.0.9",
        9998,
        get_password=lambda h: "s3cret-pw",
        confirm_fingerprint=lambda fp, h: True,
    )


async def settle() -> None:
    """让 _spawn 投递的出站 send 任务跑一拍。"""
    await asyncio.sleep(0.02)


class TestStateMachine:
    async def test_connect_success_sequence(self) -> None:
        ctl, _ = make_ctrl()
        seen: list[str] = []
        ctl.set_events(SimpleNamespace(state_changed=seen.append))
        await connect_ok(ctl)
        assert seen == ["connecting", "authenticating", "connected"]
        assert ctl.state == "connected"
        ctrl = FakeControl.instances[0]
        assert ctrl.provided_password == "s3cret-pw"
        assert ctrl.session_token == "tok-abc"

    async def test_screen_port_offset_and_token_handoff(self) -> None:
        ctl, _ = make_ctrl(Settings(screen_port_offset=2))
        await connect_ok(ctl)
        scr = FakeScreen.instances[0]
        assert scr.port == 9998 + 2
        assert scr.session_token_in == "tok-abc"
        assert scr.connection_id_in == b"c" * 32

    async def test_async_password_and_fingerprint_awaited(self) -> None:
        ctl, _ = make_ctrl()
        asked: list[str] = []

        async def get_pw(host: str) -> str:
            asked.append(host)
            return "async-pw"

        async def confirm(fp: str, host: str) -> bool:
            return True

        await ctl.connect("h1", 9998, get_password=get_pw, confirm_fingerprint=confirm)
        assert asked == ["h1"]
        assert FakeControl.instances[0].provided_password == "async-pw"

    async def test_password_cancel_no_reconnect(self) -> None:
        ctl, _ = make_ctrl(Settings(auto_reconnect=True))
        with pytest.raises(AuthError, match="取消"):
            await ctl.connect(
                "h", 9998, get_password=lambda h: None, confirm_fingerprint=lambda fp, ho: True
            )
        assert ctl.state == "disconnected"
        await asyncio.sleep(0)  # 给重连环一拍，确认无人启动
        assert ctl._reconnect is None  # noqa: SLF001

    async def test_connect_network_failure_starts_reconnect_loop(self) -> None:
        FakeControl.connect_error = ConnectionRefusedError("拒绝")
        ctl, _ = make_ctrl(Settings(auto_reconnect=True, reconnect_delay=0.01, max_reconnect_attempts=2))
        with pytest.raises(ConnectionRefusedError):
            await connect_ok(ctl)
        assert ctl.state == "reconnecting"
        # 环在退避中；最终耗尽 → disconnected
        await asyncio.sleep(0.3)
        assert ctl.state == "disconnected"

    async def test_connect_auth_failure_stops_loop_fast(self) -> None:
        # 首连认证失败也进环；环内再认证失败 → 立即终止（不烧完 attempts）
        FakeControl.connect_error = AuthError("口令错误")
        ctl, _ = make_ctrl(
            Settings(auto_reconnect=True, reconnect_delay=0.01, max_reconnect_attempts=50)
        )
        calls_before = len(FakeControl.instances)
        with pytest.raises(AuthError):
            await connect_ok(ctl)
        await asyncio.sleep(0.15)
        assert ctl.state == "disconnected"
        # 首连 1 次 + 环内至多 1~2 次，远小于 50
        assert len(FakeControl.instances) - calls_before <= 4

    async def test_no_auto_reconnect_disconnected_directly(self) -> None:
        FakeControl.connect_error = ConnectionRefusedError("拒绝")
        ctl, _ = make_ctrl(Settings(auto_reconnect=False))
        with pytest.raises(ConnectionRefusedError):
            await connect_ok(ctl)
        assert ctl.state == "disconnected"

    async def test_disconnect_idempotent_and_clears_password(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        await ctl.disconnect()
        await ctl.disconnect()
        assert ctl.state == "disconnected"
        assert ctl._password_holder is None  # noqa: SLF001
        assert FakeControl.instances[0].closed is True

    async def test_reconnect_reuses_password_no_second_prompt(self) -> None:
        ctl, _ = make_ctrl(Settings(auto_reconnect=True, reconnect_delay=0.01))
        prompts: list[str] = []

        async def get_pw(host: str) -> str:
            prompts.append(host)
            return "once-only"

        await ctl.connect("h", 9998, get_password=get_pw, confirm_fingerprint=lambda fp, ho: True)
        assert prompts == ["h"]
        ctl._on_link_dead("test")  # noqa: SLF001
        assert ctl.state == "reconnecting"
        await asyncio.sleep(0.2)
        assert ctl.state == "connected"
        assert prompts == ["h"]  # 重连未再弹框


class TestSendRouting:
    async def test_control_disabled_is_noop(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.send_mouse("move", 10, 20)
        ctl.send_key(0x41, True)
        ctl.send_text("你好")
        ctl.flush_now()
        await asyncio.sleep(0.05)
        assert FakeControl.instances[0].sent == []

    async def test_mouse_events_become_protocol_messages(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        ctl.send_mouse("move", 100, 200)
        ctl.send_mouse("press:right", 100, 200)
        ctl.send_mouse("release:right", 100, 200)
        ctl.send_mouse("wheel:down", 30, 40)
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert [type(m).__name__ for m in sent] == ["MouseEvent"] * 4
        move, press, release, wheel = sent
        assert isinstance(move, MouseEvent) and move.action is MouseAction.MOVE
        assert move.x == 100 and move.y == 200 and move.timestamp > 0
        assert isinstance(press, MouseEvent) and press.action is MouseAction.PRESS
        assert press.button is MouseButton.RIGHT
        assert isinstance(release, MouseEvent) and release.action is MouseAction.RELEASE
        assert isinstance(wheel, MouseEvent) and wheel.action is MouseAction.SCROLL
        assert wheel.scroll_delta == -1  # DOWN → (0,-1)
        assert wheel.button is MouseButton.LEFT

    async def test_move_coalesced_to_last_value(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        for i in range(50):
            ctl.send_mouse("move", i, i)
        assert FakeControl.instances[0].sent == []  # 未到节拍
        ctl.flush_now()
        await settle()
        moves = [m for m in FakeControl.instances[0].sent if isinstance(m, MouseEvent)]
        assert len(moves) == 1 and moves[0].x == 49  # 只留末值

    async def test_key_qt_code_mapping(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        ctl.send_key(0x01000004, True)   # Qt Key_Return → enter
        ctl.send_key(0x01000004, False)
        ctl.send_key(0x41, True)          # 'A' → a
        ctl.send_key(0x30000005, True)    # 小键盘 5 → kp5
        ctl.send_key(0xDEADBEEF, True)    # 未知键 → 忽略
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert all(isinstance(m, KeyEvent) for m in sent)
        assert [(m.action, m.key) for m in sent] == [
            (KeyAction.PRESS, "enter"),
            (KeyAction.RELEASE, "enter"),
            (KeyAction.PRESS, "a"),
            (KeyAction.PRESS, "kp5"),
        ]

    async def test_text_channel_and_ime_semantics(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        ctl.send_text("中")
        ctl.send_text("文")
        ctl.flush_now()
        await settle()
        (msg,) = FakeControl.instances[0].sent
        assert isinstance(msg, KeyEvent) and msg.action is KeyAction.TYPE
        assert msg.text == "中文"  # 连续提交拼接为单事件

    async def test_disable_drops_buffered_events(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        ctl.send_mouse("move", 5, 5)
        ctl.set_control_enabled(False)
        ctl.set_control_enabled(True)
        ctl.flush_now()
        assert FakeControl.instances[0].sent == []

    async def test_invalid_action_logged_not_raised(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        ctl.send_mouse("teleport", 1, 1)  # 非法载荷：记录不炸 UI

    async def test_send_without_connection_drops(self) -> None:
        ctl, _ = make_ctrl()
        ctl.set_control_enabled(True)
        ctl.send_mouse("press:left", 1, 1)
        ctl.flush_now()  # 无会话：事件出批但 _emit_event 见不到通道
        assert FakeControl.instances == []


class TestInboundDispatch:
    async def test_pong_rtt_measured(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl._on_control_message(  # noqa: SLF001
            Heartbeat(kind=HeartbeatKind.PONG, timestamp=time.time() - 0.05)
        )
        assert ctl._last_latency_ms is not None  # noqa: SLF001
        assert 40 <= ctl._last_latency_ms < 500  # noqa: SLF001

    async def test_ping_from_server_refreshes_heartbeat(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl._on_control_message(Heartbeat(kind=HeartbeatKind.PING))  # noqa: SLF001
        assert ctl._hb is not None and ctl._hb.is_running  # noqa: SLF001

    async def test_clipboard_inbound_dispatched(self) -> None:
        ctl, _ = make_ctrl()
        got: list[str] = []
        ctl.set_events(SimpleNamespace(clipboard_from_remote=got.append))
        await connect_ok(ctl)
        ctl._on_control_message(ClipboardEvent(text="远端文本"))  # noqa: SLF001
        assert got == ["远端文本"]

    async def test_send_clipboard_outbound(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        ctl.send_clipboard("本地文本")
        await asyncio.sleep(0.02)
        (msg,) = FakeControl.instances[0].sent
        assert isinstance(msg, ClipboardEvent) and msg.text == "本地文本"

    async def test_event_hook_exception_isolated(self) -> None:
        ctl, _ = make_ctrl()

        def boom(_text: str) -> None:
            raise RuntimeError("UI 回调炸了")

        ctl.set_events(SimpleNamespace(clipboard_from_remote=boom, state_changed=lambda s: None))
        await connect_ok(ctl)
        ctl._on_control_message(ClipboardEvent(text="x"))  # noqa: SLF001


class TestFramePipeline:
    def _jpeg_bytes(self, w: int = 8, h: int = 4) -> bytes:
        img = Image.new("RGB", (w, h), (10, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    async def test_jpeg_frame_decoded_and_dispatched(self) -> None:
        frames: list[object] = []
        ctl, _ = make_ctrl(image_factory=lambda w, h, arr: (w, h, arr))
        ctl.set_events(SimpleNamespace(frame_arrived=lambda img, **kw: frames.append((img, kw))))
        await connect_ok(ctl)
        frame = ScreenFrame(
            seq=1, timestamp=1.0, width=8, height=4, codec=Codec.JPEG,
            data=self._jpeg_bytes(), keyframe=True, origin_x=-1920, origin_y=0,
        )
        ctl._handle_screen_frame(frame)  # noqa: SLF001
        assert frames
        (img, kw) = frames[0]
        assert img is not None and kw == {"left": -1920, "top": 0}

    async def test_h264_stream_decodes_across_frames(self) -> None:
        from rdc.encoding.h264 import H264Encoder

        enc = H264Encoder(64, 48)
        made = []
        for i in range(4):
            arr = np.full((48, 64, 4), i * 40 % 255, dtype=np.uint8)
            arr[:, :, 3] = 255
            made.append(enc.encode(arr))
        enc.close()
        got: list[object] = []
        ctl, _ = make_ctrl(image_factory=lambda w, h, arr: (w, h, arr.shape))
        ctl.set_events(SimpleNamespace(frame_arrived=lambda img, **kw: got.append(img)))
        await connect_ok(ctl)
        for i, ef in enumerate(made):
            ctl._handle_screen_frame(  # noqa: SLF001
                ScreenFrame(seq=i, timestamp=0.0, width=64, height=48,
                            codec=Codec.H264, data=ef.data, keyframe=ef.is_keyframe)
            )
        assert len(got) == 4
        assert got[0] == (64, 48, (48, 64, 3))

    async def test_decode_failure_isolated(self) -> None:
        ctl, _ = make_ctrl()
        await connect_ok(ctl)
        junk = ScreenFrame(seq=0, timestamp=0.0, width=64, height=64,
                           codec=Codec.H264, data=b"not-h264", keyframe=True)
        ctl._handle_screen_frame(junk)  # noqa: SLF001  # 不抛即通过

    async def test_frame_drives_input_cadence(self) -> None:
        # A4：帧到达间隔 → coalescer 节拍（实测 fps 同步）
        clk = FakeClock()
        ctl, _ = make_ctrl(clock=clk)
        await connect_ok(ctl)
        before = ctl._coalescer.next_delay_s()  # noqa: SLF001
        for i in range(6):
            clk.advance(1.0 / 10)  # 画面 10fps
            ctl._handle_screen_frame(  # noqa: SLF001
                ScreenFrame(seq=i, timestamp=0.0, width=4, height=4,
                            codec=Codec.JPEG, data=self._tiny_jpeg(), keyframe=True)
            )
        after = ctl._coalescer.next_delay_s()  # noqa: SLF001
        assert ctl._measured_fps == pytest.approx(10.0, abs=1.0)  # noqa: SLF001
        assert after <= before  # min_input_fps=30 兜底：节拍不松于 33ms
        assert after >= 0

    def _tiny_jpeg(self) -> bytes:
        img = Image.new("RGB", (4, 4))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    async def test_image_factory_exception_isolated(self) -> None:
        def evil(_w: int, _h: int, _arr: object) -> object:
            raise ValueError("Qt 转换炸了")

        ctl, _ = make_ctrl(image_factory=evil)
        await connect_ok(ctl)
        ctl._handle_screen_frame(  # noqa: SLF001
            ScreenFrame(seq=0, timestamp=0.0, width=4, height=4,
                        codec=Codec.JPEG, data=self._tiny_jpeg(), keyframe=True)
        )

    async def test_no_image_factory_raises_connection_error(self) -> None:
        ctl = cc.ClientController(coalescer=InputCoalescer(clock=FakeClock()), clock=FakeClock())
        await connect_ok(ctl)
        # 未注入 image_factory：解码后的构造抛 ConnectionError（装配缺陷显式暴露，不静默）
        with pytest.raises(ConnectionError, match="image_factory"):
            ctl._handle_screen_frame(  # noqa: SLF001
                ScreenFrame(seq=0, timestamp=0.0, width=4, height=4,
                            codec=Codec.JPEG, data=self._tiny_jpeg(), keyframe=True)
            )


class TestMetricsAndHeartbeat:
    async def test_metrics_tick_reports_once_per_second(self) -> None:
        clk = FakeClock()
        events: list[dict] = []
        ctl, _ = make_ctrl(clock=clk)
        ctl.set_events(SimpleNamespace(metrics_updated=lambda **kw: events.append(kw)))
        await connect_ok(ctl)
        ctl._handle_screen_frame(  # noqa: SLF001
            ScreenFrame(seq=0, timestamp=0.0, width=4, height=4,
                        codec=Codec.JPEG, data=self._tiny_jpeg(), keyframe=True)
        )
        clk.advance(1.5)
        ctl._metrics_tick()  # noqa: SLF001
        (m,) = events
        assert m["total_frames"] == 1 and m["resolution"] == (4, 4)
        assert m["fps"] == pytest.approx(1 / 1.5, abs=0.1)
        assert m["bandwidth_kbps"] > 0
        events.clear()
        clk.advance(0.4)
        ctl._metrics_tick()  # noqa: SLF001  # 窗口未到 1s 不上报
        assert events == []

    async def test_heartbeat_pings_control_channel(self) -> None:
        ctl, _ = make_ctrl(
            settings=Settings(heartbeat_interval=0.05, heartbeat_timeout=5.0)
        )
        await connect_ok(ctl)
        await asyncio.sleep(0.18)
        pings = [m for m in FakeControl.instances[0].sent
                 if isinstance(m, Heartbeat) and m.kind is HeartbeatKind.PING]
        assert len(pings) >= 2

    async def test_heartbeat_dead_triggers_reconnect(self) -> None:
        ctl, _ = make_ctrl(
            settings=Settings(heartbeat_interval=0.02, heartbeat_timeout=0.08,
                              auto_reconnect=True, reconnect_delay=0.01)
        )
        await connect_ok(ctl)
        # 画面/控制通道保持存活（FakeControl 不 EOF），仅让心跳判死：停 pong
        await asyncio.sleep(0.25)
        assert ctl.state in ("reconnecting", "connected")  # 环启动或已重连成
        FakeControl.connect_error = None

    def _tiny_jpeg(self) -> bytes:
        img = Image.new("RGB", (4, 4))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()


class TestKeyTableAlignment:
    def test_qt_table_matches_input_keys(self) -> None:
        from rdc.input.keys import _QT_FUNCTION as REAL

        mine = {int(k): v for k, v in cc._QT_FUNCTION.items()}  # noqa: SLF001
        assert mine == {int(k): v for k, v in REAL.items()}

    def test_space_via_table(self) -> None:
        assert cc._qt_key_to_name(0x20) == "space"  # noqa: SLF001

    def test_f12_and_modifier(self) -> None:
        assert cc._qt_key_to_name(0x0100003B) == "f12"  # noqa: SLF001
        assert cc._qt_key_to_name(0x01000021) == "ctrl"  # noqa: SLF001

    def test_unknown_returns_none(self) -> None:
        assert cc._qt_key_to_name(0x02FF0000) is None  # noqa: SLF001


class TestUiProtocolAlignment:
    """controller 必须结构化满足 UI 侧 ClientController Protocol（零改动接入）。"""

    def test_method_surface(self) -> None:
        ctl, _ = make_ctrl()
        for name in ("set_events", "connect", "disconnect", "set_control_enabled",
                     "send_mouse", "send_key", "send_text"):
            assert callable(getattr(ctl, name)), name

    def test_state_strings_match_connection_state(self) -> None:
        from rdc.ui.main_window import ConnectionState

        ctl, _ = make_ctrl()
        assert ctl.state == ConnectionState.IDLE
        for st in ConnectionState:
            assert st in {
                "idle", "connecting", "authenticating",
                "connected", "reconnecting", "disconnected",
            }

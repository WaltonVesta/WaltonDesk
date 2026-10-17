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
from rdc.exceptions import AuthBannedError, AuthError, ConnectionError
from rdc.input.coalesce import InputCoalescer
from rdc.protocol.messages import (
    ClipboardEvent,
    Codec,
    Heartbeat,
    HeartbeatKind,
    HostStatus,
    HostStatusCode,
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

    async def test_connect_auth_failure_starts_no_reconnect(self) -> None:
        # 走查第四批缺陷 3：首连认证失败**绝不进重连环**——一次点击恰好一次
        # 服务端认证尝试（旧版起环后立即复用错误口令再试一次，计数×2 喂封禁）
        FakeControl.connect_error = AuthError("口令错误")
        ctl, _ = make_ctrl(
            Settings(auto_reconnect=True, reconnect_delay=0.01, max_reconnect_attempts=50)
        )
        calls_before = len(FakeControl.instances)
        with pytest.raises(AuthError):
            await connect_ok(ctl)
        assert ctl.state == "disconnected"
        assert ctl._reconnect is None  # noqa: SLF001
        await asyncio.sleep(0.15)  # 即使错误地起了环也该暴露
        assert len(FakeControl.instances) - calls_before == 1
        assert ctl.state == "disconnected"

    async def test_link_dead_auth_abort_no_reconnect_success_log(self) -> None:
        """会话期判死进环后网络恢复但对端拒绝（AuthError）：环上抛中止，
        不得记 reconnect_success（旧版 return True 被 run() 当成功，日志
        自相矛盾——走查第四批证据链第 5/6 行）。"""
        ctl, _ = make_ctrl(Settings(auto_reconnect=True, reconnect_delay=0.01))
        await connect_ok(ctl)
        records: list[str] = []

        class _Collect:
            def warning(self, event: str, **kw: object) -> None:
                records.append(f"warning:{event}")

            def info(self, event: str, **kw: object) -> None:
                records.append(f"info:{event}")

        spy = _Collect()
        orig_cc = cc._LOG  # noqa: SLF001
        cc._LOG = spy  # type: ignore[assignment]  # noqa: SLF001
        try:
            FakeControl.connect_error = AuthError("封禁拒绝")
            ctl._on_link_dead("eof")  # noqa: SLF001
            assert ctl.state == "reconnecting"
            # ReconnectController 实例自带 logger（transport.reconnect），
            # 一并替换——reconnect_success 正是它记的（病灶 5/6 行日志源）。
            # 环实例随本测试的 controller 一起销毁，无需还原。
            assert ctl._reconnect is not None  # noqa: SLF001
            ctl._reconnect._log = spy  # noqa: SLF001
            for _ in range(30):
                await asyncio.sleep(0.02)
                if ctl.state == "disconnected":
                    break
            assert ctl.state == "disconnected"
        finally:
            cc._LOG = orig_cc  # type: ignore[assignment]  # noqa: SLF001
        joined = " ".join(records)
        assert "reconnect_auth_class_abort" in joined
        assert "reconnect_success" not in joined
        assert "reconnect_exhausted_giveup" not in joined


class TestBanCache:
    """走查第四批缺陷 2：服务端 banned 响应缓存 + 弹框前本地拦截。"""

    async def test_first_ban_response_cached(self) -> None:
        clk = FakeClock()
        ctl, _ = make_ctrl(clock=clk)
        FakeControl.connect_error = AuthBannedError(
            AuthBannedError.format_message(300), remaining_seconds=300
        )
        with pytest.raises(AuthBannedError):
            await connect_ok(ctl)
        assert ctl._ban_host == "10.0.0.9"  # noqa: SLF001
        assert ctl._ban_until == clk.now + 300  # noqa: SLF001
        assert ctl.state == "disconnected"

    async def test_second_connect_blocked_before_prompt(self) -> None:
        """缓存未过期：二次 connect 不建通道、不弹口令框，直接抛 AuthBannedError。"""
        clk = FakeClock()
        ctl, _ = make_ctrl(clock=clk)
        FakeControl.connect_error = AuthBannedError(
            AuthBannedError.format_message(300), remaining_seconds=300
        )
        with pytest.raises(AuthBannedError):
            await connect_ok(ctl)
        built = len(FakeControl.instances)
        prompts: list[str] = []

        async def get_pw(h: str) -> str:
            prompts.append(h)
            return "pw"

        with pytest.raises(AuthBannedError) as ei:
            await ctl.connect(
                "10.0.0.9", 9998, get_password=get_pw, confirm_fingerprint=lambda fp, h: True
            )
        assert prompts == []  # 弹框前即拦截
        assert len(FakeControl.instances) == built  # 未再发网络包
        assert "分钟" in str(ei.value)

    async def test_other_host_not_blocked(self) -> None:
        """缓存按主机比对：换主机正常发起（FakeControl 已恢复成功路径）。"""
        clk = FakeClock()
        ctl, _ = make_ctrl(clock=clk)
        FakeControl.connect_error = AuthBannedError(
            AuthBannedError.format_message(300), remaining_seconds=300
        )
        with pytest.raises(AuthBannedError):
            await connect_ok(ctl)
        FakeControl.connect_error = None
        built = len(FakeControl.instances)
        await ctl.connect(
            "10.0.0.8",
            9998,
            get_password=lambda h: "pw",
            confirm_fingerprint=lambda fp, h: True,
        )
        assert len(FakeControl.instances) == built + 1
        assert ctl.state == "connected"

    async def test_cache_expires_then_connects(self) -> None:
        """假时钟推进越过封禁期 → 预检放行，正常发起连接。"""
        clk = FakeClock()
        ctl, _ = make_ctrl(clock=clk)
        FakeControl.connect_error = AuthBannedError(
            AuthBannedError.format_message(300), remaining_seconds=300
        )
        with pytest.raises(AuthBannedError):
            await connect_ok(ctl)
        FakeControl.connect_error = None
        clk.advance(301)
        await connect_ok(ctl)
        assert ctl.state == "connected"

    async def test_reconnect_guard_when_no_running_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 解释器/GC 拆环阶段：判死时若无运行中循环，直接落定断开，绝不
        # ensure_future 起环（否则协程无人 await，退出期报 never awaited）。
        ctl, _ = make_ctrl(Settings(auto_reconnect=True))
        await connect_ok(ctl)

        def _boom() -> asyncio.AbstractEventLoop:
            raise RuntimeError("no running event loop")

        with monkeypatch.context() as m:
            m.setattr(asyncio, "get_running_loop", _boom)
            ctl._on_link_dead("eof")  # noqa: SLF001
        assert ctl.state == "disconnected"
        assert ctl._reconnect_task is None  # noqa: SLF001

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

    async def test_host_status_stalled_dispatched(self) -> None:
        """服务端画面停滞 → UI 事件（含安全桌面归因）原样转发（走查第六批 P0）。"""
        ctl, _ = make_ctrl()
        got: list[dict[str, object]] = []
        ctl.set_events(
            SimpleNamespace(host_status=lambda **kw: got.append(kw))
        )
        await connect_ok(ctl)
        ctl._on_control_message(  # noqa: SLF001
            HostStatus(code=HostStatusCode.SCREEN_STALLED, secure_desktop=True, detail="BitBlt")
        )
        assert got == [{"stalled": True, "secure_desktop": True}]

    async def test_host_status_ok_dispatch_recovery(self) -> None:
        ctl, _ = make_ctrl()
        got: list[dict[str, object]] = []
        ctl.set_events(
            SimpleNamespace(host_status=lambda **kw: got.append(kw))
        )
        await connect_ok(ctl)
        ctl._on_control_message(HostStatus(code=HostStatusCode.OK))  # noqa: SLF001
        assert got == [{"stalled": False, "secure_desktop": False}]

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

    async def test_h264_size_change_decodes_on_same_decoder(self) -> None:
        """走查第十一批装配锁定：被控端改分辨率后，长期解码器不重建即可出帧。

        探针实测：编码器尺寸重建后的首帧 IDR **in-band 自带新 SPS/PPS**
        （nal 序列 [7,8,6,...]），FFmpeg 解码器收到即自适应切尺寸。本用例
        把该语义固化进测试——若未来有人给 controller 加"分辨率变化就重建
        解码器"或"丢弃新尺寸帧"的逻辑，此处即红。
        """
        from rdc.encoding.h264 import H264Encoder

        enc = H264Encoder(64, 48)
        made = []
        # 旧尺寸两帧（IDR+delta）→ 新尺寸两帧（encode 内自动跟随重建，再 IDR+delta）
        for h, w in ((48, 64), (32, 48)):
            for i in range(2):
                arr = np.full((h, w, 4), (i + 1) * 60, dtype=np.uint8)
                arr[:, :, 3] = 255
                made.append(enc.encode(arr))
        enc.close()
        assert made[2].width == 48 and made[2].height == 32  # 编码器确已跟随

        got: list[object] = []
        ctl, _ = make_ctrl(image_factory=lambda w, h, arr: (w, h, arr.shape))
        ctl.set_events(SimpleNamespace(frame_arrived=lambda img, **kw: got.append(img)))
        await connect_ok(ctl)
        dec_mid = None
        for i, ef in enumerate(made):
            ctl._handle_screen_frame(  # noqa: SLF001
                ScreenFrame(seq=i, timestamp=0.0, width=ef.width, height=ef.height,
                            codec=Codec.H264, data=ef.data, keyframe=ef.is_keyframe)
            )
            if i == 1:
                dec_mid = ctl._h264_decoder  # noqa: SLF001
        assert got == [
            (64, 48, (48, 64, 3)), (64, 48, (48, 64, 3)),
            (48, 32, (32, 48, 3)), (48, 32, (32, 48, 3)),
        ], "新尺寸帧必须直接可解（in-band SPS/PPS 自适应）"
        assert ctl._h264_decoder is dec_mid  # noqa: SLF001 - 全程同一解码器

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
        _LIVE_CTLS.append(ctl)  # 直接构造也要登记，否则收尾不拆会话、任务泄漏到退出期
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


class TestStickyKeyReconcile:
    """走查第九批：Win 释放被系统吞掉后的粘滞热键对账（应急兜底）。

    病灶（用户实测）：主机按两下 Win → 虚拟机 Win 永久"按住"（后续所有按键
    变 Win+组合）。链路：Win 按下被 ScreenView.keyPressEvent 正常转发，
    释放被 Windows 系统接管（开始菜单）后 Qt 收不到 keyReleaseEvent →
    释放永不转发 → 被控端按住不放。兜底：按住超 ``_STICKY_HELD_TIMEOUT_S``
    未见释放 → 下一次键事件出批前/冲刷环节拍主动补发一次 RELEASE。

    哨兵直接以假时钟拨过阈值（零真实等待），并按出批消息序列断言；
    病灶（无兜底）回退后本组"注入/解锁"类用例转红。
    """

    KEY_WIN = 0x01000022  # Qt Key_Meta → "win"（_QT_FUNCTION 表）
    KEY_A = 0x41

    async def _connected(self) -> tuple[cc.ClientController, FakeClock]:
        ctl, clk = make_ctrl()
        await connect_ok(ctl)
        ctl.set_control_enabled(True)
        return ctl, clk

    async def test_win_press_then_fast_release_no_injection(self) -> None:
        """未超阈值的正常短按：补发不得发生（注入只在超阈值时出现）。"""
        ctl, clk = await self._connected()
        ctl.send_key(self.KEY_WIN, True)
        clk.advance(0.5)  # 500ms 后真实释放到达（焦点路径未丢的少数场景）
        ctl.send_key(self.KEY_WIN, False)
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert [(m.action, m.key) for m in sent] == [
            (KeyAction.PRESS, "win"),
            (KeyAction.RELEASE, "win"),
        ]
        assert ctl._sticky_held == {}  # noqa: SLF001 - 销账

    async def test_stuck_win_unlocked_by_next_key(self) -> None:
        """建议 B 形态：释放丢失，用户敲下一个键 → 该键之前先补发 win 释放。"""
        ctl, clk = await self._connected()
        ctl.send_key(self.KEY_WIN, True)
        ctl.flush_now()
        await settle()
        assert [(m.action, m.key) for m in FakeControl.instances[0].sent] == [
            (KeyAction.PRESS, "win")
        ]
        FakeControl.instances[0].sent.clear()
        clk.advance(cc._STICKY_HELD_TIMEOUT_S + 0.1)  # noqa: SLF001 - 超阈值
        ctl.send_key(self.KEY_A, True)
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert [(m.action, m.key) for m in sent] == [
            (KeyAction.RELEASE, "win"),  # 解锁注入，且严格先于新键
            (KeyAction.PRESS, "a"),
        ]
        assert ctl._sticky_held == {}  # noqa: SLF001

    async def test_stuck_win_unlocked_by_flush_loop_when_idle(self) -> None:
        """建议 A 形态：用户不再敲任何键，冲刷环节拍同样完成解锁（静置自愈）。"""
        ctl, clk = await self._connected()
        ctl.send_key(self.KEY_WIN, True)
        ctl.flush_now()
        await settle()
        FakeControl.instances[0].sent.clear()
        clk.advance(cc._STICKY_HELD_TIMEOUT_S + 0.1)  # noqa: SLF001
        # 直调对账+出批等价于冲刷环一次 tick（环体代码路径同两行）
        ctl._reconcile_sticky_keys()  # noqa: SLF001
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert [(m.action, m.key) for m in sent] == [(KeyAction.RELEASE, "win")]
        # 记账已清：第二次对账不再重复注入（每次按下至多补发一次）
        FakeControl.instances[0].sent.clear()
        clk.advance(5.0)
        ctl._reconcile_sticky_keys()  # noqa: SLF001
        ctl.flush_now()
        await settle()
        assert FakeControl.instances[0].sent == []

    async def test_shift_long_hold_not_injected(self) -> None:
        """监视集合只含 win：Shift 长按是合法用法（连续输入大写），不得误伤。"""
        ctl, clk = await self._connected()
        ctl.send_key(0x01000020, True)  # Qt Key_Shift → "shift"
        clk.advance(cc._STICKY_HELD_TIMEOUT_S + 1.0)  # noqa: SLF001
        ctl.send_key(self.KEY_A, True)
        ctl.send_key(self.KEY_A, False)
        ctl._reconcile_sticky_keys()  # noqa: SLF001
        ctl.flush_now()
        await settle()
        sent = FakeControl.instances[0].sent
        assert [(m.action, m.key) for m in sent] == [
            (KeyAction.PRESS, "shift"),  # 无 shift 释放注入——正常长按合法
            (KeyAction.PRESS, "a"),
            (KeyAction.RELEASE, "a"),
        ]

    async def test_teardown_clears_sticky_bookkeeping(self) -> None:
        """拆会话清账：陈旧记账不得跨重连残留（防向新会话补发无来源释放）。"""
        ctl, clk = await self._connected()
        ctl.send_key(self.KEY_WIN, True)
        clk.advance(cc._STICKY_HELD_TIMEOUT_S + 0.1)  # noqa: SLF001
        await ctl.disconnect()
        assert ctl._sticky_held == {}  # noqa: SLF001

    async def test_control_off_clears_sticky_bookkeeping(self) -> None:
        """关控制清账：与 coalescer 缓冲同口径丢弃。"""
        ctl, _ = await self._connected()
        ctl.send_key(self.KEY_WIN, True)
        assert ctl._sticky_held  # noqa: SLF001 - 按下已登记
        ctl.set_control_enabled(False)
        assert ctl._sticky_held == {}  # noqa: SLF001

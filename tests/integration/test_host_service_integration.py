"""被控端服务集成测试（阶段 9 批 3）：真实 loopback TLS + 全栈装配。

覆盖任务书 e 组"service.py 集成测试（含 transport 两处回调）"：

- ``check_client``：封禁 IP 在握手**前**被拒（不消耗握手帧）；
- ``on_auth_failed``：错口令 → 滑动窗口计数 → 第 3 次触发封禁 → 准入拒绝；
- 控制会话：心跳 PING→PONG 回包、鼠标/键盘事件 → 注入替身调用序列；
- 画面会话：ScreenJoin 令牌 → registry 登记 → pipeline publish →
  客户端 ScreenChannel 收到可验签的 ScreenFrame；
- 生命周期：start/pause/resume/shutdown/restart、snapshot/get_stats 字段。

注入点：ArrayScreenSource（假屏幕）、输入替身、真实 Banlist（persist=False，
经 HostAuth 注入）、真实 TLS 自签名证书（TOFU 确认恒真）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from rdc.capture.source import ArrayScreenSource
from rdc.config.settings import Settings
from rdc.exceptions import AuthError
from rdc.input.base import MouseButton as InputButton
from rdc.protocol.messages import (
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
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod
from rdc.server.audit import AuditLogger
from rdc.server.auth import HostAuth
from rdc.server.banlist import Banlist
from rdc.server.service import HostService
from rdc.transport.control_channel import ControlChannel
from rdc.transport.screen_channel import ScreenChannel

pytestmark = pytest.mark.integration

PW = "strong-pass-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_path / "known_fp.json")
    monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _confirm(_fp: str, _host: str) -> bool:
    return True


class RecordingInputBackend:
    """输入替身：记录调用序列（不触真机，决策点 1 口径）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def move_mouse(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def click(self, x: int, y: int, button: InputButton, press: bool) -> None:
        self.calls.append(("click", x, y, button.value, press))

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.calls.append(("scroll", x, y, dx, dy))

    def key_press(self, key: str) -> None:
        self.calls.append(("press", key))

    def key_release(self, key: str) -> None:
        self.calls.append(("release", key))

    def type_text(self, text: str) -> None:
        self.calls.append(("text", text))

    def bounds(self):  # noqa: ANN201
        raise AssertionError("测试不触 bounds")

    def reset(self) -> None:
        self.calls.append(("reset",))

    def supported(self) -> bool:
        return True


def make_settings(tmp_path: Path, **kw: object) -> Settings:
    base: dict = {
        "bind_host": "127.0.0.1",
        "port": _free_port(),
        "control_port": _free_port(),
        "enable_auth": True,
        "password_hash": hash_password(PW),
        "max_auth_attempts": 3,
        "auth_ban_duration": 300,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def make_service(
    tmp_path: Path,
    settings: Settings,
    *,
    frames: list[np.ndarray] | None = None,
) -> tuple[HostService, RecordingInputBackend]:
    """构造注入替身依赖的 HostService（真实 Banlist persist=False + 内存屏幕源）。"""
    input_backend = RecordingInputBackend()
    clock = time.monotonic
    bans = Banlist(path=tmp_path / "ban.json", persist=False, clock=clock)  # type: ignore[arg-type]
    audit = AuditLogger()
    auth = HostAuth(settings, banlist=bans, audit=audit)
    src_frames = frames or [_blank_frame()]
    source = ArrayScreenSource(src_frames, loop=True)
    svc = HostService(
        settings=settings,
        source=source,
        input_backend=input_backend,
        auth=auth,
        audit=audit,
        show_tray=False,
    )
    return svc, input_backend


def _blank_frame() -> np.ndarray:
    return np.zeros((16, 16, 4), dtype=np.uint8)




def _control(settings: Settings, password: str = PW) -> ControlChannel:
    return ControlChannel(
        settings.bind_host,
        settings.control_port,
        password_provider=lambda: password,
        fingerprint_confirm=_confirm,
    )


async def _wait_until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("条件等待超时")
        await asyncio.sleep(0.01)


class TestLifecycle:
    async def test_start_pause_resume_shutdown(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            assert await svc.start() is True
            assert svc.is_running and not svc.is_paused
            assert await svc.start() is True  # 幂等
            await svc.pause()
            assert not svc.is_running and svc.is_paused
            await svc.resume()
            assert svc.is_running and not svc.is_paused
            snap = svc.snapshot()
            assert snap.running and snap.uptime_s >= 0
            stats = svc.get_stats()
            assert stats["active_connections"] == 0 and stats["banned_ips"] == 0
        finally:
            await svc.shutdown()
        assert svc.should_exit and not svc.is_running

    async def test_start_bad_port_returns_false(self, tmp_app_dir: Path) -> None:
        import socket

        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            port = blocker.getsockname()[1]
            s = make_settings(tmp_app_dir, control_port=port)
            svc, _ = make_service(tmp_app_dir, s)
            assert await svc.start() is False
            assert not svc.is_running
            await svc.shutdown()

    async def test_restart_picks_new_hash(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            assert await svc.start()
            ch = _control(s, PW)
            await ch.connect()
            await ch.close()
            await svc.pause()
            s.password_hash = hash_password("rotated-2")
            await svc.restart()
            ch2 = _control(s, PW)
            with pytest.raises(AuthError):
                await ch2.connect()
            ch3 = _control(s, "rotated-2")
            await ch3.connect()
            await ch3.close()
        finally:
            await svc.shutdown()


class TestAuthIntegration:
    async def test_wrong_password_rejected_and_banned_after_three(
        self, tmp_app_dir: Path
    ) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            assert await svc.start()
            for _ in range(3):
                ch = _control(s, "bad-guess")
                with pytest.raises(AuthError):
                    await ch.connect()
                await ch.close()
            await _wait_until(lambda: svc._auth.banlist.banned_count == 1)  # noqa: SLF001
            # 第 4 次：check_client 在握手前拒绝——TCP+TLS 可达但立即断
            with pytest.raises((AuthError, OSError, asyncio.TimeoutError)):
                ch = _control(s, PW)  # 正确口令也被准入拒绝（封禁优先）
                await asyncio.wait_for(ch.connect(), timeout=5)
        finally:
            await svc.shutdown()

    async def test_auth_success_resets_window(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            for _ in range(2):
                ch = _control(s, "oops")
                with pytest.raises(AuthError):
                    await ch.connect()
                await ch.close()
            ok = _control(s, PW)
            await ok.connect()
            await _wait_until(lambda: svc._auth.banlist.failure_count("127.0.0.1") == 0)  # noqa: SLF001
            await ok.close()
        finally:
            await svc.shutdown()

    async def test_no_password_anyone_passes(self, tmp_app_dir: Path) -> None:
        # v4 语义保留：enable_auth=False → 任意口令通过（传输安全不降级）
        s = make_settings(tmp_app_dir, enable_auth=False)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s, "whatever")
            await ch.connect()
            assert ch.stream.hmac_enabled
            await ch.close()
        finally:
            await svc.shutdown()


class TestControlSession:
    async def test_heartbeat_ping_pong(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await ch.send(Heartbeat(kind=HeartbeatKind.PING, timestamp=5.0))
            msg = await asyncio.wait_for(ch.next_message(), timeout=5)
            assert isinstance(msg, Heartbeat) and msg.kind is HeartbeatKind.PONG
            await ch.close()
        finally:
            await svc.shutdown()

    async def test_mouse_events_reach_backend(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, backend = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await ch.send(MouseEvent(x=100, y=200, action=MouseAction.MOVE))
            await ch.send(
                MouseEvent(x=100, y=200, action=MouseAction.CLICK, button=MouseButton.LEFT)
            )
            await ch.send(
                MouseEvent(x=10, y=20, action=MouseAction.SCROLL, button=MouseButton.LEFT, scroll_delta=3)
            )
            await _wait_until(lambda: len(backend.calls) >= 4)
            assert backend.calls[0] == ("move", 100, 200)
            assert backend.calls[1] == ("click", 100, 200, "left", True)
            assert backend.calls[2] == ("click", 100, 200, "left", False)
            assert backend.calls[3] == ("scroll", 10, 20, 0, 3)
            await ch.close()
        finally:
            await svc.shutdown()

    async def test_key_events_with_modifiers(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, backend = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await ch.send(KeyEvent(action=KeyAction.TYPE, text="你好"))
            await ch.send(
                KeyEvent(action=KeyAction.PRESS, key="c", modifiers=())
            )
            await ch.send(KeyEvent(action=KeyAction.RELEASE, key="c", modifiers=()))
            await _wait_until(lambda: len(backend.calls) >= 3)
            assert backend.calls[0] == ("text", "你好")
            assert backend.calls[1] == ("press", "c")
            assert backend.calls[2] == ("release", "c")
            await ch.close()
        finally:
            await svc.shutdown()

    async def test_disconnect_resets_input(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, backend = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await ch.send(MouseEvent(x=1, y=1, action=MouseAction.MOVE))
            await _wait_until(lambda: len(backend.calls) >= 1)
            await ch.close()  # EOF → 会话 finally 触发 reset
            await _wait_until(lambda: ("reset",) in backend.calls)
        finally:
            await svc.shutdown()


class TestScreenSession:
    async def test_join_subscribes_and_receives_frames(self, tmp_app_dir: Path) -> None:
        # 动态画面：两帧内容不同 → 差分判定变化 → 编码 → publish → 客户端收到
        a = np.zeros((16, 16, 4), dtype=np.uint8)
        b = np.full((16, 16, 4), 90, dtype=np.uint8)
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s, frames=[a, b])
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            sc = ScreenChannel(
                s.bind_host,
                s.port,
                session_token=ch.session_token or "",
                connection_id=ch.connection_id or b"",
                fingerprint_confirm=_confirm,
            )
            await sc.connect()
            msg = await asyncio.wait_for(sc.next_message(), timeout=10)
            assert isinstance(msg, ScreenFrame)
            assert msg.width == 16 and msg.height == 16
            assert msg.codec in (Codec.H264, Codec.JPEG)
            assert len(msg.data) > 0
            await _wait_until(lambda: svc._registry.count() == 1)  # noqa: SLF001
            await sc.close()
            await _wait_until(lambda: svc._registry.count() == 0)  # noqa: SLF001
            await ch.close()
        finally:
            await svc.shutdown()

    async def test_replay_join_rejected(self, tmp_app_dir: Path) -> None:
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            token, cid = ch.session_token or "", ch.connection_id or b""
            ok = ScreenChannel(s.bind_host, s.port, session_token=token, connection_id=cid, fingerprint_confirm=_confirm)
            await ok.connect()
            await ok.close()
            replay = ScreenChannel(s.bind_host, s.port, session_token=token, connection_id=cid, fingerprint_confirm=_confirm)
            with pytest.raises(AuthError):
                await replay.connect()
            await replay.close()
            await ch.close()
        finally:
            await svc.shutdown()


class TestHostStatusBroadcast:
    """走查第六批 P0：画面停滞/恢复经控制端口主动下行到客户端。"""

    async def test_stall_and_recovery_broadcast(
        self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rdc.server.service as svc_mod

        monkeypatch.setattr(svc_mod, "is_secure_desktop", lambda: True)
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await _wait_until(lambda: len(svc._control_sessions) == 1)  # noqa: SLF001

            svc._on_capture_stall(True, "屏幕采集失败: BitBlt failed")  # noqa: SLF001
            msg = await asyncio.wait_for(ch.next_message(), timeout=5)
            assert isinstance(msg, HostStatus)
            assert msg.code is HostStatusCode.SCREEN_STALLED
            assert msg.secure_desktop is True
            assert "BitBlt" in msg.detail

            svc._on_capture_stall(False, "")  # noqa: SLF001
            ok = await asyncio.wait_for(ch.next_message(), timeout=5)
            assert isinstance(ok, HostStatus) and ok.code is HostStatusCode.OK

            await ch.close()
        finally:
            await svc.shutdown()

    async def test_stall_without_session_no_raise(
        self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无控制会话时停滞回调不得抛错（服务可先于连接进入停滞）。"""
        import rdc.server.service as svc_mod

        monkeypatch.setattr(svc_mod, "is_secure_desktop", lambda: False)
        s = make_settings(tmp_app_dir)
        svc, _ = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            svc._on_capture_stall(True, "no session")  # noqa: SLF001
            svc._on_capture_stall(False, "")  # noqa: SLF001
        finally:
            await svc.shutdown()

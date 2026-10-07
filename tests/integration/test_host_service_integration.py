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

    async def test_silent_link_idle_timeout_resets_input(self, tmp_app_dir: Path) -> None:
        """走查第六批 Bug4：静默断网（半开链路无 FIN/RST）服务端主动判死并释放修饰键。

        病灶：``_serve_control`` 旧版 ``await channel.next_message()`` 无超时——拔网线
        时 TCP 无 FIN/RST，阻塞到操作系统 keepalive（Windows ~2 小时），``finally`` 的
        ``_reset_input()`` 迟迟不触发 → 最后按住的 Win 在远端悬空（"按住 Win→断网→
        按 D 触发显示桌面"）。修复加 heartbeat_timeout 读空闲上限，静默即 break →
        finally reset。裸 ControlChannel connect 后不发心跳，天然模拟半开链路。
        """
        s = make_settings(tmp_app_dir, heartbeat_interval=0.1, heartbeat_timeout=0.3)
        svc, backend = make_service(tmp_app_dir, s)
        ch = _control(s)
        try:
            await svc.start()
            await ch.connect()
            await _wait_until(lambda: len(svc._control_sessions) == 1)  # noqa: SLF001
            # 不发任何心跳（模拟拔线）：0.3s 空闲后服务端应主动结束会话并 reset
            await _wait_until(
                lambda: ("reset",) in backend.calls
                and not svc._control_sessions,  # noqa: SLF001
                timeout=5.0,
            )
        finally:
            # 先关客户端再关停：病灶回归（无空闲超时）时服务端会话阻塞在
            # next_message，若跳过本步 shutdown 会跟着挂死——测试必须以断言
            # 失败收场，不许挂住套件。
            await ch.close()
            await svc.shutdown()

    async def test_active_link_not_reset_by_idle(self, tmp_app_dir: Path) -> None:
        """对照：持续有消息流入（周期 PING）的活会话不被空闲超时误杀、不提前 reset。"""
        s = make_settings(tmp_app_dir, heartbeat_interval=0.1, heartbeat_timeout=0.8)
        svc, backend = make_service(tmp_app_dir, s)
        try:
            await svc.start()
            ch = _control(s)
            await ch.connect()
            await _wait_until(lambda: len(svc._control_sessions) == 1)  # noqa: SLF001
            # 每 ~0.15s 一次 PING < 0.8s 空闲上限，连续四轮：会话须存活、无 reset
            for _ in range(4):
                await ch.send(Heartbeat(kind=HeartbeatKind.PING, timestamp=0.0))
                pong = await asyncio.wait_for(ch.next_message(), timeout=2)
                assert isinstance(pong, Heartbeat) and pong.kind is HeartbeatKind.PONG
                await asyncio.sleep(0.15)
            assert svc._control_sessions, "活链路不应被空闲超时判死"  # noqa: SLF001
            assert ("reset",) not in backend.calls, "活链路不应触发修饰键释放"
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

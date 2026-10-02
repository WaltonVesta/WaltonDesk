"""server/service HostService 离线分支单元测试（阶段 9 批 3 补测）。

不启动传输监听（真机 TLS 链路归 integration 测试），本文件用注入替身直接
驱动生命周期守卫、用户证书、消息分发、流控、剪贴板、清理循环等分支。
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import numpy as np
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from rdc.capture.source import ArrayScreenSource
from rdc.config.settings import Settings
from rdc.exceptions import InputControlError, TlsError
from rdc.input.base import MouseButton as InputButton
from rdc.protocol.codec import decode_frame, encode_frame
from rdc.protocol.messages import (
    ClipboardEvent,
    Codec,
    Heartbeat,
    HeartbeatKind,
    KeyAction,
    KeyEvent,
    ModifierKey,
    MouseAction,
    MouseButton,
    MouseEvent,
    ScreenFrame,
    StreamCmd,
    StreamConfig,
    StreamControl,
)
from rdc.server.audit import AuditLogger
from rdc.server.auth import HostAuth
from rdc.server.banlist import Banlist
from rdc.server.service import HostService, _now_utc, _quality_to_crf
from rdc.transport.server import ControlSession, ScreenSession
from tests._transport_support import make_stream_pair, pump

KEY = b"k" * 32


class RecordingInputBackend:
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

    def reset(self) -> None:
        self.calls.append(("reset",))

    def supported(self) -> bool:
        return True


class BoomBackend(RecordingInputBackend):
    def move_mouse(self, x: int, y: int) -> None:
        raise InputControlError("注入失败")

    def key_press(self, key: str) -> None:
        raise InputControlError("注入失败")

    def reset(self) -> None:
        raise RuntimeError("reset 炸了")


class CapturingLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def _record(self, level: str, event: str, **kw: object) -> None:
        self.entries.append({"event": event, "log_level": level, **kw})

    def info(self, event: str, **kw: object) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: object) -> None:
        self._record("warning", event, **kw)


def _frame8() -> np.ndarray:
    return np.zeros((8, 8, 4), dtype=np.uint8)


def make_svc(
    tmp_app_dir: Path,
    *,
    settings: Settings | None = None,
    backend: RecordingInputBackend | None = None,
    cap: CapturingLogger | None = None,
) -> HostService:
    s = settings if settings is not None else Settings(enable_auth=True, password_hash="h")
    audit = AuditLogger(logger=cap) if cap is not None else AuditLogger()
    bans = Banlist(path=tmp_app_dir / "ban.json", persist=False)
    auth = HostAuth(s, banlist=bans, audit=audit)
    return HostService(
        settings=s,
        source=ArrayScreenSource([_frame8()]),
        input_backend=backend,
        auth=auth,
        audit=audit,
        show_tray=False,
    )


def make_control() -> ControlSession:
    _, stream, _, _ = make_stream_pair()
    stream.enable_hmac(KEY)
    return ControlSession(stream, "9.9.9.9", KEY)


def write_user_cert(tmp_path: Path, *, name: str, days_past: int = 0, days_valid: int = 365) -> tuple[Path, Path]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_past)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rdc.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .sign(priv, hashes.SHA256())
    )
    cp, kp = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cp, kp


class TestLifecycleGuards:
    async def test_start_refused_after_exit_request(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir)
        svc._should_exit = True
        assert await svc.start() is False

    async def test_pause_without_start_noop(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir)
        await svc.pause()
        assert not svc.is_paused

    async def test_resume_without_pause_noop(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir)
        await svc.resume()
        assert not svc.is_paused

    async def test_shutdown_when_never_started(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir)
        await svc.shutdown()  # 全部 teardown 分支空跑不抛
        assert svc.should_exit and not svc.is_running

    def test_snapshot_without_pipeline(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir)
        snap = svc.snapshot()
        assert snap.running is False and snap.encoder_quality is None
        stats = svc.get_stats()
        assert stats["frames_encoded"] == 0 and stats["banned_ips"] == 0


class TestUserCertificate:
    def test_valid_cert_bundle_loaded(self, tmp_app_dir: Path) -> None:
        cp, kp = write_user_cert(tmp_app_dir, name="ok")
        s = Settings(tls_cert_path=str(cp), tls_key_path=str(kp))
        svc = make_svc(tmp_app_dir, settings=s)
        cert, key = svc._prepare_certificates()
        assert cert == cp and key == kp
        assert svc._cert_bundle is not None and svc._cert_bundle.subject_cn == "rdc.test"

    def test_expiring_cert_audited(self, tmp_app_dir: Path) -> None:
        cap = CapturingLogger()
        cp, kp = write_user_cert(tmp_app_dir, name="soon", days_valid=3)
        s = Settings(tls_cert_path=str(cp), tls_key_path=str(kp))
        svc = make_svc(tmp_app_dir, settings=s, cap=cap)
        svc._prepare_certificates()
        assert any(e["event"] == "cert.expiring" for e in cap.entries)

    def test_broken_cert_raises_tls(self, tmp_app_dir: Path) -> None:
        cp = tmp_app_dir / "bad.crt"
        cp.write_bytes(b"garbage")
        kp = tmp_app_dir / "bad.key"
        kp.write_bytes(b"garbage")
        s = Settings(tls_cert_path=str(cp), tls_key_path=str(kp))
        svc = make_svc(tmp_app_dir, settings=s)
        with pytest.raises(TlsError):
            svc._prepare_certificates()

    async def test_start_returns_false_on_cert_error(self, tmp_app_dir: Path) -> None:
        # TlsError 与端口占用同路：start() 捕获 → False，不裸抛
        cp = tmp_app_dir / "bad.crt"
        cp.write_bytes(b"garbage")
        kp = tmp_app_dir / "bad.key"
        kp.write_bytes(b"garbage")
        s = Settings(tls_cert_path=str(cp), tls_key_path=str(kp))
        svc = make_svc(tmp_app_dir, settings=s)
        assert await svc.start() is False
        assert not svc.is_running


class TestControlDispatch:
    async def test_heartbeat_ping_gets_pong(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        ch = make_control()
        await svc._dispatch_control(ch, Heartbeat(kind=HeartbeatKind.PING, timestamp=7.0))
        pong = decode_frame(bytes(ch.stream._writer.sent), hmac_key=KEY, require_hmac=True)
        assert isinstance(pong, Heartbeat) and pong.kind is HeartbeatKind.PONG

    async def test_ping_zero_pong_timestamp_preserved(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        ch = make_control()
        await svc._dispatch_control(ch, Heartbeat(kind=HeartbeatKind.PONG))  # 非 PING 不回复
        assert len(ch.stream._writer.sent) == 0

    @pytest.mark.parametrize(
        ("ev", "expect"),
        [
            (MouseEvent(x=1, y=2, action=MouseAction.MOVE), ("move", 1, 2)),
            (
                MouseEvent(x=3, y=4, action=MouseAction.PRESS, button=MouseButton.RIGHT),
                ("click", 3, 4, "right", True),
            ),
            (
                MouseEvent(x=5, y=6, action=MouseAction.RELEASE, button=MouseButton.MIDDLE),
                ("click", 5, 6, "middle", False),
            ),
            (
                MouseEvent(x=7, y=8, action=MouseAction.SCROLL, button=MouseButton.LEFT, scroll_delta=-2),
                ("scroll", 7, 8, 0, -2),
            ),
            (MouseEvent(x=9, y=10, action=MouseAction.DRAG), ("move", 9, 10)),
        ],
    )
    async def test_mouse_action_mapping(self, tmp_app_dir: Path, ev: MouseEvent, expect: tuple) -> None:
        backend = RecordingInputBackend()
        svc = make_svc(tmp_app_dir, backend=backend)
        await svc._dispatch_control(make_control(), ev)
        assert backend.calls[0] == expect
        assert svc._audit.pending_total() == 1  # 成功注入计入控制审计聚合

    async def test_click_down_then_up(self, tmp_app_dir: Path) -> None:
        backend = RecordingInputBackend()
        svc = make_svc(tmp_app_dir, backend=backend)
        await svc._dispatch_control(
            make_control(),
            MouseEvent(x=1, y=1, action=MouseAction.CLICK, button=MouseButton.LEFT),
        )
        assert backend.calls == [
            ("click", 1, 1, "left", True),
            ("click", 1, 1, "left", False),
        ]

    async def test_mouse_unsupported_action_logged(self, tmp_app_dir: Path) -> None:
        cap = CapturingLogger()
        backend = RecordingInputBackend()
        svc = make_svc(tmp_app_dir, backend=backend, cap=cap)
        await svc._dispatch_control(
            make_control(), MouseEvent(x=0, y=0, action=MouseAction.UNSPECIFIED)
        )
        assert backend.calls == []
        # 审计只记录支持的动作；UNSPECIFIED 不进 control 聚合
        assert svc._audit.pending_total() == 0

    async def test_mouse_inject_error_does_not_audit(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=BoomBackend())
        await svc._dispatch_control(
            make_control(), MouseEvent(x=1, y=1, action=MouseAction.MOVE)
        )
        assert svc._audit.pending_total() == 0  # 注入失败不上报控制事件

    async def test_key_type_and_modifiers(self, tmp_app_dir: Path) -> None:
        backend = RecordingInputBackend()
        svc = make_svc(tmp_app_dir, backend=backend)
        ch = make_control()
        await svc._dispatch_control(ch, KeyEvent(action=KeyAction.TYPE, text="中文"))
        await svc._dispatch_control(
            ch, KeyEvent(action=KeyAction.PRESS, key="s", modifiers=(ModifierKey.CTRL,))
        )
        await svc._dispatch_control(
            ch, KeyEvent(action=KeyAction.RELEASE, key="s", modifiers=(ModifierKey.CTRL, ModifierKey.SHIFT))
        )
        assert backend.calls == [
            ("text", "中文"),
            ("press", "ctrl"), ("press", "s"),
            ("release", "s"), ("release", "shift"), ("release", "ctrl"),
        ]

    async def test_key_unsupported_and_error(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=BoomBackend())
        ch = make_control()
        await svc._dispatch_control(ch, KeyEvent(action=KeyAction.UNSPECIFIED))
        await svc._dispatch_control(ch, KeyEvent(action=KeyAction.PRESS, key="x"))
        assert svc._audit.pending_total() == 0

    async def test_unexpected_message_type(
        self, tmp_app_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        frame = ScreenFrame(seq=1, timestamp=0.0, width=8, height=8, codec=Codec.H264, data=b"x")
        await svc._dispatch_control(make_control(), frame)
        # 服务内部日志（非审计事件）：structlog → stdlib handler 可被 caplog 捕获
        assert "control_unexpected_message" in caplog.text


class FakeFramerate:
    def __init__(self) -> None:
        self.fps = 30

    def set_fps(self, fps: int) -> None:
        self.fps = fps

    def get_target_fps(self) -> float:
        return float(self.fps)


class FakePipeline:
    """轻量假管线：零 PyAV/零 executor（真 ScreenPipeline 归 pipeline 单测与
    integration 测试）。离线测试与 UI 测试同进程连跑时，真管线的 x264 原生
    线程会与 Qt 事件泵相互卡死（阶段 6 坑 t11 同族，阶段 9 实测定位）。"""

    def __init__(self) -> None:
        self.framerate = FakeFramerate()
        self.closed = False
        self.keyframe_requests = 0

    @property
    def encoder(self) -> None:
        return None

    def request_keyframe(self) -> None:
        self.keyframe_requests += 1

    async def run(self, *, stop: asyncio.Event) -> None:
        await stop.wait()

    def close(self) -> None:
        self.closed = True


class TestStreamControl:
    async def test_start_then_stop_gate_pipeline(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        svc._stop_event = asyncio.Event()
        pipeline = FakePipeline()
        svc._pipeline = pipeline
        ch = make_control()
        await svc._dispatch_control(ch, StreamControl(cmd=StreamCmd.START))
        assert svc._pipeline_task is not None and not svc._pipeline_task.done()
        await asyncio.sleep(0.05)
        await svc._dispatch_control(ch, StreamControl(cmd=StreamCmd.STOP))
        await asyncio.wait_for(svc._pipeline_task, timeout=5)
        assert svc._pipeline_task.done()
        await svc._teardown_runtime()
        assert pipeline.closed is True and svc._pipeline is None

    async def test_config_applies_fps_and_quality_hint(
        self, tmp_app_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        svc._stop_event = asyncio.Event()
        svc._pipeline = FakePipeline()
        svc._pipeline_task = asyncio.get_running_loop().create_task(asyncio.sleep(30))
        ch = make_control()
        await svc._dispatch_control(
            ch,
            StreamControl(cmd=StreamCmd.CONFIG, config=StreamConfig(max_fps=12, quality=55)),
        )
        assert svc._pipeline.framerate.get_target_fps() == 12
        assert "stream_quality_hint_ignored_rater" in caplog.text
        svc._pipeline_task.cancel()
        await svc._teardown_runtime()

    async def test_stop_without_event_noop(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        await svc._dispatch_control(make_control(), StreamControl(cmd=StreamCmd.STOP))
        # CONFIG 携带但管线未建：分支跳过不抛
        await svc._dispatch_control(
            make_control(), StreamControl(cmd=StreamCmd.CONFIG, config=StreamConfig(max_fps=10))
        )


class TestClipboard:
    async def test_single_chunk_written(self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr("rdc.server.service._clipboard_copy", seen.append)
        cap = CapturingLogger()
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend(), cap=cap)
        await svc._dispatch_control(
            make_control(), ClipboardEvent(text="hello", chunk_total=1)
        )
        assert seen == ["hello"]

    async def test_chunk_index_nonzero_skipped(self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr("rdc.server.service._clipboard_copy", seen.append)
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        await svc._dispatch_control(
            make_control(), ClipboardEvent(text="tail", chunk_index=1, chunk_total=3)
        )
        assert seen == []

    async def test_chunk_first_index_written(self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr("rdc.server.service._clipboard_copy", seen.append)
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        await svc._dispatch_control(
            make_control(), ClipboardEvent(text="head", chunk_index=0, chunk_total=3)
        )
        assert seen == ["head"]

    async def test_write_failure_logged_not_audited(
        self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_text: str) -> None:
            raise RuntimeError("剪贴板不可用")

        monkeypatch.setattr("rdc.server.service._clipboard_copy", boom)
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        await svc._dispatch_control(make_control(), ClipboardEvent(text="x"))
        assert svc._audit.pending_total() == 0


class TestLazyBackendAndReset:
    async def test_default_backend_constructed_once(
        self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = RecordingInputBackend()
        created: list[int] = []

        def factory() -> RecordingInputBackend:
            created.append(1)
            return rec

        monkeypatch.setattr("rdc.server.service.default_backend", factory)
        svc = make_svc(tmp_app_dir, backend=None)
        await svc._dispatch_control(
            make_control(), MouseEvent(x=1, y=1, action=MouseAction.MOVE)
        )
        await svc._dispatch_control(
            make_control(), MouseEvent(x=2, y=2, action=MouseAction.MOVE)
        )
        assert created == [1]  # 懒构造仅一次
        assert rec.calls == [("move", 1, 1), ("move", 2, 2)]

    def test_reset_swallows_backend_error(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=BoomBackend())
        svc._reset_input()  # 不抛（内部记录）


class TestServeScreen:
    async def test_inbound_ignored_then_eof_unregisters(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        client_stream, session_stream, cw, _ = make_stream_pair()
        session_stream.enable_hmac(KEY)
        session = ScreenSession(session_stream, "8.8.8.8", KEY)
        session.start_receiving()
        # 画面通道收到入站帧：仅 debug 忽略，不分派业务
        cw.sent.extend(encode_frame(Heartbeat(kind=HeartbeatKind.PING, timestamp=1.0), hmac_key=KEY))
        pump(cw, session_stream)
        task = asyncio.get_running_loop().create_task(svc._serve_screen(session))
        for _ in range(100):
            if svc._registry.count() == 1:
                break
            await asyncio.sleep(0.01)
        assert svc._registry.count() == 1
        await session.close()
        await asyncio.wait_for(task, timeout=5)
        assert svc._registry.count() == 0
        await svc._registry.await_close_tasks()


class TestBanCleanupLoop:
    async def test_returns_immediately_when_stopped(self, tmp_app_dir: Path) -> None:
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        svc._stop_event = asyncio.Event()
        svc._stop_event.set()
        await asyncio.wait_for(svc._ban_cleanup_loop(), timeout=1)

    async def test_periodic_cleanup_invoked(
        self, tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("rdc.server.service.BAN_CLEANUP_INTERVAL_S", 0.02)
        svc = make_svc(tmp_app_dir, backend=RecordingInputBackend())
        svc._stop_event = asyncio.Event()
        calls: list[int] = []
        monkeypatch.setattr(
            svc._auth,
            "cleanup_expired_bans",
            lambda: calls.append(1) or 0,
        )

        async def stop_later() -> None:
            await asyncio.sleep(0.08)
            svc._stop_event.set()

        _, _ = await asyncio.gather(
            svc._ban_cleanup_loop(), asyncio.create_task(stop_later())
        )
        assert calls  # 至少触发过一次周期清理


class TestHelpers:
    def test_quality_to_crf_bounds_and_monotonic(self) -> None:
        assert _quality_to_crf(1) == 45  # 下界钳制
        assert _quality_to_crf(100) == 18  # 上界
        assert _quality_to_crf(0) == 45 and _quality_to_crf(999) == 18
        assert _quality_to_crf(60) == 29  # 生产默认附近（标定域 [18,45]）
        assert _quality_to_crf(30) > _quality_to_crf(60) > _quality_to_crf(90)

    def test_now_utc_aware(self) -> None:
        assert _now_utc().tzinfo is not None

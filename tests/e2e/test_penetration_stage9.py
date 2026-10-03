"""批 5 判据②：渗透清单四项实测（S1 重放 / S2 MITM / S3 弱口令爆破 / S4 路径穿越）。

与既有测试的关系（STAGE9_PLAN B10 + "复用阶段 4 三层测试，不重写"）：

- **S1 重放**——阶段 4 已交付三层防护并各自锁定测试，本文件**不重写**，
  只补运行时"抓帧-重投"实测（既有测试未覆盖的帧字节层面）：
  ① 令牌层 `tests/unit/test_transport_session_token.py`（单次使用）；
  ② 加入层 `tests/integration/test_transport_integration.py::test_token_replay_second_join_fails`
     与 `test_host_service_integration.py::test_replay_join_rejected`；
  ③ 密钥层 `tests/unit/test_transport_handshake.py`（每连接一次 nonce、双通道密钥隔离）。
- **S2 MITM**——`tests/unit/test_transport_tls.py` 已锁 TOFU 纯函数判定；
  本文件补**真实 TLS 双证书**的中间人接入实测（换证 → 必触发确认 → 拒绝即断；
  已信任证 → 免确认直通）。
- **S3 弱口令爆破**——补 password_policy + Banlist + RateLimiter + 审计四方联合
  实测（策略拒设弱口令 → 字典连打触封禁 → 正确口令也被准入拒绝 → 到期自愈）。
- **S4 路径穿越**——四类"外部输入→本地路径"写入点逐个实测（见 TestPathTraversal
  攻击面排查表）。

⚠️ 文件传输面（阶段 8 未实现）无真实落地面：以显式 skip 声明"待阶段 8 补测"。
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import numpy as np
import pytest

from rdc.capture.source import ArrayScreenSource
from rdc.client.ratelimit import RateLimiter
from rdc.config import paths as rdc_paths
from rdc.config.settings import Settings
from rdc.exceptions import AuthError, TlsError
from rdc.input.base import MouseButton as InputButton
from rdc.input.base import ScreenRect
from rdc.protocol.messages import (
    KeyAction,
    KeyEvent,
    MouseAction,
    MouseButton,
    MouseEvent,
)
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod
from rdc.security.certs import require_within_data_root
from rdc.security.password_policy import PasswordIssue, check_password
from rdc.server.audit import AuditLogger
from rdc.server.auth import HostAuth
from rdc.server.banlist import Banlist
from rdc.server.service import HostService
from rdc.transport.control_channel import ControlChannel
from rdc.transport.hmac_nonce import derive_hmac_key, new_connection_id, new_nonce
from tests._transport_support import make_stream_pair

pytestmark = pytest.mark.security

PW = "strong-pass-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_app_dir / "known_fp.json")
    monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _confirm(_fp: str, _host: str) -> bool:
    return True


# =========================================================================
# S1 重放（帧字节级"抓帧-重投"运行时实测；三层既有测试不重写）
# =========================================================================


class TestReplay:
    """帧层无序号：会话内重投会被再次受理（威胁如实记录），
    跨会话/篡改帧则被会话绑定密钥挡死。业务影响评估见 STAGE9 报告 S1 章。
    """

    async def test_in_session_frame_replay_is_accepted_again(self) -> None:
        """同一会话重投已抓到的合法帧 → 服务端再次解出同一事件（无 seq 防线）。

        渗透结论：帧 HMAC 覆盖头部+载荷但**不含序号**，同会话原样重放不可检测。
        当前业务面（鼠标/按键/画面）重放后果为幂等噪声级；若阶段 10 引入
        文件传输/指令序列，须在该面补 seq/时间窗（报告跟进项）。
        """
        key = derive_hmac_key(new_nonce(), new_nonce(), new_connection_id())
        client, server, cw, _ = make_stream_pair()
        client.enable_hmac(key)
        server.enable_hmac(key)
        await client.send_message(
            MouseEvent(x=11, y=22, action=MouseAction.CLICK, button=MouseButton.LEFT)
        )
        wire = bytes(cw.sent)
        cw.sent.clear()

        server._reader.feed_data(wire)  # noqa: SLF001
        first = await asyncio.wait_for(server.recv_message(), timeout=2)
        assert isinstance(first, MouseEvent) and first.x == 11

        server._reader.feed_data(wire)  # 原样重投
        again = await asyncio.wait_for(server.recv_message(), timeout=2)
        assert isinstance(again, MouseEvent) and again.x == 11  # 重放被受理（如实记录）

    async def test_cross_session_frame_replay_dropped(self) -> None:
        """会话 A 的合法帧重投到会话 B（不同 nonce/连接）→ HMAC 验签失败被丢，
        且会话 B 后续合法帧不受影响（不断流）。③ 密钥层的运行时体现。"""
        key_a = derive_hmac_key(new_nonce(), new_nonce(), new_connection_id())
        key_b = derive_hmac_key(new_nonce(), new_nonce(), new_connection_id())
        a_cli, _a_srv, cw_a, _ = make_stream_pair()
        a_cli.enable_hmac(key_a)
        await a_cli.send_message(MouseEvent(x=1, y=2, action=MouseAction.MOVE))
        stolen = bytes(cw_a.sent)  # 攻击者抓到的帧字节

        b_cli, b_srv, cw_b, _ = make_stream_pair()
        b_cli.enable_hmac(key_b)
        b_srv.enable_hmac(key_b)
        b_srv._reader.feed_data(stolen)  # noqa: SLF001 - 投毒
        await b_cli.send_message(KeyEvent(action=KeyAction.PRESS, key="ok"))
        b_srv._reader.feed_data(bytes(cw_b.sent))  # noqa: SLF001

        msg = await asyncio.wait_for(b_srv.recv_message(), timeout=2)
        assert isinstance(msg, KeyEvent) and msg.key == "ok"  # stolen 帧验签失败被跳过

    async def test_tampered_frame_dropped_stream_survives(self) -> None:
        """翻转载荷一字节再投 → 验签失败丢帧；紧随的合法帧照常解出。

        篡改不可达业务层 + 单帧坏不断流（transport/base 错误契约）的运行时验证；
        离线字节级篡改由 ``test_protocol_framing_v5`` 锁定，此处不重复。
        """
        key = derive_hmac_key(new_nonce(), new_nonce(), new_connection_id())
        client, server, cw, _ = make_stream_pair()
        client.enable_hmac(key)
        server.enable_hmac(key)
        await client.send_message(MouseEvent(x=5, y=6, action=MouseAction.MOVE))
        wire = bytearray(bytes(cw.sent))
        cw.sent.clear()
        mid = len(wire) - 10  # 动载荷/HMAC 区，不动 15B 头部长度字段
        wire[mid] ^= 0xFF

        server._reader.feed_data(bytes(wire))  # noqa: SLF001
        await client.send_message(KeyEvent(action=KeyAction.PRESS, key="after"))
        server._reader.feed_data(bytes(cw.sent))  # noqa: SLF001

        msg = await asyncio.wait_for(server.recv_message(), timeout=2)
        assert isinstance(msg, KeyEvent) and msg.key == "after"


# =========================================================================
# S2 MITM（TLS 指纹校验绕过尝试 —— 真实双证书接入实测）
# =========================================================================


class TestMitm:
    async def test_cert_swap_detected_decline_blocks_connection(self, tmp_app_dir: Path) -> None:
        """预信任证书 A，服务端换成证书 B（MITM 替换）→ 指纹不符必触发确认；
        用户拒绝 → TlsError，且 B **不会**被静默写入信任列表。"""
        from tests._transport_support import gen_cert

        (tmp_app_dir / "a").mkdir()
        (tmp_app_dir / "b").mkdir()
        cert_a, key_a = gen_cert(tmp_app_dir / "a")
        cert_b, key_b = gen_cert(tmp_app_dir / "b")
        fp_b = _cert_fingerprint(cert_b)

        ctrl_port = _free_port()
        settings = _host_settings(
            tmp_app_dir,
            tls_cert_path=str(cert_b),
            tls_key_path=str(key_b),
            control_port=ctrl_port,
        )
        svc = HostService(
            settings=settings, source=_source(), input_backend=RecordingInput(), show_tray=False
        )
        assert await svc.start()
        try:
            fp_mod.get_fingerprint_manager().trust(_cert_fingerprint(cert_a), "127.0.0.1")
            asked: list[str] = []

            def decline(fp: str, _h: str) -> bool:
                asked.append(fp)
                return False

            ch = ControlChannel(
                "127.0.0.1",
                ctrl_port,
                password_provider=lambda: PW,
                fingerprint_confirm=decline,
            )
            with pytest.raises(TlsError):
                await asyncio.wait_for(ch.connect(), timeout=15)
            assert asked == [fp_b]  # 换证被检出、必经用户确认
            assert not fp_mod.get_fingerprint_manager().is_trusted(fp_b)  # 拒绝未落信任
        finally:
            await svc.shutdown()

    async def test_trusted_cert_auto_passes_no_prompt(self, tmp_app_dir: Path) -> None:
        """已信任指纹直连免确认（TOFU 正常语义，证明 S2 检出不是误杀）。"""
        from tests._transport_support import gen_cert

        (tmp_app_dir / "a").mkdir()
        cert_a, key_a = gen_cert(tmp_app_dir / "a")
        ctrl_port = _free_port()
        settings = _host_settings(
            tmp_app_dir,
            tls_cert_path=str(cert_a),
            tls_key_path=str(key_a),
            control_port=ctrl_port,
        )
        svc = HostService(
            settings=settings, source=_source(), input_backend=RecordingInput(), show_tray=False
        )
        assert await svc.start()
        try:
            fp_mod.get_fingerprint_manager().trust(_cert_fingerprint(cert_a), "127.0.0.1")
            prompted: list[str] = []

            def no_prompt(fp: str, _h: str) -> bool:
                prompted.append(fp)
                return True

            ch = ControlChannel(
                "127.0.0.1",
                ctrl_port,
                password_provider=lambda: PW,
                fingerprint_confirm=no_prompt,
            )
            await asyncio.wait_for(ch.connect(), timeout=15)
            assert prompted == []  # 已信任 → 直通，不再打扰
            await ch.close()
        finally:
            await svc.shutdown()


# =========================================================================
# S3 弱口令爆破（password_policy + banlist 联合验证）
# =========================================================================


class TestWeakPasswordBruteforce:
    def test_policy_blocks_common_weak_candidates(self) -> None:
        """爆破字典高频弱口令全部无法过策略（源头：根本设不进系统）。"""
        weak = ["1", "admin", "password", "88888888", "abcdefgh", "12345678", "qwertyuiop"]
        for pw in weak:
            issues = check_password(pw)
            assert issues, f"策略漏放弱口令: {pw}"
            # 弱口令至少命中一类违规
            assert set(issues) <= {
                PasswordIssue.TOO_SHORT,
                PasswordIssue.NO_LETTER,
                PasswordIssue.NO_DIGIT,
            }
        assert check_password("strong-pass-1") == []

    async def test_dictionary_attack_hits_ban_then_correct_pw_rejected(
        self, tmp_app_dir: Path
    ) -> None:
        """真实 TLS 字典连打 5 个弱口令变体：第 3 次失败即封禁；
        封禁后**正确口令也被准入拒绝**（爆破吞吐被掐死）。"""
        settings = _host_settings(tmp_app_dir, max_auth_attempts=3)
        svc, _ = _make_service(tmp_app_dir, settings)
        try:
            assert await svc.start()
            dictionary = ["aaaabbbb1", "bbbbcccc2", "ddddcccc3", "passw0rd9", "admin1234"]
            failures = 0
            for guess in dictionary:
                ch = _control(settings, guess)
                try:
                    await asyncio.wait_for(ch.connect(), timeout=10)
                except (AuthError, OSError, TimeoutError):
                    failures += 1
                await ch.close()
            assert failures == 5  # 含封禁后拒绝，全部未得手
            bans = svc._auth.banlist  # noqa: SLF001
            assert bans.banned_count == 1 and bans.is_banned("127.0.0.1")
            # 正确口令在封禁期同样被拒（准入优先）
            ch_ok = _control(settings, PW)
            with pytest.raises((AuthError, OSError, TimeoutError)):
                await asyncio.wait_for(ch_ok.connect(), timeout=6)
        finally:
            await svc.shutdown()

    async def test_ban_expires_self_heals(self, tmp_app_dir: Path) -> None:
        """封禁到期自动解封（假时钟零等待）：防"封禁机制反被用来 DoS 自己"。"""
        now = [1_000.0]
        bans = Banlist(
            path=tmp_app_dir / "ban.json",
            persist=False,
            max_attempts=2,
            ban_duration=60.0,
            clock=lambda: now[0],
        )
        audit = RecordingAudit()
        settings = _host_settings(tmp_app_dir, max_auth_attempts=2, auth_ban_duration=60)
        auth = HostAuth(
            settings,
            banlist=bans,
            audit=audit,
            rate_limiter=RateLimiter(max_connections_per_second=50),
        )
        svc = HostService(
            settings=settings,
            source=_source(),
            input_backend=RecordingInput(),
            auth=auth,
            audit=audit,
            show_tray=False,
        )
        try:
            assert await svc.start()
            for _ in range(2):
                ch = _control(settings, "wrong-guess")
                with pytest.raises(AuthError):
                    await asyncio.wait_for(ch.connect(), timeout=10)
                await ch.close()
            assert bans.banned_count == 1
            now[0] += 61.0  # 跨过封禁窗口
            assert bans.is_banned("127.0.0.1") is False
            ch_ok = _control(settings, PW)
            await asyncio.wait_for(ch_ok.connect(), timeout=10)
            await ch_ok.close()
        finally:
            await svc.shutdown()

    def test_rate_limiter_caps_handshake_throughput(self) -> None:
        """每秒连接限流在握手**前**生效：爆破脚本的并发吞吐天花板（与封禁互补）。"""
        audit = RecordingAudit()
        auth = HostAuth(
            _host_settings(Path(".")),
            banlist=Banlist(persist=False, clock=lambda: 0.0),
            audit=audit,
            rate_limiter=RateLimiter(max_connections_per_second=3),
        )
        verdicts = [auth.check_client("1.2.3.4") for _ in range(5)]
        assert verdicts == [True, True, True, False, False]
        assert audit.rejected_reasons.count("rate_limited") == 2

    def test_bcrypt_cost_line(self) -> None:
        """rounds=12 bcrypt 校验成本 ≥50ms/次（单连接爆破 ~20 次/分/线程，
        配合窗口封禁在小时级即触顶）——策略参数有效性锚点。"""
        import time

        stored = hash_password("strong-pass-1")
        t0 = time.perf_counter()
        ok = _verify_pw("wrong-pass-1", stored)
        cost = time.perf_counter() - t0
        assert ok is False
        assert cost >= 0.05, f"bcrypt rounds=12 成本异常低: {cost:.3f}s"

    def test_audit_trail_covers_ban_lifecycle(self) -> None:
        """爆破全过程审计事件齐全：failure×N → banned → rejected(banned)。"""
        audit = RecordingAudit()
        bans = Banlist(persist=False, max_attempts=2, clock=lambda: 5.0)
        auth = HostAuth(_host_settings(Path(".")), banlist=bans, audit=audit)
        for _ in range(2):
            auth.on_auth_failure("9.9.9.9")
        assert auth.check_client("9.9.9.9") is False
        names = [e["event"] for e in audit.entries]
        assert names.count("auth.failure") == 2
        assert "auth.banned" in names
        assert "conn.rejected" in names
        assert audit.rejected_reasons == ["banned"]


# =========================================================================
# S4 路径穿越（四类"外部输入 → 本地路径"写入点实测）
# =========================================================================


class TestPathTraversal:
    """攻击面排查结论（详见 STAGE9 报告 S4 章）：

    ============  ==============================  ==================================
    写入点        路径来源                        外部输入面与处置
    ============  ==============================  ==================================
    banlist.json  ``paths.banlist_file()``        数据根派生，无 ``..`` 注入面；
                                                  "IP" 只作 JSON 键绝不拼路径
    审计日志      ``configure_logging(log_file)`` 数据根派生（logs/rdc.log）
    证书          ``Settings.tls_cert_path/key``  **有**（config.json 任意路径）→
                                                  批 5 已加 ``require_within_data_root``
                                                  （resolve 规范化 + 根内前缀 + 拒符号链接）
    配置写回      CLI ``--config`` / save         用户显式给定（设计如此），原子替换
    文件传输      —                              阶段 8 未实现 → **待阶段 8 补测**
    ============  ==============================  ==================================
    """

    def test_all_derived_write_points_under_data_root(self, tmp_app_dir: Path) -> None:
        """派生写入点（banlist/日志/证书/配置/密钥存储）全部落在数据根内。"""
        root = tmp_app_dir.resolve()
        for path in (
            rdc_paths.banlist_file(),
            rdc_paths.log_dir() / "rdc.log",
            rdc_paths.cert_dir() / "cert.pem",
            rdc_paths.config_file(),
            rdc_paths.secret_store_file("dpapi"),
        ):
            assert root in path.resolve().parents, f"写入点越出数据根: {path}"

    def test_evil_ip_keys_never_materialize_files(self, tmp_app_dir: Path) -> None:
        """恶意"IP"串（含 ../ \\ / nul）只进 JSON 键，不创建任何路径副作用。"""
        evil = ["../evil", "..\\evil", "/", "\\", "a/b/c", "a\\b", "../../windows/system32"]
        bans = Banlist(path=tmp_app_dir / "banlist.json", max_attempts=1)
        for ip in evil:
            bans.record_failure(ip)
        bans.save()
        data = json.loads((tmp_app_dir / "banlist.json").read_text(encoding="utf-8"))
        assert set(data["ban_timestamps"]) == set(evil)  # 全部只作字典键存活
        assert (tmp_app_dir / "evil").exists() is False
        assert (tmp_app_dir.parent / "evil").exists() is False
        assert list(tmp_app_dir.glob("a*")) == []
        assert list(tmp_app_dir.parent.glob("evil*")) == []

    def test_banlist_file_poisoned_values_dropped(self, tmp_app_dir: Path) -> None:
        """banlist.json 被外部改写后 load：非字符串键/畸形值一律丢弃，
        杜绝"文件值 → 路径/类型混淆"通道。"""
        path = tmp_app_dir / "banlist.json"
        path.write_text(
            json.dumps(
                {
                    "ban_timestamps": {
                        "../../evil": "not-a-number",
                        "12345": None,
                        "1.2.3.4": 9e18,
                    },
                    "failure_timestamps": {"x": ["a", None, {"k": 1}], "y": [1.0, 2.0]},
                }
            ),
            encoding="utf-8",
        )
        bans = Banlist(path=path, ban_duration=1e12, clock=lambda: 9e18)
        bans.load()
        snap = bans.snapshot()
        bans_ts = snap["ban_timestamps"]
        fails_ts = snap["failure_timestamps"]
        assert isinstance(bans_ts, dict) and isinstance(fails_ts, dict)
        assert list(bans_ts) == ["1.2.3.4"]
        assert list(fails_ts) == ["y"]

    def test_cert_path_dotdot_escape_rejected(self, tmp_app_dir: Path) -> None:
        """``..`` 逃逸：规范化后越出数据根 → TlsError（读取副作用不发生）。"""
        secret = tmp_app_dir.parent / "outside_secret.key"
        secret.write_bytes(b"TOP-SECRET")
        with pytest.raises(TlsError, match="越|穿越"):
            require_within_data_root(
                tmp_app_dir / "certs" / ".." / ".." / "outside_secret.key", label="私钥"
            )

    def test_cert_path_absolute_outside_rejected(self, tmp_app_dir: Path) -> None:
        secret = tmp_app_dir.parent / "outside2.key"
        secret.write_bytes(b"TOP-SECRET-2")
        with pytest.raises(TlsError):
            require_within_data_root(secret, label="私钥")

    def test_cert_path_inside_root_with_dotdot_ok(self, tmp_app_dir: Path) -> None:
        """``根/certs/../x.crt`` 规范化后仍在根内 → 放行（不误伤正常路径）。"""
        inside = tmp_app_dir / "certs" / ".." / "x.crt"
        assert require_within_data_root(inside, label="证书") == (tmp_app_dir / "x.crt").resolve()

    def test_cert_symlink_escape_rejected(self, tmp_app_dir: Path) -> None:
        """符号链接逃逸（resolve 展开真实目标后判定）→ 拒。"""
        target = tmp_app_dir.parent / "real_secret.key"
        target.write_bytes(b"SECRET")
        link = tmp_app_dir / "certs" / "link.key"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("本机权限不允许创建符号链接（开发者模式未开）")
        with pytest.raises(TlsError):
            require_within_data_root(link, label="私钥")

    def test_service_rejects_escape_and_never_reads(self, tmp_app_dir: Path) -> None:
        """端到端：config 写越界证书路径 → _prepare_certificates 抛 TlsError，
        越界文件未被读取解析（错误串不含其内容）。"""
        outside_key = tmp_app_dir.parent / "outside.key"
        outside_key.write_bytes(b"SECRET-KEY-MATERIAL")
        cert = tmp_app_dir / "certs" / "x.crt"
        cert.parent.mkdir(parents=True, exist_ok=True)
        cert.write_bytes(b"FAKE-PEM")
        settings = _host_settings(
            tmp_app_dir, tls_cert_path=str(cert), tls_key_path=str(outside_key)
        )
        svc = HostService(settings=settings, source=_source(), show_tray=False)
        with pytest.raises(TlsError) as exc_info:
            svc._prepare_certificates()  # noqa: SLF001
        assert "SECRET" not in str(exc_info.value)
        assert svc._cert_bundle is None  # noqa: SLF001

    def test_config_save_atomic_and_scoped(self, tmp_app_dir: Path) -> None:
        """配置写入：显式给定路径（便携模式设计如此），原子替换无 .tmp 残留。"""
        from rdc.config.settings import save_settings

        cfg = tmp_app_dir / "portable" / "config.json"
        settings = _host_settings(tmp_app_dir)
        save_settings(settings, cfg)
        assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == settings.port
        assert list(cfg.parent.glob("*.tmp")) == []

    def test_file_transfer_surface_pending_stage8(self) -> None:
        """文件传输面（阶段 8 未实现）：显式 skip——待补测，不算通过。"""
        pytest.skip("文件传输面渗透测试待阶段 8 实现后补测（STAGE9 报告已声明）")


# =========================================================================
# 支撑件
# =========================================================================


class RecordingInput:
    """输入替身：只记录，不动真机。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

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

    def bounds(self) -> ScreenRect:
        raise AssertionError("测试不触 bounds")

    def reset(self) -> None:
        self.calls.append(("reset",))

    def supported(self) -> bool:
        return True


class RecordingAudit(AuditLogger):
    """审计替身：事件连同字段记进内存（渗透取证口径）。"""

    def __init__(self) -> None:
        super().__init__(logger=self, verbose_control=True)
        self.entries: list[dict[str, object]] = []

    def info(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})

    def warning(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})

    def error(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})

    def debug(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})

    @property
    def rejected_reasons(self) -> list[str]:
        return [str(e.get("reason", "")) for e in self.entries if e["event"] == "conn.rejected"]


def _verify_pw(plain: str, stored: str) -> bool:
    from rdc.secrets.password import verify_password

    return verify_password(plain, stored)


def _source() -> ArrayScreenSource:
    a = np.zeros((16, 16, 4), dtype=np.uint8)
    b = np.full((16, 16, 4), 90, dtype=np.uint8)
    return ArrayScreenSource([a, b], loop=True)


def _host_settings(_tmp: Path, **kw: object) -> Settings:
    base: dict[str, object] = {
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


def _make_service(tmp_app_dir: Path, settings: Settings) -> tuple[HostService, RecordingInput]:
    backend = RecordingInput()
    bans = Banlist(path=tmp_app_dir / "ban.json", persist=False)
    audit = RecordingAudit()
    auth = HostAuth(
        settings,
        banlist=bans,
        audit=audit,
        rate_limiter=RateLimiter(max_connections_per_second=50),
    )
    svc = HostService(
        settings=settings,
        source=_source(),
        input_backend=backend,
        auth=auth,
        audit=audit,
        show_tray=False,
    )
    return svc, backend


def _control(settings: Settings, password: str) -> ControlChannel:
    return ControlChannel(
        settings.bind_host,
        settings.control_port,
        password_provider=lambda: password,
        fingerprint_confirm=_confirm,
    )


def _cert_fingerprint(cert_path: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return fp_mod.compute_fingerprint(cert.public_bytes(Encoding.DER))

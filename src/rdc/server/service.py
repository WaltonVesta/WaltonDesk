"""被控端服务（阶段 9 批 3 重写，D14）：asyncio 装配与生命周期编排。

v4 ``RemoteService``（1,213 行线程模型，原 ``server/service.py``，批 3 已拆除删除、
git 历史可恢复）→ v5 ``HostService`` 按"瘦身为生命周期编排"的原计划拆分落地：

::

    HostService（本文件）
      ├─ TransportServer        (transport/server.py)  TLS 双端口监听/握手
      │    ├─ check_client      → server/auth.HostAuth.check_client   ┐ 批3
      │    └─ on_auth_failed    → HostAuth.on_auth_failure            ┘ 扩展回调
      ├─ HostAuth               (server/auth.py)  准入/封禁/限流/审计接线
      ├─ SubscriptionRegistry   (server/broadcaster.py) 画面订阅/回收/关键帧
      │    └─ ScreenBroadcaster (transport/broadcaster.py, B3 修复件)
      ├─ ScreenPipeline         (server/pipeline.py) 采集→差分→编码→publish
      ├─ InputBackend           (input/base.default_backend) 输入注入执行
      └─ AuditLogger / Banlist  (server/audit.py / server/banlist.py)

与 v4 的行为差异（均记录于 STAGE9_REPORT）：
- accept 线程 + ThreadPoolExecutor + 每客户端线程 → 单事件循环协程；
- 裸 4B 握手 / JSON 载荷 → 15B 分帧 + Protobuf + HMAC（阶段 3/4 栈）；
- ``welcome`` 帧不再发送（``AuthResponse`` 即欢迎，批 4 主控端按 v5 协议实现）；
- 剪贴板 ``tk.Tk()`` 工作线程（B2）→ ``pyperclip`` + ``asyncio.to_thread``（零 Qt）；
- Semaphore 计数对象从"客户端"变为"会话"（控制+画面各 1）：``max_connections=10``
  ≈ 5 个客户端（个人自用可接受）；
- 口令/开关热更新需 :meth:`restart`（v4 同样经重启路径；transport 握手读
  构造期哈希）。

线程红线（阶段 6 固化）：``broadcaster.publish`` 与输入注入调用固定事件循环
线程；采集/差分/编码在 pipeline 自有单 worker executor。UI 触达一律经信号，
本模块零 Qt 导入（被控端核心可无 GUI 运行，CLI ``rdc serve``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import time
from pathlib import Path
from typing import Any

from rdc.capture.desktop import is_secure_desktop
from rdc.capture.framerate import FramerateController
from rdc.capture.mss_source import MssScreenSource, virtual_desktop_bounds
from rdc.capture.source import ScreenSource
from rdc.config.paths import cert_dir, ensure_dirs
from rdc.config.settings import Settings, current_settings
from rdc.encoding.raters import AdaptiveRater, RaterSignals
from rdc.exceptions import TlsError
from rdc.input.base import InputBackend, default_backend
from rdc.input.base import MouseButton as InputMouseButton
from rdc.logging import get_logger
from rdc.protocol.messages import (
    ClipboardEvent,
    Heartbeat,
    HeartbeatKind,
    HostStatus,
    HostStatusCode,
    KeyAction,
    KeyEvent,
    Message,
    ModifierKey,
    MouseAction,
    MouseButton,
    MouseEvent,
    StreamCmd,
    StreamControl,
)
from rdc.security.audit_events import EVENTS
from rdc.security.certs import (
    ExpiryStatus,
    UserCertBundle,
    check_expiry,
    load_user_cert_bundle,
    require_within_data_root,
)
from rdc.server.audit import AuditLogger
from rdc.server.auth import REASON_BAD_PASSWORD, HostAuth
from rdc.server.broadcaster import SubscriptionRegistry
from rdc.server.pid_manager import PIDManager
from rdc.server.pipeline import ScreenPipeline
from rdc.server.stats import HostSnapshot
from rdc.transport.base import StreamChannel
from rdc.transport.server import ControlSession, ScreenSession, TransportServer
from rdc.transport.session_token import SessionTokenStore
from rdc.transport.tls import ensure_server_cert

__all__ = ["HostService"]

_LOG = get_logger("server.service")

#: 协议鼠标键枚举 → input 层枚举。
_PROTO_BUTTON_TO_INPUT: dict[MouseButton, InputMouseButton] = {
    MouseButton.LEFT: InputMouseButton.LEFT,
    MouseButton.RIGHT: InputMouseButton.RIGHT,
    MouseButton.MIDDLE: InputMouseButton.MIDDLE,
}

#: 协议修饰键枚举 → input 规范键名（阶段 7 D31 值域）。
_MODIFIER_NAMES: dict[ModifierKey, str] = {
    ModifierKey.CTRL: "ctrl",
    ModifierKey.SHIFT: "shift",
    ModifierKey.ALT: "alt",
    ModifierKey.WIN: "win",
    ModifierKey.ALTGR: "altgr",
}

#: 到期封禁批清理周期（秒）。
BAN_CLEANUP_INTERVAL_S = 60.0

_SERVER_NAME = "RemoteControlService"


class HostService:
    """被控端服务：TLS 双端口 + 画面推流 + 输入执行 + 安全接线。

    Args:
        settings: 运行时配置（``None`` 取 :func:`current_settings` 单例）。
        source: 采集源注入（测试注入 ``ArrayScreenSource``；``None`` 生产用
            :class:`MssScreenSource`，构造延后到 :meth:`start`——避免
            import/实例化期触屏）。
        input_backend: 输入后端注入（测试注入替身；``None`` 首次需要时经
            :func:`default_backend` 平台工厂构造——Windows=SendInput，
            macOS/Linux 空实现按阶段 7 范围回退）。
        auth: 认证准入门注入（测试替身；``None`` 用 settings 构造 :class:`HostAuth`）。
        audit: 审计发射器注入（缺省新建；与 ``auth`` 共享同一实例时
            控制操作聚合可经服务节拍统一 flush）。
        show_tray: 是否由装配层启用托盘（本核心类不含 GUI：``True`` 仅记录
            意图，托盘接线归 ``rdc.ui`` 的 serve 装配入口，批 4）。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        source: ScreenSource | None = None,
        input_backend: InputBackend | None = None,
        auth: HostAuth | None = None,
        audit: AuditLogger | None = None,
        show_tray: bool = True,
    ) -> None:
        self._settings = settings if settings is not None else current_settings()
        self._source = source
        self._input_backend = input_backend
        self._audit = audit if audit is not None else AuditLogger()
        self._auth = auth if auth is not None else HostAuth(self._settings, audit=self._audit)
        self.show_tray = show_tray

        self.pid_manager = PIDManager(_SERVER_NAME)
        self._server: TransportServer | None = None
        self._tokens = SessionTokenStore()
        #: 画面订阅登记表（含其持有的 broadcaster）——**每个运行周期新建**
        #: （走查第十二批 Bug 14/15 主导根因）：旧实现本表只在构造期建一次，
        #: 而 pause/shutdown/restart 收尾必经 ``registry.shutdown()`` →
        #: broadcaster ``_stopped=True`` 永久置位；resume/restart 走 start()
        #: 复用同一张已停表 → 新画面订阅 ``register()`` 抛
        #: "ScreenBroadcaster 已停止" → 服务端画面会话建立即失败关闭 →
        #: 主控端只收到 screen EOF、画面永久卡在最后一帧（控制通道独立存活，
        #: 故"控制恢复但画面不恢复"）。
        self._registry: SubscriptionRegistry = self._new_registry()
        self._pipeline: ScreenPipeline | None = None
        self._stop_event: asyncio.Event | None = None
        self._pipeline_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._control_sessions: set[ControlSession] = set()
        self._status_tasks: set[asyncio.Task[None]] = set()
        #: 注入失败日志节流：event -> (上次记录时刻 monotonic, 被抑制条数)
        self._inject_log_state: dict[str, tuple[float, int]] = {}
        self._paused = False
        self._should_exit = False
        self._start_time = 0.0
        self._cert_bundle: UserCertBundle | None = None

    # ================================================================== 生命周期

    def _new_registry(self) -> SubscriptionRegistry:
        """构造全新的画面订阅登记表（含其持有的活跃 broadcaster）。"""
        return SubscriptionRegistry(
            on_new_subscriber=self._on_new_subscriber,
            on_removed=self._on_subscriber_removed,
        )

    async def start(self) -> bool:
        """启动服务（幂等；paused 态调用等同 :meth:`resume`）。

        Returns:
            ``True`` 监听成功；``False`` 端口占用等启动失败（可观测，已回滚）。
        """
        if self._should_exit:
            _LOG.warning("host_service_exit_requested_skip_start")
            return False
        if self._server is not None and self._server.listening:
            _LOG.info("host_service_already_running")
            return True

        self.pid_manager.cleanup_stale()
        s = self._settings
        try:
            cert_path, key_path = self._prepare_certificates()
            self._server = TransportServer(
                host=s.bind_host,
                control_port=s.control_port,
                screen_port=s.port,
                cert_path=cert_path,
                key_path=key_path,
                password_hash=self._auth.password_hash,
                tokens=self._tokens,
                max_connections=s.max_connections,
                on_control_session=self._serve_control,
                on_screen_session=self._serve_screen,
                check_client=self._auth.check_client,
                on_auth_failed=self._on_auth_failed_hook,
                ban_query=self._auth.ban_remaining_seconds,
            )
            await self._server.start()
        except (OSError, TlsError) as exc:
            # TlsError：用户证书加载失败（S1 口径不回落自签名）→ 启动失败可观测
            _LOG.error("host_service_start_failed", error=str(exc))
            self._server = None
            return False

        self._paused = False
        self._start_time = time.time()
        self.pid_manager.write()
        # 走查第十二批 Bug 14/15 主导根因修复：订阅登记表（连同其持有的
        # broadcaster）**每个运行周期重建**。旧实现跨 pause/resume/restart
        # 复用同一张表，而 _teardown_runtime 必经 registry.shutdown() →
        # broadcaster._stopped 永久置位 → 恢复后新画面订阅 register() 抛
        # RuntimeError → 画面会话建立即关 → 主控端"控制可用但画面卡在最后
        # 一帧"。必须在新会话开闸（TransportServer 已启动）后、构造管线前
        # 重建——管线注入的 broadcaster 与本表必须是同一实例。
        self._registry = self._new_registry()
        self._ensure_screen_source()
        # 走查第十二批 Bug 14/15：上一周期 pipeline.close() 连带关闭了采集源
        # （pause→resume/restart 复用同一源实例），必须可逆重开，否则新管线
        # 每帧 capture 抛 ScreenCaptureError 退避——"恢复/重启后画面卡死"的
        # 第二病灶。无 reopen 面的替身源（单测自造对象）降级为不处理：
        # 其行为与修复前一致，不引入新故障面。
        reopen = getattr(self._source, "reopen", None)
        if callable(reopen):
            reopen()
        self._ensure_pipeline()
        self._stop_event = asyncio.Event()
        assert self._pipeline is not None and self._stop_event is not None  # nosec B101 -- 内部不变量：两件资源紧前构造
        loop = asyncio.get_running_loop()
        self._pipeline_task = loop.create_task(self._pipeline.run(stop=self._stop_event), name="pipeline")
        self._cleanup_task = loop.create_task(self._ban_cleanup_loop(), name="ban-cleanup")
        _LOG.info(
            "host_service_started",
            bind_host=s.bind_host,
            control_port=s.control_port,
            screen_port=s.port,
            auth=s.enable_auth and bool(s.password_hash),
            user_cert=self._cert_bundle is not None,
        )
        return True

    async def pause(self) -> None:
        """暂停：停止监听与推流，进程保留（v4 pause 语义，托盘菜单用）。"""
        if self._paused or self._server is None:
            return
        # 走查第十三批 Bug 1：``_paused`` 入口即置位（旧版在 teardown 之后）。
        # 旧 teardown 被会话空闲窗口拖住（实测 stop()≈10s），期间 is_paused
        # 恒 False → 托盘"暂停服务"文案不动、再点仍重复下发暂停。前置置位后
        # 重入即刻短路（幂等），配合 TransportServer.stop 顺序修复，暂停在
        # 毫秒级完成、文案随即刷新。
        self._paused = True
        # 走查第十二批 Bug 12：拆会话**前**先给全部控制会话下行 PAUSED 状态帧
        # （并发、单会话限时 2s 隔离）。旧版 pause 静默关监听，主控端只看到
        # EOF——画面卡住、无任何"对方已暂停"信息。
        await self._broadcast_paused_notice()
        try:
            await self._teardown_runtime()
        except Exception:
            # 收尾异常回滚标志：避免"未暂停却显示已暂停"的反向不一致
            self._paused = False
            raise
        _LOG.info("host_service_paused")

    async def _broadcast_paused_notice(self, timeout_s: float = 2.0) -> None:
        """向活动控制会话广播暂停状态（单会话失败隔离，不阻断暂停流程）。"""
        status = HostStatus(code=HostStatusCode.PAUSED, detail="服务已暂停")

        async def _one(session: ControlSession) -> None:
            try:
                await asyncio.wait_for(session.send(status), timeout=timeout_s)
            except Exception as exc:  # noqa: BLE001 - 通知失败只降级记录（会话本就要被拆除）
                _LOG.warning(
                    "paused_notice_send_failed", client=session.client_ip, error=repr(exc)
                )

        sessions = list(self._control_sessions)
        if sessions:
            await asyncio.gather(*(_one(session) for session in sessions))

    async def resume(self) -> None:
        """恢复监听与推流（pause 的逆操作）。"""
        if not self._paused:
            return
        self._paused = False
        await self.start()

    async def shutdown(self) -> None:
        """关停全部资源并请求进程退出（幂等）。"""
        self._should_exit = True
        await self._teardown_runtime()
        self.pid_manager.remove()
        _LOG.info("host_service_shutdown")

    @property
    def is_running(self) -> bool:
        """服务处于监听状态。"""
        return self._server is not None and self._server.listening

    @property
    def is_paused(self) -> bool:
        """服务处于暂停状态。"""
        return self._paused

    @property
    def should_exit(self) -> bool:
        """收到退出请求（装配层主循环据此结束进程）。"""
        return self._should_exit

    async def restart(self) -> None:
        """关停后重新启动（口令/开关/证书热更的生效路径，v4 _restart 同语义）。"""
        await self._teardown_runtime()
        self._paused = False
        await self.start()

    # ================================================================== 观测面

    def snapshot(self) -> HostSnapshot:
        """UI 状态面板快照（实现 :class:`~rdc.ui.status_panel.HostStatsProvider`）。"""
        encoder = self._pipeline.encoder if self._pipeline is not None else None
        enc_stats = encoder.stats() if encoder is not None else None
        return HostSnapshot(
            running=self.is_running,
            paused=self._paused,
            active_connections=len(self._control_sessions),
            subscriber_count=self._registry.count(),
            bytes_sent_total=enc_stats.total_bytes if enc_stats else 0,
            uptime_s=time.time() - self._start_time if self._start_time else 0.0,
            encoder_quality=enc_stats.quality if enc_stats else None,
            encoder_avg_kb=enc_stats.avg_bytes / 1024.0 if enc_stats else 0.0,
            frames_encoded=enc_stats.frames if enc_stats else 0,
        )

    def get_stats(self) -> dict[str, Any]:
        """诊断统计（v4 ``get_stats`` 键面延续：CLI/托盘用，UI 走 :meth:`snapshot`）。"""
        snap = self.snapshot()
        bc_stats = self._registry.broadcaster.stats()
        return {
            "start_time": self._start_time,
            "uptime": snap.uptime_s,
            "active_connections": snap.active_connections,
            "subscriber_count": snap.subscriber_count,
            "published_frames": bc_stats.published,
            "total_dropped": bc_stats.total_dropped,
            "bytes_sent_total": snap.bytes_sent_total,
            "encoder_avg_kb": snap.encoder_avg_kb,
            "frames_encoded": snap.frames_encoded,
            "rate_limiter_count": self._auth.rate_connection_count(),
            "banned_ips": self._auth.banlist.banned_count,
        }

    # ================================================================== 证书

    def _prepare_certificates(self) -> tuple[Path, Path]:
        """解析服务端证书路径：用户证书（9.3）优先，缺省自签名（阶段 4 件）。

        用户证书加载失败抛 :class:`~rdc.exceptions.TlsError`（**不回落自签名**，
        S1 口径），由 :meth:`start` 捕获记为启动失败。
        """
        ensure_dirs()
        s = self._settings
        if s.tls_cert_path and s.tls_key_path:
            # 路径穿越防护（渗透清单 S4）：config.json 里的证书路径必须先
            # 规范化并限定在应用数据根内——否则恶意/误配路径会把数据根外
            # 的任意文件（他人私钥、系统文件）读入内存尝试解析。
            cert_p = require_within_data_root(Path(s.tls_cert_path), label="证书")
            key_p = require_within_data_root(Path(s.tls_key_path), label="私钥")
            bundle = load_user_cert_bundle(cert_p, key_p)
            self._cert_bundle = bundle
            status = check_expiry(bundle.not_before, bundle.not_after)
            if status is not ExpiryStatus.VALID:
                self._audit.cert_expiring(
                    fingerprint_prefix=bundle.fingerprint[:23],
                    remaining_days=max(0, (bundle.not_after - _now_utc()).days),
                )
            _LOG.info("host_service_user_cert_loaded", cert=str(bundle.cert_path))
            return bundle.cert_path, bundle.key_path
        self._cert_bundle = None
        cert_path = cert_dir() / "cert.pem"
        key_path = cert_dir() / "key.pem"
        ensure_server_cert(cert_path, key_path)
        return cert_path, key_path

    # ================================================================== 采集/管线

    def _ensure_screen_source(self) -> None:
        """生产采集源延后构造（测试注入则跳过）。

        走查第十一批：动态分辨率自适应靠 ``geometry_probe``（虚拟桌面
        GetSystemMetrics 包络）——选择器/mss 的几何都是缓存值，被控端运行
        中改分辨率必须显式探测 OS 才能感知。探针失败/非 Windows 返回 None
        即静默停用，绝不引入新故障面。
        """
        if self._source is None:
            self._source = MssScreenSource(geometry_probe=virtual_desktop_bounds)

    def _ensure_pipeline(self) -> None:
        """按当前 settings 构造广播管线（rater 以队列/丢帧信号自适应）。"""
        if self._pipeline is not None:
            return
        assert self._source is not None  # nosec B101 -- 内部不变量：调用点均先经 _ensure_screen_source
        s = self._settings
        self._pipeline = ScreenPipeline(
            self._source,
            broadcaster=self._registry.broadcaster,
            framerate=FramerateController(min_fps=s.min_fps, max_fps=s.max_fps),
            rater=AdaptiveRater(
                initial_crf=_quality_to_crf(s.jpeg_quality), target_fps=float(s.max_fps)
            ),
            signals_provider=self._net_signals,
            capture_stall_hook=self._on_capture_stall,
        )

    def _on_capture_stall(self, stalled: bool, detail: str) -> None:
        """采集停滞/恢复边沿（事件循环线程，pipeline 红线）→ 控制会话广播。

        安全桌面归因用桌面探针即时判定（停滞当下 UAC 可能仍在/已结束，
        探针如实反映）；HostStatus 发送失败（会话已关）单个隔离，不影响
        其余会话，也不波及采集循环。
        """
        code = HostStatusCode.SCREEN_STALLED if stalled else HostStatusCode.OK
        status = HostStatus(
            code=code,
            secure_desktop=is_secure_desktop() if stalled else False,
            detail=detail,
        )
        if stalled:
            _LOG.warning("capture_stalled", detail=detail, secure_desktop=status.secure_desktop)
        else:
            _LOG.info("capture_recovered")
        for session in list(self._control_sessions):
            with contextlib.suppress(Exception):  # 单会话发送失败隔离
                self._spawn_control_send(session, status)

    def _spawn_control_send(self, session: ControlSession, msg: Message) -> None:
        """异步下发一条服务端主动消息（fire-and-forget，任务异常自记）。"""
        task = asyncio.get_running_loop().create_task(session.send(msg))
        self._status_tasks.add(task)
        task.add_done_callback(self._status_tasks.discard)

    def _net_signals(self) -> RaterSignals:
        """服务侧网络信号：RTT/丢包在 S→C 单向流上无测量源（v4 BandwidthProbe
        的等效职责由 DropOldestQueue 背压统计承担，pipeline 内部补齐）。"""
        return RaterSignals(rtt_ms=0.0, loss_rate=0.0)

    def _on_new_subscriber(self) -> None:
        """新画面订阅建立 → 请求关键帧（I 帧起解，防花屏）。"""
        if self._pipeline is not None:
            self._pipeline.request_keyframe()

    def _on_subscriber_removed(self, subscriber_id: str, reason: str) -> None:
        self._audit.conn_close(ip=subscriber_id, kind="screen", reason=reason)

    # ================================================================== 会话：控制通道

    async def _serve_control(self, channel: StreamChannel) -> None:
        """控制会话主循环：消息分发（输入执行/心跳/流控/剪贴板）直到 EOF 或空闲超时。

        走查第六批 Bug4：客户端连接后每 ``heartbeat_interval`` 秒发一次心跳 PING，
        存活链路必有消息流入。用 ``heartbeat_timeout`` 作**读空闲上限**：静默断网
        （拔线/VM 断网，无 FIN/RST，TCP 半开）时 ``next_message()`` 会一直阻塞到
        操作系统 TCP 超时（Windows 默认约 2 小时），期间 ``finally`` 的
        ``_reset_input()`` 迟迟不触发 → 最后一次按住的 Win 等修饰键在远端悬空
        （"按住 Win→断网→按 D 触发显示桌面"）。空闲超时把该窗口从 ~2 小时收敛到
        heartbeat_timeout（默认 10s），且与客户端对服务端的判死预算对称（活客户端
        每 3s 必发 PING，10s 静默 = 真死，不误伤正常会话）。
        """
        assert isinstance(channel, ControlSession)  # nosec B101 -- TransportServer 按端口交付对应会话类型
        ip = channel.client_ip
        self._control_sessions.add(channel)
        self._auth.on_auth_success(ip)
        self._audit.conn_open(ip=ip, kind="control")
        idle = self._settings.heartbeat_timeout
        reason = "eof"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(channel.next_message(), timeout=idle)
                except TimeoutError:  # 静默断网：链路半开但无消息 → 判死退出
                    reason = "idle_timeout"
                    break
                if msg is None:
                    break
                await self._dispatch_control(channel, msg)
                self._audit.maybe_flush()
        finally:
            self._control_sessions.discard(channel)
            self._audit.conn_close(ip=ip, kind="control", reason=reason)
            # 断连兜底：释放记账中的按键/鼠标键（v4 卡键病灶，D37 对面）。
            # 走查第六批 Bug4：正常 EOF 与空闲超时两条退出路径都经此释放，
            # 静默断网不再滞留修饰键（旧版仅靠 finally，半开时永不触发）。
            self._reset_input()

    async def _dispatch_control(self, channel: ControlSession, msg: Message) -> None:
        """单条控制消息的分派（固定事件循环线程；输入注入为轻量同步 API）。"""
        if isinstance(msg, MouseEvent):
            self._apply_mouse(msg, channel.client_ip)
        elif isinstance(msg, KeyEvent):
            self._apply_key(msg, channel.client_ip)
        elif isinstance(msg, Heartbeat):
            if msg.kind is HeartbeatKind.PING:
                await channel.send(Heartbeat(kind=HeartbeatKind.PONG, timestamp=msg.timestamp))
        elif isinstance(msg, StreamControl):
            self._apply_stream_control(msg)
        elif isinstance(msg, ClipboardEvent):
            await self._apply_clipboard(msg, channel.client_ip)
        else:
            _LOG.warning("control_unexpected_message", client=channel.client_ip, type=type(msg).__name__)

    # -- 输入执行 ---------------------------------------------------------------

    def _get_input_backend(self) -> InputBackend:
        """输入后端懒构造（测试注入优先；首次触达按平台工厂选择）。"""
        if self._input_backend is None:
            self._input_backend = default_backend()
        return self._input_backend

    def _reset_input(self) -> None:
        """断连兜底释放按键（契约保证幂等且无键按下时空操作，注入替身同样调用）。"""
        if self._input_backend is not None:
            try:
                self._input_backend.reset()
            except Exception as exc:  # InputControlError 等：兜底记录不静默（约束 6）
                _LOG.error("input_reset_failed", error=repr(exc))

    #: 注入失败日志节流窗口（秒）：安全桌面期间每事件必失败，防刷屏。
    _INJECT_LOG_WINDOW_S = 5.0

    def _log_inject_failure(self, event: str, ip: str, exc: BaseException) -> None:
        """注入失败降级记录（走查第六批 P0）：warning + 同类 5s 节流。

        被抑制的条数计入下一条日志（可观测性不打折：刷屏被止住，但总量
        仍能看出规模）。恢复后首条立即输出。
        """
        now = time.monotonic()
        last, suppressed = self._inject_log_state.get(event, (0.0, 0))
        if now - last < self._INJECT_LOG_WINDOW_S:
            self._inject_log_state[event] = (last, suppressed + 1)
            return
        self._inject_log_state[event] = (now, 0)
        _LOG.warning(
            event, client=ip, error=repr(exc), suppressed=f"前 {suppressed} 条同类已抑制"
        )

    def _apply_mouse(self, ev: MouseEvent, ip: str) -> None:
        """鼠标事件 → :class:`InputBackend`（坐标=远端虚拟桌面绝对坐标，D32）。"""
        backend = self._get_input_backend()
        button = _PROTO_BUTTON_TO_INPUT.get(ev.button)
        try:
            if ev.action in (MouseAction.MOVE, MouseAction.DRAG):
                # DRAG 语义：按键态由 PRESS/RELEASE 维护，DRAG 仅移动光标。
                backend.move_mouse(ev.x, ev.y)
            elif ev.action is MouseAction.PRESS and button is not None:
                backend.click(ev.x, ev.y, button, press=True)
            elif ev.action is MouseAction.RELEASE and button is not None:
                backend.click(ev.x, ev.y, button, press=False)
            elif ev.action is MouseAction.CLICK and button is not None:
                backend.click(ev.x, ev.y, button, press=True)
                backend.click(ev.x, ev.y, button, press=False)
            elif ev.action is MouseAction.SCROLL:
                backend.scroll(ev.x, ev.y, 0, ev.scroll_delta)
            else:
                _LOG.warning("mouse_action_unsupported", client=ip, action=ev.action.name)
                return
        except Exception as exc:  # 单事件注入失败不断流，可观测（约束 6）
            # 走查第六批 P0：安全桌面期间 SendInput 必然持续失败（属 Windows
            # 安全边界而非缺陷），降级 warning + 节流，避免每鼠标移动刷一条 error。
            self._log_inject_failure("mouse_inject_failed", ip, exc)
            return
        self._audit.control(EVENTS.Ctrl.MOUSE, ip=ip, action=ev.action.name)

    def _apply_key(self, ev: KeyEvent, ip: str) -> None:
        """键盘事件 → 后端（PRESS/RELEASE 带修饰键顺序，TYPE 走文本通道）。"""
        backend = self._get_input_backend()
        try:
            if ev.action is KeyAction.TYPE:
                backend.type_text(ev.text)
            elif ev.action is KeyAction.PRESS:
                for mod in ev.modifiers:  # v4 组合语义：先修饰键后主键
                    backend.key_press(_MODIFIER_NAMES[mod])
                backend.key_press(ev.key)
            elif ev.action is KeyAction.RELEASE:
                backend.key_release(ev.key)
                for mod in reversed(ev.modifiers):  # 释放逆序
                    backend.key_release(_MODIFIER_NAMES[mod])
            else:
                _LOG.warning("key_action_unsupported", client=ip, action=ev.action.name)
                return
        except Exception as exc:
            self._log_inject_failure("key_inject_failed", ip, exc)
            return
        self._audit.control(EVENTS.Ctrl.KEY, ip=ip, action=ev.action.name)

    # -- 流控 / 剪贴板 ----------------------------------------------------------

    def _apply_stream_control(self, msg: StreamControl) -> None:
        """流控制：START/STOP 推流门控，CONFIG 帧率即时应用。"""
        if msg.cmd is StreamCmd.START:
            if self._stop_event is not None:
                self._stop_event.clear()
            if self._pipeline_task is None or self._pipeline_task.done():
                assert self._pipeline is not None and self._stop_event is not None  # nosec B101 -- 内部不变量：管线与停件事件仅在 start 后到达此处
                self._pipeline_task = asyncio.get_running_loop().create_task(
                    self._pipeline.run(stop=self._stop_event), name="pipeline"
                )
        elif msg.cmd is StreamCmd.STOP:
            if self._stop_event is not None:
                self._stop_event.set()
        elif msg.cmd is StreamCmd.CONFIG and msg.config is not None:
            cfg = msg.config
            if cfg.max_fps and self._pipeline is not None:
                self._pipeline.framerate.set_fps(cfg.max_fps)
            if cfg.quality:
                _LOG.info("stream_quality_hint_ignored_rater", quality=cfg.quality)
                # v5 画质由 AdaptiveRater 按背压自适应（JPEG quality 语义与 CRF 不互通，
                # 映射标定归阶段 10，禁止发明未验证参数——D24 口径）。
        _LOG.debug("stream_control_applied", cmd=msg.cmd.name)

    async def _apply_clipboard(self, ev: ClipboardEvent, ip: str) -> None:
        """剪贴板写入本地（v4 线程内 ``tk.Tk()`` 崩溃病灶 B2 消除）。

        ⚠️ 同步调用 ``pyperclip``（**不用** :func:`asyncio.to_thread`）：剪贴板
        为低频事件路径，pyperclip 实为轻量 ctypes/COM 调用；to_thread 会占用
        事件循环默认 executor，与 UI 测试同进程连跑时 loop 关停
        （``shutdown_default_executor``）在 Qt 环境不泵送 → 死锁
        （阶段 6 坑 t11 同族，阶段 9 实测定位）。
        """
        if ev.chunk_total > 1:
            _LOG.info(
                "clipboard_chunk_pending_stage8",
                client=ip,
                index=ev.chunk_index,
                total=ev.chunk_total,
            )
            # 分块重组属文件传输协议面（阶段 8），本批只接受单块文本。
            if ev.chunk_index != 0:
                return
        try:
            _clipboard_copy(ev.text)
        except Exception as exc:  # pyperclip 平台后端异常面不可枚举：记录不静默
            _LOG.error("clipboard_write_failed", client=ip, error=repr(exc))
            return
        self._audit.control(EVENTS.Ctrl.CLIPBOARD, ip=ip, length=len(ev.text))

    # ================================================================== 会话：画面通道

    async def _serve_screen(self, channel: StreamChannel) -> None:
        """画面会话：登记订阅 → 保持至 EOF → 注销（回收统一走 registry）。"""
        assert isinstance(channel, ScreenSession)  # nosec B101 -- TransportServer 按端口交付对应会话类型
        sid = self._registry.register(channel)
        self._audit.conn_open(ip=channel.client_ip, kind="screen", connection_id=sid)
        try:
            while True:
                msg = await channel.next_message()
                if msg is None:
                    break
                # 画面通道为 S→C 单向：收到消息仅心跳/诊断，无业务分派。
                _LOG.debug("screen_inbound_ignored", subscriber=sid, type=type(msg).__name__)
        finally:
            await self._registry.unregister(sid, "eof")

    # ================================================================== 认证/后台

    def _on_auth_failed_hook(self, ip: str, channel: str) -> None:
        # 认证失败原因按通道区分（画面通道为令牌无效/绑定不符）
        reason = REASON_BAD_PASSWORD if channel == "control" else "token_invalid"
        self._auth.on_auth_failure(ip, reason)

    async def _ban_cleanup_loop(self) -> None:
        """到期封禁批清理（循环可被 stop_event 立即打断）。"""
        assert self._stop_event is not None  # nosec B101 -- 内部不变量：任务仅在 start 成功（stop_event 已建）后派生
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=BAN_CLEANUP_INTERVAL_S)
                return
            except TimeoutError:
                self._auth.cleanup_expired_bans()

    async def _teardown_runtime(self) -> None:
        """停任务 → 关管线/订阅 → 停传输监听（幂等，pause/shutdown/restart 共用）。"""
        if self._stop_event is not None:
            self._stop_event.set()
        for task in (self._pipeline_task, self._cleanup_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._pipeline_task = None
        self._cleanup_task = None
        await self._registry.shutdown()
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._stop_event = None


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------


def _quality_to_crf(quality: int) -> int:
    """UI 画质分（越大越好，1-100）→ H.264 CRF 初值（越小越好）。

    线性映射到标定区间 [18, 45]（STAGE5 §3.4 实测域）：quality=60 → CRF 28
    （生产默认）；仅用作 AdaptiveRater 的初始 CRF，运行期由背压信号接管。
    """
    clamped = max(1, min(100, quality))
    return round(45 - (clamped - 1) * (45 - 18) / 99)


def _clipboard_copy(text: str) -> None:
    """本地剪贴板写入（独立函数便于测试 monkeypatch）。"""
    import pyperclip

    pyperclip.copy(text)


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)

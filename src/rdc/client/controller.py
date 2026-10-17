"""主控端会话控制器（阶段 9 批 4）：ui 侧 ClientController Protocol 的真实现。

**红线：零 Qt/Widgets 导入**——本模块只依赖 transport/input/protocol/config
层，可脱离 GUI 单测、可被 CLI 复用；:class:`rdc.ui.main_window.ClientController`
Protocol 由本类结构化实现（不显式继承，UI 零改动）。

装配全景（阶段 9 任务 A1）::

    UI(ScreenView 信号) ──send_mouse/send_key/send_text──► ClientController
        ▲                                                      │
   frame_arrived ◄── image_factory 注入产帧（解码 RGB 像素）───┤
   metrics_updated ◄── _flush_loop 每 ~1s（fps/RTT/带宽/分辨率）┤
   clipboard_from_remote ◄── ClipboardEvent（控制通道下行）────┤
                                                              ├ ControlChannel（TLS+认证+nonce+HMAC）
    InputCoalescer(feed/poll/drain，D34) ──批 → 协议消息───────┤ ScreenChannel（一次性令牌加入，S→C帧）
                                                              │ 接收环×2 + 心跳 + 重连环

关键设计决定：

- **异步口令/指纹直通**：通道 TLS 升级在 await 上下文调用
  :func:`rdc.transport.tls.verify_peer_trust_async`，UI 非模态确认框（协程，
  D26）直接作为 confirm 回调传入——controller 不再自建"预检探测"（探测连接
  会在被控端握手期产生伪认证失败，污染封禁窗口，批 4 设计评审否决）。
  口令经 :meth:`_provide_password` 协程索取一次、会话期复用（重连不重复弹框，
  v4 重连弹 N 次口令框病灶消除）。
- **坐标直通**：ScreenView 场景坐标 = 远端虚拟桌面绝对坐标（D25），本类不
  换算；帧原点经协议 ``ScreenFrame.origin_x/y``（批 4 扩展）回给
  ``frame_arrived(image, left=, top=)``，多屏负原点对齐闭环。
- **输入挂画面节拍（A4）**：入站视频帧到达间隔估计实测 fps（EMA 平滑），
  经 :meth:`InputCoalescer.sync_to_frame` 消费——帧对齐即"与画面帧同节拍
  出批"；``_flush_loop`` 按 coalescer 节拍出批（画面静止 idle 档时输入
  不饿死，D34）。
- **RTT 口径**：PING 的 ``timestamp`` 用 wall clock（服务端 PONG 原样回显），
  RTT = 收 PONG 时刻 − 回显值。
- **重连**：链路死亡（控制 EOF/心跳判死）且 ``auto_reconnect`` →
  RECONNECTING，:class:`ReconnectController` 按 settings 指数退避；
  认证/TLS/用户取消类失败**不重试**——继续尝试只会喂对端封禁窗口。
- **会话口令明文驻内存**（``_password_holder``）：握手必需（bcrypt 在
  服务端校验），断开（disconnect/最终失败）即清空；绝不写日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil
from typing import Any, Final

from rdc.config.settings import Settings, current_settings
from rdc.exceptions import (
    AuthBannedError,
    AuthCancelledError,
    AuthError,
    ConnectionError,
    TlsError,
)
from rdc.input.base import MouseButton as InputMouseButton
from rdc.input.base import normalize_key_name
from rdc.input.coalesce import InputCoalescer, InputEvent, InputKind, WheelDirection
from rdc.logging import get_logger
from rdc.protocol.messages import (
    ClipboardEvent,
    Codec,
    Heartbeat,
    HeartbeatKind,
    HostStatus,
    HostStatusCode,
    KeyAction,
    KeyEvent,
    Message,
    MouseAction,
    MouseButton,
    MouseEvent,
    ScreenFrame,
)
from rdc.transport.control_channel import ControlChannel
from rdc.transport.heartbeat import HeartbeatController
from rdc.transport.reconnect import ReconnectController
from rdc.transport.screen_channel import ScreenChannel

__all__ = ["ClientController"]

_LOG = get_logger("client.controller")

#: 口令索取回调：同步返回或返回协程（UI 非模态框）。``None`` = 用户取消。
PasswordGetter = Callable[[str], "str | None | Awaitable[str | None]"]
#: 指纹确认回调：同步返回或返回协程；传输层 awaitable 感知。
FingerprintConfirmer = Callable[[str, str], "bool | Awaitable[bool]"]
#: ``(width, height, rgb_ndarray) -> 帧对象``：GUI 传 QImage 包装闭包，
#: 无 GUI 场景传 ``lambda w, h, arr: object()`` 冒烟替身。
ImageFactory = Callable[[int, int, Any], object]

#: 认证类失败异常集（rdc ``ConnectionError``=握手/连接被业务层拒绝，非
#: builtins 网络错误的 ConnectionRefusedError——后者是 OSError 子类，走网络
#: 重试路径）。**统一语义：一律不进重连环、不重试**——重试只会用同一份错误
#: 口令再烧一次服务端失败计数，喂封禁窗口（2026-10-06 走查第四批缺陷 3）。
_AUTH_CLASS_ERRORS: Final = (AuthError, TlsError, ConnectionError)

#: 会被操作系统**吞掉释放事件**的热键（走查第九批，2026-10-17 用户实测）：
#: Windows 按 Win 时由系统接管开始菜单，Qt 应用层收不到 keyReleaseEvent →
#: 按下已转发、释放丢失 → 被控端 Win 永久按住，后续所有按键变 Win+组合。
#: v5.0.0 应急兜底（完整解决=低级键盘钩子 WH_KEYBOARD_LL，记入 v5.1.0 特权
#: 操作范围）：按住超过阈值仍未见释放 → 主动补发一次 RELEASE。
#: **只监视 Win**：Shift/Alt/Ctrl 长按是合法用法（Shift 连续输入大写、
#: AltGr 打字、Ctrl 游戏组合键），纳入监视会误伤——用户实测丢释放的仅 Win。
_STICKY_WATCH_KEYS: Final = frozenset({"win"})
#: 按住超过该秒数即判定"释放事件已被系统吞掉"。人类相邻按键间隔远小于
#: 此值，正常长按（哪怕故意按住 Win 思考）最坏只多容忍 2s 即解锁。
_STICKY_HELD_TIMEOUT_S: Final = 2.0


def _default_image_factory(_width: int, _height: int, _arr: Any) -> object:
    raise ConnectionError(
        "未注入 image_factory：controller 需要帧图像构造器"
        "（GUI 传 QImage 工厂；无 GUI 冒烟传替身）"
    )


@dataclass
class _FpsSnapshot:
    """实测帧率快照（鸭子协议喂 :meth:`InputCoalescer.sync_to_frame`）。

    非 frozen：``coalesce._HasFps`` 协议声明 ``fps: float`` 为可写属性。
    """

    fps: float


class ClientController:
    """主控端会话控制器（实现 ui.main_window.ClientController Protocol）。

    Args:
        settings: 运行时配置（重连/心跳/画面端口偏移；``None`` 取全局单例）。
        image_factory: 帧图像构造器（见 :data:`ImageFactory`）。
        coalescer: 输入合并器（``None`` 新建默认参数；测试注入假时钟）。
        clock: 单调时钟（指标窗口/帧率估计共用；测试注入）。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        image_factory: ImageFactory | None = None,
        coalescer: InputCoalescer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings if settings is not None else current_settings()
        self._image_factory: ImageFactory = image_factory or _default_image_factory
        self._coalescer = coalescer if coalescer is not None else InputCoalescer(clock=clock)
        self._clock = clock
        self._events: Any = None
        self._state = "idle"
        # 会话通道与后台环
        self._control: ControlChannel | None = None
        self._screen: ScreenChannel | None = None
        self._hb: HeartbeatController | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._screen_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._reconnect: ReconnectController | None = None
        #: 重连环后台任务（teardown 不自我取消；disconnect 显式取消）
        self._reconnect_task: asyncio.Task[object] | None = None
        self._tasks: set[asyncio.Task[object]] = set()
        # connect() 参数与派生会话状态
        self._host = ""
        self._port = 0
        self._get_password: PasswordGetter = _no_password
        self._confirm_fingerprint: FingerprintConfirmer = _no_confirm
        self._password_holder: list[str] | None = None
        self._user_disconnect = False
        self._control_enabled = False
        self._tearing_down = False
        # 画面解码器（跨帧维持 H.264 参考链；会话拆除即丢引用释放）
        self._h264_decoder: Any = None
        # 帧节拍估计（A4：coalesce 挂画面节拍）
        self._last_frame_at: float | None = None
        self._measured_fps = 0.0
        # 指标窗口
        self._frames_in_window = 0
        self._bytes_in_window = 0
        self._window_started = clock()
        self._total_frames = 0
        self._last_latency_ms: float | None = None
        self._resolution: tuple[int, int] | None = None
        # 封禁缓存（走查第四批缺陷 2）：服务端 banned 响应的本地到期时刻。
        # 命中后 connect 在弹口令框**前**即拒绝，用户不再白输口令。
        self._ban_until = 0.0
        self._ban_host = ""
        # 粘滞热键记账（走查第九批）：键名 → 按下时刻（self._clock 口径）。
        # 仅 _STICKY_WATCH_KEYS 成员会被登记；释放到达即销账。
        self._sticky_held: dict[str, float] = {}

    # ================================================================== UI 契约

    def set_events(self, events: Any) -> None:
        """注册事件回调（:class:`rdc.ui.main_window.ClientEvents` 结构体）。

        所有回调在本类运行所在线程（qasync 下 = GUI 线程）被直调。
        """
        self._events = events

    @property
    def state(self) -> str:
        """当前状态串（值域同 ``ConnectionState``：idle/connecting/...）。"""
        return self._state

    async def connect(
        self,
        host: str,
        port: int,
        *,
        get_password: PasswordGetter,
        confirm_fingerprint: FingerprintConfirmer,
    ) -> None:
        """建连+认证（先拆旧会话）。失败抛异常并落定状态。

        口令/指纹经回调向 UI 索取（awaitable 感知直通传输层）。失败分支：

        - 用户取消口令 → 视为放弃，状态 ``disconnected``，不重连；
        - 认证/TLS 类失败（AuthError/TlsError/ConnectionError）→ **直接落定
          ``disconnected`` 并上抛，绝不进重连环**（2026-10-06 走查第四批：
          旧版首连认证失败也起环，环内立即复用错误口令再试一次，一次点击
          烧两次服务端失败计数、喂封禁窗口——"点 2 次就封"病灶）；
        - 网络类失败（OSError 等）且 ``auto_reconnect`` → 进入重连环
          （状态 ``reconnecting``，指数退避；环内认证/TLS 类失败同样终止）；
        - 未开自动重连 → ``disconnected`` + 异常上抛。

        封禁缓存（走查第四批缺陷 2）：上次握手收到 ``banned`` 响应即缓存
        到期时刻；本主机仍在封禁期内时，connect **不发网络包、不弹口令框**，
        直接抛 :class:`AuthBannedError`（剩余时长按本地时钟折算）。
        """
        if host == self._ban_host and self._clock() < self._ban_until:
            self._set_state("disconnected")
            raise self._banned_error_now()
        await self._teardown()
        self._user_disconnect = False
        self._host, self._port = host, port
        self._get_password = get_password
        self._confirm_fingerprint = confirm_fingerprint
        self._password_holder = None
        self._set_state("connecting")
        try:
            await self._open_session(host, port)
        except _AUTH_CLASS_ERRORS as exc:
            self._record_ban(exc)
            await self._final_disconnect()
            raise
        except BaseException:
            if self._settings.auto_reconnect and not self._user_disconnect:
                self._start_reconnect_loop()
            else:
                self._set_state("disconnected")
            raise
        self._set_state("connected")

    async def disconnect(self) -> None:
        """用户主动断开（幂等；终止重连环，口令缓冲即清）。"""
        self._user_disconnect = True
        if self._reconnect is not None:
            self._reconnect.stop()
        ring_task = self._reconnect_task
        if ring_task is not None and not ring_task.done():
            ring_task.cancel()
            # 必须 await：被 cancel 却从未开始执行的协程会在 GC 时报
            # "coroutine was never awaited"（重连环在退避 sleep 中被打断即此）。
            with contextlib.suppress(asyncio.CancelledError):
                await ring_task
        await self._teardown()
        self._password_holder = None
        self._set_state("disconnected")

    def set_control_enabled(self, enabled: bool) -> None:
        """远程控制开关：False 时 send_* 为 no-op 并丢弃缓冲（防恢复时旧事件突发）。"""
        self._control_enabled = enabled
        if not enabled:
            self._coalescer.reset()
            # 走查第九批：关控制=用户放弃转发，本地粘滞记账一并清掉
            # （防 2s 后向新会话补发陈旧 RELEASE；被控端侧由断连 reset 兜底）。
            self._sticky_held.clear()

    def send_mouse(self, action: str, x: int, y: int) -> None:
        """转发鼠标（ScreenView 载荷口径：``move``/``press:<btn>``/``wheel:<dir>``）。"""
        if not self._control_enabled:
            return
        try:
            self._emit_batches(self._coalescer.feed_mouse(action, x, y))
        except Exception as exc:  # InputControlError（载荷非法）：记录不炸 UI
            _LOG.warning("feed_mouse_rejected", action=action, error=str(exc))

    def send_key(self, qt_key: int, press: bool) -> None:
        """转发按键（Qt 键码 → 规范名，值表与 ``input/keys`` 同源，零 Qt 导入）。"""
        if not self._control_enabled:
            return
        name = _qt_key_to_name(qt_key)
        if name is None:
            # 走查第六批 Bug2：未映射键不静默吞掉——提级 warning 便于定位
            # 键盘链路断点（哪些物理键没进值表）。
            _LOG.warning("send_key_unmapped_qt_code", qt_key=qt_key)
            return
        # 走查第九批：本键出批前先对账粘滞热键——"下一个键到达"是释放
        # 已被系统吞掉后最确定的解锁时机（建议 B 的确定性形态）。
        self._reconcile_sticky_keys()
        if press and name in _STICKY_WATCH_KEYS:
            self._sticky_held[name] = self._clock()
        elif not press and name in _STICKY_WATCH_KEYS:
            # 正常释放到达（Qt 未丢，如焦点回窗口后的 keyReleaseEvent）：
            # 已随下方 feed 转发，本地记账销账，不再补发。
            self._sticky_held.pop(name, None)
        self._emit_batches(self._coalescer.feed_key(name, press))

    def send_text(self, text: str) -> None:
        """转发 IME 已上屏文本（D35：物理键走 keyEvent、提交串走文本通道）。"""
        if not self._control_enabled:
            return
        self._emit_batches(self._coalescer.feed_text(text))

    def send_clipboard(self, text: str) -> None:
        """本地剪贴板出站（GUI 的 ClipboardSync.localChanged 接此处）。

        剪贴板同步不受控制开关约束（v4 语义：``enable_clipboard`` 由 UI 侧判定）。
        """
        control = self._control
        if not text or control is None or not control.is_connected:
            return
        self._spawn(control.send(ClipboardEvent(text=text, timestamp=time.time())))

    def flush_now(self) -> None:
        """强制冲刷输入缓冲（切窗/失焦等 UI 时机点；保证按键不滞留）。"""
        self._emit_batches(self._coalescer.drain())

    # ================================================================== 会话建立

    async def _open_session(self, host: str, port: int) -> None:
        """控制通道 → 画面通道 → 心跳/接收环/输入节拍。"""
        control = ControlChannel(
            host,
            port,
            password_provider=lambda: self._provide_password(host),
            fingerprint_confirm=self._confirm_fingerprint,
            client_name="rdc-v5-client",
        )
        self._set_state("authenticating")
        await control.connect()
        self._control = control
        screen_port = port + self._settings.screen_port_offset
        screen = ScreenChannel(
            host,
            screen_port,
            session_token=control.session_token or "",
            connection_id=control.connection_id or b"",
            fingerprint_confirm=self._confirm_fingerprint,
        )
        try:
            await screen.connect()
        except BaseException:
            await control.close()
            self._control = None
            raise
        self._screen = screen
        self._hb = HeartbeatController(
            send_ping=self._send_ping,
            on_timeout=lambda: self._on_link_dead("heartbeat_dead"),
            interval=self._settings.heartbeat_interval,
            timeout=self._settings.heartbeat_timeout,
        )
        self._hb.start()
        loop = asyncio.get_running_loop()
        self._control_task = loop.create_task(self._control_recv_loop(), name="ctrl-recv")
        self._screen_task = loop.create_task(self._screen_recv_loop(), name="scr-recv")
        self._flush_task = loop.create_task(self._flush_loop(), name="input-flush")
        _LOG.info("client_session_open", host=host, port=port, screen_port=screen_port)

    async def _provide_password(self, host: str) -> str:
        """口令供给协程：会话首连向 UI 索取一次；重连复用 holder（不重复弹框）。

        Returns:
            会话口令明文（供通道 :meth:`ControlChannel.connect` 内
            ``await _resolve(provider())`` 消费）。

        Raises:
            AuthCancelledError: 用户取消（回调返回 ``None``；AuthError 子类，
                认证类终止语义不变）。
            AuthBannedError: 本地封禁缓存未过期（弹框**前**拦截；正常路径由
                :meth:`connect` 入口预检，本处兜住重连环等旁路进入）。
        """
        if host == self._ban_host and self._clock() < self._ban_until:
            raise self._banned_error_now()
        if self._password_holder is None:
            value = self._get_password(host)
            if inspect.isawaitable(value):
                value = await value
            if value is None:
                self._user_disconnect = True  # 取消 = 放弃本次连接，不进重连环
                raise AuthCancelledError("用户取消口令输入")
            self._password_holder = [str(value)]
        return self._password_holder[0]

    def _record_ban(self, exc: BaseException) -> None:
        """握手失败异常若为封禁 → 缓存到期时刻（走查第四批缺陷 2）。

        非封禁异常零副作用；旧缓存属其他主机时直接覆盖（预检带主机比对）。
        """
        if isinstance(exc, AuthBannedError) and exc.remaining_seconds > 0:
            self._ban_until = self._clock() + exc.remaining_seconds
            self._ban_host = self._host or ""
            _LOG.info(
                "server_ban_cached", host=self._ban_host, remaining_s=exc.remaining_seconds
            )

    def _banned_error_now(self) -> AuthBannedError:
        """按本地时钟折算剩余秒，重建封禁异常（文案与首连路径同口径）。"""
        remaining = max(0, ceil(self._ban_until - self._clock()))
        return AuthBannedError(AuthBannedError.format_message(remaining), remaining_seconds=remaining)

    # ================================================================== 接收环

    async def _control_recv_loop(self) -> None:
        control = self._control
        assert control is not None  # nosec B101 -- 内部不变量：接收环/心跳仅在 _open_session 成功后创建
        try:
            while True:
                msg = await control.next_message()
                if msg is None:
                    break
                self._on_control_message(msg)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            _LOG.warning("control_recv_error", error=repr(exc))
        finally:
            self._on_link_dead("control_eof")

    def _on_control_message(self, msg: Message) -> None:
        if isinstance(msg, Heartbeat):
            if self._hb is not None:
                self._hb.on_pong()
            if msg.kind is HeartbeatKind.PONG and msg.timestamp > 0:
                rtt = max(0.0, time.time() - msg.timestamp)
                self._last_latency_ms = round(rtt * 1000.0, 1)
            return
        if isinstance(msg, ClipboardEvent):
            self._dispatch_event("clipboard_from_remote", msg.text)
            return
        if isinstance(msg, HostStatus):
            if msg.code is HostStatusCode.SCREEN_STALLED:
                _LOG.warning(
                    "host_status_stalled",
                    secure_desktop=msg.secure_desktop,
                    detail=msg.detail,
                )
            else:
                _LOG.info("host_status_ok")
            self._dispatch_event(
                "host_status", stalled=msg.code is HostStatusCode.SCREEN_STALLED,
                secure_desktop=msg.secure_desktop,
            )
            return
        _LOG.debug("control_unexpected_inbound", type=type(msg).__name__)

    async def _screen_recv_loop(self) -> None:
        screen = self._screen
        assert screen is not None  # nosec B101 -- 内部不变量：接收环仅在 _open_session 成功后创建
        try:
            while True:
                msg = await screen.next_message()
                if msg is None:
                    break
                if isinstance(msg, ScreenFrame):
                    self._handle_screen_frame(msg)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            _LOG.warning("screen_recv_error", error=repr(exc))
        finally:
            # 画面流 EOF：控制链路仍存活则等待服务端重推（新订阅=新关键帧，
            # registry 已接线），不判死整会话；控制侧断开自会触发重连。
            _LOG.info("screen_stream_eof")

    async def _send_ping(self) -> None:
        control = self._control
        assert control is not None  # nosec B101 -- 内部不变量：接收环/心跳仅在 _open_session 成功后创建
        await control.send(Heartbeat(kind=HeartbeatKind.PING, timestamp=time.time()))

    # ================================================================== 帧处理

    def _handle_screen_frame(self, frame: ScreenFrame) -> None:
        self._sync_input_to_frame()
        try:
            decoded = self._decode_frame(frame)
        except Exception as exc:  # 解码异常面宽（PyAV/PIL）：记录并等关键帧恢复
            _LOG.warning("frame_decode_failed", seq=frame.seq, error=repr(exc))
            return
        if decoded is None:
            return  # H.264 前瞻缓冲未出帧（正常节流）
        try:
            image = self._to_image(decoded)
        except ConnectionError:
            raise
        except Exception as exc:  # image_factory（GUI 转换）异常不得炸接收环
            _LOG.warning("frame_image_build_failed", error=repr(exc))
            return
        self._total_frames += 1
        self._frames_in_window += 1
        self._bytes_in_window += len(frame.data)
        self._resolution = (frame.width, frame.height)
        self._dispatch_event(
            "frame_arrived", image, left=frame.origin_x, top=frame.origin_y
        )

    def _sync_input_to_frame(self) -> None:
        """A4：以帧到达间隔估计实测 fps，喂 coalescer 节拍（帧对齐）。"""
        now = self._clock()
        if self._last_frame_at is not None:
            gap = now - self._last_frame_at
            if gap > 0:
                inst = 1.0 / gap
                if self._measured_fps <= 0:
                    self._measured_fps = inst
                else:  # EMA：画面节流的偶发抖动不平摊进节拍
                    self._measured_fps = 0.7 * self._measured_fps + 0.3 * inst
                self._coalescer.sync_to_frame(_FpsSnapshot(fps=self._measured_fps))
        self._last_frame_at = now

    def _decode_frame(self, frame: ScreenFrame) -> object | None:
        """codec 分派解码：JPEG→PIL.Image；H.264→(H,W,3) RGB ndarray。"""
        if frame.codec is Codec.JPEG:
            return self._decode_jpeg(frame)
        return self._decode_h264(frame)

    @staticmethod
    def _decode_jpeg(frame: ScreenFrame) -> object:
        import io

        from PIL import Image

        return Image.open(io.BytesIO(frame.data)).convert("RGB")

    def _decode_h264(self, frame: ScreenFrame) -> object | None:
        import av

        if self._h264_decoder is None:
            self._h264_decoder = av.CodecContext.create("h264", "r")
        ctx = self._h264_decoder
        for video_frame in ctx.decode(av.Packet(frame.data)):
            arr: object = video_frame.to_ndarray(format="rgb24")
            return arr
        return None

    def _to_image(self, decoded: object) -> object:
        """统一为 (H,W,3) RGB 像素后交 image_factory（GUI 端做 Qt 转换）。"""
        arr: Any = decoded
        if not hasattr(arr, "shape"):  # PIL.Image（JPEG 路径）
            import numpy as np

            arr = np.asarray(arr, dtype=np.uint8)
        h, w = int(arr.shape[0]), int(arr.shape[1])
        return self._image_factory(w, h, arr)

    # ================================================================== 输入出批

    def _emit_batches(self, batch: list[InputEvent]) -> None:
        for ev in batch:
            self._emit_event(ev)

    def _emit_event(self, ev: InputEvent) -> None:
        control = self._control
        if control is None or not control.is_connected:
            return
        msg = _event_to_message(ev)
        if msg is not None:
            self._spawn(control.send(msg))

    async def _flush_loop(self) -> None:
        """输入节拍出批 + 指标秒级上报（节拍由画面帧流 sync + coalescer 决定）。"""
        while True:
            delay = self._coalescer.next_delay_s()
            try:
                await asyncio.sleep(delay if delay > 0 else 1.0 / 30.0)
            except asyncio.CancelledError:
                raise
            # 走查第九批：静置兜底——用户不再敲任何键时，按环节拍同样完成
            # 粘滞热键对账（建议 A 的周期形态；打字场景由 send_key 即时对账）。
            self._reconcile_sticky_keys()
            self._emit_batches(self._coalescer.poll())
            self._metrics_tick()

    def _reconcile_sticky_keys(self) -> None:
        """补发被操作系统吞掉的粘滞热键释放（走查第九批应急兜底）。

        按住超过 :data:`_STICKY_HELD_TIMEOUT_S` 仍未收到释放（Win 键场景：
        系统接管开始菜单后 Qt 收不到 keyReleaseEvent，释放永远不会到）→
        主动喂一条 KEY_RELEASE 解锁被控端，并落本地记账（防每个后续键重复
        补发）。用户真实释放若在其后到达（少数焦点路径仍会送达），多补一次
        释放对被控端幂等无害；记账已清，不会三次补发。
        """
        if not self._sticky_held:
            return
        now = self._clock()
        stale = [k for k, t in self._sticky_held.items() if now - t >= _STICKY_HELD_TIMEOUT_S]
        for key in stale:
            del self._sticky_held[key]
            _LOG.warning("sticky_key_release_injected", key=key)
            self._emit_batches(self._coalescer.feed_key(key, False))

    # ================================================================== 指标

    def _metrics_tick(self) -> None:
        now = self._clock()
        elapsed = now - self._window_started
        if elapsed < 1.0:
            return
        fps = self._frames_in_window / elapsed
        bandwidth_kbps = self._bytes_in_window * 8.0 / 1000.0 / elapsed
        self._frames_in_window = 0
        self._bytes_in_window = 0
        self._window_started = now
        self._dispatch_event(
            "metrics_updated",
            fps=round(fps, 1),
            latency_ms=self._last_latency_ms,
            bandwidth_kbps=round(bandwidth_kbps, 1),
            resolution=self._resolution,
            total_frames=self._total_frames,
        )

    # ================================================================== 重连

    def _start_reconnect_loop(self) -> None:
        """进入重连环：后台跑 ReconnectController，耗尽仍未成功 → 落定断开。

        守卫：无运行中事件循环（解释器/GC 拆环阶段的收尾回调）直接放弃
        ——此时 ``ensure_future`` 会抛 "no running event loop" 并留下从未
        被 await 的协程，属正常销毁路径而非链路死亡，无需重连。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._set_state("disconnected")
            return
        self._set_state("reconnecting")
        self._reconnect = ReconnectController(
            self._reconnect_attempt,
            max_attempts=self._settings.max_reconnect_attempts,
            delay=self._settings.reconnect_delay,
        )
        task = asyncio.ensure_future(self._run_reconnect_loop())
        self._reconnect_task = task
        self._tasks.add(task)
        task.add_done_callback(self._reconnect_done)

    async def _run_reconnect_loop(self) -> None:
        if self._reconnect is None:
            return
        try:
            ok = await self._reconnect.run()
        except _AUTH_CLASS_ERRORS as exc:
            # 环内认证/TLS 类失败（如网络恢复后口令仍错）：终止且不重试。
            # run() 只吞 OSError/TimeoutError，此类异常在此上抛；_teardown
            # 已在 attempt 开头执行过，落定只需清口令+置态。
            _LOG.warning("reconnect_abort", error=str(exc))
            self._record_ban(exc)
            await self._final_disconnect()
            return
        finally:
            self._reconnect_task = None
        if not ok and not self._user_disconnect:
            _LOG.warning("reconnect_exhausted_giveup", host=self._host)
            await self._final_disconnect()

    def _reconnect_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOG.error("reconnect_loop_crashed", error=repr(exc))

    def _on_link_dead(self, reason: str) -> None:
        """链路判死统一入口（EOF/心跳超时；幂等）。

        ``_tearing_down`` 守卫：拆会话时接收环 ``finally`` 必然走到这里
        （任务取消/通道 EOF），那不是链路死亡而是主动拆除——放行会引发
        重连风暴（每次 teardown 自触发一次 attempt）。
        """
        if self._tearing_down or self._user_disconnect or self._state != "connected":
            return
        _LOG.info("link_dead", reason=reason)
        if self._settings.auto_reconnect:
            self._start_reconnect_loop()
        else:
            self._spawn(self._final_disconnect())

    async def _final_disconnect(self) -> None:
        await self._teardown()
        self._password_holder = None
        self._set_state("disconnected")

    async def _reconnect_attempt(self) -> bool:
        """单次重连：拆旧会话 → 重建（口令仍持有则复用、不重复弹框）。

        Returns:
            ``True`` = 连接成功终止环；``False`` = 网络类失败，退避续试。
            认证/TLS 类失败**上抛**由 :meth:`_run_reconnect_loop` 捕获落定
            ——旧版在此 ``return True`` 会被 ReconnectController 记成
            ``reconnect_success``（走查第四批：日志明明失败却报成功，即此）。
        """
        await self._teardown()
        try:
            await self._open_session(self._host, self._port)
        except _AUTH_CLASS_ERRORS as exc:
            _LOG.warning("reconnect_auth_class_abort", error=str(exc))
            raise
        except OSError as exc:  # 含 builtins.ConnectionError（拒绝/重置）
            _LOG.warning("reconnect_network_failed", error=repr(exc))
            return False
        self._set_state("connected")
        return True

    async def _teardown(self) -> None:
        """幂等拆会话：停环 → 停心跳 → 关通道 → 复位输入/解码/帧节拍。

        ⚠️ 取消集**排除当前任务与重连环任务**：二者可能在调用栈上
        （attempt → _teardown），自我取消 = ``await`` 自身死锁或半拆会话；
        环任务由 :meth:`disconnect` / 循环自然收尾显式管理。

        守卫：无运行中事件循环（GC 拆协程 / 解释器退出期的收尾 await）直接
        返回——``current_task()``/``await task`` 此时必抛 "no running event
        loop"，且半拆状态随进程退出无害（同 :meth:`_start_reconnect_loop`
        口径，2026-10-05 走查第二批）。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._tearing_down = True
            return
        self._tearing_down = True
        current = asyncio.current_task()
        ring: list[asyncio.Task[Any]] = [
            t
            for t in (self._control_task, self._screen_task, self._flush_task)
            if t is not None and t is not current
        ]
        ring.extend(
            t
            for t in self._tasks
            if t is not current and t is not self._reconnect_task
        )
        for task in ring:
            task.cancel()
        for task in ring:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._control_task = self._screen_task = self._flush_task = None
        self._tasks.difference_update(ring)
        if self._reconnect is not None:
            self._reconnect.stop()
            self._reconnect = None
        if self._reconnect_task is not None and self._reconnect_task.done():
            self._reconnect_task = None
        if self._hb is not None:
            await self._hb.stop()
            self._hb = None
        for channel in (self._control, self._screen):
            if channel is not None:
                await channel.close()
        self._control = None
        self._screen = None
        self._coalescer.reset()
        # 走查第九批：会话已拆，本地粘滞热键记账一并作废（被控端复位由
        # 服务端 reset 链路负责；留着会在重连后补发陈旧 RELEASE）。
        self._sticky_held.clear()
        self._h264_decoder = None
        self._last_frame_at = None
        self._measured_fps = 0.0
        self._frames_in_window = 0
        self._bytes_in_window = 0
        self._tearing_down = False

    # ================================================================== 工具

    def _set_state(self, state: str) -> None:
        if self._state == state:
            return
        self._state = state
        self._dispatch_event("state_changed", state)

    def _dispatch_event(self, hook: str, *args: object, **kw: object) -> None:
        events = self._events
        if events is None:
            return
        fn = getattr(events, hook, None)
        if fn is None:
            return
        try:
            fn(*args, **kw)
        except Exception as exc:  # UI 回调异常不得炸会话环（记录，约束 6）
            _LOG.error("event_hook_failed", hook=hook, error=repr(exc))

    def _spawn(self, coro: Awaitable[object]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOG.warning("client_task_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# 模块级映射/纯函数
# ---------------------------------------------------------------------------


def _no_password(_host: str) -> str | None:
    raise AuthError("未提供 get_password 回调")


def _no_confirm(_fp: str, _host: str) -> bool:
    raise TlsError("未提供 confirm_fingerprint 回调")


#: input 层鼠标键 → 协议枚举（与 server/service 入站映射互逆）。
_BTN_TO_PROTO: dict[InputMouseButton | None, MouseButton] = {
    InputMouseButton.LEFT: MouseButton.LEFT,
    InputMouseButton.RIGHT: MouseButton.RIGHT,
    InputMouseButton.MIDDLE: MouseButton.MIDDLE,
    None: MouseButton.LEFT,
}


def _event_to_message(ev: InputEvent) -> Message | None:
    """合并事件 → 协议消息（坐标透传，时间戳 epoch 秒）。"""
    stamp = time.time()
    kind = ev.kind
    if kind is InputKind.MOVE:
        return MouseEvent(x=ev.x, y=ev.y, action=MouseAction.MOVE, timestamp=stamp)
    if kind in (InputKind.PRESS, InputKind.RELEASE):
        action = MouseAction.PRESS if kind is InputKind.PRESS else MouseAction.RELEASE
        button = _BTN_TO_PROTO.get(ev.button, MouseButton.LEFT)
        return MouseEvent(x=ev.x, y=ev.y, action=action, button=button, timestamp=stamp)
    if kind is InputKind.WHEEL:
        dx, dy = (ev.direction or WheelDirection.UP).axis_delta
        # 协议 scroll_delta 为垂直语义（服务端按 (0, delta) 注入）；
        # 水平滚轮罕见，非零时透传水平值（不丢事件）。
        return MouseEvent(
            x=ev.x,
            y=ev.y,
            action=MouseAction.SCROLL,
            button=MouseButton.LEFT,
            scroll_delta=dy if dy else dx,
            timestamp=stamp,
        )
    if kind in (InputKind.KEY_PRESS, InputKind.KEY_RELEASE):
        key_action = KeyAction.PRESS if kind is InputKind.KEY_PRESS else KeyAction.RELEASE
        return KeyEvent(action=key_action, key=ev.key, timestamp=stamp)
    if kind is InputKind.TEXT:
        return KeyEvent(action=KeyAction.TYPE, text=ev.text, timestamp=stamp)
    return None


#: Qt 键码 → v5 规范名。**值表与 rdc.input.keys._QT_FUNCTION 逐条同源**
#: （测试 test_client_controller_key_table_matches_keys 锁定）。此处内联
#: 数字表的原因：keys.py 导入 PySide6.QtCore，controller 有零 Qt 红线。
_QT_FUNCTION: dict[int, str] = {
    0x00000020: "space",
    0x01000000: "esc",
    0x01000001: "tab",
    0x01000002: "tab",
    0x01000003: "backspace",
    0x01000004: "enter",
    0x01000005: "enter",
    0x01000006: "insert",
    0x01000007: "delete",
    0x01000008: "pause",
    0x01000009: "printscreen",
    0x01000010: "home",
    0x01000011: "end",
    0x01000012: "left",
    0x01000013: "up",
    0x01000014: "right",
    0x01000015: "down",
    0x01000016: "pageup",
    0x01000017: "pagedown",
    0x01000020: "shift",
    0x01000021: "ctrl",
    0x01000022: "win",
    0x01000023: "alt",
    0x01000024: "capslock",
    0x01000025: "numlock",
    0x01000026: "scrolllock",
}
_QT_FUNCTION.update({0x01000030 + i: f"f{i + 1}" for i in range(24)})


def _qt_key_to_name(qt_key: int) -> str | None:
    """Qt 键码 → 规范键名（映射规则与 ``input/keys.qt_key_to_name`` 一致）。"""
    code = int(qt_key)
    name = _QT_FUNCTION.get(code)
    if name is not None:
        return normalize_key_name(name)
    if 0x21 <= code <= 0x7E:
        return chr(code).lower()
    if 0x30000000 <= code <= 0x30000009:
        return f"kp{code - 0x30000000}"
    return None

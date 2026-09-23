"""v5 领域消息（wire-format 无关的 dataclass，阶段 3，3.3）。

上层（server/client 业务）只见本模块的 dataclass；Protobuf 生成类
（``rdc.protocol.pb.rdc_pb2``）只出现在 :mod:`rdc.protocol.codec` 内部。
字段与 ``proto/rdc.proto`` 一一对应；IntEnum 数值与 proto enum 逐一对齐
（由 ``tests/unit/test_protocol_messages.py`` 锁定）。

不变量（构造时即校验，违反抛 :class:`~rdc.exceptions.ProtocolError`）：

- ``MouseEvent``：``action`` 为 CLICK/PRESS/RELEASE/SCROLL 时 ``button`` 不得为 UNSPECIFIED
- ``KeyEvent``：``action`` 为 PRESS/RELEASE 时 ``key`` 不得为空；TYPE 时 ``text`` 不得为空
- ``ClipboardEvent``：``chunk_total >= 1`` 且 ``0 <= chunk_index < chunk_total``
- ``ScreenFrame``：``width/height`` 为正、``data`` 非空

坐标与时间戳的语义：``timestamp`` 为发送端 epoch 秒（float），x/y 为被控端
绝对屏幕坐标（阶段 7 输入层负责归一化与 DPI 换算，协议层只透传）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from rdc.exceptions import ProtocolError
from rdc.protocol import constants as C

__all__ = [
    "AuthRequest",
    "AuthResponse",
    "ClipboardEvent",
    "Codec",
    "Heartbeat",
    "HeartbeatKind",
    "KeyEvent",
    "KeyAction",
    "Message",
    "MouseButton",
    "MouseEvent",
    "MouseAction",
    "ModifierKey",
    "PayloadType",
    "ScreenFrame",
    "ScreenJoin",
    "StreamCmd",
    "StreamConfig",
    "StreamControl",
]

_NONCE_SIZE = 32


def _check_nonce(value: bytes, name: str) -> None:
    """nonce 字段语义校验：空串（未提供）或恰好 32 字节，其余拒绝。"""
    if value and len(value) != _NONCE_SIZE:
        raise ProtocolError(f"{name} 必须为 {_NONCE_SIZE} 字节或空（未提供），实际 {len(value)}")


class PayloadType(IntEnum):
    """载荷类型（与包头 Type 字节、proto PayloadType 一致）。"""

    SCREEN_FRAME = C.PKT_TYPE_SCREEN
    CONTROL = C.PKT_TYPE_CONTROL  # v5 即 MOUSE_EVENT（沿用旧值保持 wire 兼容窗口为零）
    HEARTBEAT = C.PKT_TYPE_HEARTBEAT
    FILE = C.PKT_TYPE_FILE
    CLIPBOARD_EVENT = C.PKT_TYPE_CLIPBOARD
    AUTH_REQUEST = C.PKT_TYPE_AUTH
    AUTH_RESPONSE = C.PKT_TYPE_AUTH_RESPONSE
    KEY_EVENT = C.PKT_TYPE_KEY_EVENT
    SESSION_JOIN = C.PKT_TYPE_SESSION_JOIN
    STREAM_CONTROL = C.PKT_TYPE_STREAM_CONTROL


class MouseAction(IntEnum):
    """鼠标动作。"""

    UNSPECIFIED = 0
    MOVE = 1
    PRESS = 2
    RELEASE = 3
    CLICK = 4
    SCROLL = 5
    DRAG = 6


class MouseButton(IntEnum):
    """鼠标按键。"""

    UNSPECIFIED = 0
    LEFT = 1
    RIGHT = 2
    MIDDLE = 3


class KeyAction(IntEnum):
    """键盘动作。"""

    UNSPECIFIED = 0
    PRESS = 1
    RELEASE = 2
    TYPE = 3


class ModifierKey(IntEnum):
    """修饰键。"""

    UNSPECIFIED = 0
    CTRL = 1
    SHIFT = 2
    ALT = 3
    WIN = 4
    ALTGR = 5


class Codec(IntEnum):
    """画面编码格式。"""

    UNSPECIFIED = 0
    JPEG = 1
    H264 = 2


class StreamCmd(IntEnum):
    """流控制命令。"""

    UNSPECIFIED = 0
    START = 1
    STOP = 2
    CONFIG = 3


class HeartbeatKind(IntEnum):
    """心跳类型。"""

    UNSPECIFIED = 0
    PING = 1
    PONG = 2


# ---------------------------------------------------------------------------
# 消息 dataclass（frozen：往返/哈希语义要求不可变，构造后不得再改）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthRequest:
    """认证请求（C→S）。

    ``client_nonce``：F1 方案 B 的客户端 HMAC nonce（32B 随机，TLS 通道内交换，
    每连接一次）。默认空字节兼容测试构造；真实握手由 transport 层生成并填充。
    """

    password: str
    client_name: str = ""
    protocol_version: int = C.PROTOCOL_VERSION
    client_nonce: bytes = b""

    def __post_init__(self) -> None:
        _check_nonce(self.client_nonce, "client_nonce")

    @property
    def payload_type(self) -> PayloadType:
        """返回 AUTH_REQUEST。"""
        return PayloadType.AUTH_REQUEST


@dataclass(frozen=True)
class AuthResponse:
    """认证响应（S→C）。

    ``server_nonce`` / ``connection_id``：F1 方案 B 的服务端 nonce 与连接标识
    （HKDF salt），均 32B 随机、每连接一次。
    """

    ok: bool
    token: str = ""
    reason: str = ""
    protocol_version: int = C.PROTOCOL_VERSION
    server_nonce: bytes = b""
    connection_id: bytes = b""

    def __post_init__(self) -> None:
        _check_nonce(self.server_nonce, "server_nonce")
        _check_nonce(self.connection_id, "connection_id")

    @property
    def payload_type(self) -> PayloadType:
        """返回 AUTH_RESPONSE。"""
        return PayloadType.AUTH_RESPONSE


@dataclass(frozen=True)
class ScreenJoin:
    """画面通道加入（C→S）：一次性令牌 + 画面通道独立 nonce + 连接标识回显。

    凭控制通道 ``AuthResponse`` 发放的会话令牌连接画面端口：
    ``connection_id`` 回显控制通道的连接标识（32B 每连接随机值，经 TLS 通道
    下发，即为令牌绑定证明），``client_nonce`` 与画面端口服务端的
    server nonce 一起派生画面通道独立 HMAC 密钥（与控制通道密钥不同）。
    """

    token: str
    client_nonce: bytes = b""
    protocol_version: int = C.PROTOCOL_VERSION
    connection_id: bytes = b""

    def __post_init__(self) -> None:
        if not self.token:
            raise ProtocolError("ScreenJoin.token 不能为空")
        _check_nonce(self.client_nonce, "client_nonce")
        _check_nonce(self.connection_id, "connection_id")

    @property
    def payload_type(self) -> PayloadType:
        """返回 SESSION_JOIN。"""
        return PayloadType.SESSION_JOIN


@dataclass(frozen=True)
class ScreenFrame:
    """一帧画面（S→C）。``data`` 为编码后的压缩字节。"""

    seq: int
    timestamp: float
    width: int
    height: int
    codec: Codec
    data: bytes
    keyframe: bool = False

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ProtocolError(f"画面尺寸非法: {self.width}x{self.height}")
        if not self.data:
            raise ProtocolError("画面帧 data 不能为空")
        if self.seq < 0:
            raise ProtocolError(f"帧序号不能为负: {self.seq}")

    @property
    def payload_type(self) -> PayloadType:
        """返回 SCREEN_FRAME。"""
        return PayloadType.SCREEN_FRAME


@dataclass(frozen=True)
class MouseEvent:
    """鼠标事件（C→S）。"""

    x: int
    y: int
    action: MouseAction
    button: MouseButton = MouseButton.UNSPECIFIED
    scroll_delta: int = 0
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        needs_button = self.action in (
            MouseAction.PRESS,
            MouseAction.RELEASE,
            MouseAction.CLICK,
            MouseAction.SCROLL,
        )
        if needs_button and self.button is MouseButton.UNSPECIFIED:
            raise ProtocolError(f"鼠标动作 {self.action.name} 必须携带按键")

    @property
    def payload_type(self) -> PayloadType:
        """返回 CONTROL（v5 wire 上即鼠标）。"""
        return PayloadType.CONTROL


@dataclass(frozen=True)
class KeyEvent:
    """键盘事件（C→S）。PRESS/RELEASE 带 key 名，TYPE 带文本。"""

    action: KeyAction
    key: str = ""
    text: str = ""
    modifiers: tuple[ModifierKey, ...] = ()
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.action in (KeyAction.PRESS, KeyAction.RELEASE) and not self.key:
            raise ProtocolError("键盘 PRESS/RELEASE 必须携带 key")
        if self.action is KeyAction.TYPE and not self.text:
            raise ProtocolError("键盘 TYPE 必须携带 text")

    @property
    def payload_type(self) -> PayloadType:
        """返回 KEY_EVENT。"""
        return PayloadType.KEY_EVENT


@dataclass(frozen=True)
class ClipboardEvent:
    """剪贴板分块事件（双向）。"""

    text: str
    chunk_index: int = 0
    chunk_total: int = 1
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.chunk_total < 1:
            raise ProtocolError(f"chunk_total 至少为 1: {self.chunk_total}")
        if not 0 <= self.chunk_index < self.chunk_total:
            raise ProtocolError(
                f"chunk_index {self.chunk_index} 超出 [0, {self.chunk_total})"
            )

    @property
    def payload_type(self) -> PayloadType:
        """返回 CLIPBOARD_EVENT。"""
        return PayloadType.CLIPBOARD_EVENT


@dataclass(frozen=True)
class StreamConfig:
    """流参数（0/False 表示"不修改该项"由调用方语义决定，见 proto 注释）。"""

    quality: int = 0
    max_fps: int = 0
    min_fps: int = 0
    enable_diff: bool = False
    enable_compression: bool = False


@dataclass(frozen=True)
class StreamControl:
    """流控制命令（C→S）。"""

    cmd: StreamCmd
    config: StreamConfig | None = None

    def __post_init__(self) -> None:
        if self.cmd is StreamCmd.CONFIG and self.config is None:
            raise ProtocolError("StreamControl: cmd=CONFIG 必须携带 config")

    @property
    def payload_type(self) -> PayloadType:
        """返回 STREAM_CONTROL。"""
        return PayloadType.STREAM_CONTROL


@dataclass(frozen=True)
class Heartbeat:
    """心跳（双向）。"""

    kind: HeartbeatKind
    timestamp: float = 0.0

    @property
    def payload_type(self) -> PayloadType:
        """返回 HEARTBEAT。"""
        return PayloadType.HEARTBEAT


#: 所有领域消息的联合类型（codec / 上层分发用）
Message = (
    AuthRequest
    | AuthResponse
    | ScreenJoin
    | ScreenFrame
    | MouseEvent
    | KeyEvent
    | ClipboardEvent
    | StreamControl
    | Heartbeat
)

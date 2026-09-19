"""
全局常量定义
"""

# 协议常量
PROTOCOL_MAGIC = b"RDPC"  # Remote Desktop Protocol Control
PROTOCOL_VERSION = 1

# 数据包类型
PKT_TYPE_SCREEN = 0x01
PKT_TYPE_CONTROL = 0x02
PKT_TYPE_HEARTBEAT = 0x03
PKT_TYPE_FILE = 0x04
PKT_TYPE_CLIPBOARD = 0x05
PKT_TYPE_AUTH = 0x10
PKT_TYPE_AUTH_RESPONSE = 0x11
PKT_TYPE_STREAM_CONTROL = 0x20

# 数据包标志
PKT_FLAG_NONE = 0x00
PKT_FLAG_COMPRESSED = 0x01
PKT_FLAG_ENCRYPTED = 0x02
PKT_FLAG_HAS_CRC = 0x04

# 默认端口
DEFAULT_PORT = 9999
DEFAULT_CONTROL_PORT = 9998  # 控制通道独立端口

# 端口范围
PORT_MIN = 1024
PORT_MAX = 65535

# 屏幕参数
DEFAULT_JPEG_QUALITY = 60
MIN_JPEG_QUALITY = 30
MAX_JPEG_QUALITY = 95
DEFAULT_MAX_FPS = 30
DEFAULT_MIN_FPS = 5
DIFF_BLOCK_SIZE = 32  # 差分块大小
DIFF_BLOCK_SIZE_MIN = 16
DIFF_BLOCK_SIZE_MAX = 128

# 网络参数
HEARTBEAT_INTERVAL = 3.0
HEARTBEAT_TIMEOUT = 10.0
RECV_BUFFER_SIZE = 65536
SEND_QUEUE_SIZE = 10
MAX_CONNECTIONS = 10
CONNECTION_RATE_LIMIT = 5  # 每秒最大连接数

# 单包大小上限（防止恶意/损坏的长度字段耗尽内存）
MAX_PACKET_SIZE = 32 * 1024 * 1024  # 32MB（支持4K高画质）

# 加密常量（v4.0.0：TLS强制开启，证书自动生成）
TLS_CERT_FILE = "cert.pem"
TLS_KEY_FILE = "key.pem"

# ===== Handshake V4 协议常量 =====
HANDSHAKE_MAGIC = b"RDPC-HS"
HANDSHAKE_HEADER_LEN = 4  # 4字节大端长度前缀

# 文件传输常量
FILE_CHUNK_SIZE = 64 * 1024  # 64KB
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GB

# 认证常量
AUTH_TIMEOUT = 10  # 认证超时秒数
MAX_AUTH_ATTEMPTS = 3  # 最大认证尝试次数

# ===== 流控制常量 =====
STREAM_CMD_START = b"START"
STREAM_CMD_STOP = b"STOP"
STREAM_CMD_CONFIG = b"CONFIG"

# 按键映射（pyautogui格式）
KEY_MAP = {
    # 功能键
    'F1': 'f1', 'F2': 'f2', 'F3': 'f3', 'F4': 'f4',
    'F5': 'f5', 'F6': 'f6', 'F7': 'f7', 'F8': 'f8',
    'F9': 'f9', 'F10': 'f10', 'F11': 'f11', 'F12': 'f12',
    # 方向键
    'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
    # 编辑键
    'Return': 'enter', 'BackSpace': 'backspace', 'Delete': 'delete',
    'Tab': 'tab', 'Escape': 'esc', 'space': 'space',
    'Insert': 'insert', 'Home': 'home', 'End': 'end',
    'Page_Up': 'pageup', 'Page_Down': 'pagedown',
    # 修饰键
    'Control_L': 'ctrl', 'Control_R': 'ctrl',
    'Shift_L': 'shift', 'Shift_R': 'shift',
    'Alt_L': 'alt', 'Alt_R': 'alt',
    'Super_L': 'win', 'Super_R': 'win',
    'AltGr': 'altgr',
}

# 修饰键集合
MODIFIER_KEYS = {
    'Control_L': 'ctrl', 'Control_R': 'ctrl',
    'Shift_L': 'shift', 'Shift_R': 'shift',
    'Alt_L': 'alt', 'Alt_R': 'alt',
    'Super_L': 'win', 'Super_R': 'win',
}
"""
自定义异常类
"""


class RemoteDesktopError(Exception):
    """基础异常类"""
    pass


class ConnectionError(RemoteDesktopError):
    """连接相关错误"""
    pass


class ProtocolError(RemoteDesktopError):
    """协议解析错误"""
    pass


class HmacVerificationError(ProtocolError):
    """帧完整性校验失败（阶段 3 新增：HMAC-SHA256 trailer 不匹配）。

    继承 :class:`ProtocolError`，使传输层可用统一异常面处理"帧不可信"。
    """
    pass


class TlsError(RemoteDesktopError):
    """TLS 层错误（阶段 4 新增）：握手失败、证书校验被拒、TOFU 用户拒绝等。

    继承 :class:`RemoteDesktopError` 而非内置 ``ssl`` 异常，便于上层用
    项目统一异常面处理连接安全类失败。
    """
    pass


class ScreenCaptureError(RemoteDesktopError):
    """屏幕捕获错误"""
    pass


class InputControlError(RemoteDesktopError):
    """输入控制错误"""
    pass


class PlatformUnsupportedError(RemoteDesktopError):
    """当前平台缺少可用实现（阶段 7 新增：输入后端空实现/依赖缺失）。"""
    pass


class ServiceError(RemoteDesktopError):
    """服务管理错误"""
    pass


class ConfigError(RemoteDesktopError):
    """配置错误"""
    pass


class AuthError(RemoteDesktopError):
    """认证错误"""
    pass


class BlacklistError(RemoteDesktopError):
    """IP黑名单错误"""
    pass


class SecretStoreError(RemoteDesktopError):
    """密钥存储错误（阶段 2 新增：SecretStore 各后端统一异常）"""
    pass
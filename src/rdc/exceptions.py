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


class AuthCancelledError(AuthError):
    """用户在口令/确认环节主动取消（阶段 9 走查第四批缺陷 1）。

    继承 :class:`AuthError`——语义上仍属"认证类失败、绝不重连重试"（缺陷 3
    口径）；单列子类供 UI 区分"用户放弃"（静默未连接）与"认证被拒"（红字
    展示原因），避免把主动取消误报成认证失败。
    """
    pass


class AuthBannedError(AuthError):
    """服务端处于封禁期拒绝认证（阶段 9 走查第四批缺陷 2）。

    ``remaining_seconds``：封禁剩余秒数（服务端向上取整回传）。客户端
    收到后缓存到期时刻，之后的连接尝试在弹口令框**前**即被拒绝，
    不再白输口令。继承 :class:`AuthError`——认证类失败绝不重连（缺陷 3）
    与 UI 红字展示（缺陷 1）语义天然保持。
    """

    def __init__(self, message: str, *, remaining_seconds: int = 0) -> None:
        super().__init__(message)
        self.remaining_seconds = remaining_seconds

    @staticmethod
    def format_message(remaining_seconds: float) -> str:
        """用户可读封禁文案：≥60 秒折算分钟（向上取整），否则按秒。"""
        secs = max(0, int(remaining_seconds))
        if secs >= 60:
            minutes = -(-secs // 60)  # 向上取整：300s→5分钟、181s→4分钟
            return f"已被服务端封禁，请约 {minutes} 分钟后重试"
        return f"已被服务端封禁，请约 {secs} 秒后重试"


class BlacklistError(RemoteDesktopError):
    """IP黑名单错误"""
    pass


class SecretStoreError(RemoteDesktopError):
    """密钥存储错误（阶段 2 新增：SecretStore 各后端统一异常）"""
    pass
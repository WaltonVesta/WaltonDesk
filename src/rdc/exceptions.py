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


class ScreenCaptureError(RemoteDesktopError):
    """屏幕捕获错误"""
    pass


class InputControlError(RemoteDesktopError):
    """输入控制错误"""
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
"""
服务模块
守护进程、自启动、PID管理
"""

from .daemon import RemoteService
from .autostart import AutostartManager
from .pid_manager import PIDManager

__all__ = ['RemoteService', 'AutostartManager', 'PIDManager']
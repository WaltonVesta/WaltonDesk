"""
系统操作工具
"""

import os
import subprocess
import sys

from rdc.logger import get_logger


class SystemHelper:
    """系统辅助工具"""
    
    def __init__(self):
        self.logger = get_logger()
        self._system = sys.platform
    
    def is_windows(self) -> bool:
        """是否为Windows"""
        return self._system == 'win32'
    
    def is_macos(self) -> bool:
        """是否为macOS"""
        return self._system == 'darwin'
    
    def is_linux(self) -> bool:
        """是否为Linux"""
        return self._system == 'linux'
    
    def lock_screen(self) -> bool:
        """锁屏"""
        try:
            if self.is_windows():
                import ctypes
                ctypes.windll.user32.LockWorkStation()
                return True
            elif self.is_macos():
                subprocess.run(['pmset', 'displaysleepnow'], check=True)
                return True
            elif self.is_linux():
                # 尝试gnome/unity
                subprocess.run(['gnome-screensaver-command', '-l'], check=True)
                return True
        except Exception as e:
            self.logger.error(f"锁屏失败: {e}")
        return False
    
    def restart(self) -> bool:
        """重启"""
        try:
            if self.is_windows():
                subprocess.run(['shutdown', '/r', '/t', '1'], check=True)
            elif self.is_macos():
                subprocess.run(['sudo', 'shutdown', '-r', 'now'], check=True)
            elif self.is_linux():
                subprocess.run(['sudo', 'reboot'], check=True)
            return True
        except Exception as e:
            self.logger.error(f"重启失败: {e}")
        return False
    
    def shutdown(self) -> bool:
        """关机"""
        try:
            if self.is_windows():
                subprocess.run(['shutdown', '/s', '/t', '1'], check=True)
            elif self.is_macos() or self.is_linux():
                subprocess.run(['sudo', 'shutdown', '-h', 'now'], check=True)
            return True
        except Exception as e:
            self.logger.error(f"关机失败: {e}")
        return False
    
    def get_hostname(self) -> str:
        """获取主机名"""
        return os.uname().nodename if hasattr(os, 'uname') else 'Unknown'
    
    def get_os_info(self) -> str:
        """获取操作系统信息"""
        if self.is_windows():
            import platform
            return f"Windows {platform.win32_ver()[0]}"
        elif self.is_macos():
            import platform
            return f"macOS {platform.mac_ver()[0]}"
        elif self.is_linux():
            try:
                with open('/etc/os-release') as f:
                    for line in f:
                        if line.startswith('PRETTY_NAME='):
                            return line.split('=')[1].strip().strip('"')
            except OSError:
                # 读取发行版信息失败不应影响主流程，降级为通用标识；记录以便排查。
                self.logger.debug("读取 /etc/os-release 失败，降级为 'Linux'", exc_info=True)
            return "Linux"
        return "Unknown"
    
    def is_admin(self) -> bool:
        """检查当前进程是否具有管理员 / root 权限。

        Returns:
            具有管理员权限返回 True；无权限或无法判定返回 False。
        """
        try:
            if self.is_windows():
                import ctypes
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            # os.geteuid 仅存在于 POSIX 平台，Windows 上无该属性，
            # 故用 getattr 动态取用，避免 mypy 在 win32 平台检查时报 attr-defined。
            geteuid = getattr(os, "geteuid", None)
            if geteuid is None:
                return False
            return bool(geteuid() == 0)
        except Exception:
            # 权限探测失败不阻断主流程，降级为"非管理员"并记录原因。
            self.logger.exception("管理员权限探测失败，按非管理员处理")
            return False
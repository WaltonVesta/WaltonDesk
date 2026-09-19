"""
系统操作工具
"""

import os
import sys
import subprocess
from typing import Optional

from logger import get_logger


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
            elif self.is_macos():
                subprocess.run(['sudo', 'shutdown', '-h', 'now'], check=True)
            elif self.is_linux():
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
                with open('/etc/os-release', 'r') as f:
                    for line in f:
                        if line.startswith('PRETTY_NAME='):
                            return line.split('=')[1].strip().strip('"')
            except:
                pass
            return "Linux"
        return "Unknown"
    
    def is_admin(self) -> bool:
        """检查是否管理员权限"""
        try:
            if self.is_windows():
                import ctypes
                return ctypes.windll.shell32.IsUserAnAdmin() != 0
            else:
                return os.geteuid() == 0
        except:
            return False
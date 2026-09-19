"""
开机自启动（跨平台）
Windows: 注册表
macOS: launchd
Linux: systemd
"""

import os
import platform
import sys
from pathlib import Path

from rdc.logger import get_logger


class AutostartManager:
    """开机自启动管理器"""
    
    def __init__(self, service_name: str = "RemoteControlService"):
        self.service_name = service_name
        self.logger = get_logger()
        self._script_path = os.path.abspath(sys.argv[0])
        self._python_path = sys.executable
    
    def install(self) -> bool:
        """安装开机自启动"""
        system = platform.system()
        try:
            if system == 'Windows':
                return self._install_windows()
            elif system == 'Darwin':
                return self._install_macos()
            elif system == 'Linux':
                return self._install_linux()
            else:
                self.logger.warning(f"不支持的系统: {system}")
                return False
        except Exception as e:
            self.logger.error(f"安装开机自启动失败: {e}")
            return False
    
    def uninstall(self) -> bool:
        """卸载开机自启动"""
        system = platform.system()
        try:
            if system == 'Windows':
                return self._uninstall_windows()
            elif system == 'Darwin':
                return self._uninstall_macos()
            elif system == 'Linux':
                return self._uninstall_linux()
            else:
                return False
        except Exception as e:
            self.logger.error(f"卸载开机自启动失败: {e}")
            return False
    
    def _install_windows(self) -> bool:
        """Windows注册表自启动"""
        try:
            import winreg
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_SET_VALUE) as regkey:
                # 使用pythonw.exe避免控制台窗口
                pythonw = self._python_path.replace('python.exe', 'pythonw.exe')
                if not pythonw.endswith('pythonw.exe'):
                    pythonw = self._python_path
                cmd = f'"{pythonw}" "{self._script_path}" --service'
                winreg.SetValueEx(regkey, self.service_name, 0, winreg.REG_SZ, cmd)
            self.logger.info("Windows开机自启动已安装")
            return True
        except ImportError:
            self.logger.error("缺少winreg模块")
            return False
        except Exception as e:
            self.logger.error(f"Windows自启动安装失败: {e}")
            return False
    
    def _uninstall_windows(self) -> bool:
        """卸载Windows自启动"""
        try:
            import winreg
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_SET_VALUE) as regkey:
                try:
                    winreg.DeleteValue(regkey, self.service_name)
                    self.logger.info("Windows开机自启动已卸载")
                except FileNotFoundError:
                    pass
            return True
        except Exception as e:
            self.logger.error(f"Windows自启动卸载失败: {e}")
            return False
    
    def _install_macos(self) -> bool:
        """macOS launchd自启动"""
        try:
            home = Path.home()
            launch_dir = home / "Library" / "LaunchAgents"
            launch_dir.mkdir(parents=True, exist_ok=True)
            
            plist_file = launch_dir / f"{self.service_name}.plist"
            plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self.service_name}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{self._python_path}</string>
        <string>{self._script_path}</string>
        <string>--service</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>'''
            
            with open(plist_file, 'w') as f:
                f.write(plist_content)
            
            # 加载服务
            import subprocess
            subprocess.run(['launchctl', 'load', str(plist_file)], check=True)
            self.logger.info("macOS开机自启动已安装")
            return True
        except Exception as e:
            self.logger.error(f"macOS自启动安装失败: {e}")
            return False
    
    def _uninstall_macos(self) -> bool:
        """卸载macOS自启动"""
        try:
            plist_file = Path.home() / "Library" / "LaunchAgents" / f"{self.service_name}.plist"
            if plist_file.exists():
                import subprocess
                subprocess.run(['launchctl', 'unload', str(plist_file)], check=True)
                plist_file.unlink()
                self.logger.info("macOS开机自启动已卸载")
            return True
        except Exception as e:
            self.logger.error(f"macOS自启动卸载失败: {e}")
            return False
    
    def _install_linux(self) -> bool:
        """Linux systemd自启动"""
        try:
            user_dir = Path.home() / ".config" / "systemd" / "user"
            user_dir.mkdir(parents=True, exist_ok=True)
            
            service_file = user_dir / f"{self.service_name}.service"
            service_content = f'''[Unit]
Description={self.service_name}
After=network.target

[Service]
Type=simple
ExecStart={self._python_path} {self._script_path} --service
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target'''
            
            with open(service_file, 'w') as f:
                f.write(service_content)
            
            # 启用服务
            import subprocess
            subprocess.run(['systemctl', '--user', 'enable', str(service_file.name)], check=True)
            subprocess.run(['systemctl', '--user', 'start', str(service_file.name)], check=True)
            self.logger.info("Linux开机自启动已安装")
            return True
        except Exception as e:
            self.logger.error(f"Linux自启动安装失败: {e}")
            return False
    
    def _uninstall_linux(self) -> bool:
        """卸载Linux自启动"""
        try:
            service_file = Path.home() / ".config" / "systemd" / "user" / f"{self.service_name}.service"
            if service_file.exists():
                import subprocess
                subprocess.run(['systemctl', '--user', 'stop', service_file.name], check=True)
                subprocess.run(['systemctl', '--user', 'disable', service_file.name], check=True)
                service_file.unlink()
                self.logger.info("Linux开机自启动已卸载")
            return True
        except Exception as e:
            self.logger.error(f"Linux自启动卸载失败: {e}")
            return False
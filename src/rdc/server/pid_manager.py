"""
PID文件管理（带超时清理）
"""

import os
import sys
from pathlib import Path

from rdc.config.paths import config_dir
from rdc.exceptions import ServiceError
from rdc.logging import get_logger


class PIDManager:
    """PID文件管理器"""
    
    def __init__(self, service_name: str):
        self.service_name = service_name
        # 阶段 9：路径收敛到 rdc.config.paths（原 v4 焊死 Path.home()，
        # 测试无法重定向会污染真实 HOME；set_override_root 后自动跟随 tmp 目录）
        self.pid_file = Path(config_dir()) / f"{service_name}.pid"
        self.logger = get_logger()
    
    def write(self) -> None:
        """写入PID"""
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.pid_file, 'w') as f:
                f.write(str(os.getpid()))
            self.logger.debug(f"PID写入: {os.getpid()}")
        except OSError as e:
            raise ServiceError(f"写入PID失败: {e}") from e
    
    def read(self) -> int | None:
        """读取PID"""
        try:
            if self.pid_file.exists():
                with open(self.pid_file) as f:
                    return int(f.read().strip())
        except (OSError, ValueError):
            pass
        return None
    
    def remove(self) -> None:
        """删除PID文件"""
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
                self.logger.debug("PID文件已删除")
        except OSError as e:
            self.logger.warning(f"删除PID文件失败: {e}")
    
    def is_running(self) -> bool:
        """检查服务是否运行"""
        pid = self.read()
        if pid is None:
            return False

        if sys.platform == 'win32':
            # 优先使用 psutil
            try:
                import psutil
                return psutil.pid_exists(pid)
            except ImportError:
                pass
            # psutil 回退：使用 Windows API 检查进程句柄
            return self._win32_pid_exists(pid)
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    @staticmethod
    def _win32_pid_exists(pid: int) -> bool:
        """Windows下使用ctypes OpenProcess检查PID是否存在（psutil回退）"""
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    
    def cleanup_stale(self) -> bool:
        """
        清理过期的PID文件
        
        Returns:
            bool: 是否清理了过期PID
        """
        if not self.pid_file.exists():
            return False
        
        pid = self.read()
        if pid is None:
            self.remove()
            return True
        
        try:
            if sys.platform == 'win32':
                # 优先 psutil，回退到 Win32 API
                try:
                    import psutil
                    if not psutil.pid_exists(pid):
                        self.remove()
                        return True
                except ImportError:
                    if not self._win32_pid_exists(pid):
                        self.remove()
                        return True
            else:
                os.kill(pid, 0)
        except OSError:
            self.remove()
            return True

        return False
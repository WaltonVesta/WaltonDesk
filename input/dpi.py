"""
DPI/高DPI坐标适配
支持Windows、macOS、Linux（X11/Wayland）
"""

import sys
import ctypes
from typing import Tuple

try:
    import objc
    from AppKit import NSScreen
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False

from logger import get_logger


class DPIAdapter:
    """DPI适配器"""
    
    def __init__(self):
        self.logger = get_logger()
        self.scale_factor = self._get_dpi_scale()
        self.logger.info(f"DPI缩放系数: {self.scale_factor}")
    
    def _get_dpi_scale(self) -> float:
        """获取DPI缩放系数"""
        try:
            if sys.platform == 'win32':
                return self._get_windows_scale()
            elif sys.platform == 'darwin':
                return self._get_macos_scale()
            else:
                return self._get_linux_scale()
        except Exception as e:
            self.logger.warning(f"获取DPI缩放失败，使用默认值1.0: {e}")
            return 1.0
    
    def _get_windows_scale(self) -> float:
        """Windows DPI"""
        try:
            # 设置进程DPI感知
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except AttributeError:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except AttributeError:
                    pass
            
            try:
                hwnd = ctypes.windll.user32.GetDesktopWindow()
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                return dpi / 96.0
            except AttributeError:
                # Windows 8.1及以下
                try:
                    DCX_WINDOW = 0x00000001
                    hdc = ctypes.windll.user32.GetDCEx(
                        ctypes.windll.user32.GetDesktopWindow(), 0, DCX_WINDOW
                    )
                    dpi_x = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
                    dpi_y = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)
                    ctypes.windll.user32.ReleaseDC(
                        ctypes.windll.user32.GetDesktopWindow(), hdc
                    )
                    return (dpi_x + dpi_y) / (2 * 96.0)
                except Exception:
                    return 1.0
        except Exception as e:
            self.logger.warning(f"获取Windows DPI失败: {e}")
            return 1.0
    
    def _get_macos_scale(self) -> float:
        """macOS Retina缩放"""
        try:
            if HAS_APPKIT:
                scale = NSScreen.mainScreen().backingScaleFactor()
                return float(scale)
        except Exception as e:
            self.logger.warning(f"获取macOS DPI失败: {e}")
        return 1.0
    
    def _get_linux_scale(self) -> float:
        """Linux DPI（支持X11和Wayland）"""
        # 1. 检查环境变量
        for env_var in ['GDK_SCALE', 'QT_SCALE_FACTOR', 'GDK_DPI_SCALE']:
            scale_str = __import__('os').environ.get(env_var, '')
            if scale_str:
                try:
                    return float(scale_str)
                except ValueError:
                    pass
        
        # 2. 检查Wayland
        if __import__('os').environ.get('XDG_SESSION_TYPE') == 'wayland':
            try:
                import subprocess
                import json
                output = subprocess.check_output(['wlr-randr', '--json'], text=True)
                data = json.loads(output)
                for output in data.get('outputs', []):
                    mode = output.get('mode', {})
                    if mode:
                        w = mode.get('width', 0)
                        if w >= 3840:
                            return 2.0
                        elif w >= 2560:
                            return 1.5
            except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
                pass
            
            # Wayland fallback: 使用GDK_SCALE
            gdk_scale = __import__('os').environ.get('GDK_SCALE', '')
            if gdk_scale:
                try:
                    return float(gdk_scale)
                except ValueError:
                    pass
        
        # 3. X11 fallback (使用xrandr)
        try:
            import subprocess
            output = subprocess.check_output(['xrandr', '--query'], text=True)
            for line in output.split('\n'):
                if ' connected' in line and 'x' in line:
                    parts = line.split()
                    for part in parts:
                        if 'x' in part and '+' in part:
                            res = part.split('+')[0]
                            w_str, _ = res.split('x')
                            try:
                                w = int(w_str)
                                if w >= 3840:
                                    return 2.0
                                elif w >= 2560:
                                    return 1.5
                            except ValueError:
                                pass
                            break
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            pass
        
        # 4. 尝试从配置文件读取
        try:
            import os
            config_paths = [
                '/etc/environment',
                os.path.expanduser('~/.config/environment.d/'),
                os.path.expanduser('~/.pam_environment')
            ]
            for path in config_paths:
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        for line in f:
                            if 'GDK_SCALE=' in line or 'QT_SCALE_FACTOR=' in line:
                                val = line.strip().split('=')[-1]
                                try:
                                    return float(val)
                                except ValueError:
                                    pass
        except Exception:
            pass
        
        return 1.0
    
    def to_remote(self, canvas_x: int, canvas_y: int,
                  canvas_w: int, canvas_h: int,
                  remote_w: int, remote_h: int,
                  offset_x: int = 0, offset_y: int = 0) -> Tuple[int, int]:
        """将画布坐标转换为远程坐标"""
        # 应用偏移
        cx = canvas_x - offset_x
        cy = canvas_y - offset_y
        
        # 裁剪到画布范围
        cx = max(0, min(canvas_w - 1, cx)) if canvas_w > 0 else 0
        cy = max(0, min(canvas_h - 1, cy)) if canvas_h > 0 else 0
        
        if canvas_w <= 0 or canvas_h <= 0:
            return 0, 0
        
        # 计算缩放比例（保持宽高比）
        scale_x = remote_w / canvas_w
        scale_y = remote_h / canvas_h
        scale = min(scale_x, scale_y)
        
        # 计算显示区域偏移（居中）
        display_w = remote_w / scale
        display_h = remote_h / scale
        disp_offset_x = (canvas_w - display_w) / 2
        disp_offset_y = (canvas_h - display_h) / 2
        
        # 转换坐标
        remote_x = (cx - disp_offset_x) * scale
        remote_y = (cy - disp_offset_y) * scale
        
        # 边界检查
        remote_x = max(0, min(remote_w - 1, int(round(remote_x))))
        remote_y = max(0, min(remote_h - 1, int(round(remote_y))))
        
        return remote_x, remote_y
    
    def get_scale_factor(self) -> float:
        """获取缩放系数"""
        return self.scale_factor
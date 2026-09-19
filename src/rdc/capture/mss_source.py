"""
屏幕截图模块
支持跨平台（mss），多显示器，高DPI适配
"""


try:
    import mss
    import mss.tools
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

from typing import Any

from PIL import Image

from rdc.exceptions import ScreenCaptureError
from rdc.logger import get_logger

# mss 句柄为可选依赖：HAS_MSS 为假时保持 None。
# 显式标注为 Any 而非留空推断——若让 mypy 推断为 None，
# 则 `if HAS_MSS and self._mss` 恒为假，_capture_mss 等分支会被判为不可达。


class ScreenCapturer:
    """屏幕捕获器"""
    
    def __init__(self, monitor: int = 1):
        """
        Args:
            monitor: 显示器编号（1开始）
        """
        self.monitor = monitor
        self._mss: Any = None
        self._last_size: tuple[int, int] | None = None
        self.logger = get_logger()
        self._init_capturer()
    
    def _init_capturer(self) -> None:
        """初始化捕获器"""
        if HAS_MSS:
            self._mss = mss.mss()
        else:
            self.logger.warning("mss未安装，尝试使用PIL.ImageGrab")
    
    def capture(self) -> Image.Image:
        """捕获屏幕"""
        try:
            if HAS_MSS and self._mss:
                return self._capture_mss()
            else:
                return self._capture_pil()
        except Exception as e:
            self.logger.error(f"截图失败: {e}")
            raise ScreenCaptureError(f"截图失败: {e}") from e
    
    def _capture_mss(self) -> Image.Image:
        """使用mss截图"""
        monitor = self._mss.monitors[self.monitor]
        # 获取屏幕区域
        bbox = {
            'left': monitor['left'],
            'top': monitor['top'],
            'width': monitor['width'],
            'height': monitor['height']
        }
        screenshot = self._mss.grab(bbox)
        # 转换为PIL Image
        return Image.frombytes('RGB', screenshot.size, screenshot.rgb)
    
    def _capture_pil(self) -> Image.Image:
        """使用PIL ImageGrab截图（fallback）"""
        try:
            from PIL import ImageGrab
            return ImageGrab.grab()
        except ImportError as e:
            raise ScreenCaptureError("无法截图：PIL.ImageGrab不可用") from e
    
    def get_screen_size(self) -> tuple[int, int]:
        """获取屏幕尺寸"""
        try:
            if HAS_MSS and self._mss:
                monitor = self._mss.monitors[self.monitor]
                return monitor['width'], monitor['height']
            else:
                img = self.capture()
                return img.width, img.height
        except Exception as e:
            self.logger.error(f"获取屏幕尺寸失败: {e}")
            return 1920, 1080
    
    def get_monitors(self) -> list[dict]:
        """获取所有显示器信息"""
        if HAS_MSS and self._mss:
            return self._mss.monitors[1:]  # 跳过虚拟显示器
        return [{'width': 1920, 'height': 1080, 'left': 0, 'top': 0}]
    
    def set_monitor(self, monitor: int) -> None:
        """切换显示器"""
        if monitor >= 1:
            self.monitor = monitor
            self.logger.info(f"切换到显示器 {monitor}")
    
    def close(self) -> None:
        """释放资源"""
        if self._mss:
            self._mss.close()
            self._mss = None
"""
鼠标控制
统一使用pynput监听，坐标DPI适配
"""

import pyautogui
from pynput.mouse import Button, Listener
from typing import Optional, Tuple, Callable

from .dpi import DPIAdapter
from logger import get_logger


class MouseController:
    """鼠标控制器"""
    
    def __init__(self, dpi_adapter: DPIAdapter):
        self.dpi = dpi_adapter
        self.listener: Optional[Listener] = None
        self._enabled = False
        self._on_event: Optional[Callable] = None
        self._canvas_bounds: Optional[Tuple[int, int, int, int]] = None  # x, y, w, h
        self.logger = get_logger()
        
        # 按钮映射
        self.button_map = {
            Button.left: 'left',
            Button.right: 'right',
            Button.middle: 'middle'
        }
    
    def start(self, on_event: Callable) -> None:
        """启动鼠标监听"""
        if self._enabled:
            return
        self._enabled = True
        self._on_event = on_event
        self.listener = Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll
        )
        self.listener.start()
        self.logger.info("鼠标监听已启动")
    
    def stop(self) -> None:
        """停止鼠标监听"""
        self._enabled = False
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.logger.info("鼠标监听已停止")
    
    def set_canvas_bounds(self, x: int, y: int, w: int, h: int) -> None:
        """设置画布边界（用于过滤鼠标事件）"""
        self._canvas_bounds = (x, y, w, h)
    
    def _is_within_canvas(self, x: int, y: int) -> bool:
        """检查鼠标是否在画布内"""
        if self._canvas_bounds is None:
            return True
        cx, cy, cw, ch = self._canvas_bounds
        return cx <= x <= cx + cw and cy <= y <= cy + ch
    
    def _on_move(self, x: int, y: int) -> None:
        """鼠标移动"""
        if not self._enabled or not self._on_event:
            return
        if not self._is_within_canvas(x, y):
            return
        # 坐标转换为远程坐标
        self._on_event({
            'type': 'mouse_move',
            'x': x,
            'y': y
        })
    
    def _on_click(self, x: int, y: int, button: Button, pressed: bool) -> None:
        """鼠标点击"""
        if not self._enabled or not self._on_event:
            return
        if not self._is_within_canvas(x, y):
            return
        btn = self.button_map.get(button, 'left')
        action = 'press' if pressed else 'release'
        self._on_event({
            'type': 'mouse_click',
            'x': x,
            'y': y,
            'button': btn,
            'action': action,
            'pressed': pressed
        })
    
    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """鼠标滚轮"""
        if not self._enabled or not self._on_event:
            return
        if not self._is_within_canvas(x, y):
            return
        self._on_event({
            'type': 'mouse_scroll',
            'x': x,
            'y': y,
            'dx': dx,
            'dy': dy
        })
    
    def execute_event(self, event: dict) -> None:
        """执行鼠标事件"""
        try:
            event_type = event.get('type', '')
            
            if event_type == 'mouse_move':
                x = event.get('x', 0)
                y = event.get('y', 0)
                pyautogui.moveTo(x, y)
            
            elif event_type == 'mouse_click':
                x = event.get('x', 0)
                y = event.get('y', 0)
                button = event.get('button', 'left')
                action = event.get('action', 'click')
                if action == 'press':
                    pyautogui.mouseDown(x, y, button=button)
                elif action == 'release':
                    pyautogui.mouseUp(button=button)
                elif action == 'click':
                    pyautogui.click(x, y, button=button)
            
            elif event_type == 'mouse_scroll':
                dx = event.get('dx', 0)
                dy = event.get('dy', 0)
                # Windows滚轮值较大，做归一化
                if dy != 0:
                    pyautogui.scroll(dy)
            
        except Exception as e:
            self.logger.error(f"执行鼠标事件失败: {e}")
    
    def is_running(self) -> bool:
        """检查是否运行中"""
        return self._enabled and self.listener is not None
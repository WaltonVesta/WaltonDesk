"""
键盘控制
完整按键映射，组合键支持
"""

import pyautogui
from pynput.keyboard import Key, KeyCode
from typing import Optional, List

from .modifiers import ModifierState
from constants import KEY_MAP
from logger import get_logger


class KeyboardController:
    """键盘控制器"""
    
    def __init__(self):
        self.modifiers = ModifierState()
        self.logger = get_logger()
        self._last_keys: List[str] = []
        self._debounce_time = 0.05  # 防抖时间
        self._last_send_time = 0
    
    def on_press(self, key) -> Optional[dict]:
        """
        处理按键按下
        
        Returns:
            dict: 事件数据，或None（忽略）
        """
        keysym = self._get_keysym(key)
        if keysym is None:
            return None
        
        # 更新修饰键状态
        if self.modifiers.is_modifier(keysym):
            self.modifiers.press(keysym)
            return None  # 修饰键不单独发送
        
        # 翻译按键
        translated = self._translate_key(keysym)
        if translated is None:
            return None
        
        # 检查是否重复（防抖）
        import time
        now = time.time()
        if now - self._last_send_time < self._debounce_time:
            return None
        self._last_send_time = now
        
        # 构建事件
        event = {
            'type': 'key_press',
            'key': translated,
            'modifiers': self.modifiers.get_pressed()
        }
        
        self.logger.debug(f"按键按下: {translated}, 修饰键: {self.modifiers.get_pressed()}")
        return event
    
    def on_release(self, key) -> Optional[dict]:
        """
        处理按键释放
        
        Returns:
            dict: 事件数据，或None（忽略）
        """
        keysym = self._get_keysym(key)
        if keysym is None:
            return None
        
        # 更新修饰键状态
        if self.modifiers.is_modifier(keysym):
            self.modifiers.release(keysym)
            return None
        
        # 翻译按键
        translated = self._translate_key(keysym)
        if translated is None:
            return None
        
        event = {
            'type': 'key_release',
            'key': translated
        }
        
        self.logger.debug(f"按键释放: {translated}")
        return event
    
    def _get_keysym(self, key) -> Optional[str]:
        """获取键名"""
        if isinstance(key, KeyCode):
            char = key.char
            if char is not None:
                return char
            return key.name
        elif isinstance(key, Key):
            return key.name
        return None
    
    def _translate_key(self, keysym: str) -> Optional[str]:
        """翻译为pyautogui格式"""
        # 检查映射表
        if keysym in KEY_MAP:
            return KEY_MAP[keysym]
        
        # 普通字符
        if len(keysym) == 1:
            return keysym.lower()
        
        # 其他
        return keysym.lower()
    
    def reset(self) -> None:
        """重置所有状态"""
        self.modifiers.reset()
    
    def execute_event(self, event: dict) -> None:
        """执行键盘事件"""
        try:
            action = event.get('type', '')
            key = event.get('key', '')
            modifiers = event.get('modifiers', [])
            
            if action == 'key_press':
                # 先按修饰键
                for mod in modifiers:
                    pyautogui.keyDown(mod)
                pyautogui.keyDown(key)
                # 释放修饰键（逆序）
                for mod in reversed(modifiers):
                    pyautogui.keyUp(mod)
            elif action == 'key_release':
                pyautogui.keyUp(key)
        except Exception as e:
            self.logger.error(f"执行键盘事件失败: {e}")
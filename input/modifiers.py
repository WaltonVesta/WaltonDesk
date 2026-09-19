"""
修饰键状态管理
"""

from typing import List, Set
import threading
from constants import MODIFIER_KEYS


class ModifierState:
    """修饰键状态管理（防残留）"""
    
    def __init__(self):
        self._pressed: Set[str] = set()
        self._lock = threading.Lock()
    
    def press(self, keysym: str) -> None:
        """按下修饰键"""
        if keysym in MODIFIER_KEYS:
            with self._lock:
                self._pressed.add(MODIFIER_KEYS[keysym])
    
    def release(self, keysym: str) -> None:
        """释放修饰键"""
        if keysym in MODIFIER_KEYS:
            with self._lock:
                self._pressed.discard(MODIFIER_KEYS[keysym])
    
    def get_pressed(self) -> List[str]:
        """获取当前按下的修饰键列表"""
        with self._lock:
            return list(self._pressed)
    
    def has_modifiers(self) -> bool:
        """是否有修饰键按下"""
        with self._lock:
            return len(self._pressed) > 0
    
    def reset(self) -> None:
        """重置所有状态"""
        with self._lock:
            self._pressed.clear()
    
    def is_modifier(self, keysym: str) -> bool:
        """检查是否为修饰键"""
        return keysym in MODIFIER_KEYS
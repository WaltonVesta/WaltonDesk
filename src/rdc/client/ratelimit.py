"""
速率限制器
用于限制连接频率
"""

import threading
import time
from collections import deque


class RateLimiter:
    """速率限制器"""
    
    def __init__(self, max_connections_per_second: int = 5, window_size: int = 1):
        """
        Args:
            max_connections_per_second: 每秒最大连接数
            window_size: 统计窗口大小（秒）
        """
        self.max_connections = max_connections_per_second
        self.window_size = window_size
        self._timestamps: deque = deque()
        self._lock = threading.Lock()
    
    def allow(self) -> bool:
        """检查是否允许新连接"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_size
            
            # 清理过期时间戳
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            
            # 检查是否超过限制
            if len(self._timestamps) < self.max_connections:
                self._timestamps.append(now)
                return True
            
            return False
    
    def get_connection_count(self) -> int:
        """获取当前窗口内的连接数"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_size
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return len(self._timestamps)
    
    def reset(self) -> None:
        """重置限制器"""
        with self._lock:
            self._timestamps.clear()
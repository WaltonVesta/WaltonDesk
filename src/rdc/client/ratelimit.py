"""速率限制器（v4 迁移件；阶段 9 批 4 补全注解，移出 mypy 宽松清单）。

滑动窗口：只统计 ``window_size`` 秒内的连接到达时间戳，超限拒绝。
唯一消费方 :class:`rdc.server.auth.HostAuth` 在单事件循环内调用（无竞争），
锁保留以兼容多线程调用面。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """速率限制器。"""

    def __init__(self, max_connections_per_second: int = 5, window_size: float = 1.0) -> None:
        """
        Args:
            max_connections_per_second: 窗口内最大连接数。
            window_size: 统计窗口大小（秒）。
        """
        self.max_connections = max_connections_per_second
        self.window_size = window_size
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """记录一次到达；滑动窗口内未超限返回 ``True``。"""
        with self._lock:
            now = time.time()
            self._prune(now)
            if len(self._timestamps) < self.max_connections:
                self._timestamps.append(now)
                return True
            return False

    def get_connection_count(self) -> int:
        """获取当前窗口内的连接数。"""
        with self._lock:
            self._prune(time.time())
            return len(self._timestamps)

    def reset(self) -> None:
        """重置限制器。"""
        with self._lock:
            self._timestamps.clear()

    def _prune(self, now: float) -> None:
        """丢弃滑出窗口的旧时间戳（调用方持锁）。"""
        cutoff = now - self.window_size
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

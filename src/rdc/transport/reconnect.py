"""
自动重连逻辑
"""

import threading
import time
from collections.abc import Callable

from rdc.logger import get_logger


class ReconnectManager:
    """自动重连管理器"""
    
    def __init__(self, connect_callback: Callable, 
                 max_attempts: int = 10,
                 delay: float = 2.0,
                 use_backoff: bool = True):
        """
        Args:
            connect_callback: 连接回调，返回bool表示是否成功
            max_attempts: 最大重试次数
            delay: 重试间隔（秒）
            use_backoff: 是否使用指数退避
        """
        self.connect_callback = connect_callback
        self.max_attempts = max_attempts
        self.delay = delay
        self.use_backoff = use_backoff
        self._attempts = 0
        self._stop = False
        self._running = False
        self._thread: threading.Thread | None = None
        self.logger = get_logger()
        self._last_result = False
        self._success_callback: Callable | None = None
        self._failure_callback: Callable | None = None
    
    def start(self, on_success: Callable | None = None, 
              on_failure: Callable | None = None) -> None:
        """开始重连"""
        if self._running:
            return
        
        self._stop = False
        self._attempts = 0
        self._running = True
        self._success_callback = on_success
        self._failure_callback = on_failure
        
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        
        self.logger.info(f"开始重连，最多{self.max_attempts}次，间隔{self.delay}秒")
    
    def stop(self) -> None:
        """停止重连"""
        self._stop = True
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self.logger.debug("重连已停止")
    
    def _loop(self) -> None:
        """重连循环"""
        while not self._stop and self._attempts < self.max_attempts:
            self._attempts += 1
            
            # 计算退避延迟
            if self.use_backoff:
                wait_time = min(self.delay * (2 ** (self._attempts - 1)), 60)
            else:
                wait_time = self.delay
            
            self.logger.info(f"重连尝试 {self._attempts}/{self.max_attempts}，等待 {wait_time:.1f}秒")
            
            try:
                result = self.connect_callback()
                if result:
                    self._last_result = True
                    self.logger.info("✅ 重连成功")
                    self._running = False
                    if self._success_callback:
                        self._success_callback()
                    return
                else:
                    self._last_result = False
            except Exception as e:
                self._last_result = False
                self.logger.warning(f"重连失败: {e}")
            
            if self._stop:
                break
            
            # 等待后重试
            time.sleep(wait_time)
        
        if self._attempts >= self.max_attempts:
            self.logger.warning("❌ 达到最大重试次数，停止重连")
            self._running = False
            if self._failure_callback:
                self._failure_callback()
    
    def reset(self) -> None:
        """重置重连计数"""
        self._attempts = 0
        self._last_result = False
    
    def is_running(self) -> bool:
        """检查是否正在重连"""
        return self._running
    
    def get_attempts(self) -> int:
        """获取当前尝试次数"""
        return self._attempts
    
    def get_last_result(self) -> bool:
        """获取最后一次连接结果"""
        return self._last_result
    
    def get_progress(self) -> float:
        """获取重连进度 (0-1)"""
        if self.max_attempts <= 0:
            return 0
        return min(1.0, self._attempts / self.max_attempts)
"""
心跳保活机制
"""

import threading
import time
from collections.abc import Callable

from rdc.constants import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
from rdc.logger import get_logger


class HeartbeatManager:
    """心跳管理器"""
    
    def __init__(self, send_callback: Callable, on_timeout: Callable):
        """
        Args:
            send_callback: 发送心跳的回调函数
            on_timeout: 超时回调
        """
        self.send_callback = send_callback
        self.on_timeout = on_timeout
        self._last_recv = time.time()
        self._last_send = 0.0
        self._stop = False
        self._alive = True
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.logger = get_logger()
        self._consecutive_failures = 0
        self._max_failures = 3
    
    def start(self) -> None:
        """启动心跳"""
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._alive = True
        self._last_recv = time.time()
        self._last_send = 0.0
        self._consecutive_failures = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.logger.debug("心跳已启动")
    
    def stop(self) -> None:
        """停止心跳"""
        self._stop = True
        thread = self._thread
        # v4.0.3 修复：on_timeout 回调运行在心跳线程内部，若在其中调用
        # stop()，join 自身线程会抛出 RuntimeError（cannot join current
        # thread），中断重连流程导致主控端卡死。自身线程跳过 join。
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self.logger.debug("心跳已停止")
    
    def _loop(self) -> None:
        """心跳循环"""
        while not self._stop and self._alive:
            now = time.time()
            
            # 发送心跳
            if now - self._last_send >= HEARTBEAT_INTERVAL:
                try:
                    self.send_callback()
                    self._last_send = now
                    self._consecutive_failures = 0
                except Exception as e:
                    self._consecutive_failures += 1
                    self.logger.warning(
                        f"发送心跳失败 ({self._consecutive_failures}/{self._max_failures}): {e}"
                    )
                    if self._consecutive_failures >= self._max_failures:
                        self.logger.error("心跳发送连续失败，断开连接")
                        self._alive = False
                        try:
                            self.on_timeout()
                        except Exception as e:
                            self.logger.error(f"心跳超时回调异常: {e}")
                        break
            
            # 检测超时
            if now - self._last_recv >= HEARTBEAT_TIMEOUT:
                self.logger.warning("心跳超时")
                self._alive = False
                try:
                    self.on_timeout()
                except Exception as e:
                    # v4.0.3：防止回调异常导致心跳线程崩溃且无人知晓
                    self.logger.error(f"心跳超时回调异常: {e}")
                break
            
            time.sleep(0.5)
    
    def on_pong(self) -> None:
        """收到心跳回应"""
        with self._lock:
            self._last_recv = time.time()
            self._alive = True
            self._consecutive_failures = 0
    
    def is_alive(self) -> bool:
        """检查连接是否存活"""
        return self._alive and not self._stop
    
    def reset(self) -> None:
        """重置心跳状态"""
        with self._lock:
            self._last_recv = time.time()
            self._alive = True
            self._consecutive_failures = 0
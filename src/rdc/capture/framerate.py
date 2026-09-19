"""
动态帧率控制器
"""

import time

from rdc.logger import get_logger


class FramerateController:
    """动态帧率控制器"""
    
    def __init__(self, min_fps: int = 5, max_fps: int = 30):
        self.min_fps = min_fps
        self.max_fps = max_fps
        # current_fps 会在 adjust() 中按丢包/延迟写入浮点值（见第 73-76 行），
        # 故标注为 float 而非沿用 max_fps 的 int 推断。
        self.current_fps: float = max_fps
        self._last_frame_time = 0.0
        self._frame_count = 0
        self._fps_measure_start = time.time()
        self._measured_fps = 0.0
        self._target_frame_interval = 1.0 / max_fps
        self.logger = get_logger()
    
    def wait_frame(self) -> float:
        """等待到下一帧，返回实际等待时间"""
        now = time.time()
        elapsed = now - self._last_frame_time
        
        # 计算目标间隔
        target_interval = 1.0 / self.current_fps
        
        if elapsed < target_interval:
            wait_time = target_interval - elapsed
            time.sleep(wait_time)
            total_wait = target_interval
        else:
            total_wait = elapsed
        
        self._last_frame_time = time.time()
        self._update_fps()
        
        return total_wait
    
    def _update_fps(self) -> None:
        """更新FPS统计"""
        self._frame_count += 1
        now = time.time()
        if now - self._fps_measure_start >= 1.0:
            self._measured_fps = self._frame_count / (now - self._fps_measure_start)
            self._frame_count = 0
            self._fps_measure_start = now
    
    def adjust_fps(self, network_latency: float, packet_loss: float) -> float:
        """
        根据网络状况调整帧率
        
        Args:
            network_latency: 网络延迟（秒）
            packet_loss: 丢包率（0-1）
        
        Returns:
            调整后的帧率
        """
        # 延迟高 -> 降低帧率
        if network_latency > 0.1:
            new_fps = self.max_fps * (1 - (network_latency - 0.1) / 0.5)
            new_fps = max(self.min_fps, min(self.max_fps, new_fps))
        else:
            new_fps = self.max_fps
        
        # 丢包 -> 降低帧率
        if packet_loss > 0.01:
            new_fps = new_fps * (1 - packet_loss * 2)
            new_fps = max(self.min_fps, new_fps)
        
        self.current_fps = new_fps
        return self.current_fps
    
    def set_fps(self, fps: int) -> None:
        """手动设置帧率"""
        self.current_fps = max(self.min_fps, min(self.max_fps, fps))
    
    def get_measured_fps(self) -> float:
        """获取实测帧率"""
        return self._measured_fps
    
    def get_target_fps(self) -> float:
        """获取目标帧率"""
        return self.current_fps
    
    def reset(self) -> None:
        """重置统计"""
        self._frame_count = 0
        self._fps_measure_start = time.time()
        self._measured_fps = 0.0
"""
动态JPEG编码器
根据网络状况调整质量/分辨率，支持带宽限制
"""

import io
import time
from collections import deque
from PIL import Image
from typing import Tuple, Optional
from logger import get_logger
from config import Config


def _check_h264_available():
    """检测 H.264 编码是否可用"""
    try:
        import cv2
        fourcc = cv2.VideoWriter_fourcc(*'H264')
        return True
    except Exception:
        return False


HAS_H264 = _check_h264_available()


class BandwidthProbe:
    """带宽探测器"""

    def __init__(self):
        self._probe_start = 0
        self._probe_bytes = 0
        self._estimated_bandwidth = 0
        self._samples = []

    def record_send(self, bytes_sent: int) -> None:
        """记录发送数据"""
        now = time.time()
        if self._probe_start == 0:
            self._probe_start = now
        self._probe_bytes += bytes_sent

        # 每1秒计算一次
        if now - self._probe_start >= 1.0:
            bps = self._probe_bytes / (now - self._probe_start)
            self._samples.append(bps)
            if len(self._samples) > 10:
                self._samples.pop(0)
            self._estimated_bandwidth = sum(self._samples) / len(self._samples)
            self._probe_bytes = 0
            self._probe_start = now

    def get_bandwidth(self) -> float:
        """获取估算带宽（字节/秒）"""
        return self._estimated_bandwidth


class JPEGEncoder:
    """JPEG编码器（动态质量 + 带宽限制 + 质量平滑）"""
    
    def __init__(self):
        self.config = Config()
        self.current_quality = self.config.get('jpeg_quality', 60)
        self.min_quality = self.config.get('min_quality', 30)
        self.max_quality = self.config.get('max_quality', 95)
        
        # 带宽限制
        self._bandwidth_limit = self.config.get('bandwidth_limit', 0)  # KB/s
        self._bandwidth_limit_bytes = self._bandwidth_limit * 1024 if self._bandwidth_limit > 0 else 0
        
        # 质量平滑
        self._quality_history = deque(maxlen=30)
        self._size_history = deque(maxlen=30)
        
        self.logger = get_logger()
        self._stats = {
            'avg_size': 0,
            'sample_count': 0,
            'last_size': 0,
            'total_bytes': 0,
            'total_frames': 0
        }
    
    def encode(self, img: Image.Image, quality: Optional[int] = None,
               use_h264: bool = False) -> Tuple[bytes, int, str]:
        """
        编码图像

        Args:
            img: PIL图像
            quality: 指定质量（None则使用当前动态质量）
            use_h264: 是否尝试使用H.264编码

        Returns:
            (数据, 质量, 编码格式)
        """
        if quality is None:
            quality = self.current_quality

        # 尝试使用 H.264
        if use_h264 and HAS_H264:
            try:
                data, q = self.encode_h264(img, quality)
                return data, q, 'h264'
            except Exception as e:
                self.logger.warning(f"H.264编码失败，降级到JPEG: {e}")

        # 回退到 JPEG
        data, q = self._encode_jpeg(img, quality)
        return data, q, 'jpeg'

    def _encode_jpeg(self, img: Image.Image, quality: int) -> Tuple[bytes, int]:
        """编码图像为JPEG"""
        # 确保质量在有效范围
        quality = max(self.min_quality, min(self.max_quality, quality))

        try:
            buffer = io.BytesIO()
            # 使用优化参数
            img.save(buffer, format='JPEG', quality=quality,
                    optimize=True, subsampling=0, progressive=False)
            data = buffer.getvalue()
            buffer.close()

            # 更新统计
            self._stats['last_size'] = len(data)
            self._stats['sample_count'] += 1
            self._stats['total_frames'] += 1
            self._stats['total_bytes'] += len(data)
            self._stats['avg_size'] = (
                self._stats['avg_size'] * (self._stats['sample_count'] - 1) + len(data)
            ) / self._stats['sample_count']

            # 记录历史
            self._quality_history.append(quality)
            self._size_history.append(len(data))

            return data, quality
        except Exception as e:
            self.logger.error(f"JPEG编码失败: {e}")
            raise

    def encode_h264(self, img: Image.Image, quality: int = 60) -> Tuple[bytes, int]:
        """使用 H.264 编码（需要 OpenCV）"""
        if not HAS_H264:
            return self._encode_jpeg(img, quality)

        import cv2
        import numpy as np

        # 转换为 OpenCV 格式
        img_np = np.array(img)
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        # 设置编码参数
        encode_param = [
            int(cv2.VIDEOWRITER_PROP_QUALITY),
            quality // 10,  # H.264 质量范围 0-10
        ]

        _, buffer = cv2.imencode('.h264', img_cv, encode_param)
        return buffer.tobytes(), quality
    
    def adjust_quality(self, target_size: int, current_fps: float, 
                       target_fps: float, bandwidth: float = 0) -> int:
        """
        根据目标大小和帧率调整质量
        
        Args:
            target_size: 目标包大小（字节）
            current_fps: 当前帧率
            target_fps: 目标帧率
            bandwidth: 当前可用带宽（字节/秒），0表示自动
        
        Returns:
            调整后的质量
        """
        # 1. 帧率调整
        if current_fps < target_fps * 0.7:
            self.current_quality = max(self.min_quality, self.current_quality - 5)
        elif current_fps > target_fps * 1.1 and self._stats['avg_size'] < target_size * 0.8:
            self.current_quality = min(self.max_quality, self.current_quality + 3)
        
        # 2. 带宽限制
        effective_bandwidth = bandwidth if bandwidth > 0 else self._bandwidth_limit_bytes
        if effective_bandwidth > 0 and self._stats['avg_size'] > 0:
            estimated_bps = self._stats['avg_size'] * current_fps
            if estimated_bps > effective_bandwidth:
                # 需要降低质量
                reduction_ratio = estimated_bps / effective_bandwidth
                new_quality = int(self.current_quality / reduction_ratio)
                self.current_quality = max(self.min_quality, new_quality)
            elif estimated_bps < effective_bandwidth * 0.5 and current_fps >= target_fps:
                # 带宽充足，可以适当提高质量
                if self.current_quality < self.max_quality:
                    self.current_quality = min(self.max_quality, self.current_quality + 2)
        
        # 3. 根据包大小微调
        if self._stats['avg_size'] > target_size * 1.2:
            self.current_quality = max(self.min_quality, self.current_quality - 2)
        elif self._stats['avg_size'] < target_size * 0.5 and current_fps >= target_fps:
            self.current_quality = min(self.max_quality, self.current_quality + 2)
        
        # 4. 质量平滑（使用移动平均）
        self._quality_history.append(self.current_quality)
        if len(self._quality_history) >= 3:
            avg_quality = sum(self._quality_history) / len(self._quality_history)
            self.current_quality = int(avg_quality)
        
        # 确保在范围内
        self.current_quality = max(self.min_quality, min(self.max_quality, self.current_quality))

        return self.current_quality

    def adjust_quality_by_bandwidth(self, probe: 'BandwidthProbe', target_fps: float) -> int:
        """根据带宽探测调整质量

        Args:
            probe: 带宽探测器
            target_fps: 目标帧率

        Returns:
            调整后的质量
        """
        bandwidth = probe.get_bandwidth()
        if bandwidth <= 0:
            return self.current_quality

        # 目标：每帧大小 = 带宽 / FPS
        target_frame_size = bandwidth / target_fps

        # 根据当前帧大小调整
        if self._stats['avg_size'] > 0:
            ratio = target_frame_size / self._stats['avg_size']
            if ratio < 0.8:
                self.current_quality = max(self.min_quality, int(self.current_quality * ratio))
            elif ratio > 1.2 and self.current_quality < self.max_quality:
                self.current_quality = min(self.max_quality, self.current_quality + 2)

        return self.current_quality

    def set_quality(self, quality: int) -> None:
        """手动设置质量"""
        self.current_quality = max(self.min_quality, min(self.max_quality, quality))
        self.config.set('jpeg_quality', self.current_quality)
        self.logger.info(f"JPEG质量设置为: {self.current_quality}")
    
    def set_bandwidth_limit(self, limit_kbps: int) -> None:
        """设置带宽限制（KB/s）"""
        self._bandwidth_limit = max(0, limit_kbps)
        self._bandwidth_limit_bytes = self._bandwidth_limit * 1024 if self._bandwidth_limit > 0 else 0
        self.logger.info(f"带宽限制设置为: {self._bandwidth_limit} KB/s")
    
    def get_stats(self) -> dict:
        """获取编码统计"""
        avg_quality = sum(self._quality_history) / len(self._quality_history) if self._quality_history else self.current_quality
        return {
            'current_quality': self.current_quality,
            'avg_quality': avg_quality,
            'avg_size': self._stats['avg_size'],
            'last_size': self._stats['last_size'],
            'sample_count': self._stats['sample_count'],
            'total_frames': self._stats['total_frames'],
            'total_bytes': self._stats['total_bytes'],
            'bandwidth_limit': self._bandwidth_limit,
            'quality_history_len': len(self._quality_history)
        }
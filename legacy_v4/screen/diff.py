"""
差分/增量截图
块级变化检测，自适应块大小
"""

import hashlib
from typing import List, Tuple, Optional
from PIL import Image
import numpy as np

from constants import DIFF_BLOCK_SIZE, DIFF_BLOCK_SIZE_MIN, DIFF_BLOCK_SIZE_MAX
from logger import get_logger


class ScreenDiffer:
    """屏幕差分检测器（自适应块大小）"""
    
    def __init__(self, block_size: int = DIFF_BLOCK_SIZE):
        self.block_size = block_size
        self._last_frame: Optional[np.ndarray] = None  # 直接存储numpy数组
        self._last_image: Optional[Image.Image] = None  # 保留原图用于渲染
        self._cols = 0
        self._rows = 0
        self._total_blocks = 0
        self._adaptive_block_size = True
        self._change_ratio_history = []
        self._max_history = 10
        self.logger = get_logger()
    
    def get_changed_blocks(self, img: Image.Image) -> Optional[List[Tuple[int, int]]]:
        """
        获取变化的块坐标
        
        Returns:
            None: 发送完整帧（变化太多）
            List[Tuple[int, int]]: 变化的块坐标列表
        """
        # 转换为RGB模式
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # 转换为numpy数组
        curr_arr = np.array(img)
        height, width = curr_arr.shape[:2]
        
        if self._last_frame is None:
            self._last_frame = curr_arr.copy()
            self._last_image = img.copy()
            return None  # 首帧发送完整
        
        # 自适应块大小
        if self._adaptive_block_size:
            self._adjust_block_size(curr_arr, self._last_frame)
        
        # 计算差异块
        changed = self._find_changed_blocks(curr_arr, self._last_frame, width, height)
        
        # 更新缓存
        self._last_frame = curr_arr.copy()
        self._last_image = img.copy()
        
        # 计算变化比例
        self._cols = (width + self.block_size - 1) // self.block_size
        self._rows = (height + self.block_size - 1) // self.block_size
        self._total_blocks = self._cols * self._rows
        
        # 记录变化比例
        change_ratio = len(changed) / self._total_blocks if self._total_blocks > 0 else 0
        self._change_ratio_history.append(change_ratio)
        if len(self._change_ratio_history) > self._max_history:
            self._change_ratio_history.pop(0)
        
        # 如果变化超过阈值，发送完整帧
        if change_ratio > 0.15:  # 15% 阈值
            self.logger.debug(f"变化块 {len(changed)}/{self._total_blocks} ({change_ratio:.1%})，发送完整帧")
            return None
        
        self.logger.debug(f"变化块: {len(changed)}/{self._total_blocks} ({change_ratio:.1%})")
        return changed
    
    def _find_changed_blocks(self, curr_arr: np.ndarray, last_arr: np.ndarray,
                              width: int, height: int) -> List[Tuple[int, int]]:
        """查找变化的块（使用numpy加速）"""
        changed = []
        block_size = self.block_size
        
        # 计算块数量
        cols = (width + block_size - 1) // block_size
        rows = (height + block_size - 1) // block_size
        
        for y in range(rows):
            y_start = y * block_size
            y_end = min(y_start + block_size, height)
            for x in range(cols):
                x_start = x * block_size
                x_end = min(x_start + block_size, width)
                
                # 提取块
                curr_block = curr_arr[y_start:y_end, x_start:x_end]
                last_block = last_arr[y_start:y_end, x_start:x_end]
                
                # 直接比较（比哈希快）
                if curr_block.shape != last_block.shape:
                    changed.append((x, y))
                elif not np.array_equal(curr_block, last_block):
                    changed.append((x, y))
        
        return changed
    
    def _adjust_block_size(self, curr_arr: np.ndarray, last_arr: np.ndarray) -> None:
        """根据变化程度自适应调整块大小"""
        # 计算整体差异比例
        if curr_arr.shape != last_arr.shape:
            return
        
        # 计算差异
        diff = np.abs(curr_arr.astype(np.int16) - last_arr.astype(np.int16))
        diff_ratio = np.sum(diff > 30) / diff.size if diff.size > 0 else 0
        
        # 调整块大小
        if diff_ratio > 0.08:
            # 变化大，用小块提高精度
            self.block_size = max(DIFF_BLOCK_SIZE_MIN, self.block_size - 4)
        elif diff_ratio < 0.01:
            # 变化小，用大块减少计算
            self.block_size = min(DIFF_BLOCK_SIZE_MAX, self.block_size + 4)
    
    def get_changed_region(self, img: Image.Image, changed_blocks: List[Tuple[int, int]]) -> Tuple[Image.Image, Tuple[int, int]]:
        """
        根据变化的块裁剪区域
        
        Returns:
            (裁剪后的图像, (原图位置x, 原图位置y))
        """
        if not changed_blocks:
            return img, (0, 0)
        
        # 计算变化区域边界
        min_x = min(x for x, _ in changed_blocks) * self.block_size
        max_x = max(x for x, _ in changed_blocks) * self.block_size + self.block_size
        min_y = min(y for _, y in changed_blocks) * self.block_size
        max_y = max(y for _, y in changed_blocks) * self.block_size + self.block_size
        
        # 边界检查
        width, height = img.size
        min_x = max(0, min_x)
        min_y = max(0, min_y)
        max_x = min(width, max_x)
        max_y = min(height, max_y)
        
        # 裁剪
        cropped = img.crop((min_x, min_y, max_x, max_y))
        return cropped, (min_x, min_y)
    
    def reset(self) -> None:
        """重置缓存"""
        self._last_frame = None
        self._last_image = None
        self._change_ratio_history = []
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        avg_change = sum(self._change_ratio_history) / len(self._change_ratio_history) if self._change_ratio_history else 0
        return {
            'cols': self._cols,
            'rows': self._rows,
            'total_blocks': self._total_blocks,
            'block_size': self.block_size,
            'adaptive_enabled': self._adaptive_block_size,
            'avg_change_ratio': avg_change,
            'history_count': len(self._change_ratio_history)
        }
    
    def set_adaptive(self, enabled: bool) -> None:
        """启用/禁用自适应块大小"""
        self._adaptive_block_size = enabled
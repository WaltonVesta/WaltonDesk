"""
状态栏
"""

import tkinter as tk
from tkinter import ttk
import time
from typing import Optional


class StatusBar(ttk.Frame):
    """状态栏"""
    
    def __init__(self, parent):
        super().__init__(parent)
        self._create_widgets()
        
        # FPS统计
        self._frame_count = 0
        self._fps_start = time.time()
        self._fps = 0
    
    def _create_widgets(self):
        """创建组件"""
        # 状态信息
        self.status_label = ttk.Label(self, text="状态: 未连接", font=("Arial", 8))
        self.status_label.pack(side=tk.LEFT, padx=5)
        
        # 分隔线
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 连接信息
        self.info_label = ttk.Label(self, text="💡 请输入IP地址", 
                                    font=("Arial", 8), foreground="gray")
        self.info_label.pack(side=tk.LEFT, padx=5)
        
        # FPS
        self.fps_label = ttk.Label(self, text="", font=("Arial", 8), foreground="gray")
        self.fps_label.pack(side=tk.RIGHT, padx=5)
        
        # 延迟
        self.latency_label = ttk.Label(self, text="", font=("Arial", 8), foreground="gray")
        self.latency_label.pack(side=tk.RIGHT, padx=5)
        
        # 画质
        self.quality_label = ttk.Label(self, text="", font=("Arial", 8), foreground="gray")
        self.quality_label.pack(side=tk.RIGHT, padx=5)
    
    def set_status(self, text: str, status_type: str = "info"):
        """设置状态"""
        colors = {
            "info": "gray",
            "connected": "green",
            "disconnected": "gray",
            "connecting": "orange",
            "error": "red",
            "control": "green",
            "reconnecting": "orange"
        }
        color = colors.get(status_type, "gray")
        self.status_label.config(text=f"状态: {text}", foreground=color)
    
    def set_info(self, text: str):
        """设置信息"""
        self.info_label.config(text=text)
    
    def update_fps(self):
        """更新FPS"""
        self._frame_count += 1
        now = time.time()
        if now - self._fps_start >= 1.0:
            self._fps = self._frame_count / (now - self._fps_start)
            self._frame_count = 0
            self._fps_start = now
            self.fps_label.config(text=f"{self._fps:.1f} FPS")
    
    def set_latency(self, latency_ms: float):
        """设置延迟"""
        self.latency_label.config(text=f"延迟: {latency_ms:.0f}ms")
    
    def set_quality(self, quality: int):
        """设置画质"""
        self.quality_label.config(text=f"画质: {quality}%")
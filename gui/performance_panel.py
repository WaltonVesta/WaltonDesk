"""
性能监控面板
显示实时FPS、延迟、带宽、画质等信息
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional


class PerformancePanel(ttk.LabelFrame):
    """性能监控面板"""
    
    def __init__(self, parent, controller):
        super().__init__(parent, text="📊 性能监控")
        self.controller = controller
        self._update_timer: Optional[str] = None
        self._running = False
        
        self._create_widgets()
        self._start_updating()
    
    def _create_widgets(self):
        """创建监控组件"""
        # 使用网格布局
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        
        # 第一行：FPS和延迟
        self.fps_label = ttk.Label(self, text="FPS: --", font=("Arial", 9))
        self.fps_label.grid(row=0, column=0, sticky="w", padx=10, pady=2)
        
        self.latency_label = ttk.Label(self, text="延迟: --ms", font=("Arial", 9))
        self.latency_label.grid(row=0, column=1, sticky="w", padx=10, pady=2)
        
        # 第二行：带宽和画质
        self.bandwidth_label = ttk.Label(self, text="带宽: --KB/s", font=("Arial", 9))
        self.bandwidth_label.grid(row=1, column=0, sticky="w", padx=10, pady=2)
        
        self.quality_label = ttk.Label(self, text="画质: --%", font=("Arial", 9))
        self.quality_label.grid(row=1, column=1, sticky="w", padx=10, pady=2)
        
        # 第三行：分辨率和帧数
        self.resolution_label = ttk.Label(self, text="分辨率: --x--", font=("Arial", 9))
        self.resolution_label.grid(row=2, column=0, sticky="w", padx=10, pady=2)
        
        self.frames_label = ttk.Label(self, text="总帧数: --", font=("Arial", 9))
        self.frames_label.grid(row=2, column=1, sticky="w", padx=10, pady=2)
        
        # 进度条（用于显示带宽使用率）
        self.bandwidth_bar = ttk.Progressbar(self, length=150, mode='determinate')
        self.bandwidth_bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
        
        self.bandwidth_bar_label = ttk.Label(self, text="带宽使用率: --%", font=("Arial", 8))
        self.bandwidth_bar_label.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10)
    
    def _start_updating(self):
        """开始更新"""
        self._running = True
        self._update()
    
    def _update(self):
        """更新数据"""
        if not self._running:
            return
        
        try:
            # 获取统计数据
            if hasattr(self.controller, 'get_stats'):
                stats = self.controller.get_stats()
            else:
                stats = {}
            
            # 更新显示
            fps = stats.get('fps', 0)
            self.fps_label.config(text=f"FPS: {fps:.1f}" if fps else "FPS: --")
            
            latency = stats.get('latency', 0)
            self.latency_label.config(text=f"延迟: {latency:.0f}ms" if latency else "延迟: --ms")
            
            bandwidth = stats.get('bandwidth', 0) / 1024  # 转换为KB/s
            self.bandwidth_label.config(text=f"带宽: {bandwidth:.1f}KB/s" if bandwidth else "带宽: --KB/s")
            
            quality = stats.get('quality', 0)
            self.quality_label.config(text=f"画质: {quality}%" if quality else "画质: --%")
            
            # 分辨率
            width = stats.get('width', 0)
            height = stats.get('height', 0)
            if width and height:
                self.resolution_label.config(text=f"分辨率: {width}x{height}")
            
            # 总帧数
            frames = stats.get('total_frames', 0)
            self.frames_label.config(text=f"总帧数: {frames}" if frames else "总帧数: --")
            
            # 带宽使用率
            bandwidth_limit = stats.get('bandwidth_limit', 0)
            if bandwidth_limit > 0 and bandwidth > 0:
                usage = min(100, (bandwidth / bandwidth_limit) * 100)
                self.bandwidth_bar['value'] = usage
                self.bandwidth_bar_label.config(text=f"带宽使用率: {usage:.0f}%")
                
                # 颜色变化
                if usage > 80:
                    self.bandwidth_bar['style'] = 'red.Horizontal.TProgressbar'
                elif usage > 60:
                    self.bandwidth_bar['style'] = 'yellow.Horizontal.TProgressbar'
                else:
                    self.bandwidth_bar['style'] = 'green.Horizontal.TProgressbar'
            else:
                self.bandwidth_bar['value'] = 0
                self.bandwidth_bar_label.config(text="带宽使用率: --")
            
        except Exception as e:
            # 静默处理错误
            pass
        
        # 每秒更新一次
        self._update_timer = self.after(1000, self._update)
    
    def stop(self):
        """停止更新"""
        self._running = False
        if self._update_timer:
            self.after_cancel(self._update_timer)
            self._update_timer = None
    
    def destroy(self):
        """销毁组件"""
        self.stop()
        super().destroy()
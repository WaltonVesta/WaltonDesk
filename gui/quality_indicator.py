"""
连接质量指示器（增强版）
显示：延迟、丢包、FPS、连接质量等级
"""

import tkinter as tk
from tkinter import ttk


class QualityIndicator(ttk.Frame):
    """连接质量指示器（增强版）"""

    def __init__(self, parent):
        super().__init__(parent)
        self._quality = "unknown"  # unknown / good / medium / poor / offline
        self._create_widgets()

    def _create_widgets(self):
        # 指示灯（圆点）
        self.indicator_frame = ttk.Frame(self)
        self.indicator_frame.pack(side=tk.LEFT, padx=2)

        # 安全获取背景色
        try:
            bg_color = self.master['bg']
        except (tk.TclError, KeyError):
            bg_color = 'SystemButtonFace'

        self.indicator = tk.Canvas(self.indicator_frame, width=14, height=14,
                                   highlightthickness=0, bg=bg_color)
        self.indicator.pack()
        self._draw_indicator("gray")

        # 状态标签
        self.label = ttk.Label(self, text="等待连接...", font=("Arial", 8))
        self.label.pack(side=tk.LEFT, padx=3)

        # 详细数据（小字）
        self.detail_label = ttk.Label(self, text="", font=("Arial", 7), foreground="gray")
        self.detail_label.pack(side=tk.LEFT, padx=3)

    def _draw_indicator(self, color, pulse=False):
        """绘制指示灯"""
        self.indicator.delete("dot")
        if pulse:
            # 脉冲动画效果：画两个圆
            self.indicator.create_oval(1, 1, 13, 13, fill=color, outline=color, tags="dot")
            self.indicator.create_oval(3, 3, 11, 11, fill="white", outline="", tags="pulse")
            self.indicator.after(300, lambda: self.indicator.delete("pulse"))
        else:
            self.indicator.create_oval(2, 2, 12, 12, fill=color, outline=color, tags="dot")

    def update_quality(self, latency: float, fps: float, packet_loss: float = 0,
                       is_connected: bool = True):
        """更新质量显示"""
        if not is_connected:
            self._quality = "offline"
            self._draw_indicator("gray")
            self.label.config(text="已断开", foreground="gray")
            self.detail_label.config(text="")
            return

        # 判断质量等级
        if latency < 50 and packet_loss < 0.01 and fps > 20:
            color = "#00cc66"  # 绿色
            status = "优秀"
            self._quality = "good"
        elif latency < 100 and packet_loss < 0.03 and fps > 15:
            color = "#66cc00"  # 黄绿色
            status = "良好"
            self._quality = "good"
        elif latency < 200 and packet_loss < 0.08 and fps > 10:
            color = "#ffaa00"  # 橙色
            status = "一般"
            self._quality = "medium"
        elif latency < 400 and packet_loss < 0.15:
            color = "#ff6600"  # 橙红
            status = "较差"
            self._quality = "poor"
        else:
            color = "#ff3333"  # 红色
            status = "极差"
            self._quality = "poor"

        self._draw_indicator(color)
        self.label.config(text=f"{status}",
                         foreground="green" if self._quality in ["good"] else
                                    "orange" if self._quality == "medium" else "red")
        self.detail_label.config(text=f"{latency:.0f}ms | {fps:.1f}fps")

    def get_quality(self) -> str:
        """获取当前质量等级"""
        return self._quality

    def set_offline(self):
        """设置为离线状态"""
        self._quality = "offline"
        self._draw_indicator("gray")
        self.label.config(text="已断开", foreground="gray")
        self.detail_label.config(text="")

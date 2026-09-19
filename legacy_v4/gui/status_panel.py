"""
被控端状态监控面板
显示：连接数、带宽、帧率、编码格式
"""

import tkinter as tk
from tkinter import ttk
import threading
import time


class StatusPanel(ttk.LabelFrame):
    """被控端状态面板"""

    def __init__(self, parent, service):
        super().__init__(parent, text="📊 服务状态")
        self.service = service
        self._running = True
        self._create_widgets()
        self._update_loop()

    def _create_widgets(self):
        self.status_label = ttk.Label(self, text="状态: 运行中", foreground="green")
        self.status_label.pack(anchor='w', padx=10, pady=2)

        self.connections_label = ttk.Label(self, text="连接数: 0")
        self.connections_label.pack(anchor='w', padx=10, pady=2)

        self.bandwidth_label = ttk.Label(self, text="带宽: 0 KB/s")
        self.bandwidth_label.pack(anchor='w', padx=10, pady=2)

        self.uptime_label = ttk.Label(self, text="运行时间: 00:00:00")
        self.uptime_label.pack(anchor='w', padx=10, pady=2)

        # ===== 新增：编码质量标签 =====
        self.quality_label = ttk.Label(self, text="编码质量: --")
        self.quality_label.pack(anchor='w', padx=10, pady=2)

        # v4.0.0：广播订阅者数
        self.subscriber_label = ttk.Label(self, text="订阅者: 0")
        self.subscriber_label.pack(anchor='w', padx=10, pady=2)

    def _update_loop(self):
        if not self._running:
            return

        stats = self.service.get_stats()
        self.connections_label.config(text=f"连接数: {stats.get('active_connections', 0)}")

        # 带宽计算
        bytes_sent = stats.get('bytes_sent', 0)
        uptime = stats.get('uptime', 0)
        if uptime > 0:
            kbps = (bytes_sent / uptime) / 1024
            self.bandwidth_label.config(text=f"带宽: {kbps:.1f} KB/s")

        # 运行时间
        if uptime > 0:
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            self.uptime_label.config(text=f"运行时间: {hours:02d}:{minutes:02d}:{seconds:02d}")

        # 状态
        if self.service.is_running():
            self.status_label.config(text="状态: 运行中", foreground="green")
        elif self.service.is_paused():
            self.status_label.config(text="状态: 已暂停", foreground="orange")
        else:
            self.status_label.config(text="状态: 已停止", foreground="red")

        # ===== 新增：编码统计（v4.0.0：来自ScreenBroadcaster） =====
        encoder_stats = stats.get('encoder_stats', {})
        if encoder_stats:
            quality = encoder_stats.get('current_quality', 0)
            avg_size = encoder_stats.get('avg_size', 0)
            frames = encoder_stats.get('frames_encoded', 0)
            self.quality_label.config(
                text=f"编码质量: {quality}% | 帧大小: {avg_size/1024:.0f}KB | 总帧: {frames}"
            )
        # v4.0.0：订阅者数
        subscriber_count = stats.get('subscriber_count', 0)
        self.subscriber_label.config(text=f"订阅者: {subscriber_count}")
        # ===== 编码统计结束 =====

        self.after(1000, self._update_loop)

    def stop(self):
        """停止更新"""
        self._running = False

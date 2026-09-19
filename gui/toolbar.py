"""
工具栏
添加设置入口、连接状态指示器
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional
from enum import Enum

from config import Config
from logger import get_logger


# v4.0.0：连接状态（与 main_window.ConnectionState 对应，避免循环导入）
class ConnState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


class Toolbar(ttk.Frame):
    """工具栏"""
    
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.config = Config()
        self.logger = get_logger()
        self._create_widgets()
        self._load_recent_hosts()
    
    def _create_widgets(self):
        """创建工具栏组件"""
        # IP输入
        ttk.Label(self, text="IP:", font=("Arial", 9)).pack(side=tk.LEFT, padx=3)
        
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(self, textvariable=self.ip_var, width=18, font=("Arial", 9))
        self.ip_entry.pack(side=tk.LEFT, padx=3)
        self.ip_entry.bind('<Return>', lambda e: self._on_connect())
        
        # 最近连接下拉
        self.recent_combo = ttk.Combobox(self, width=12, font=("Arial", 8))
        self.recent_combo.pack(side=tk.LEFT, padx=2)
        self.recent_combo.bind('<<ComboboxSelected>>', self._on_recent_selected)
        
        # 连接按钮
        self.connect_btn = ttk.Button(self, text="连接", command=self._on_connect, width=6)
        self.connect_btn.pack(side=tk.LEFT, padx=3)
        
        # 断开按钮
        self.disconnect_btn = ttk.Button(self, text="断开", command=self._on_disconnect, 
                                         state=tk.DISABLED, width=6)
        self.disconnect_btn.pack(side=tk.LEFT, padx=3)
        
        # 分隔线
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 控制按钮
        self.control_btn = ttk.Button(self, text="启用控制", command=self._on_toggle_control,
                                      state=tk.DISABLED, width=8)
        self.control_btn.pack(side=tk.LEFT, padx=3)
        
        # 控制指示器
        self.indicator = ttk.Label(self, text="●", foreground="gray", font=("Arial", 12))
        self.indicator.pack(side=tk.LEFT, padx=2)
        
        self.control_status = ttk.Label(self, text="未启用", foreground="gray", font=("Arial", 8))
        self.control_status.pack(side=tk.LEFT, padx=2)
        
        # 分隔线
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 画质设置
        ttk.Label(self, text="画质:", font=("Arial", 8)).pack(side=tk.LEFT, padx=3)
        self.quality_var = tk.IntVar(value=self.config.get('jpeg_quality', 60))
        self.quality_scale = ttk.Scale(self, from_=30, to=95, variable=self.quality_var,
                                       orient=tk.HORIZONTAL, length=80)
        self.quality_scale.pack(side=tk.LEFT, padx=3)
        self.quality_label = ttk.Label(self, text="60%", font=("Arial", 8))
        self.quality_label.pack(side=tk.LEFT, padx=2)
        self.quality_var.trace('w', self._on_quality_change)
        
        # 分隔线
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 全屏按钮
        self.fullscreen_btn = ttk.Button(self, text="⛶ 全屏", command=self._on_fullscreen, width=6)
        self.fullscreen_btn.pack(side=tk.LEFT, padx=3)
        
        # 设置按钮（新增）
        self.settings_btn = ttk.Button(self, text="⚙️", command=self._on_settings, width=3)
        self.settings_btn.pack(side=tk.LEFT, padx=3)
        
        # 分隔线
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 快捷键提示
        ttk.Label(self, text="F11=全屏", font=("Arial", 7), 
                  foreground="gray").pack(side=tk.LEFT, padx=2)
        
        # 状态标签（右侧）
        self.status_label = ttk.Label(self, text="状态: 未连接", font=("Arial", 8))
        self.status_label.pack(side=tk.RIGHT, padx=5)
    
    def _load_recent_hosts(self):
        """加载最近连接"""
        hosts = self.config.get_recent_hosts()
        self.recent_combo['values'] = hosts
        if hosts:
            self.recent_combo.set(hosts[0])
    
    def _on_recent_selected(self, event):
        """选择最近连接"""
        self.ip_var.set(self.recent_combo.get())
    
    def _on_connect(self):
        """连接按钮"""
        host = self.ip_var.get().strip()
        if not host:
            messagebox.showwarning("提示", "请输入IP地址")
            return
        self.controller.connect(host)
    
    def _on_disconnect(self):
        """断开按钮"""
        self.controller.disconnect()
    
    def _on_toggle_control(self):
        """切换控制"""
        self.controller.toggle_control()
    
    def _on_quality_change(self, *args):
        """画质变化"""
        quality = self.quality_var.get()
        self.quality_label.config(text=f"{quality}%")
        self.config.set('jpeg_quality', quality)
    
    def _on_fullscreen(self):
        """全屏切换"""
        self.controller.toggle_fullscreen()
    
    def _on_settings(self):
        """打开设置对话框（新增）"""
        try:
            from .settings_dialog import SettingsDialog
            SettingsDialog(self.controller)
        except ImportError as e:
            self.logger.error(f"打开设置对话框失败: {e}")
            messagebox.showerror("错误", "设置对话框未实现")
    
    def update_connect_state(self, connected: bool):
        """更新连接状态（兼容旧接口）"""
        if connected:
            self.connect_btn.config(text="已连", state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self.control_btn.config(state=tk.NORMAL)
            self.status_label.config(text="状态: 已连接", foreground="green")
        else:
            self.connect_btn.config(text="连接", state=tk.NORMAL)
            self.disconnect_btn.config(state=tk.DISABLED)
            self.control_btn.config(state=tk.DISABLED)
            self.indicator.config(foreground="gray")
            self.control_status.config(text="未启用", foreground="gray")
            self.control_btn.config(text="启用控制")
            self.status_label.config(text="状态: 未连接", foreground="gray")

    def update_connection_state(self, state):
        """v4.0.0：根据连接状态机枚举更新按钮状态"""
        # 兼容传入字符串或枚举
        state_val = state.value if hasattr(state, 'value') else str(state)

        if state_val in ("connecting", "authenticating"):
            self.connect_btn.config(text="连接中", state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.DISABLED)
            self.control_btn.config(state=tk.DISABLED)
            self.status_label.config(text=f"状态: {state_val}", foreground="orange")
        elif state_val == "connected":
            self.update_connect_state(True)
        elif state_val == "reconnecting":
            self.connect_btn.config(text="重连中", state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self.control_btn.config(state=tk.DISABLED)
            self.status_label.config(text="状态: 重连中", foreground="orange")
        else:
            # idle / disconnected
            self.update_connect_state(False)
    
    def update_control_state(self, enabled: bool):
        """更新控制状态"""
        if enabled:
            self.control_btn.config(text="禁用控制")
            self.indicator.config(foreground="green")
            self.control_status.config(text="控制中", foreground="green")
        else:
            self.control_btn.config(text="启用控制")
            self.indicator.config(foreground="gray")
            self.control_status.config(text="未启用", foreground="gray")
    
    def update_fullscreen_state(self, fullscreen: bool):
        """更新全屏状态"""
        if fullscreen:
            self.fullscreen_btn.config(text="⛶ 退出全屏")
        else:
            self.fullscreen_btn.config(text="⛶ 全屏")
    
    def get_host(self) -> str:
        """获取主机IP"""
        return self.ip_var.get().strip()
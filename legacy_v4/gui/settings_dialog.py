"""
设置对话框
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

from config import Config
from logger import get_logger


class SettingsDialog:
    """设置对话框"""
    
    def __init__(self, controller):
        self.controller = controller
        self.config = Config()
        self.logger = get_logger()
        
        self.dialog = tk.Toplevel()
        self.dialog.title("设置")
        self.dialog.geometry("550x500")
        self.dialog.resizable(False, False)
        self.dialog.transient(self.controller.root)
        self.dialog.grab_set()
        
        self._create_widgets()
        self._load_settings()
    
    def _create_widgets(self):
        """创建设置界面"""
        # 使用Notebook分页
        notebook = ttk.Notebook(self.dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 通用设置
        general_frame = ttk.Frame(notebook)
        notebook.add(general_frame, text="通用")
        self._create_general_tab(general_frame)
        
        # 网络设置
        network_frame = ttk.Frame(notebook)
        notebook.add(network_frame, text="网络")
        self._create_network_tab(network_frame)
        
        # 安全设置
        security_frame = ttk.Frame(notebook)
        notebook.add(security_frame, text="安全")
        self._create_security_tab(security_frame)
        
        # 按钮
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Button(btn_frame, text="保存", 
                  command=self._save_settings).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", 
                  command=self.dialog.destroy).pack(side=tk.RIGHT, padx=5)
    
    def _create_general_tab(self, parent):
        """通用设置"""
        # 画质
        frame1 = ttk.Frame(parent)
        frame1.pack(fill=tk.X, pady=5)
        ttk.Label(frame1, text="默认画质:").pack(side=tk.LEFT)
        self.quality_var = tk.IntVar()
        ttk.Scale(frame1, from_=20, to=95, variable=self.quality_var,
                 orient=tk.HORIZONTAL, length=200).pack(side=tk.LEFT, padx=10)
        self.quality_label = ttk.Label(frame1, text="60%", width=5)
        self.quality_label.pack(side=tk.LEFT)
        self.quality_var.trace('w', lambda *_: self.quality_label.config(
            text=f"{self.quality_var.get()}%"))
        
        # 最大帧率
        frame2 = ttk.Frame(parent)
        frame2.pack(fill=tk.X, pady=5)
        ttk.Label(frame2, text="最大帧率:").pack(side=tk.LEFT)
        self.fps_var = tk.IntVar()
        ttk.Combobox(frame2, textvariable=self.fps_var, 
                    values=[5, 10, 15, 20, 25, 30, 60], width=10).pack(side=tk.LEFT, padx=10)
        
        # 最小帧率
        frame3 = ttk.Frame(parent)
        frame3.pack(fill=tk.X, pady=5)
        ttk.Label(frame3, text="最小帧率:").pack(side=tk.LEFT)
        self.min_fps_var = tk.IntVar()
        ttk.Combobox(frame3, textvariable=self.min_fps_var, 
                    values=[1, 2, 3, 5, 8, 10], width=10).pack(side=tk.LEFT, padx=10)
        
        # 画质范围
        frame4 = ttk.Frame(parent)
        frame4.pack(fill=tk.X, pady=5)
        ttk.Label(frame4, text="画质范围:").pack(side=tk.LEFT)
        self.min_quality_var = tk.IntVar()
        ttk.Spinbox(frame4, from_=10, to=50, textvariable=self.min_quality_var, 
                   width=5).pack(side=tk.LEFT, padx=5)
        ttk.Label(frame4, text="-").pack(side=tk.LEFT)
        self.max_quality_var = tk.IntVar()
        ttk.Spinbox(frame4, from_=50, to=100, textvariable=self.max_quality_var,
                   width=5).pack(side=tk.LEFT, padx=5)
        
        # 差分检测
        frame5 = ttk.Frame(parent)
        frame5.pack(fill=tk.X, pady=5)
        self.diff_var = tk.BooleanVar()
        ttk.Checkbutton(frame5, text="启用差分检测 (减少带宽)", 
                       variable=self.diff_var).pack(side=tk.LEFT)
        
        # 压缩
        frame6 = ttk.Frame(parent)
        frame6.pack(fill=tk.X, pady=5)
        self.compression_var = tk.BooleanVar()
        ttk.Checkbutton(frame6, text="启用数据压缩", 
                       variable=self.compression_var).pack(side=tk.LEFT)
    
    def _create_network_tab(self, parent):
        """网络设置"""
        # 服务端口
        frame1 = ttk.Frame(parent)
        frame1.pack(fill=tk.X, pady=5)
        ttk.Label(frame1, text="服务端口:").pack(side=tk.LEFT)
        self.port_var = tk.IntVar()
        ttk.Spinbox(frame1, from_=1024, to=65535, 
                   textvariable=self.port_var, width=10).pack(side=tk.LEFT, padx=10)
        
        # 控制端口
        frame2 = ttk.Frame(parent)
        frame2.pack(fill=tk.X, pady=5)
        ttk.Label(frame2, text="控制端口:").pack(side=tk.LEFT)
        self.control_port_var = tk.IntVar()
        ttk.Spinbox(frame2, from_=1024, to=65535, 
                   textvariable=self.control_port_var, width=10).pack(side=tk.LEFT, padx=10)
        
        # 最大连接数
        frame3 = ttk.Frame(parent)
        frame3.pack(fill=tk.X, pady=5)
        ttk.Label(frame3, text="最大连接数:").pack(side=tk.LEFT)
        self.max_conn_var = tk.IntVar()
        ttk.Spinbox(frame3, from_=1, to=50, 
                   textvariable=self.max_conn_var, width=10).pack(side=tk.LEFT, padx=10)
        
        # 带宽限制
        frame4 = ttk.Frame(parent)
        frame4.pack(fill=tk.X, pady=5)
        ttk.Label(frame4, text="带宽限制 (KB/s):").pack(side=tk.LEFT)
        self.bandwidth_var = tk.IntVar()
        ttk.Spinbox(frame4, from_=0, to=10240, 
                   textvariable=self.bandwidth_var, width=10).pack(side=tk.LEFT, padx=10)
        ttk.Label(frame4, text="(0=无限制)", font=("Arial", 8), 
                 foreground="gray").pack(side=tk.LEFT, padx=5)
        
        # 自动重连
        frame5 = ttk.Frame(parent)
        frame5.pack(fill=tk.X, pady=5)
        self.reconnect_var = tk.BooleanVar()
        ttk.Checkbutton(frame5, text="启用自动重连", 
                       variable=self.reconnect_var).pack(side=tk.LEFT)
        
        # 重连延迟
        frame6 = ttk.Frame(parent)
        frame6.pack(fill=tk.X, pady=5)
        ttk.Label(frame6, text="重连延迟 (秒):").pack(side=tk.LEFT)
        self.reconnect_delay_var = tk.DoubleVar()
        ttk.Spinbox(frame6, from_=1, to=10, increment=0.5,
                   textvariable=self.reconnect_delay_var, width=10).pack(side=tk.LEFT, padx=10)
        
        # 最大重试次数
        frame7 = ttk.Frame(parent)
        frame7.pack(fill=tk.X, pady=5)
        ttk.Label(frame7, text="最大重试次数:").pack(side=tk.LEFT)
        self.max_retries_var = tk.IntVar()
        ttk.Spinbox(frame7, from_=1, to=30, 
                   textvariable=self.max_retries_var, width=10).pack(side=tk.LEFT, padx=10)
    
    def _create_security_tab(self, parent):
        """安全设置"""
        # 启用认证
        frame1 = ttk.Frame(parent)
        frame1.pack(fill=tk.X, pady=5)
        self.auth_var = tk.BooleanVar()
        ttk.Checkbutton(frame1, text="启用认证", 
                       variable=self.auth_var).pack(side=tk.LEFT)
        ttk.Label(frame1, text="(需要设置密码)", font=("Arial", 8), 
                 foreground="gray").pack(side=tk.LEFT, padx=5)
        
        # 密码设置
        frame2 = ttk.Frame(parent)
        frame2.pack(fill=tk.X, pady=5)
        ttk.Label(frame2, text="密码:").pack(side=tk.LEFT)
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(frame2, textvariable=self.password_var, show="*", width=20)
        self.password_entry.pack(side=tk.LEFT, padx=10)

        # ===== 记住密码复选框（v4.0.1：非Windows平台检查密钥环可用性） =====
        self.remember_var = tk.BooleanVar()
        self.remember_checkbox = ttk.Checkbutton(frame2, text="记住密码",
                              variable=self.remember_var)
        self.remember_checkbox.pack(side=tk.LEFT, padx=5)
        # v4.0.1：非Windows平台若无密钥环依赖，禁用并提示
        try:
            from config import is_keyring_available
            if not is_keyring_available():
                self.remember_checkbox.config(state=tk.DISABLED)
                tip = "需要安装密钥环依赖：macOS: pip install pyobjc；Linux: pip install secretstorage"
                ttk.Label(frame2, text="(不可用)", font=("Arial", 8),
                         foreground="red").pack(side=tk.LEFT, padx=2)
                self.remember_checkbox.config(text="记住密码")
        except Exception:
            pass
        # ===== 记住密码结束 =====
        
        # v4.0.0：TLS 加密已强制开启，不再提供开关
        frame3 = ttk.Frame(parent)
        frame3.pack(fill=tk.X, pady=5)
        ttk.Label(frame3, text="🔐 TLS 1.2+ 加密已强制开启（自签名证书自动生成）",
                  font=("Arial", 9), foreground="green").pack(side=tk.LEFT)
        
        # 剪贴板同步
        frame4 = ttk.Frame(parent)
        frame4.pack(fill=tk.X, pady=5)
        self.clipboard_var = tk.BooleanVar()
        ttk.Checkbutton(frame4, text="启用剪贴板同步", 
                       variable=self.clipboard_var).pack(side=tk.LEFT)
    
    def _load_settings(self):
        """加载设置"""
        self.quality_var.set(self.config.get('jpeg_quality', 60))
        self.fps_var.set(self.config.get('max_fps', 30))
        self.min_fps_var.set(self.config.get('min_fps', 5))
        self.min_quality_var.set(self.config.get('min_quality', 30))
        self.max_quality_var.set(self.config.get('max_quality', 95))
        self.diff_var.set(self.config.get('use_diff', True))
        self.compression_var.set(self.config.get('enable_compression', True))
        
        self.port_var.set(self.config.get('port', 9999))
        self.control_port_var.set(self.config.get('control_port', 9998))
        self.max_conn_var.set(self.config.get('max_connections', 10))
        self.bandwidth_var.set(self.config.get('bandwidth_limit', 0))
        self.reconnect_var.set(self.config.get('auto_reconnect', True))
        self.reconnect_delay_var.set(self.config.get('reconnect_delay', 2.0))
        self.max_retries_var.set(self.config.get('max_reconnect_attempts', 10))
        
        self.auth_var.set(self.config.get('enable_auth', False))
        # ===== 密码字段显示占位符（不显示实际哈希） =====
        self.remember_var.set(self.config.get('remember_password', False))
        if self.config.get('remember_password', False):
            saved = self.config.get_saved_password() if hasattr(self.config, 'get_saved_password') else ""
            if saved:
                self.password_var.set("••••••••")
            elif self.config.get('password_hash', ''):
                self.password_var.set("••••••••")
            else:
                self.password_var.set("")
        elif self.config.get('password_hash', ''):
            self.password_var.set("••••••••")
        else:
            self.password_var.set("")
        # ===== 密码占位符结束 =====
        self.clipboard_var.set(self.config.get('enable_clipboard', True))
    
    def _save_settings(self):
        """保存设置"""
        try:
            # 验证端口
            port = self.port_var.get()
            if not (1024 <= port <= 65535):
                messagebox.showerror("错误", "端口必须在 1024-65535 之间")
                return
            
            control_port = self.control_port_var.get()
            if not (1024 <= control_port <= 65535):
                messagebox.showerror("错误", "控制端口必须在 1024-65535 之间")
                return
            
            if port == control_port:
                messagebox.showerror("错误", "服务端口和控制端口不能相同")
                return

            # ===== v4.0.0：密码使用Bcrypt（通过config.set_password） =====
            password = self.password_var.get()
            remember = self.remember_var.get()

            if password and password != "••••••••":
                # 设置新密码（Bcrypt加密）
                self.config.set_password(password)
                # 保存明文密码（用于自动填充，DPAPI加密）
                if hasattr(self.config, 'save_password'):
                    self.config.save_password(password, remember)
            elif password == "••••••••":
                # 保持原有密码不变
                pass
            else:
                # 清空密码
                self.config.set_password("")
                if hasattr(self.config, 'save_password'):
                    self.config.save_password("", False)
            # ===== 密码处理结束 =====

            # 保存（v4.0.0：移除enable_encryption和password_hash，密码通过set_password处理）
            settings = {
                'jpeg_quality': self.quality_var.get(),
                'max_fps': self.fps_var.get(),
                'min_fps': self.min_fps_var.get(),
                'min_quality': self.min_quality_var.get(),
                'max_quality': self.max_quality_var.get(),
                'use_diff': self.diff_var.get(),
                'enable_compression': self.compression_var.get(),
                'port': port,
                'control_port': control_port,
                'max_connections': self.max_conn_var.get(),
                'bandwidth_limit': self.bandwidth_var.get(),
                'auto_reconnect': self.reconnect_var.get(),
                'reconnect_delay': self.reconnect_delay_var.get(),
                'max_reconnect_attempts': self.max_retries_var.get(),
                'enable_auth': self.auth_var.get(),
                'remember_password': remember,
                'enable_clipboard': self.clipboard_var.get(),
            }
            
            self.config.update(settings)
            
            # 通知控制器
            if hasattr(self.controller, 'on_settings_changed'):
                self.controller.on_settings_changed(settings)
            
            messagebox.showinfo("成功", "设置已保存")
            self.dialog.destroy()
            
        except ValueError as e:
            messagebox.showerror("错误", f"输入无效: {e}")
        except Exception as e:
            self.logger.error(f"保存设置失败: {e}")
            messagebox.showerror("错误", f"保存失败: {e}")
"""
主控端主窗口 - 完整修复版
支持鼠标持续移动、焦点自动禁用控制
"""

import tkinter as tk
from tkinter import ttk
import threading
import time
from enum import Enum
from typing import Optional

from config import Config
from logger import get_logger
from network.control_channel import ControlChannel
from network.screen_channel import ScreenChannel
from core.heartbeat import HeartbeatManager
from core.reconnect import ReconnectManager
from core.protocol import Packet
from constants import PKT_TYPE_STREAM_CONTROL
from screen.renderer import ScreenRenderer
from input.dpi import DPIAdapter
from .toolbar import Toolbar
from .screen_widget import ScreenWidget
from .status_bar import StatusBar
from .clipboard import ClipboardManager
from .performance_panel import PerformancePanel
from .quality_indicator import QualityIndicator

# v4.0.0：从包 __init__ 动态导入版本号
try:
    from __init__ import __version__
except ImportError:
    __version__ = "4.0.0"


class ConnectionState(Enum):
    """主控端连接状态机（v4.0.0）"""
    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


class MainWindow:
    """主控端主窗口"""

    # ===== v4.0.0：连接状态机属性 =====
    @property
    def _connected(self) -> bool:
        """兼容旧代码：仅在CONNECTED状态返回True"""
        return self._state == ConnectionState.CONNECTED

    @_connected.setter
    def _connected(self, value: bool):
        """兼容旧代码的布尔赋值"""
        if value:
            self._set_state(ConnectionState.CONNECTED)
        else:
            self._set_state(ConnectionState.DISCONNECTED)

    def _set_state(self, new_state: ConnectionState) -> None:
        """状态流转（统一入口，同步UI组件）"""
        old = getattr(self, '_state', None)
        self._state = new_state
        if old != new_state:
            self.logger.debug(f"状态机: {old} → {new_state}")
        # 同步 screen_widget 连接状态
        if hasattr(self, 'screen_widget'):
            self.screen_widget.set_connected(new_state == ConnectionState.CONNECTED)
        # 同步 toolbar 按钮状态
        if hasattr(self, 'toolbar'):
            try:
                self.toolbar.update_connection_state(new_state)
            except Exception:
                pass

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"远程桌面控制 v{__version__}")

        # 窗口尺寸
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = int(screen_w * 0.92)
        win_h = int(screen_h * 0.88)
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(1024, 768)

        # 组件
        self.config = Config()
        self.logger = get_logger()
        self.dpi = DPIAdapter()

        # 网络
        self.control_channel = ControlChannel()
        self.screen_channel = ScreenChannel()
        # v4.0.1：密码弹窗必须在主线程执行，设置回调
        self.control_channel.password_callback = self._request_password_on_main_thread
        self._password_result: Optional[str] = None
        self._password_event = threading.Event()
        self._password_timed_out = False  # v4.0.2：密码输入超时标志
        # v4.0.1：证书指纹确认回调
        self.control_channel.fingerprint_callback = self._confirm_fingerprint_on_main_thread
        self._fingerprint_result: bool = False
        self._fingerprint_event = threading.Event()
        self.heartbeat: Optional[HeartbeatManager] = None
        self.reconnect: Optional[ReconnectManager] = None

        # v4.0.0：连接状态机（替代 _connected 布尔值）
        self._state = ConnectionState.IDLE
        self._control_enabled = False
        self._remote_width = 1920
        self._remote_height = 1080
        
        # 重连同步事件
        self._connect_event = threading.Event()
        
        # 统计
        self._stats = {
            'fps': 0,
            'latency': 0,
            'bandwidth': 0,
            'quality': 0,
            'total_frames': 0,
            'width': 0,
            'height': 0,
            'bandwidth_limit': self.config.get('bandwidth_limit', 0),
        }
        self._frame_count = 0
        self._fps_start = time.time()
        
        # 画面请求控制
        self._request_active = False
        self._request_timer = None

        # v4.0.3：主线程渲染调度状态（画面帧产生于网络线程，必须调度到主线程渲染）
        self._pending_frame = None   # (img_data, is_diff, quality, offset_x, offset_y)
        self._render_pending = False
        
        # 创建UI
        self._create_ui()
        
        # 设置快捷键
        self._setup_shortcuts()
        
        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
    
    def _create_ui(self):
        """创建UI"""
        # 主布局
        self.root.grid_rowconfigure(0, weight=0)  # 工具栏
        self.root.grid_rowconfigure(1, weight=1)  # 画面
        self.root.grid_rowconfigure(2, weight=0)  # 状态栏
        self.root.grid_columnconfigure(0, weight=1)
        
        # 工具栏
        self.toolbar = Toolbar(self.root, self)
        self.toolbar.grid(row=0, column=0, sticky="ew", padx=3, pady=3)
        
        # 画面和监控面板（水平布局）
        content_frame = ttk.Frame(self.root)
        content_frame.grid(row=1, column=0, sticky="nsew", padx=3, pady=3)
        content_frame.grid_columnconfigure(0, weight=1)
        content_frame.grid_rowconfigure(0, weight=1)
        
        # 画面显示
        self.screen_widget = ScreenWidget(content_frame)
        self.screen_widget.grid(row=0, column=0, sticky="nsew")
        
        # ========== 设置焦点丢失回调 ==========
        self.screen_widget.set_focus_loss_callback(self._on_focus_loss)
        
        # 性能监控面板（右侧）
        self.performance_panel = PerformancePanel(content_frame, self)
        self.performance_panel.grid(row=0, column=1, sticky="n", padx=(5, 0))
        
        # 状态栏
        self.status_bar = StatusBar(self.root)
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=3, pady=2)

        # ===== 新增：质量指示器（添加到状态栏右侧） =====
        self.quality_indicator = QualityIndicator(self.status_bar)
        self.quality_indicator.pack(side=tk.RIGHT, padx=10)
        # ===== 质量指示器结束 =====
        
        # 剪贴板管理
        self.clipboard = ClipboardManager(self)
        
        # 绑定画面事件
        self.screen_widget.bind_mouse_events(self._on_mouse_event)
        self.screen_widget.bind_key_events(self._on_key_event)
        
        # 显示初始消息
        self.screen_widget.show_message("📌 请输入被控端IP地址，点击「连接」")
    
    def _setup_shortcuts(self):
        """设置快捷键"""
        self.root.bind('<F11>', lambda e: self.toggle_fullscreen())
        self.root.bind('<Escape>', lambda e: self._on_escape())
        self.root.bind('<Control-q>', lambda e: self._on_close())
        self.root.bind('<Control-r>', lambda e: self._refresh_screen())
        self.root.bind('<Control-c>', lambda e: self._copy_clipboard())
        self.root.bind('<Control-v>', lambda e: self._paste_clipboard())
        self.root.bind('<Control-f>', lambda e: self.toggle_fullscreen())
        # ===== 新增：紧急断开（Ctrl+Shift+Q） =====
        self.root.bind('<Control-Shift-Q>', lambda e: self._emergency_disconnect())
        # ===== 紧急断开结束 =====
    
    def _on_escape(self):
        """ESC键处理"""
        if self.root.attributes('-fullscreen'):
            self.toggle_fullscreen()

    def _request_password_on_main_thread(self) -> Optional[str]:
        """v4.0.1：在主线程弹出密码输入框（网络线程调用，阻塞等待结果）

        Tkinter 不允许在子线程创建窗口，因此通过 root.after 调度到主线程，
        网络线程阻塞在 Event 上等待用户输入，超时30秒自动取消。
        """
        self._password_result = None
        self._password_event.clear()
        try:
            self.root.after(0, self._show_password_dialog)
        except Exception as e:
            self.logger.error(f"调度密码弹窗失败: {e}")
            return None
        # 阻塞等待用户输入（30秒超时）
        if not self._password_event.wait(timeout=30.0):
            self.logger.warning("密码输入超时，取消连接")
            self._password_timed_out = True  # v4.0.2：标记超时，供连接失败时区分原因
            return None
        self._password_timed_out = False
        return self._password_result

    def _show_password_dialog(self):
        """v4.0.1：在主线程显示密码输入框"""
        try:
            from tkinter import simpledialog
            password = simpledialog.askstring(
                "认证", "请输入被控端密码:", show='*', parent=self.root
            )
            self._password_result = password or ""
        except Exception as e:
            self.logger.error(f"密码弹窗异常: {e}")
            self._password_result = None
        finally:
            self._password_event.set()

    def _confirm_fingerprint_on_main_thread(self, fingerprint: str, host: str) -> bool:
        """v4.0.1：在主线程弹出证书指纹确认框（网络线程调用，阻塞等待）"""
        self._fingerprint_result = False
        self._fingerprint_event.clear()
        try:
            self.root.after(0, lambda: self._show_fingerprint_dialog(fingerprint, host))
        except Exception as e:
            self.logger.error(f"调度指纹确认弹窗失败: {e}")
            return False
        if not self._fingerprint_event.wait(timeout=60.0):
            self.logger.warning("证书指纹确认超时，拒绝连接")
            return False
        return self._fingerprint_result

    def _show_fingerprint_dialog(self, fingerprint: str, host: str):
        """v4.0.1：在主线程显示证书指纹确认对话框"""
        try:
            from tkinter import messagebox
            msg = (
                f"检测到自签名证书（首次连接或证书变更）。\n\n"
                f"主机: {host}\n"
                f"SHA-256 指纹:\n{fingerprint}\n\n"
                f"请确认指纹与被控端显示的一致。\n"
                f"确认后将加入信任列表，后续自动信任。\n\n"
                f"是否信任此证书？"
            )
            result = messagebox.askyesno("证书指纹确认", msg, parent=self.root)
            self._fingerprint_result = bool(result)
        except Exception as e:
            self.logger.error(f"指纹确认弹窗异常: {e}")
            self._fingerprint_result = False
        finally:
            self._fingerprint_event.set()
    
    def _refresh_screen(self):
        """手动刷新画面（重启推送）"""
        if self._connected:
            self.logger.info("手动刷新画面")
            self._stop_streaming()
            self._start_streaming()
    
    def _copy_clipboard(self):
        """复制到剪贴板"""
        try:
            selected = self.root.clipboard_get()
            if selected:
                self.clipboard._send_to_remote(selected)
        except Exception:
            pass
    
    def _paste_clipboard(self):
        """从剪贴板粘贴"""
        try:
            import pyperclip
            text = pyperclip.paste()
            if text:
                self.clipboard._send_to_remote(text)
        except Exception:
            pass
    
    def connect(self, host: str, port: int = 9999):
        """连接被控端（v4.0.0：状态机驱动 IDLE→CONNECTING→CONNECTED）"""
        if self._state == ConnectionState.CONNECTED:
            self.disconnect()
            return
        if self._state in (ConnectionState.CONNECTING, ConnectionState.AUTHENTICATING):
            self.logger.warning("正在连接中，忽略重复请求")
            return

        if not host:
            self.status_bar.set_status("请输入IP地址", "error")
            return

        self._set_state(ConnectionState.CONNECTING)
        self.status_bar.set_status(f"正在连接 {host}...", "connecting")
        self.toolbar.update_connect_state(False)
        self._connect_event.clear()

        def do_connect():
            # 连接控制通道
            control_port = self.config.get('control_port', 9998)
            if not self.control_channel.connect(host, control_port):
                # v4.0.2：区分密码超时与其他连接失败原因
                if getattr(self, '_password_timed_out', False):
                    self.root.after_idle(lambda: self.status_bar.set_status("密码输入超时，请重试", "error"))
                else:
                    self.root.after_idle(lambda: self.status_bar.set_status("控制通道连接失败", "error"))
                self._set_state(ConnectionState.DISCONNECTED)
                self._connect_event.set()
                return

            # v4.0.0：复用控制通道的TLS上下文
            tls_ctx = self.control_channel.get_tls_context()
            if tls_ctx is not None:
                self.screen_channel.set_tls_context(tls_ctx)

            # v4.0.1：传递会话令牌给画面通道
            token = self.control_channel.get_session_token()
            if token:
                self.screen_channel.set_session_token(token)

            # 连接画面通道
            if not self.screen_channel.connect(host, port):
                self.root.after_idle(lambda: self.status_bar.set_status("画面通道连接失败", "error"))
                self.control_channel.close()
                self._set_state(ConnectionState.DISCONNECTED)
                self._connect_event.set()
                return

            # 设置回调
            self.control_channel.set_on_packet(self._on_control_packet)
            self.screen_channel.set_on_packet(self._on_screen_packet)
            # v4.0.3：注册画面通道断开回调（断开后自动重连，避免画面静止）
            self.screen_channel.set_on_disconnect(self._on_screen_channel_lost)

            # 启动心跳
            self.heartbeat = HeartbeatManager(
                send_callback=self._send_heartbeat,
                on_timeout=self._on_heartbeat_timeout
            )
            self.heartbeat.start()

            self._set_state(ConnectionState.CONNECTED)
            self._connect_event.set()
            self.root.after_idle(self._on_connected)

        threading.Thread(target=do_connect, daemon=True).start()
    
    def disconnect(self):
        """断开连接（v4.0.0：状态机驱动 → DISCONNECTED）"""
        # 停止画面推送
        if self._state == ConnectionState.CONNECTED:
            self._stop_streaming()

        # ===== 新增：更新质量指示器为离线 =====
        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.set_offline()
        # ===== 质量指示器结束 =====

        self._set_state(ConnectionState.DISCONNECTED)
        self._connect_event.clear()

        # 停止画面请求（兼容旧变量）
        self._request_active = False
        if self._request_timer:
            try:
                self.root.after_cancel(self._request_timer)
            except Exception:
                pass
            self._request_timer = None
        
        # 如果控制启用，先禁用
        if self._control_enabled:
            self._control_enabled = False
            self.toolbar.update_control_state(False)
            self.screen_widget.enable_control(False)
        
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None
        
        if self.reconnect:
            self.reconnect.stop()
            self.reconnect = None
        
        self.control_channel.close()
        self.screen_channel.close()
        
        self.screen_widget.show_message("📌 连接已断开", "请检查被控端是否运行")
        self.status_bar.set_status("已断开", "disconnected")
        self.toolbar.update_connect_state(False)
        self.toolbar.update_control_state(False)
        
        # 重置统计
        self._stats['fps'] = 0
        self._stats['total_frames'] = 0
        
        self.logger.info("已断开连接")
    
    def _on_connected(self):
        """连接成功回调"""
        self.status_bar.set_status("已连接", "connected")
        self.toolbar.update_connect_state(True)
        self.toolbar.update_control_state(False)
        self.screen_widget.clear()
        self.logger.info("连接成功")

        # ===== 新增：更新质量指示器 =====
        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.update_quality(0, 0, 0, True)
        # ===== 质量指示器结束 =====

        # 更新最近连接
        self.config.add_recent_host(self.toolbar.get_host())

        # ========== 改为发送启动流指令 ==========
        self._start_streaming()
    
    def _start_streaming(self):
        """启动画面推送"""
        if not self._connected:
            return

        # 获取配置参数
        config = {
            'quality': self.config.get('jpeg_quality', 60),
            'max_fps': self.config.get('max_fps', 30),
            'min_fps': self.config.get('min_fps', 5),
            'enable_diff': self.config.get('use_diff', True),
            'enable_compression': self.config.get('enable_compression', True),
        }

        # 发送启动指令（带配置）
        import json
        payload = json.dumps({
            'cmd': 'start',
            'config': config
        }).encode('utf-8')

        packet = Packet(PKT_TYPE_STREAM_CONTROL, payload)
        self.screen_channel.send(packet)
        self.logger.info("📷 已请求启动画面推送")

    def _stop_streaming(self):
        """停止画面推送"""
        if not self._connected:
            return
        import json
        payload = json.dumps({'cmd': 'stop'}).encode('utf-8')
        packet = Packet(PKT_TYPE_STREAM_CONTROL, payload)
        self.screen_channel.send(packet)
        self.logger.info("📷 已请求停止画面推送")

    def _on_control_packet(self, packet):
        """处理控制包"""
        try:
            data = packet.to_json()
            if data.get('type') == 'pong':
                if self.heartbeat:
                    self.heartbeat.on_pong()
                if 'timestamp' in data:
                    self._stats['latency'] = (time.time() - data['timestamp']) * 1000
            elif data.get('type') == 'clipboard':
                chunk = data.get('chunk', 0)
                total = data.get('total', 1)
                self.clipboard.on_remote_clipboard(
                    data.get('text', ''), 
                    chunk, 
                    total
                )
            elif data.get('type') == 'welcome':
                self.logger.info(f"被控端版本: {data.get('version', 'unknown')}")
        except Exception as e:
            self.logger.error(f"处理控制包错误: {e}")
    
    def _on_screen_packet(self, packet):
        """处理画面包"""
        try:
            # 添加包类型检查
            if packet.type != 1:  # 1 = PKT_TYPE_SCREEN
                self.logger.debug(f"忽略非画面包: type={packet.type}")
                return
            meta, img_data = packet.parse_screen()
            self._remote_width = meta.get('width', 1920)
            self._remote_height = meta.get('height', 1080)
            
            # 更新统计
            self._stats['width'] = self._remote_width
            self._stats['height'] = self._remote_height
            self._stats['quality'] = meta.get('quality', 0)
            self._stats['total_frames'] += 1
            self._stats['bandwidth'] = len(img_data)
            
            # 计算FPS
            self._frame_count += 1
            now = time.time()
            if now - self._fps_start >= 1.0:
                self._stats['fps'] = self._frame_count / (now - self._fps_start)
                self._frame_count = 0
                self._fps_start = now

                # ===== v4.0.3：更新质量指示器（调度到主线程，避免跨线程操作控件） =====
                if hasattr(self, 'quality_indicator'):
                    self.root.after_idle(
                        lambda: self.quality_indicator.update_quality(
                            self._stats.get('latency', 0),
                            self._stats['fps'],
                            0,  # packet_loss 暂未统计
                            self._connected
                        )
                    )
                # ===== 质量指示器结束 =====
            
            # 提取偏移量
            is_diff = meta.get('is_diff', False)
            quality = meta.get('quality', 60)
            offset_x = meta.get('offset_x', 0)
            offset_y = meta.get('offset_y', 0)
            
            # v4.0.3 修复“连接成功但画面不显示”：Tkinter 不允许在非主线程操作控件，
            # 旧版在网络接收线程直接调用 display_image，跨线程创建的图像静默失效，
            # 导致画面永远黑屏。此处改为调度到主线程渲染，并合并连续帧
            # （只保留最新一帧），避免渲染任务堆积。
            self._pending_frame = (img_data, is_diff, quality, offset_x, offset_y)
            if not self._render_pending:
                self._render_pending = True
                self.root.after_idle(self._render_frame_on_main_thread)
            
            # 更新UI状态
            self.root.after_idle(self._update_ui_stats)
            
        except Exception as e:
            self.logger.error(f"处理画面包错误: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def _render_frame_on_main_thread(self):
        """v4.0.3：在主线程渲染最新一帧（连续帧已合并，只渲染最新的）"""
        self._render_pending = False
        frame = self._pending_frame
        if frame is None:
            return
        self._pending_frame = None
        img_data, is_diff, quality, offset_x, offset_y = frame
        try:
            self.screen_widget.display_image(
                img_data,
                is_diff=is_diff,
                quality=quality,
                offset_x=offset_x,
                offset_y=offset_y
            )
        except Exception as e:
            self.logger.error(f"渲染画面帧失败: {e}")

    def _update_ui_stats(self):
        """更新UI统计信息"""
        self.status_bar.update_fps()
        if self._stats['latency'] > 0:
            self.status_bar.set_latency(self._stats['latency'])
        self.status_bar.set_quality(self._stats['quality'])
    
    def _send_heartbeat(self):
        """发送心跳"""
        if self._connected:
            self.control_channel.send_json({
                'type': 'ping', 
                'timestamp': time.time()
            })
    
    def _on_heartbeat_timeout(self):
        """心跳超时（v4.0.3：调度到主线程处理，避免在心跳线程内操作状态和UI）"""
        self.logger.warning("心跳超时，触发重连状态流转")
        try:
            self.root.after_idle(lambda: self._trigger_reconnect("心跳超时"))
        except Exception as e:
            self.logger.error(f"调度重连失败: {e}")

    def _on_screen_channel_lost(self):
        """v4.0.3：画面通道意外断开（网络线程调用），触发重连"""
        if self._state != ConnectionState.CONNECTED:
            return
        self.logger.warning("画面通道已断开，准备重连")
        try:
            self.root.after_idle(lambda: self._trigger_reconnect("画面通道断开"))
        except Exception as e:
            self.logger.error(f"调度重连失败: {e}")

    def _trigger_reconnect(self, reason: str):
        """v4.0.3：统一重连入口（主线程执行，状态检查保证幂等）"""
        if self._state != ConnectionState.CONNECTED:
            return
        self.logger.warning(f"{reason}，进入重连流程")
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None
        self._set_state(ConnectionState.RECONNECTING)
        self._start_reconnect()
    
    def _start_reconnect(self):
        """启动重连（v4.0.0：RECONNECTING状态，先清理旧通道）"""
        # 清理旧通道（状态已为RECONNECTING，不再调用完整disconnect）
        try:
            self.control_channel.close()
        except Exception:
            pass
        try:
            self.screen_channel.close()
        except Exception:
            pass

        # ===== 新增：显示重连状态 =====
        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.label.config(text="重连中...", foreground="orange")
            self.quality_indicator._draw_indicator("orange", pulse=True)
        # ===== 重连状态结束 =====

        if self.config.get('auto_reconnect', True):
            host = self.toolbar.get_host()
            self.reconnect = ReconnectManager(
                connect_callback=lambda: self._reconnect_attempt(host),
                max_attempts=self.config.get('max_reconnect_attempts', 10),
                delay=self.config.get('reconnect_delay', 2.0)
            )
            self.reconnect.start(
                on_success=lambda: self.root.after_idle(lambda: self._on_reconnect_success()),
                on_failure=lambda: self.root.after_idle(lambda: self._on_reconnect_failure())
            )
            self.status_bar.set_status("重连中...", "reconnecting")
    
    def _reconnect_attempt(self, host: str) -> bool:
        """重连尝试（阻塞等待结果）"""
        self._connect_event.clear()
        self.connect(host)
        # 等待最多5秒
        if self._connect_event.wait(timeout=5.0):
            return self._connected
        return False

    def _on_reconnect_success(self):
        """重连成功回调"""
        self.status_bar.set_status("重连成功", "connected")
        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.update_quality(0, 0, 0, True)
        self.logger.info("重连成功")

    def _on_reconnect_failure(self):
        """重连失败回调"""
        self.status_bar.set_status("重连失败", "error")
        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.set_offline()
        self.logger.warning("重连失败")

    def _emergency_disconnect(self):
        """紧急断开（立即切断所有连接，状态机 → DISCONNECTED）"""
        self.logger.warning("用户触发紧急断开")
        self._set_state(ConnectionState.DISCONNECTED)
        self._stop_streaming()

        # 强制关闭所有通道
        if self.control_channel:
            self.control_channel.close()
        if self.screen_channel:
            self.screen_channel.close()
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None
        if self.reconnect:
            self.reconnect.stop()
            self.reconnect = None

        self.screen_widget.show_message("已紧急断开", "按 Ctrl+Shift+Q 再次连接")
        self.status_bar.set_status("已紧急断开", "error")
        self.toolbar.update_connect_state(False)
        self.toolbar.update_control_state(False)

        if hasattr(self, 'quality_indicator'):
            self.quality_indicator.set_offline()
    
    # ========== 控制功能 ==========
    
    def toggle_control(self):
        """切换控制状态"""
        if not self._connected:
            return
        
        self._control_enabled = not self._control_enabled
        self.toolbar.update_control_state(self._control_enabled)
        
        if self._control_enabled:
            self.status_bar.set_status("控制已启用", "control")
            self.screen_widget.enable_control(True)
            self.logger.info("🟢 控制已启用")
        else:
            self.status_bar.set_status("控制已禁用", "connected")
            self.screen_widget.enable_control(False)
            self.logger.info("🔴 控制已禁用")
    
    def _on_focus_loss(self):
        """程序失去焦点 - 自动禁用控制"""
        if self._control_enabled:
            self.logger.info("🛑 程序失去焦点，自动禁用控制")
            self._control_enabled = False
            self.toolbar.update_control_state(False)
            self.screen_widget.enable_control(False)
            self.status_bar.set_status("控制已禁用（失去焦点）", "connected")
    
    # ========== 鼠标/键盘事件 ==========
    
    def _on_mouse_event(self, event: dict):
        """鼠标事件"""
        if not self._control_enabled or not self._connected:
            return
        
        action = event.get('action', 'move')
        
        # 移动事件直接使用已转换好的坐标
        if action == 'move':
            x = event.get('x', 0)
            y = event.get('y', 0)
            self.control_channel.send_json({
                'type': 'mouse',
                'action': 'move',
                'x': x,
                'y': y
            })
        elif action == 'press':
            x = event.get('x', 0)
            y = event.get('y', 0)
            button = event.get('button', 'left')
            self.control_channel.send_json({
                'type': 'mouse',
                'action': 'press',
                'x': x,
                'y': y,
                'button': button
            })
        elif action == 'release':
            x = event.get('x', 0)
            y = event.get('y', 0)
            button = event.get('button', 'left')
            self.control_channel.send_json({
                'type': 'mouse',
                'action': 'release',
                'x': x,
                'y': y,
                'button': button
            })
        elif action == 'scroll':
            delta = event.get('delta', 0)
            self.control_channel.send_json({
                'type': 'mouse',
                'action': 'scroll',
                'delta': delta
            })
    
    def _on_key_event(self, event: dict):
        """键盘事件"""
        if not self._control_enabled or not self._connected:
            return
        
        self.control_channel.send_json({
            'type': 'key',
            'action': event.get('action', 'press'),
            'key': event.get('key', ''),
            'modifiers': event.get('modifiers', [])
        })
    
    # ========== 其他功能 ==========
    
    def toggle_fullscreen(self):
        """切换全屏"""
        current_state = self.root.attributes('-fullscreen')
        self.root.attributes('-fullscreen', not current_state)
        self.toolbar.update_fullscreen_state(not current_state)
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return self._stats.copy()
    
    def on_settings_changed(self, settings: dict):
        """设置变更回调"""
        if 'bandwidth_limit' in settings:
            self._stats['bandwidth_limit'] = settings['bandwidth_limit']
        self.logger.info("设置已更新")
    
    def _on_close(self):
        """关闭窗口"""
        self._request_active = False
        if self._request_timer:
            self.root.after_cancel(self._request_timer)
            self._request_timer = None
        self.disconnect()
        if hasattr(self, 'performance_panel'):
            self.performance_panel.stop()
        self.root.quit()
        self.root.destroy()
    
    def run(self):
        """运行主循环"""
        self.root.mainloop()
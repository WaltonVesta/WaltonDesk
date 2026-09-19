"""
画面显示组件 - 修复鼠标连续移动和焦点问题
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional, Callable
import io
from PIL import Image, ImageTk

from screen.renderer import ScreenRenderer
from input.dpi import DPIAdapter
from logger import get_logger


class ScreenWidget(ttk.Frame):
    """画面显示组件"""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.logger = get_logger()
        self.dpi = DPIAdapter()
        
        self._create_canvas()
        
        # 渲染器
        self.renderer = ScreenRenderer(self.canvas)
        
        # 状态
        self._control_enabled = False
        self._connected = False  # v4.0.0：连接状态机检查
        self._remote_width = 1920
        self._remote_height = 1080
        self._mouse_callbacks: list = []
        self._key_callbacks: list = []
        
        # 修饰键状态
        self._modifiers = []
        
        # 鼠标跟踪 - 用于持续移动
        self._last_mouse_x = 0
        self._last_mouse_y = 0
        self._last_sent_x = 0
        self._last_sent_y = 0
        self._same_count = 0
        self._mouse_move_timer = None
        self._mouse_moving = False
        # v4.0.1：鼠标移动最小位移阈值（远程像素），低于此值不发送，减少带宽占用
        self._MOVE_THRESHOLD = 3
        self._MOVE_INTERVAL_MS = 30  # v4.0.1：30ms ≈ 33fps（原16ms≈60fps）
        
        # 焦点丢失回调
        self._focus_loss_callback: Optional[Callable] = None
        self._ignore_focus_loss = False
        
    def _create_canvas(self):
        """创建画布"""
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        
        self.canvas = tk.Canvas(self, bg="#1a1a2e", highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        
        # 鼠标事件绑定
        self.canvas.bind("<Button-1>", self._on_mouse_click)
        self.canvas.bind("<Button-3>", self._on_mouse_click)
        self.canvas.bind("<Button-2>", self._on_mouse_click)
        
        self.canvas.bind("<ButtonRelease-1>", self._on_mouse_release)
        self.canvas.bind("<ButtonRelease-3>", self._on_mouse_release)
        self.canvas.bind("<ButtonRelease-2>", self._on_mouse_release)
        
        # 鼠标持续移动
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.canvas.bind("<B3-Motion>", self._on_mouse_drag)
        self.canvas.bind("<B2-Motion>", self._on_mouse_drag)
        
        # 鼠标滚轮
        self.canvas.bind("<MouseWheel>", self._on_mouse_scroll)
        
        # 焦点事件
        self.canvas.bind("<FocusIn>", self._on_focus_in)
        self.canvas.bind("<FocusOut>", self._on_focus_out)
        
        # 键盘事件
        self.canvas.bind("<Key>", self._on_key_press)
        self.canvas.bind("<KeyRelease>", self._on_key_release)
        
        # 画布大小变化
        self.canvas.bind("<Configure>", self._on_resize)
        
        # 设置焦点
        self.canvas.focus_set()
    
    def display_image(self, img_data: bytes, is_diff: bool = False, quality: int = 60, offset_x: int = 0, offset_y: int = 0):
        """显示图像"""
        try:
            img = Image.open(io.BytesIO(img_data))
            self._remote_width = img.width
            self._remote_height = img.height
            # 传递偏移和差分标志给渲染器
            self.renderer.render(img, is_diff=is_diff, offset_x=offset_x, offset_y=offset_y)
        except Exception as e:
            self.logger.error(f"显示图像失败: {e}")
    
    def show_message(self, message: str, sub_message: str = ""):
        """显示消息"""
        self.renderer.show_message(message, sub_message)
    
    def clear(self):
        """清除画面"""
        self.renderer.clear()
    
    def canvas_to_remote(self, canvas_x: int, canvas_y: int) -> tuple:
        """将画布坐标转换为远程坐标（v4.0.0：实时计算，不依赖renderer缓存偏移）"""
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1 or self._remote_height <= 0:
            return 0, 0

        # 实时计算图像绘制区域（居中保持宽高比）
        img_ratio = self._remote_width / self._remote_height
        canvas_ratio = canvas_w / canvas_h
        if canvas_ratio > img_ratio:
            # 画布更宽，图像以高度为准，水平居中
            draw_h = canvas_h
            draw_w = draw_h * img_ratio
            offset_x = (canvas_w - draw_w) / 2.0
            offset_y = 0.0
            scale = draw_w / self._remote_width
        else:
            # 画布更高，图像以宽度为准，垂直居中
            draw_w = canvas_w
            draw_h = draw_w / img_ratio
            offset_x = 0.0
            offset_y = (canvas_h - draw_h) / 2.0
            scale = draw_w / self._remote_width

        if scale <= 0:
            return 0, 0

        remote_x = (canvas_x - offset_x) / scale
        remote_y = (canvas_y - offset_y) / scale

        remote_x = max(0, min(self._remote_width - 1, int(remote_x)))
        remote_y = max(0, min(self._remote_height - 1, int(remote_y)))

        return remote_x, remote_y
    
    def bind_mouse_events(self, callback: Callable):
        """绑定鼠标事件回调"""
        self._mouse_callbacks.append(callback)
    
    def bind_key_events(self, callback: Callable):
        """绑定键盘事件回调"""
        self._key_callbacks.append(callback)
    
    def enable_control(self, enabled: bool):
        """启用/禁用控制"""
        self._control_enabled = enabled
        if enabled:
            self._ignore_focus_loss = True   # 临时忽略焦点丢失
            self.canvas.focus_force()
            self.canvas.after(200, lambda: setattr(self, '_ignore_focus_loss', False))
            self.logger.info("控制已启用")
        else:
            # ===== 加强清理：停止所有鼠标/键盘事件 =====
            if self._mouse_move_timer:
                self.canvas.after_cancel(self._mouse_move_timer)
                self._mouse_move_timer = None
            self._mouse_moving = False
            self._modifiers.clear()
            self._ignore_focus_loss = False
            self.logger.info("控制已禁用，所有鼠标/键盘事件已停止")

    def set_connected(self, connected: bool):
        """v4.0.0：设置连接状态（状态机驱动）"""
        self._connected = connected
    
    def is_control_enabled(self) -> bool:
        """检查控制是否启用"""
        return self._control_enabled
    
    def set_focus_loss_callback(self, callback: Callable):
        """设置焦点丢失回调"""
        self._focus_loss_callback = callback
    
    # ========== 鼠标事件处理 ==========
    
    def _on_mouse_click(self, event):
        """鼠标点击"""
        if not self._control_enabled:
            return
        button_map = {1: 'left', 3: 'right', 2: 'middle'}
        button = button_map.get(event.num, 'left')
        
        self.canvas.focus_set()
        
        remote_x, remote_y = self.canvas_to_remote(event.x, event.y)
        for cb in self._mouse_callbacks:
            cb({
                'action': 'press',
                'x': remote_x,
                'y': remote_y,
                'button': button
            })
    
    def _on_mouse_release(self, event):
        """鼠标释放"""
        if not self._control_enabled:
            return
        button_map = {1: 'left', 3: 'right', 2: 'middle'}
        button = button_map.get(event.num, 'left')
        
        remote_x, remote_y = self.canvas_to_remote(event.x, event.y)
        for cb in self._mouse_callbacks:
            cb({
                'action': 'release',
                'x': remote_x,
                'y': remote_y,
                'button': button
            })
    
    def _on_mouse_move(self, event):
        """鼠标移动 - 持续发送（v4.0.0：仅在CONNECTED状态生效）"""
        if not self._control_enabled or not self._connected:
            return
        
        self._last_mouse_x = event.x
        self._last_mouse_y = event.y
        
        if not self._mouse_moving:
            self._mouse_moving = True
            self._send_mouse_move_continuous()
    
    def _send_mouse_move_continuous(self):
        """持续发送鼠标移动事件（v4.0.1：最小位移阈值+30ms间隔+静止检测）"""
        if not self._control_enabled or not self._connected:
            self._mouse_moving = False
            if self._mouse_move_timer:
                self.canvas.after_cancel(self._mouse_move_timer)
                self._mouse_move_timer = None
            return

        remote_x, remote_y = self.canvas_to_remote(self._last_mouse_x, self._last_mouse_y)

        # v4.0.1：最小位移阈值，低于阈值不发送（减少带宽）
        dx = abs(remote_x - self._last_sent_x)
        dy = abs(remote_y - self._last_sent_y)
        if dx < self._MOVE_THRESHOLD and dy < self._MOVE_THRESHOLD:
            self._same_count += 1
            if self._same_count >= 3:
                # 连续3次低于阈值，停止轮询（鼠标静止）
                self._mouse_moving = False
                self._same_count = 0
                if self._mouse_move_timer:
                    self.canvas.after_cancel(self._mouse_move_timer)
                    self._mouse_move_timer = None
                return
        else:
            # 位移超过阈值，发送并重置计数
            self._last_sent_x, self._last_sent_y = remote_x, remote_y
            self._same_count = 0
            for cb in self._mouse_callbacks:
                cb({'action': 'move', 'x': remote_x, 'y': remote_y})

        # v4.0.1：30ms 间隔（≈33fps），平衡精度与带宽
        self._mouse_move_timer = self.canvas.after(
            self._MOVE_INTERVAL_MS, self._send_mouse_move_continuous
        )
    
    def _on_mouse_drag(self, event):
        """鼠标拖拽"""
        if not self._control_enabled:
            return
        
        self._last_mouse_x = event.x
        self._last_mouse_y = event.y
        
        remote_x, remote_y = self.canvas_to_remote(event.x, event.y)
        for cb in self._mouse_callbacks:
            cb({
                'action': 'move',
                'x': remote_x,
                'y': remote_y
            })
    
    def _on_mouse_scroll(self, event):
        """鼠标滚轮"""
        if not self._control_enabled:
            return
        # 归一化滚轮值
        delta = 1 if event.delta > 0 else -1
        for cb in self._mouse_callbacks:
            cb({
                'action': 'scroll',
                'delta': delta
            })
    
    # ========== 焦点事件处理 ==========
    
    def _on_focus_in(self, event):
        """获得焦点"""
        if self._control_enabled:
            self.logger.debug("画面获得焦点")
    
    def _on_focus_out(self, event):
        """失去焦点 - 自动禁用控制（v4.0.3：延迟校验，仅焦点真正离开主窗口时才处理）"""
        if self._control_enabled and not self._ignore_focus_loss:
            # v4.0.3 修复“禁用控制需双击”：点击窗口内控件（如工具栏按钮）也会
            # 触发画布 FocusOut，旧版立即自动禁用，与随后的按钮点击事件竞争，
            # 把控制状态又翻回启用，导致单击无法禁用。
            # 现改为延迟100ms校验焦点去向：焦点仍在程序窗口内则不处理。
            self.after(100, self._check_focus_loss)

    def _check_focus_loss(self):
        """v4.0.3：延迟校验焦点是否真正离开程序窗口"""
        if not self._control_enabled or self._ignore_focus_loss:
            return
        try:
            focused = self.winfo_toplevel().focus_get()
        except Exception:
            focused = None
        if focused is not None:
            try:
                if focused.winfo_toplevel() is self.winfo_toplevel():
                    # 焦点仍在程序窗口内（例如移到工具栏按钮），不禁用
                    return
            except Exception:
                pass
        self.logger.info("程序失去焦点，自动禁用控制")
        # 清理修饰键
        self._modifiers.clear()
        # 通知外部禁用控制
        if self._focus_loss_callback:
            self._focus_loss_callback()
    
    # ========== 键盘事件处理 ==========
    
    def _on_key_press(self, event):
        """键盘按下"""
        if not self._control_enabled:
            return
        
        # 处理修饰键
        if event.keysym in ['Control_L', 'Control_R', 'Shift_L', 'Shift_R', 'Alt_L', 'Alt_R']:
            mod_map = {
                'Control_L': 'ctrl', 'Control_R': 'ctrl',
                'Shift_L': 'shift', 'Shift_R': 'shift',
                'Alt_L': 'alt', 'Alt_R': 'alt'
            }
            mod = mod_map.get(event.keysym)
            if mod and mod not in self._modifiers:
                self._modifiers.append(mod)
            return
        
        for cb in self._key_callbacks:
            cb({
                'action': 'press',
                'key': event.keysym,
                'modifiers': self._modifiers.copy()
            })
    
    def _on_key_release(self, event):
        """键盘释放"""
        if not self._control_enabled:
            return
        
        # 处理修饰键
        if event.keysym in ['Control_L', 'Control_R', 'Shift_L', 'Shift_R', 'Alt_L', 'Alt_R']:
            mod_map = {
                'Control_L': 'ctrl', 'Control_R': 'ctrl',
                'Shift_L': 'shift', 'Shift_R': 'shift',
                'Alt_L': 'alt', 'Alt_R': 'alt'
            }
            mod = mod_map.get(event.keysym)
            if mod in self._modifiers:
                self._modifiers.remove(mod)
            return
        
        for cb in self._key_callbacks:
            cb({
                'action': 'release',
                'key': event.keysym
            })
    
    def _on_resize(self, event):
        """画布大小变化"""
        if hasattr(self.renderer, '_last_image') and self.renderer._last_image:
            self.renderer.render(self.renderer._last_image)
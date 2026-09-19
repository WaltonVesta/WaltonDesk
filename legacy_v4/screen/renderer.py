"""
画面渲染器（主控端）
支持缩放、局部合成
"""

import tkinter as tk
from PIL import Image, ImageTk
from typing import Optional
from logger import get_logger


class ScreenRenderer:
    """画面渲染器"""

    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self._image_id: Optional[int] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._last_image: Optional[Image.Image] = None   # 存储完整图像（缩放前）
        self._scale = 1.0
        self._offset_x = 0
        self._offset_y = 0
        self.logger = get_logger()

    def render(self, img: Image.Image,
               is_diff: bool = False,
               offset_x: int = 0, offset_y: int = 0,
               update_region: bool = False) -> None:
        """
        渲染图像

        Args:
            img: 图像（完整或局部）
            is_diff: 是否为局部更新
            offset_x: 局部图像在完整图像中的x偏移
            offset_y: 局部图像在完整图像中的y偏移
            update_region: 是否局部刷新（保留兼容，忽略）
        """
        # 如果是差分并且有缓存图像，则合成完整图像
        if is_diff and self._last_image is not None:
            # 复制缓存图像
            full_img = self._last_image.copy()
            # 将小块粘贴到对应位置
            full_img.paste(img, (offset_x, offset_y))
            img = full_img
        else:
            # 全量帧，直接作为完整图像
            pass

        # 更新缓存（总是存储完整图像）
        self._last_image = img.copy() if img else None

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()

        if canvas_w <= 1 or canvas_h <= 1:
            return

        # 计算缩放
        scale_x = canvas_w / img.width
        scale_y = canvas_h / img.height
        scale = min(scale_x, scale_y)

        # 如果缩放变化不大，可以复用之前的PhotoImage，但为了简单，每次都重新创建
        if scale < 0.95 or scale > 1.05:
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        else:
            img_resized = img

        self._photo = ImageTk.PhotoImage(img_resized)
        self._scale = scale

        # 清除旧图像
        self.canvas.delete("screen")
        # 居中绘制
        self._offset_x = (canvas_w - self._photo.width()) // 2
        self._offset_y = (canvas_h - self._photo.height()) // 2
        self._image_id = self.canvas.create_image(
            self._offset_x, self._offset_y,
            image=self._photo, anchor="nw", tags="screen"
        )
        self.canvas.image = self._photo  # 保持引用

    def get_image_coords(self, canvas_x: int, canvas_y: int) -> tuple:
        """将画布坐标转换为图像坐标"""
        img_x = (canvas_x - self._offset_x) / self._scale
        img_y = (canvas_y - self._offset_y) / self._scale
        return img_x, img_y

    def get_scale(self) -> float:
        """获取当前缩放比例"""
        return self._scale

    def clear(self) -> None:
        """清除画面"""
        self.canvas.delete("screen")
        self.canvas.delete("message")
        self._photo = None
        self._image_id = None
        self._last_image = None

    def show_message(self, message: str, sub_message: str = "") -> None:
        """显示居中消息"""
        self.canvas.delete("all")
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        center_x = canvas_w // 2
        center_y = canvas_h // 2

        self.canvas.create_text(
            center_x, center_y - 20,
            text=message,
            fill="#88ccff",
            font=("Arial", 22, "bold"),
            tags="message"
        )
        if sub_message:
            self.canvas.create_text(
                center_x, center_y + 40,
                text=sub_message,
                fill="#8899aa",
                font=("Arial", 13),
                tags="message"
            )
"""深色主题设计令牌（阶段 6.2）。

单一事实源：颜色/间距/字号/圆角。``dark.qss`` 用 ``{token}`` 占位符引用本模块，
由 :func:`rdc.ui.app.load_stylesheet` 渲染；Python 侧绘图（QPainter 图标、
质量指示灯）直接 import 常量，保证两侧色值永不漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TOKENS", "Tokens"]


@dataclass(frozen=True)
class Tokens:
    """设计令牌（全部 CSS 颜色字符串 + px 数值）。"""

    # -- 底色层 ----------------------------------------------------------------
    bg: str = "#1e2124"          # 窗口底
    bg_alt: str = "#26292d"      # 面板/交替行
    bg_input: str = "#2b2f34"    # 输入框/下拉
    bg_hover: str = "#33383e"    # 悬停
    bg_selected: str = "#3d4450" # 选中
    border: str = "#3a3f45"
    border_focus: str = "#4f9cf9"

    # -- 文本层 ----------------------------------------------------------------
    text: str = "#e8eaed"
    text_dim: str = "#9aa0a6"
    text_disabled: str = "#5f6368"

    # -- 语义色 ----------------------------------------------------------------
    accent: str = "#4f9cf9"      # 主操作
    ok: str = "#3fb950"          # 已连接/正常
    warn: str = "#d29922"        # 连接中/弱网
    error: str = "#f85149"       # 错误/断开
    info: str = "#58a6ff"

    # -- 几何 --------------------------------------------------------------------
    radius: int = 6              # px 圆角
    spacing: int = 8             # px 基础间距
    spacing_lg: int = 16
    font_size: int = 9           # pt（Qt pointSize）
    font_size_lg: int = 11
    font_family: str = "Segoe UI"


#: 全局唯一令牌实例。
TOKENS = Tokens()

"""输入注入（阶段 7 完成）：InputBackend 抽象 + 平台实现 + 事件合并。

模块地图：
- ``base``：InputBackend 协议 / ScreenRect / 键名归一 / 后端工厂；
- ``windows``：SendInput（ctypes，本机可验，主交付）；
- ``macos`` / ``linux_x11`` / ``linux_wayland``：**空实现**（阶段 7 范围
  调整：Windows 优先；补实现只改本包，协议不动）；
- ``legacy_pyautogui``：v4 路径回退（原生后端不可用时）；
- ``coalesce``：事件流合并 + 帧对齐（纯逻辑）；
- ``keys``：Qt 键码 → 规范键名（IME 双通道的键名侧）。

v4 的 mouse/keyboard/dpi/modifiers 四件已删除（语义由以上模块承接，
git 历史可查）。

注意：本 __init__ 刻意**不做** eager re-export。
v4 的包 __init__ 全部 eager import 子模块，形成了
service/__init__ → daemon → gui.tray → tkinter 的加载链，
并迫使 18 处 ``from logger import ...`` 写进函数体规避循环导入。
v5 起一律用完整模块路径导入（如 ``from rdc.protocol.framing import Packet``）。
"""

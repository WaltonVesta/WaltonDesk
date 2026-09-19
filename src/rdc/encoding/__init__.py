"""编码层：H.264（v5 全帧）与 JPEG（v4 兼容差分）双路径。

注意：本 __init__ 刻意**不做** eager re-export。
v4 的包 __init__ 全部 eager import 子模块，形成了
service/__init__ → daemon → gui.tray → tkinter 的加载链，
并迫使 18 处 ``from logger import ...`` 写进函数体规避循环导入。
v5 起一律用完整模块路径导入（如 ``from rdc.protocol.framing import Packet``）。
"""

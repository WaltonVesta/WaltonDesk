"""宿主系统集成：开机自启 / 网络信息 / 电源操作。（原名 platform，避免遮蔽标准库）

注意：本 __init__ 刻意**不做** eager re-export。
v4 的包 __init__ 全部 eager import 子模块，形成了
service/__init__ → daemon → gui.tray → tkinter 的加载链，
并迫使 18 处 ``from logger import ...`` 写进函数体规避循环导入。
v5 起一律用完整模块路径导入（如 ``from rdc.protocol.framing import Packet``）。
"""

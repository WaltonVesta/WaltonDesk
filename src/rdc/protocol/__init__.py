"""协议层：15 字节分帧 + v4(JSON)/v5(Protobuf) 双载荷编解码。

注意：本 __init__ 刻意**不做** eager re-export。
v4 的包 __init__ 全部 eager import 子模块，形成了
service/__init__ → daemon → gui.tray → tkinter 的加载链，
并迫使 18 处 ``from logger import ...`` 写进函数体规避循环导入。
v5 起一律用完整模块路径导入（如 ``from rdc.protocol.framing import Packet``）。
"""

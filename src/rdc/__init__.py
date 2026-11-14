"""WaltonDesk（导入包名 ``rdc``）— v5.0.0。

跨平台主控端 / 被控端双模式远程控制软件。

阶段 10 品牌更名为 WaltonDesk，导入包名保持 ``rdc`` 不变（用户可见品牌一律
经 :mod:`rdc.brand` 取，本文件不重复展开品牌字符串）。

本包由 v4.0.3 重构而来，采用 src layout。版本号在此单点声明，
通过 ``importlib.metadata`` 与打包元数据保持一致。

Attributes:
    __version__: 语义化版本字符串。
    __protocol_version__: wire protocol 版本。v4=1（JSON 载荷 + 裸握手），
        v5=2（Protobuf 载荷 + HMAC 完整性）。协议头 Version 字段据此分派，
        详见 ``rdc.protocol``（阶段 3 实现）。
"""

from __future__ import annotations

__version__ = "5.0.0"

# 协议版本：v4 客户端使用 1，v5 使用 2。用于向后兼容分派。
# 注意：v4 源码中此常量从未被真正使用（见 ASSESSMENT.md §4.1），
# v5 起在协议头 Version 字段中启用。
__protocol_version__ = 2

__all__ = ["__version__", "__protocol_version__"]

"""品牌名单点声明（阶段 10：rdc / 远程桌面控制 → WaltonDesk）。

用户可见的品牌字符串一律从这里取，避免"改了窗口标题漏了托盘 tooltip"这类
更名不彻底。结构信息（导入包名 ``rdc``、数据目录名、CLI 子命令）不在此处——
它们各有单一事实源（``rdc`` 包、:data:`rdc.config.paths.APP_DIR_NAME`、
``rdc.cli.build_parser``）。

⚠️ 与品牌**无关**、刻意保持旧值的三类标识（改了会静默破坏既有状态）：

1. :data:`rdc.secrets.dpapi.DPAPI_ENTROPY` —— 熵字符串参与 DPAPI 密文派生，
   改动即让已存口令永久解不开（密钥库单测锁死其字面值）；
2. :mod:`rdc.secrets.keychain` 的 ``SERVICE`` / :mod:`rdc.secrets.libsecret` 的
   ``APP_NAME`` —— macOS/Linux 密钥环的既有条目归属键；
3. :mod:`rdc.transport.tls` 自签证书的 ``CN=RemoteDesktop`` —— 已有 TOFU 指纹
   信任按证书主体记录，改动会让存量信任全部失效（且证书早已落盘，改代码
   也改不了历史证书）。

v5.1.0 若要做"密钥库命名空间迁移"，须与凭证重签一并设计，不在 v5.0.0 范围内。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    pass

__all__ = ["BRAND_NAME", "brand_title"]

#: 产品品牌名（窗口标题、托盘提示、CLI 横幅、安装包名称的唯一来源）。
BRAND_NAME: Final[str] = "WaltonDesk"


def brand_title(suffix: str = "", *, with_version: bool = True) -> str:
    """拼接品牌标题。

    Args:
        suffix: 子窗体后缀（如"主控端""初始密码"），非空时以全角破折号连接。
        with_version: 是否带上 ``v{__version__}``。

    Returns:
        形如 ``WaltonDesk v5.0.0`` 或 ``WaltonDesk v5.0.0 — 主控端``。
    """
    from rdc import __version__

    base = f"{BRAND_NAME} v{__version__}" if with_version else BRAND_NAME
    return f"{base} — {suffix}" if suffix else base

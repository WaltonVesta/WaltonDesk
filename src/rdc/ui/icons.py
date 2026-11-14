"""品牌图标资源加载（阶段 10：图标接入）。

图标事实源：``icon.png``（项目根，设计稿）→ ``scripts/gen_icon.py`` 生成

- ``icon.ico``（项目根）：PyInstaller ``icon=`` 与 Inno Setup ``SetupIconFile``
  在构建期需要**磁盘路径**，只能用根目录副本；
- ``src/rdc/ui/theme/icon.ico``：运行期唯一加载点。

为什么运行期读包内而不是根目录：打包产物里没有项目根目录，读根路径在 exe 下
必然失败；而 ``theme/`` 目录已作为 ``datas`` 整体进包（``dark.qss`` 同路径），
``importlib.resources`` 在源码与 frozen 两种形态下都能取到，零额外配置。

加载失败一律返回 ``None``（调用方跳过 setWindowIcon），**绝不抛异常**——
图标缺失是外观问题，不该让主控端起不了窗。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from PySide6.QtGui import QIcon

from rdc.logging import get_logger

if TYPE_CHECKING:
    pass

__all__ = ["ICON_RESOURCE", "app_icon_path", "build_app_icon", "reset_icon_cache"]

_LOG = get_logger("ui.icons")

#: 包内图标资源名（与 ``dark.qss`` 同目录，同属 spec 的 datas）。
ICON_RESOURCE: Final[str] = "icon.ico"

#: 进程级缓存：``None`` = 未加载，``False`` = 加载失败（不重复尝试与刷日志）。
_CACHE: QIcon | bool | None = None


def app_icon_path() -> str:
    """图标文件的绝对路径字符串（作为 QIcon 路径加载，非临时 extract）。

    Raises:
        FileNotFoundError: 包内资源缺失。
    """
    from importlib import resources

    ref = resources.files("rdc.ui.theme").joinpath(ICON_RESOURCE)
    if not ref.is_file():
        raise FileNotFoundError(f"包内图标资源缺失: {ICON_RESOURCE}")
    return str(ref)


def build_app_icon() -> QIcon | None:
    """加载品牌图标（进程级缓存，失败返回 ``None`` 且只告警一次）。"""
    global _CACHE
    if _CACHE is False:
        return None
    if _CACHE is None:
        try:
            path = app_icon_path()
        except Exception as exc:  # noqa: BLE001 - 资源缺失只降级外观，不炸启动
            _LOG.warning("app_icon_missing", error=repr(exc))
            _CACHE = False
            return None
        icon = QIcon(path)
        if icon.isNull():
            _LOG.warning("app_icon_unreadable", path=path)
            _CACHE = False
            return None
        _CACHE = icon
    return _CACHE if isinstance(_CACHE, QIcon) else None


def reset_icon_cache() -> None:
    """清空缓存（仅供测试用）。"""
    global _CACHE
    _CACHE = None

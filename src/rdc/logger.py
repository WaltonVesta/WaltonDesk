"""``rdc.logger`` —— v4 兼容 shim（阶段 2，2.8 structlog 改造后保留）。

实现已迁移到 :mod:`rdc.logging`（structlog 双 renderer + 轮转）。
阶段 1 机械搬迁的 v4 代码共 24 处 ``from rdc.logger import get_logger``，
本模块把它们无感转发到新栈；``get_logger()`` 无参返回 stdlib 风格适配器
（%-format / 五方法镜像），存量调用行为不变。

TODO(phase-4/6): 各模块重写为 ``from rdc.logging import get_logger`` 并传
logger 名后，删除本文件。
"""

from __future__ import annotations

from rdc.logging.setup import configure_logging, get_logger

# v4 面保留的别名（无存量调用方使用，仅防外部/脚本按旧名 import）。
setup_logger = configure_logging


def set_log_level(level: str) -> None:
    """动态设置控制台日志级别（v4 兼容面）。"""
    import logging

    root = configure_logging(level)
    root.setLevel(logging.DEBUG)


__all__ = ["configure_logging", "get_logger", "set_log_level", "setup_logger"]

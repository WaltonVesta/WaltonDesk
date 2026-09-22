"""rdc 日志包（阶段 2，2.8：structlog 改造）。

D1 偏离落地：v4 的 ``rdc/logger.py`` 单模块重组为 ``rdc/logging/`` 包。
对外接口只暴露 :func:`get_logger` / :func:`configure_logging`；
``rdc.logger`` 旧模块保留为薄 re-export shim，存量调用点零改动。
"""

from __future__ import annotations

from rdc.logging.setup import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]

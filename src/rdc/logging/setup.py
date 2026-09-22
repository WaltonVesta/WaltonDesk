"""structlog 日志初始化（阶段 2，2.8）。

双 renderer 架构：

- **structlog 路径**（新代码）：``get_logger(__name__)`` → JSONRenderer 写文件、
  ConsoleRenderer 写 stdout，保留 v4 的轮转（RotatingFileHandler）与
  emoji 清理（Windows 控制台/文件乱码兜底）。
- **stdlib 兼容路径**（阶段 1 搬迁的 v4 代码仍 ``from rdc.logger import
  get_logger``）：``get_logger()`` 无参调用返回 :class:`_StdlibLoggerAdapter`，
  镜像 ``Logger.debug/info/warning/error/exception`` 与 %-format 语义，
  底层复用同一个 structlog 配置。阶段 4/6 重写各模块时逐步换成有参调用。

幂等：重复调用 :func:`configure_logging` 只更新级别，不叠加 handler
（与 v4 setup_logger 行为一致）。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from rdc.config import paths

__all__ = ["configure_logging", "get_logger"]

MAX_LOG_SIZE = 10 * 1024 * 1024
BACKUP_COUNT = 5

# 与 v4 EmojiFilter 同一区段表：清理 Windows 日志乱码源头。
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F"
    "\u2700-\u27BF\u2600-\u26FF\u23F0-\u23FF"
    "]+",
    flags=re.UNICODE,
)

_configured = False
_root: logging.Logger | None = None


def strip_emoji(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]  # noqa: ANN401
) -> MutableMapping[str, Any]:
    """structlog 处理器：移除事件消息中的 emoji 字符。"""
    msg = event_dict.get("event")
    if isinstance(msg, str):
        event_dict["event"] = _EMOJI_RE.sub("", msg)
    return event_dict


class _StdlibLoggerAdapter:
    """stdlib-Logger 风格的最小兼容面（v4 搬迁代码 ``get_logger()`` 路径专用）。

    仅实现 v4 实际用到的五个方法与 %-format 延迟渲染；阶段 4/6 各模块重写为
    structlog 有参调用后，本类与 ``rdc.logger`` shim 一并删除。
    """

    def __init__(self, name: str) -> None:
        self._log = structlog.get_logger(name)

    @staticmethod
    def _render(msg: str, args: tuple[Any, ...]) -> str:
        if args:
            try:
                return msg % args
            except (TypeError, ValueError):
                return f"{msg} {args!r}"
        return msg

    def debug(self, msg: str, *args: Any) -> None:  # noqa: ANN401
        self._log.debug(self._render(msg, args))

    def info(self, msg: str, *args: Any) -> None:  # noqa: ANN401
        self._log.info(self._render(msg, args))

    def warning(self, msg: str, *args: Any) -> None:  # noqa: ANN401
        self._log.warning(self._render(msg, args))

    def error(self, msg: str, *args: Any) -> None:  # noqa: ANN401
        self._log.error(self._render(msg, args))

    def exception(self, msg: str, *args: Any) -> None:  # noqa: ANN401
        self._log.exception(self._render(msg, args))


def configure_logging(level: str = "INFO", *, log_file: Path | None = None) -> logging.Logger:
    """初始化 structlog + stdlib 双栈（幂等）。

    Args:
        level: 控制台级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）。
        log_file: 覆盖日志文件路径（默认 ``paths.log_dir()/rdc.log``，
            测试注入 tmp_path 用）。

    Returns:
        底层 stdlib logger（structlog 的落盘出口）。
    """
    global _configured, _root

    level_num = getattr(logging, level.upper(), logging.INFO)

    if _configured and _root is not None:
        for h in _root.handlers:
            h.setLevel(logging.DEBUG if isinstance(h, RotatingFileHandler) else level_num)
        _root.setLevel(level_num)
        return _root

    if log_file is None:
        paths.ensure_dirs()
        log_file = paths.log_dir() / "rdc.log"

    _root = logging.getLogger("rdc")
    for h in _root.handlers:
        h.close()
    _root.handlers.clear()
    _root.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level_num)

    timestamper = structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False)
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        strip_emoji,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            foreign_pre_chain=shared,
        )
    )
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
            ],
            foreign_pre_chain=shared,
        )
    )
    _root.addHandler(file_handler)
    _root.addHandler(console_handler)

    _configured = True
    return _root


def get_logger(name: str | None = None) -> Any:  # noqa: ANN401
    """返回结构化 logger（挂在 ``rdc.`` 命名树下，统一经根 handler 落盘）。

    - 带 ``name``（新代码）：structlog ``BoundLogger``，如
      ``get_logger("config.migration")`` → logger 名 ``rdc.config.migration``。
    - 无参（v4 搬迁代码兼容面）：:class:`_StdlibLoggerAdapter`，
      挂在根 ``rdc`` logger 上，保持 v4 单 logger 语义。
    """
    if not _configured:
        configure_logging()
    if name is None:
        return _StdlibLoggerAdapter("rdc")
    return structlog.get_logger(f"rdc.{name}")

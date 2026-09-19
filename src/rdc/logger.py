"""
分级日志系统
支持文件+控制台输出，日志轮转
"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".remote_desktop" / "logs"
LOG_FILE = LOG_DIR / "remote_desktop.log"
MAX_LOG_SIZE = 10 * 1024 * 1024
BACKUP_COUNT = 5

_logger: logging.Logger | None = None


class EmojiFilter(logging.Filter):
    """移除日志中的emoji字符"""

    # 编译正则表达式，匹配所有emoji
    EMOJI_PATTERN = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # 表情符号
        "\U0001F300-\U0001F5FF"  # 符号与象形文字
        "\U0001F680-\U0001F6FF"  # 交通与地图
        "\U0001F700-\U0001F77F"  # 炼金术符号
        "\U0001F780-\U0001F7FF"  # 几何形状
        "\U0001F800-\U0001F8FF"  # 补充箭头
        "\U0001F900-\U0001F9FF"  # 补充符号与象形文字
        "\U0001FA00-\U0001FA6F"  # 棋盘棋子
        "\u2700-\u27BF"          # 装饰符号
        "\u2600-\u26FF"          # 杂项符号
        "\u23F0-\u23FF"          # 其他符号
        "]+",
        flags=re.UNICODE
    )

    def filter(self, record):
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            record.msg = self.EMOJI_PATTERN.sub('', record.msg)
        if hasattr(record, 'args') and record.args and isinstance(record.args, tuple):
            # 处理格式化参数中的emoji
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(self.EMOJI_PATTERN.sub('', arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        return True


def setup_logger(level: str = "INFO") -> logging.Logger:
    """初始化日志系统"""
    global _logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _logger is None:
        _logger = logging.getLogger("RemoteDesktop")
        _logger.setLevel(getattr(logging, level.upper(), logging.INFO))

        # 清除可能存在的旧handler
        _logger.handlers.clear()

        # 文件Handler - 使用UTF-8
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_LOG_SIZE,
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        # 给文件Handler也添加emoji过滤器，解决Windows下日志文件乱码（锟斤拷）
        file_handler.addFilter(EmojiFilter())
        _logger.addHandler(file_handler)

        # 控制台Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        ))
        # 添加emoji过滤器
        console_handler.addFilter(EmojiFilter())
        _logger.addHandler(console_handler)

    else:
        # 如果logger已存在，确保过滤器已添加
        for handler in _logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not handler.filters:
                handler.addFilter(EmojiFilter())
        _logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    return _logger


def get_logger() -> logging.Logger:
    """获取日志实例"""
    if _logger is None:
        return setup_logger()
    return _logger


def get_quality_logger() -> logging.Logger:
    """获取质量日志实例"""
    logger = logging.getLogger("RemoteDesktop.Quality")
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "quality.log",
            maxBytes=MAX_LOG_SIZE,
            backupCount=3,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(handler)
    return logger


def set_log_level(level: str) -> None:
    """动态设置日志级别"""
    logger = get_logger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

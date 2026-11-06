"""控制台流安全守卫（走查第十二批：PyInstaller ``--windowed`` 兼容）。

背景（真实崩溃面）：``--windowed``/``pythonw`` 启动时 ``sys.stdout`` /
``sys.stderr`` / ``sys.stdin`` 可能是 ``None``（无控制台窗口）。此时任何
``sys.stdout.isatty()`` / ``sys.stdin.isatty()`` 都是 ``AttributeError``，
``logging.StreamHandler(sys.stdout)`` 拿到 ``None`` 后首条日志即崩。

设计红线（用户明确要求，且前车之鉴严重）：
本模块**只做"取不到就当作不可用"的纯判断**，绝不替换、伪造或接管真实流，
绝不影响 GUI 启动分支与连接状态机：

- :func:`is_tty` —— 有终端 → ``True``；``None``/非可调用/自抛异常 → ``False``。
  正常交互终端下的返回值与 ``stream.isatty()`` **逐字节一致**（既有测试
  ``test_*tty*`` 契约不变）。
- :func:`console_stream` —— 仅在 ``sys.stdout`` 为 ``None`` 时才返回丢弃写入的
  :class:`_NullStream`；非 ``None`` 一律原样返回真流（不包装、不复制，
  日志输出目标与修复前完全相同）。

``print`` 无需守卫：CPython 的 ``print`` 在 ``sys.stdout is None`` 时本就静默
返回（不是异常），因此 :func:`rdc.cli._print_host_banner` 等调用点零改动。
"""

from __future__ import annotations

import sys
from typing import IO, Any

__all__ = ["console_stream", "is_tty", "stdin_is_absent", "stdin_is_tty", "stdout_is_tty"]


class _NullStream:
    """丢弃一切写入的空流（仅在无控制台打包形态下顶替 ``sys.stdout``）。

    只做 logging handler 需要的最小面：``write`` / ``flush`` / ``isatty`` /
    ``close``。永不抛错、永不分配（无缓冲累积），也不提供 ``fileno``——
    避免任何"看起来像真终端"的误判面。
    """

    def write(self, _s: str) -> int:  # noqa: D401 - 接口对齐用
        return len(_s)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        return None


def is_tty(stream: object) -> bool:
    """安全判定流是否连接终端；取不到答案一律 ``False``。

    Args:
        stream: 任意对象（通常是 ``sys.stdout`` / ``sys.stdin``，打包形态
            下可能是 ``None``）。

    Returns:
        ``True`` 仅当 ``stream.isatty()`` 明确返回真值。``stream`` 为
        ``None``、无 ``isatty``、``isatty`` 非可调用、或调用抛
        ``Exception``/``ValueError``/``OSError`` 时一律 ``False``。

    Note:
        这里捕获的异常**只允许来自探测性 API 调用**（isatty 本身无副作用），
        与"业务路径静默吞错"红线无关——本模块不做任何业务判断，且失败方向
        是保守的（判定为非终端 → 走非交互分支，不会误以为在真人终端前）。
    """
    probe = getattr(stream, "isatty", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception as exc:  # noqa: BLE001 - 仅限无副作用探测调用，保守落 False
        # 不静默：本模块无 logger 依赖（避免与 logging 装配循环导入），
        # 借 sys.stderr 尽力留痕；无控制台形态下 print 自身安全。
        print(f"console_safety: isatty 探测失败，按非终端处理: {exc!r}", file=sys.stderr)
        return False


def stdout_is_tty() -> bool:
    """``sys.stdout`` 是否为真实终端（打包无控制台 → ``False``）。"""
    return is_tty(sys.stdout)


def stdin_is_tty() -> bool:
    """``sys.stdin`` 是否为真实终端（打包无控制台 → ``False``）。"""
    return is_tty(sys.stdin)


def stdin_is_absent() -> bool:
    """``sys.stdin`` 是否**根本不存在**（PyInstaller ``--windowed`` 形态）。

    与 :func:`stdin_is_tty` 的区别是 GUI 启动判定必须掌握的事实：

    - ``stdin is None`` = **没有控制台**（windowed 打包产物 / pythonw）：
      这是"双击图标的桌面程序"，**应当**起 GUI；
    - ``stdin`` 存在但 ``not isatty()`` = 有控制台、只是被重定向/管道
      （``rdc < notes.txt``、CI 无终端）：这不是交互用户，打印帮助更安全。

    前车之鉴：把这两种情形合并成"非终端就不起 GUI"，windowed 包永远只会
    打印帮助后退出 → 双击没窗口（用户报告的"GUI 随机失效"病灶之一）。
    """
    return sys.stdin is None


def console_stream() -> IO[Any]:
    """返回可安全交给 ``logging.StreamHandler`` 的输出流。

    ``sys.stdout`` 存在 → **原对象**（行为与修复前逐字节一致）；
    为 ``None`` → :class:`_NullStream`（日志继续落文件 handler，控制台侧丢弃）。
    """
    stream = sys.stdout
    return stream if stream is not None else _NullStream()  # type: ignore[return-value]

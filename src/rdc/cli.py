"""rdc 命令行入口。

阶段 1 为脚手架最小实现，仅提供 ``--version`` 与占位帮助。
完整子命令（serve / control / set-password / install-autostart 等）
在后续阶段随对应模块迁移补齐。

v4 对照：``main.py:189 main()`` 的 argparse 逻辑将在此重写为子命令结构，
Tkinter 启动菜单（``main.py:show_gui()``）迁移到 ``rdc.ui.launcher``（阶段 6）。
"""

from __future__ import annotations

import argparse

from rdc import __version__


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        配置好的 ``ArgumentParser`` 实例。
    """
    parser = argparse.ArgumentParser(
        prog="rdc",
        description=f"远程桌面控制 v{__version__}",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"远程桌面控制 v{__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """程序主入口。

    Args:
        argv: 命令行参数列表；为 ``None`` 时读取 ``sys.argv[1:]``。

    Returns:
        进程退出码，0 表示成功。
    """
    parser = build_parser()
    parser.parse_args(argv)
    # 阶段 1：无子命令，仅打印提示。后续阶段接入 serve/control 分派。
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

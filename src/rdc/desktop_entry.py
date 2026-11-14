"""打包入口与控制台形态裁决（阶段 10）。

PyInstaller 产物必须是 **console 形态**（``console=True``），原因不是"需要打印"，
而是两条 CLI 硬需求：
1. ``--version`` / ``--help`` / ``serve`` 横幅要能在命令行看到（argparse 内部写
   ``sys.stdout``，``--windowed`` 下该流为 ``None`` → 直接 AttributeError 崩）；
2. ``set-password`` 的 ``getpass`` 交互必须有真实控制台。

而 GUI/托盘形态（双击图标、开机自启）**不能**留黑框——Feature 17 的硬要求是
"静默进被控端"。裁决办法：console exe 在**进入 CLI 装配之前**对 GUI 形态调用
``FreeConsole()``，Windows 立即销毁那个启动瞬间的控制台窗口。

⚠️ 关键收尾：FreeConsole 后必须把 ``sys.stdout/stderr/stdin`` 置 ``None``。
否则 :func:`rdc.console_safety.console_stream` 仍认为"stdout 存在"并返回一个
指向已失效句柄的对象——首启随机密码横幅（Feature 18）写它时抛 OSError 炸掉
被控端启动。置 None 后：
- ``console_stream()`` → ``_NullStream``（写操作安全静默）；
- ``stdin_is_absent()`` → True，正好落回守卫既有的"**无控制台必须起 GUI**"分支
  （:func:`rdc.cli._gui_launch_expected` 三态判定语义天然继续成立）。

非 frozen（源码/venv）不裁决：开发期在终端里跑 ``serve --tray`` 本就该看到日志。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from rdc.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["detached_console", "frozen_entry", "keeps_console"]

_LOG = get_logger("desktop_entry")


def keeps_console(argv: Sequence[str]) -> bool:
    """本次调用是否需要保留控制台窗口（纯函数，测试主断言面）。

    保留 → 用户主动在终端里用命令行；释放 → 桌面 GUI / 托盘常驻形态。

    ======  ============================================
    调用    裁决
    ======  ============================================
    无参数  False（双击图标 → GUI）
    --gui   False
    serve   True（headless 被控端：横幅 + Ctrl+C）
    serve --tray  False（托盘常驻 = 桌面形态，含开机自启 --silent）
    其余    True（control / set-password / --version / --help）
    ======  ============================================
    """
    args = list(argv)
    if not args:
        return False
    head = args[0]
    if head == "--gui":
        return False
    if head == "serve":
        return "--tray" not in args
    return True


def detached_console() -> bool:
    """释放控制台并把三个标准流置 None（失败返回 False，不抛）。

    非 Windows / 无控制台 / ``FreeConsole`` 失败一律返回 False：调用方按"保持
    现状"继续，绝不因释放失败影响主流程。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        if not ctypes.windll.kernel32.FreeConsole():
            return False
    except Exception as exc:  # noqa: BLE001 - 释放失败保持现状即可
        _LOG.warning("console_detach_failed", error=repr(exc))
        return False
    # 句柄已失效，流对象必须一起作废（见模块 docstring 的 Feature 18 病灶）。
    # mypy 允许对这三个模块属性赋 None（types.ModuleType 属性不受字面类型约束），
    # 故无需 type: ignore —— 加了反而触发 unused-ignore（项目 enable_error_code
    # 开了 ignore-without-code，冗余抑制同样算错误）。
    sys.stdout = None
    sys.stderr = None
    sys.stdin = None
    return True


def frozen_entry(argv: Sequence[str] | None = None) -> int:
    """打包形态真入口：先裁决控制台形态，再进 CLI 装配。

    Args:
        argv: 参数列表（``None`` 读 ``sys.argv[1:]``；测试注入）。

    Returns:
        进程退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False) and not keeps_console(args):
        detached_console()
    from rdc.cli import main

    return main(args)


if __name__ == "__main__":  # pragma: no cover - 打包入口
    raise SystemExit(frozen_entry())

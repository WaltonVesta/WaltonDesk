"""打包入口与控制台形态裁决（阶段 10；收口批架构反转）。

PyInstaller 产物是 **windowed 形态**（``console=False``）。真机验收发现
console=True 的产物双击时必然闪过一个控制台黑框（bootloader 先建控制台、
我们的代码再销毁它，"先有再关"物理上关不掉那一瞬），用户明确要求消除。
windowed 从根上解决：**进程本来就没有控制台**，双击永远无黑框。

windowed 的代价是 ``sys.stdout/stderr/stdin`` 为 ``None``，两条 CLI 硬需求
必须另行满足：

1. ``--version`` / ``--help`` / ``serve`` 横幅要能显示（argparse 内部写
   ``sys.stdout``；console_safety 只保证"不崩"，不保证"看得见"）；
2. ``set-password`` 的 ``getpass`` 交互、``control`` 的指纹确认要有可输入的终端。

裁决办法：:func:`keeps_console` 判定本次调用需要真实终端时，由
:func:`ensure_console` **按需补一个控制台**——优先 ``AttachConsole`` 挂接父
控制台（从 cmd/PowerShell 启动的场景），挂不上再 ``AllocConsole`` 新建
（资源管理器双击 + 参数的场景），随后把三个标准流重连到控制台设备
（``CONOUT$``/``CONIN$``）。GUI/托盘形态（无参数、``--gui``、
``serve --tray``）**什么都不做**——不创建、不附加，物理上不可能有黑框。

⚠️ 已重定向标准流时（管道/文件，含测试 harness）绝不触碰控制台：
``sys.stdout`` 非 None 即直接返回，输出照旧进重定向目标——否则会把用户的
`exe > out.txt` 重定向劫持进弹出来的控制台窗口。

⚠️ GUI 形态三标准为 None 的既有防线不动：``rdc.console_safety`` 把日志流
换成 ``_NullStream``、CPython ``print`` 对 stdout=None 本就静默、首启密码
有托盘气泡 + 非模态弹窗兜底（走查第十四批 Feature 18 三通道设计）。

已知取舍（如实记录，见 STAGE10_REPORT）：windowed 是 GUI 子系统进程，从
cmd 里跑 ``serve``（headless）时 cmd 不会等待该进程——横幅可见但提示符立即
返回、Ctrl+C 也发不到它。日常被控端用法是双击/自启的 ``--tray`` 形态，不
受影响；headless 调试推荐源码形态 ``python -m rdc serve``。

非 frozen（源码/venv）不裁决：开发期在终端里跑什么形态都该看到日志。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO

from rdc.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

__all__ = ["ensure_console", "frozen_entry", "keeps_console"]

_LOG = get_logger("desktop_entry")

#: AttachConsole 的特殊参数：附加到调用进程（父进程）的控制台。
_ATTACH_PARENT_PROCESS = 0


def keeps_console(argv: Sequence[str]) -> bool:
    """本次调用是否需要可用终端（纯函数，测试主断言面）。

    需要 → 用户主动在命令行里用它；不需要 → 桌面 GUI / 托盘常驻形态。

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


def _reopen_std_streams(*, kernel32: Any | None = None) -> bool:
    """从继承的 Win32 标准句柄重建 Python 标准流（重定向/继承双保真）。

    windowed（GUI 子系统）进程的 ``sys.stdout`` 恒为 None，但 Win32 层句柄
    往往**仍然有效**：cmd/PowerShell 起进程时照常传 STARTUPINFO 句柄，重定向
    （``exe > log.txt``）时 hStdOutput 就是那个文件。若视"sys.stdout 为 None"
    为"没有输出目标"而直接 AttachConsole，``> log.txt`` 会被劫持进控制台
    ——首启密码/横幅静默离开用户的重定向文件（收口批核验实测：重定向文件
    零字节）。

    正确顺序：**句柄能用就用句柄**——文件/管道/控制台一律从真句柄重建流，
    一个控制台窗口都不弹；只有三句柄全无效（资源管理器双击、explorer 不传
    句柄）才轮到 Attach/Alloc 补建。

    返回 True = 至少 stdout 已重建可用。stdin 单独判定：无效则保持 None，
    交互式口令由 console_safety 的"非交互终端"降级提示接管，绝不半接半漏。
    """
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    if kernel32 is None:
        kernel32 = ctypes.windll.kernel32
        # 句柄必须按 HANDLE 声明，ctypes 缺省 c_int 会把 64 位句柄截断成 32 位
        # （与 input/windows.py 的 argtypes 审计哨兵同一条纪律）
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetFileType.restype = wintypes.DWORD
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]

    invalid = wintypes.HANDLE(-1).value

    def _stream(idx: int, flags: int) -> TextIO | None:
        try:
            handle = kernel32.GetStdHandle(idx)
            if not handle or handle == invalid:
                return None
            if kernel32.GetFileType(handle) == 0:  # FILE_TYPE_UNKNOWN = 无目标
                return None
            fd = msvcrt.open_osfhandle(int(handle), flags)
            return os.fdopen(fd, "w" if flags == os.O_WRONLY else "r", encoding="utf-8", errors="replace")
        except OSError:
            return None

    out = _stream(-11, os.O_WRONLY)  # STD_OUTPUT_HANDLE
    if out is None:
        return False
    sys.stdout = out
    err = _stream(-12, os.O_WRONLY)  # STD_ERROR_HANDLE
    if err is not None:
        sys.stderr = err
    else:
        sys.stderr = out
    inp = _stream(-10, os.O_RDONLY)  # STD_INPUT_HANDLE
    if inp is not None:
        sys.stdin = inp
    return True


def _reconnect_stdio() -> bool:
    """把三个标准流重连到当前控制台设备（CONOUT$/CONIN$）。

    windowed 进程附加/新建控制台后，Win32 标准句柄**不会**自动更新，必须在
    Python 层显式打开控制台设备名。open 出来的流 ``isatty()`` 为 True，
    argparse 输出与 getpass 交互因此恢复；打开失败返回 False（调用方保持
    现状，绝不半接半漏）。
    """
    try:
        # 无 with 是**故意的**：这三个流替换 sys.stdout/stderr/stdin，生命周期=
        # 整个进程，with 退出会把它们关掉（等价于把标准流关掉）。SIM115 属误报。
        out: TextIO = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115
        err: TextIO = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115
        inp: TextIO = open("CONIN$", encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError as exc:  # 设备打不开（罕见的策略限制/句柄耗尽）
        _LOG.warning("console_stdio_reconnect_failed", error=repr(exc))
        return False
    sys.stdout = out
    sys.stderr = err
    sys.stdin = inp
    return True


def ensure_console(
    *,
    attach: Callable[[int], int] | None = None,
    alloc: Callable[[], int] | None = None,
    reconnect: Callable[[], bool] | None = None,
    reopen: Callable[[], bool] | None = None,
) -> bool:
    """CLI 形态在 windowed 产物里让标准输出真正可用（失败返回 False，不抛）。

    四个依赖参数全部可注入（测试用假实现验证调用序列），缺省走真实实现；
    决策顺序（每一步都有实测场景对应）：

    1. ``sys.stdout`` 已非 None（源码/venv 形态）→ True，不碰任何 Win32 面。
    2. :func:`_reopen_std_streams`：继承的 Win32 句柄可用就**直接用句柄**——
       重定向（``> log.txt``）与管道保真、继承控制台同样保真，全程零弹框。
       收口批实测：跳过这步直接 Attach 会把重定向劫持进控制台（log 零字节）。
    3. ``AttachConsole(ATTACH_PARENT_PROCESS)``：explorer 系父进程不传句柄、
       但存在可挂接的父控制台时，挂回去。
    4. ``AllocConsole``：纯双击带参数（无任何控制台）→ 新建一个窗口，
       用户主动调命令行、本来就该看到输出。
    5. 全部失败 / 非 Windows → False，console_safety 的无终端降级路径接管。

    黑框保证依然成立：GUI/托盘形态（无参数、``--gui``、``serve --tray``）
    根本不调本函数；4 的 AllocConsole 只在 CLI 参数场景出现。
    """
    if sys.platform != "win32":
        return False
    if getattr(sys, "stdout", None) is not None:
        return True  # 源码形态：本来就在真终端里

    reopen = reopen or (lambda: _reopen_std_streams())
    if reopen():
        return True

    if attach is None or alloc is None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            attach = attach or kernel32.AttachConsole
            alloc = alloc or kernel32.AllocConsole
        except Exception as exc:  # noqa: BLE001 - 拿不到 kernel32 按无终端处理
            _LOG.warning("console_api_unavailable", error=repr(exc))
            return False
    reconnect = reconnect or _reconnect_stdio

    try:
        ok = bool(attach(_ATTACH_PARENT_PROCESS))
        if not ok:
            ok = bool(alloc())
    except Exception as exc:  # noqa: BLE001 - 控制台补建失败保持无终端现状
        _LOG.warning("console_ensure_failed", error=repr(exc))
        return False
    if not ok:
        return False
    return reconnect()


def frozen_entry(argv: Sequence[str] | None = None) -> int:
    """打包形态真入口：先裁决控制台形态，再进 CLI 装配。

    windowed 产物里 GUI/托盘形态**零控制台操作**（这是"双击不闪黑框"的
    全部秘密——不是"先建再关"，是"根本不建"）；仅 ``keeps_console`` 为真
    的调用补建控制台。

    Args:
        argv: 参数列表（``None`` 读 ``sys.argv[1:]``；测试注入）。

    Returns:
        进程退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False) and keeps_console(args):
        ensure_console()
    from rdc.cli import main

    return main(args)


if __name__ == "__main__":  # pragma: no cover - 打包入口
    raise SystemExit(frozen_entry())

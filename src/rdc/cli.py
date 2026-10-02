"""rdc 命令行入口（阶段 9 批 3：serve / set-password 子命令落地）。

- ``rdc serve``：被控端服务（纯 asyncio，**零 Qt**——GUI/托盘装配归批 4 的
  ``rdc.ui`` 入口；``--tray`` 选项仅置意图位，核心行为不变）；
- ``rdc set-password``：设置被控端口令——强度校验走
  :mod:`rdc.security.password_policy`（≥8 位 + 字母 + 数字，任务书 b 组第 5 项），
  bcrypt 哈希写 ``config.json``（方案 A：``Settings.password_hash``）；
- 口令输入经 ``getpass`` 不回显；**口令明文绝不落日志/返回值/异常文案**。

v4 对照：``main.py`` 的 argparse + Tkinter 启动菜单 → 子命令化；GUI 模式
（launcher 对话框）批 4 并入 ``rdc`` 无参路径。
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
from pathlib import Path

from rdc import __version__
from rdc.config.settings import Settings, load_settings, save_settings
from rdc.logging import get_logger
from rdc.security.password_policy import check_password, policy_error_text

_LOG = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（子命令：serve / set-password）。

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
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动被控端服务（画面推送 + 输入执行）")
    serve.add_argument("--host", dest="bind_host", default=None, help="监听地址（覆盖配置，如 127.0.0.1）")
    serve.add_argument("--config", default=None, help="配置文件路径（缺省用标准位置；便携/测试模式）")
    serve.add_argument("--tray", action="store_true", help="启用托盘（需 GUI 环境；批 4 接线）")

    setpw = sub.add_parser("set-password", help="设置被控端连接口令（强度校验 + bcrypt）")
    setpw.add_argument("--config", default=None, help="配置文件路径（缺省用标准位置）")
    setpw.add_argument(
        "--password",
        default=None,
        help="直接给定口令（脚本化用；缺省交互式 getpass 输入）",
    )
    return parser


def _run_serve(args: argparse.Namespace) -> int:
    """启动被控端并运行至 Ctrl+C（纯 asyncio 事件循环）。"""
    import asyncio

    from rdc.server.service import HostService

    settings = load_settings(Path(args.config)) if args.config else load_settings()
    if args.bind_host:
        settings.bind_host = args.bind_host

    async def _amain() -> int:
        import signal as _signal

        service = HostService(settings=settings, show_tray=args.tray)
        if not await service.start():
            _LOG.error("cli_serve_start_failed")
            return 1
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(_signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError):  # Windows Proactor 部分信号不支持
                loop.add_signal_handler(sig, stop.set)
        _print_host_banner(service, settings)
        try:
            await stop.wait()
        finally:
            await service.shutdown()
        return 0

    try:
        return _asyncio_run(_amain())
    except KeyboardInterrupt:
        return 0


def _print_host_banner(service: object, settings: Settings) -> None:
    """控制台就绪提示（只含地址/端口/开关，无敏感值）。"""
    print(
        f"被控端已启动: 监听 {settings.bind_host} "
        f"控制端口={settings.control_port} 画面端口={settings.port} "
        f"认证={'开' if settings.enable_auth and settings.password_hash else '关（任意口令）'} "
        f"(Ctrl+C 退出)"
    )


def _run_set_password(args: argparse.Namespace) -> int:
    """交互式校验强度并写入 bcrypt 哈希；返回进程退出码。"""
    from rdc.secrets.password import hash_password

    password = args.password
    if password is None:
        if not sys.stdin.isatty():
            print("非交互终端且未提供 --password，退出", file=sys.stderr)
            return 2
        password = getpass.getpass("新口令（≥8 位，含字母+数字）: ")
    issues = check_password(password)
    if issues:
        print(policy_error_text(issues), file=sys.stderr)
        return 2
    confirm = getpass.getpass("再次输入确认: ") if (args.password is None and sys.stdin.isatty()) else password
    if confirm != password:
        print("两次输入不一致，未保存", file=sys.stderr)
        return 2
    path = Path(args.config) if args.config else None
    settings = load_settings(path) if path else load_settings()
    settings.password_hash = hash_password(password)
    save_settings(settings, path)
    print("口令已保存（bcrypt rounds=12）。运行中的被控端需重启后生效。")
    _LOG.info("cli_password_set", backend="bcrypt")  # 只记事件，不记任何口令派生值
    return 0


def _asyncio_run(coro: object) -> int:
    """``asyncio.run`` 包装（Windows SIGINT 下 KeyboardInterrupt → 返回 0）。"""
    import asyncio

    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    """程序主入口。

    Args:
        argv: 命令行参数列表；为 ``None`` 时读取 ``sys.argv[1:]``。

    Returns:
        进程退出码，0 表示成功。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "set-password":
        return _run_set_password(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

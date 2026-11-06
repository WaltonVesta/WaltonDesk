"""rdc 命令行入口（阶段 9 批 4：serve / control / set-password / GUI 装配）。

- ``rdc serve``：被控端服务（纯 asyncio，**零 Qt**；``--tray`` 改走 GUI 装配
  ——托盘是 QSystemTrayIcon，需 Qt 事件循环，见 :mod:`rdc.ui.session`）；
- ``rdc control HOST``：主控端 CLI 冒烟（无 GUI，纯 asyncio）——连接、
  认证、接收画面帧统计、退出；
- ``rdc`` 无参：GUI 模式（launcher 选被控端/主控端，批 4 装配）；
- ``rdc set-password``：设置被控端口令——强度校验走
  :mod:`rdc.security.password_policy`（≥8 位 + 字母 + 数字，任务书 b 组第 5 项），
  bcrypt 哈希写 ``config.json``（方案 A：``Settings.password_hash``）；
- 口令输入经 ``getpass`` 不回显；**口令明文绝不落日志/返回值/异常文案**。

v4 对照：``main.py`` 的 argparse + Tkinter 启动菜单 → 子命令化；GUI 模式
（launcher 对话框）并入 ``rdc`` 无参路径（``--gui`` 显式入口同效）。
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdc import __version__
from rdc.config.settings import Settings, load_settings, save_settings, set_current
from rdc.console_safety import stdin_is_absent, stdin_is_tty
from rdc.logging import get_logger
from rdc.security.password_policy import check_password, policy_error_text

_LOG = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（子命令：serve / control / set-password）。

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
    parser.add_argument(
        "--gui",
        action="store_true",
        help="启动图形界面（等价于无子命令；launcher 选模式）",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动被控端服务（画面推送 + 输入执行）")
    serve.add_argument("--host", dest="bind_host", default=None, help="监听地址（覆盖配置，如 127.0.0.1）")
    serve.add_argument("--config", default=None, help="配置文件路径（缺省用标准位置；便携/测试模式）")
    serve.add_argument("--tray", action="store_true", help="启用系统托盘（走 GUI 事件循环装配）")
    serve.add_argument(
        "--silent",
        action="store_true",
        help="静默模式（开机自启用）：不打印控制台横幅，直接进被控端、不弹 Launcher",
    )

    control = sub.add_parser("control", help="主控端 CLI 冒烟（无 GUI：认证+收帧统计）")
    control.add_argument("host", help="被控端地址")
    control.add_argument("--port", type=int, default=None, help="控制端口（缺省取配置）")
    control.add_argument("--frames", type=int, default=5, help="收到 N 帧后退出（默认 5）")
    control.add_argument("--config", default=None, help="配置文件路径")

    setpw = sub.add_parser("set-password", help="设置被控端连接口令（强度校验 + bcrypt）")
    setpw.add_argument("--config", default=None, help="配置文件路径（缺省用标准位置）")
    setpw.add_argument(
        "--password",
        default=None,
        help="直接给定口令（脚本化用；缺省交互式 getpass 输入）",
    )
    return parser


def _run_serve(args: argparse.Namespace) -> int:
    """启动被控端：默认纯 asyncio 事件循环；``--tray`` 走 GUI 循环（QSystemTrayIcon）。"""
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path) if config_path else load_settings()
    if args.bind_host:
        settings.bind_host = args.bind_host
    # Feature 18：首次运行（无口令）自动生成随机强口令并落盘，供启动回显；
    # Feature 17：--silent（开机自启形态）不打印横幅、不进 GUI，纯后台服务。
    from rdc.server.initial_password import ensure_host_password

    silent = bool(getattr(args, "silent", False))
    ensure_host_password(settings)
    # 落盘写回**加载来源同一文件**（--config 便携/测试模式若写默认位置，
    # 生成的口令下次启动即丢失——用户拿到的初始密码永远连不上）。
    save_settings(settings, config_path)
    set_current(settings)
    if args.tray:
        from rdc.ui.session import host_session

        return _run_gui_with(host_session, settings)
    return _run_serve_headless(settings, silent=silent)


def _run_serve_headless(settings: Settings, *, silent: bool = False) -> int:
    """无 GUI 被控端：asyncio 跑至 Ctrl+C。``silent``（开机自启）不打印横幅。"""
    import asyncio

    from rdc.server.service import HostService

    async def _amain() -> int:
        import signal as _signal

        service = HostService(settings=settings, show_tray=False)
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
        if not silent:
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


def _run_gui_with(
    session: Callable[[Any, Settings], Awaitable[int]], settings: Settings
) -> int:
    """在 Qt/qasync 事件循环里跑一个会话协程（``serve --tray`` 与 GUI 入口共用）。"""
    import asyncio

    from rdc.ui.app import create_app, event_loop

    app = create_app([])
    # 退出由会话协程显式控制（stop Event → app.quit）：托盘常驻阶段无任何
    # 可见窗口，Qt 的"最后窗口关闭即退出"会在启动早期抢先 quit 夭折协程链
    # （同 ui/session.run_gui，2026-10-05 走查第二批）
    app.setQuitOnLastWindowClosed(False)
    loop = event_loop(app)
    asyncio.set_event_loop(loop)

    async def _amain() -> None:
        await session(app, settings)
        app.quit()

    loop.create_task(_amain())
    # run_forever 而非裸 app.exec()：qasync 需先登记 asyncio running-loop，
    # 否则协程第一步即抛 "is not the running loop"（同 ui/session.run_gui，
    # 2026-10-04 走查 bug；哨兵见 tests/integration/test_gui_startup_smoke.py）
    return int(loop.run_forever())


def _print_host_banner(service: object, settings: Settings) -> None:
    """控制台就绪提示（地址/端口/开关无敏感值；Feature 18 追加初始口令显眼回显）。"""
    print(
        f"被控端已启动: 监听 {settings.bind_host} "
        f"控制端口={settings.control_port} 画面端口={settings.port} "
        f"认证={'开' if settings.enable_auth and settings.password_hash else '关（任意口令）'} "
        f"(Ctrl+C 退出)"
    )
    notice = _initial_notice_text_safe(settings)
    if notice:
        # 显眼回显：空行 + 分隔线包裹，控制台里一眼可见（Feature 18 要求）
        print("=" * 48)
        print(notice)
        print("=" * 48)


def _initial_notice_text_safe(settings: Settings) -> str:
    """横幅里的初始口令回显（延迟导入 + 取不到则空串，绝不因回显炸掉启动）。"""
    try:
        from rdc.server.initial_password import initial_notice_text

        return initial_notice_text(settings)
    except Exception as exc:  # noqa: BLE001 - 回显失败不影响被控端已启动的事实
        _LOG.warning("initial_notice_render_failed", error=repr(exc))
        return ""


def _run_control(args: argparse.Namespace) -> int:
    """主控端 CLI 冒烟：连接 → 认证 → 收 N 帧统计 → 退出（无 GUI，零 Qt）。"""
    import asyncio

    from rdc.client.controller import ClientController
    from rdc.exceptions import AuthError, TlsError

    settings = load_settings(Path(args.config)) if args.config else load_settings()
    port = args.port or settings.control_port
    target_frames = max(1, args.frames)
    got = asyncio.Event()

    @dataclass
    class _Counter:
        frames: int = 0
        size: object = None
        left: int = 0
        top: int = 0

    counter = _Counter()

    class _Sink:
        def state_changed(self, state: str) -> None:
            print(f"[state] {state}")

        def frame_arrived(self, image: object, *, left: int = 0, top: int = 0) -> None:
            counter.frames += 1
            counter.size, counter.left, counter.top = image, left, top
            if counter.frames >= target_frames:
                got.set()

        def metrics_updated(self, **kw: object) -> None:
            if kw.get("fps"):
                print(f"[metrics] {kw}")

        def clipboard_from_remote(self, text: str) -> None:
            print(f"[clipboard] {text[:40]}")

    async def _amain() -> int:
        ctl = ClientController(
            settings=settings,
            image_factory=lambda w, h, arr: (w, h),  # CLI 冒烟不渲染，只计数
        )
        ctl.set_events(_Sink())
        try:
            await ctl.connect(
                args.host,
                port,
                get_password=lambda host: _cli_password(),
                confirm_fingerprint=lambda fp, host: _cli_fingerprint(fp, host),
            )
        except (AuthError, TlsError, ConnectionError, OSError) as exc:
            print(f"连接失败: {exc}", file=sys.stderr)
            return 1
        try:
            await asyncio.wait_for(got.wait(), timeout=60)
        except TimeoutError:
            print(f"{target_frames} 帧未在 60s 内到齐（已收 {counter.frames}）", file=sys.stderr)
            await ctl.disconnect()
            return 2
        print(
            f"[ok] 收到 {counter.frames} 帧；末帧尺寸={counter.size} "
            f"原点=({counter.left},{counter.top})"
        )
        await ctl.disconnect()
        return 0

    try:
        return _asyncio_run(_amain())
    except KeyboardInterrupt:
        return 0


def _cli_password() -> str:
    if not stdin_is_tty():
        print("非交互终端：无法索取口令（被控端未设密码时可忽略本提示）", file=sys.stderr)
        return ""
    return getpass.getpass("被控端口令: ")


def _cli_fingerprint(fingerprint: str, host: str) -> bool:
    print(f"首次连接 {host}，证书指纹：\n  {fingerprint}\n请核对后确认。")
    if not stdin_is_tty():
        return False  # 非交互默认拒绝（TOFU 安全口径）
    return input("信任该指纹? [y/N] ").strip().lower() == "y"


def _run_set_password(args: argparse.Namespace) -> int:
    """交互式校验强度并写入 bcrypt 哈希；返回进程退出码。"""
    from rdc.secrets.password import hash_password
    from rdc.server.initial_password import mark_password_user_set

    password = args.password
    if password is None:
        if not stdin_is_tty():
            print("非交互终端且未提供 --password，退出", file=sys.stderr)
            return 2
        password = getpass.getpass("新口令（≥8 位，含字母+数字）: ")
    issues = check_password(password)
    if issues:
        print(policy_error_text(issues), file=sys.stderr)
        return 2
    confirm = (
        getpass.getpass("再次输入确认: ")
        if (args.password is None and stdin_is_tty())
        else password
    )
    if confirm != password:
        print("两次输入不一致，未保存", file=sys.stderr)
        return 2
    path = Path(args.config) if args.config else None
    settings = load_settings(path) if path else load_settings()
    settings.password_hash = hash_password(password)
    # Feature 18：用户自行设口令 → 清"自动生成"态并删密钥库里的初始明文，
    # 否则启动会持续弹"初始密码"提示（与"这是你的密码"的事实矛盾）。
    mark_password_user_set(settings)
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


def _gui_launch_expected() -> bool:
    """无子命令时是否应当直接起 GUI（走查第十二批打包守卫关键判定）。

    三态不可合并（上次"死逻辑"回归的根源）：

    - ``sys.stdin`` 是终端 → 交互用户，起 GUI；
    - ``sys.stdin is None`` → **无控制台**（PyInstaller ``--windowed`` /
      pythonw）：这是双击图标的桌面用法，**必须**起 GUI；若与下一态合并成
      "非终端"，产物只会打印帮助后退出，表现为"窗口再也不出现"；
    - ``stdin`` 存在但非 tty（管道/重定向、CI）→ 不是交互用户，打印帮助。
    """
    return stdin_is_tty() or stdin_is_absent()


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
    if args.command == "control":
        return _run_control(args)
    if args.command == "set-password":
        return _run_set_password(args)
    if args.command is None and (args.gui or (_gui_launch_expected() and _has_display())):
        return _run_gui()
    parser.print_help()
    return 0


def _has_display() -> bool:
    """是否存在可用显示环境（无 GUI 的 CI/服务环境返回 False）。"""
    import os

    if sys.platform == "win32":
        return True  # Windows 桌面恒可起 Qt（offscreen 除外，交给 QT_QPA_PLATFORM）
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run_gui() -> int:
    """GUI 模式入口（launcher 选被控端/主控端）。"""
    from rdc.ui.session import run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())

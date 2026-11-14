"""开机自启（走查第十二批 Feature 17 重写）。

Windows: ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 注册表值
macOS: launchd LaunchAgent
Linux: systemd user unit

v4 遗留病灶（本批修复）：
- 旧命令为 ``"{pythonw}" "{script}" --service``——v5 CLI **没有** ``--service``
  参数，开机后落入"无子命令 → GUI 判定"分支弹出 Launcher，违背"自启必须
  静默进被控端"的产品要求；
- ``sys.argv[0]`` 在打包/模块启动下指向不稳（可能拿到临时引导脚本路径）。

新命令口径（三平台统一子命令，静默 = ``serve --silent``：不打印控制台横幅；
Windows 走 ``--tray`` 托盘常驻，用户仍可看到状态与初始密码气泡）：

- frozen（PyInstaller）：``"{exe}" serve --tray --silent``
- 源码/venv：``"{pythonw}" -m rdc serve --tray --silent``（pythonw 无控制台窗体）

可测性：Windows 注册表读写集中在 ``_winreg()``（函数内 import，测试经
``sys.modules`` 注入假模块）；命令组装独立成 :meth:`launch_command`，纯函数
断言不触注册表。非 Windows 分支保持 v4 行为（本机不可测，CI 补验口径同
阶段 7 范围决定）。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from rdc.brand import SERVICE_ID
from rdc.logging import get_logger

__all__ = ["AutostartManager", "REGISTRY_RUN_KEY", "SERVICE_NAME", "LEGACY_SERVICE_NAME"]

#: HKCU 自启动注册表位置（值名 = service_name）。
REGISTRY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 阶段 10 品牌更名：注册表值名 / plist / unit 名统一用它。
#: 单一来源 = :data:`rdc.brand.SERVICE_ID`（与 PID 文件同名，品牌不散落）。
SERVICE_NAME = SERVICE_ID

#: 更名前的旧值名。用户若曾勾选自启，旧项指向过时的解释器/脚本路径，开机
#: 既拉不起新程序又与新项并存（表现为"勾了自启却没生效"）。install/uninstall
#: 顺带清理它；query 在仅有旧项时仍如实报"已开启"（托盘勾选态不失真）。
LEGACY_SERVICE_NAME = "RemoteControlService"

_LOG = get_logger("host.autostart")


def _pythonw_interpreter(python_path: str) -> str:
    """python.exe → pythonw.exe（无控制台窗口形态）；已是 pythonw/非 CPython
    布局时原样返回。"""
    if python_path.endswith("pythonw.exe"):
        return python_path
    candidate = python_path.replace("python.exe", "pythonw.exe")
    if candidate != python_path and Path(candidate).exists():
        return candidate
    return python_path


class AutostartManager:
    """开机自启动管理器（Windows 注册表 / macOS launchd / Linux systemd）。

    Args:
        service_name: 注册表值名 / plist 与 unit 文件名（默认品牌名
            :data:`SERVICE_NAME`；阶段 10 前为 ``RemoteControlService``）。
        python_path: 解释器路径（测试注入）。
        frozen: 是否打包形态（``None`` = 读 ``sys.frozen`` 事实；测试注入）。
        exe_path: frozen 形态的可执行文件路径（``None`` = ``sys.executable``）。
    """

    def __init__(
        self,
        service_name: str = SERVICE_NAME,
        *,
        python_path: str | None = None,
        frozen: bool | None = None,
        exe_path: str | None = None,
    ) -> None:
        self.service_name = service_name
        self._python_path = python_path if python_path is not None else sys.executable
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._exe_path = exe_path if exe_path is not None else sys.executable

    # -- 命令组装（纯函数，测试主断言面） ---------------------------------------

    def launch_command(self) -> str:
        """当前平台开机执行的完整命令串（Windows 为注册表值内容）。

        静默三要素：子命令 ``serve`` 直进被控端（不弹 Launcher）；
        ``--silent`` 不打印控制台横幅；Windows 用 pythonw/exe 本体（无黑窗）。
        """
        if self._frozen:
            return f'"{self._exe_path}" serve --tray --silent'
        interpreter = _pythonw_interpreter(self._python_path)
        return f'"{interpreter}" -m rdc serve --tray --silent'

    # -- 平台分发 ---------------------------------------------------------------

    def is_enabled(self) -> bool:
        """自启当前是否已注册（读取失败按 False，可观测）。"""
        system = platform.system()
        try:
            if system == "Windows":
                return self._query_windows()
            if system == "Darwin":
                return self._plist_path().exists()
            return self._unit_path().exists()
        except Exception as exc:  # noqa: BLE001 - 探测失败=未知，保守 False 且留痕
            _LOG.warning("autostart_query_failed", system=system, error=repr(exc))
            return False

    def install(self) -> bool:
        """安装开机自启动。"""
        system = platform.system()
        try:
            if system == "Windows":
                return self._install_windows()
            if system == "Darwin":
                return self._install_macos()
            if system == "Linux":
                return self._install_linux()
            _LOG.warning("autostart_unsupported_system", system=system)
            return False
        except Exception as exc:  # noqa: BLE001 - 边界：结果经返回值可观测，不静默
            _LOG.error("autostart_install_failed", system=system, error=repr(exc))
            return False

    def uninstall(self) -> bool:
        """卸载开机自启动（未注册时视为成功，幂等）。"""
        system = platform.system()
        try:
            if system == "Windows":
                return self._uninstall_windows()
            if system == "Darwin":
                return self._uninstall_macos()
            if system == "Linux":
                return self._uninstall_linux()
            return False
        except Exception as exc:  # noqa: BLE001 - 同上，边界处可观测降级
            _LOG.error("autostart_uninstall_failed", system=system, error=repr(exc))
            return False

    # -- Windows：HKCU Run 注册表 -------------------------------------------------

    def _install_windows(self) -> bool:
        import winreg

        command = self.launch_command()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTRY_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as regkey:
            winreg.SetValueEx(regkey, self.service_name, 0, winreg.REG_SZ, command)
            self._drop_legacy_value(regkey)
        _LOG.info("autostart_windows_installed", command=command)
        return True

    def _drop_legacy_value(self, regkey: object) -> None:
        """删除更名前的旧自启值（幂等，失败不阻断本次注册）。

        只在 ``service_name`` 就是当前品牌名时清理——显式传入自定义服务名
        （多实例/测试场景）时不得越界删除别人的条目。
        """
        if self.service_name != SERVICE_NAME:
            return
        import winreg

        try:
            winreg.DeleteValue(regkey, LEGACY_SERVICE_NAME)  # type: ignore[arg-type]
            _LOG.info("autostart_legacy_value_removed", legacy=LEGACY_SERVICE_NAME)
        except FileNotFoundError:
            pass  # 从未勾过自启：正常路径
        except OSError as exc:  # noqa: BLE001 - 清理失败不影响新项已写入的事实
            _LOG.warning("autostart_legacy_value_remove_failed", error=repr(exc))

    def _uninstall_windows(self) -> bool:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTRY_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as regkey:
            try:
                winreg.DeleteValue(regkey, self.service_name)
                _LOG.info("autostart_windows_uninstalled")
            except FileNotFoundError:
                pass  # 本就未注册：幂等成功
            # 取消勾选时一并清掉旧名残留，否则它会继续开机拉起旧路径
            self._drop_legacy_value(regkey)
        return True

    def _query_windows(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_RUN_KEY) as regkey:
                value, _type = winreg.QueryValueEx(regkey, self.service_name)
                return bool(value)
        except FileNotFoundError:
            pass
        # 新名没有：回读旧名，如实反映"用户确实勾过自启"（托盘勾选态不至于
        # 显示未开启、用户点一下却什么都没发生）。
        if self.service_name != SERVICE_NAME:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_RUN_KEY) as regkey:
                legacy_value, _type = winreg.QueryValueEx(regkey, LEGACY_SERVICE_NAME)
        except (FileNotFoundError, OSError):
            return False
        return bool(legacy_value)

    # -- macOS：launchd（v4 行为保留，仅统一 serve 子命令） -----------------------

    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.service_name}.plist"

    def _install_macos(self) -> bool:
        plist_file = self._plist_path()
        plist_file.parent.mkdir(parents=True, exist_ok=True)
        args = ["serve", "--silent"]
        if self._frozen:
            program_args = [self._exe_path, *args]
        else:
            program_args = [self._python_path, "-m", "rdc", *args]
        args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self.service_name}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>"""
        plist_file.write_text(plist_content, encoding="utf-8")
        import subprocess  # nosec B404 -- 固定 argv 系统管理命令（不经 shell），无注入面

        subprocess.run(  # nosec B603 B607 -- argv 固定列表；路径为应用自写
            ["launchctl", "load", str(plist_file)], check=True
        )
        _LOG.info("autostart_macos_installed")
        return True

    def _uninstall_macos(self) -> bool:
        plist_file = self._plist_path()
        if plist_file.exists():
            import subprocess  # nosec B404 -- 固定 argv 系统管理命令（不经 shell）

            subprocess.run(  # nosec B603 B607 -- argv 固定；路径为应用自写
                ["launchctl", "unload", str(plist_file)], check=True
            )
            plist_file.unlink()
            _LOG.info("autostart_macos_uninstalled")
        return True

    # -- Linux：systemd user（v4 行为保留，仅统一 serve 子命令） ------------------

    def _unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{self.service_name}.service"

    def _install_linux(self) -> bool:
        user_dir = self._unit_path().parent
        user_dir.mkdir(parents=True, exist_ok=True)
        service_file = user_dir / f"{self.service_name}.service"
        if self._frozen:
            exec_start = f"{self._exe_path} serve --silent"
        else:
            exec_start = f"{self._python_path} -m rdc serve --silent"
        service_content = f"""[Unit]
Description={self.service_name}
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target"""
        service_file.write_text(service_content, encoding="utf-8")
        import subprocess  # nosec B404 -- 固定 argv 系统管理命令（不经 shell），无注入面

        subprocess.run(  # nosec B603 B607 -- argv 固定列表；unit 名应用自写
            ["systemctl", "--user", "daemon-reload"], check=True
        )
        subprocess.run(  # nosec B603 B607 -- 同上
            ["systemctl", "--user", "enable", "--now", service_file.name], check=True
        )
        _LOG.info("autostart_linux_installed")
        return True

    def _uninstall_linux(self) -> bool:
        service_file = self._unit_path()
        if service_file.exists():
            import subprocess  # nosec B404 -- 固定 argv 系统管理命令（不经 shell）

            # 字面 argv 逐条调用（与 _install_linux 同风格）：循环变量会让
            # bandit 看不到 argv 字面量，nosec 标注反而虚标（B607 不触发）。
            subprocess.run(  # nosec B603 B607 -- argv 固定列表；unit 名应用自写
                ["systemctl", "--user", "disable", "--now", service_file.name], check=True
            )
            subprocess.run(  # nosec B603 B607 -- argv 固定列表
                ["systemctl", "--user", "daemon-reload"], check=True
            )
            service_file.unlink()
            _LOG.info("autostart_linux_uninstalled")
        return True


# v4 兼容面：模块级便捷函数（托盘/Launcher 用）
_DEFAULT = AutostartManager()


def is_autostart_enabled() -> bool:
    """默认服务名的自启注册状态。"""
    return _DEFAULT.is_enabled()


def set_autostart(enable: bool) -> bool:
    """开启/关闭开机自启（返回最终成功与否；失败已记日志，调用方须提示用户）。"""
    return _DEFAULT.install() if enable else _DEFAULT.uninstall()

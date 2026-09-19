"""
系统托盘图标（被控端）
"""

import threading
import time
from typing import Optional, Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False

from logger import get_logger
from utils.ip import get_local_ip_v4, get_local_ip_v6


class SystemTray:
    def __init__(self, on_toggle_pause: Callable, on_restart: Optional[Callable] = None,
                 on_exit: Optional[Callable] = None,
                 is_paused_callback: Optional[Callable[[], bool]] = None):
        self.on_toggle_pause = on_toggle_pause
        self.on_restart = on_restart
        self.on_exit = on_exit
        self.is_paused_callback = is_paused_callback
        self.icon: Optional[pystray.Icon] = None
        self._running = False
        self._start_time = time.time()
        self._client_count = 0
        self._client_count_callback: Optional[Callable] = None
        self.logger = get_logger()
        self._menu_updated = False

    def set_client_count_callback(self, callback: Callable) -> None:
        self._client_count_callback = callback

    def create_icon_image(self) -> Image.Image:
        size = 64
        image = Image.new('RGB', (size, size), color='#1a1a2e')
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, size-4, size-4), fill='#2d7d2d')
        draw.ellipse((6, 6, size-6, size-6), fill='#4caf50')
        draw.text((20, 16), "R", fill='white', font=None)
        draw.text((12, 38), "DP", fill='white', font=None)
        return image

    def _update_client_count(self) -> None:
        if self._client_count_callback:
            try:
                self._client_count = self._client_count_callback()
            except Exception as e:
                self.logger.warning(f"获取客户端数量失败: {e}")

    def update_client_count(self, count: int) -> None:
        self._client_count = count
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        if not self.icon:
            return
        ip_v4 = get_local_ip_v4()
        ip_v6 = get_local_ip_v6()
        uptime = self._get_uptime()
        paused = self.is_paused_callback() if self.is_paused_callback else False
        status = "⏸ 已暂停" if paused else "▶ 运行中"
        self.icon.title = (
            f"🖥️ 远程控制服务\n"
            f"{status}\n"
            f"📡 IPv4: {ip_v4}\n"
            f"📡 IPv6: {ip_v6 or '未获取'}\n"
            f"👤 客户端: {self._client_count} 个\n"
            f"⏱️ 运行: {uptime}"
        )

    def _get_uptime(self) -> str:
        elapsed = time.time() - self._start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

    def _build_menu(self):
        """构建菜单"""
        paused = self.is_paused_callback() if self.is_paused_callback else False
        pause_label = "▶ 继续服务" if paused else "⏸ 暂停服务"
        
        menu_items = [
            pystray.MenuItem("📡 状态", self._show_status, default=True),
            pystray.MenuItem(f"📋 复制IPv4: {get_local_ip_v4()}", self._copy_ipv4),
            pystray.MenuItem(f"📋 复制IPv6: {get_local_ip_v6() or '无'}", self._copy_ipv6),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(pause_label, self._toggle_pause),
        ]
        if self.on_restart:
            menu_items.append(pystray.MenuItem("🔄 重启服务", self._restart_service))
        menu_items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🚪 退出", self._exit_app),
        ])
        return pystray.Menu(*menu_items)

    def update_menu(self) -> None:
        """更新菜单（动态改变暂停/继续文字）"""
        if self.icon:
            new_menu = self._build_menu()
            self.icon.menu = new_menu
            self._update_tooltip()

    def run(self) -> None:
        if not HAS_PYSTRAY:
            self.logger.warning("pystray未安装，托盘功能不可用")
            return
        if self._running:
            return
        self._running = True
        self._start_time = time.time()

        self._update_client_count()

        self.icon = pystray.Icon(
            "remote_service",
            self.create_icon_image(),
            self._get_tooltip_text(),
            self._build_menu()
        )

        def update_loop():
            while self._running and self.icon:
                time.sleep(5)
                if self.icon:
                    self._update_client_count()
                    self._update_tooltip()
                    # 动态更新菜单（如果暂停状态变化）
                    if self.is_paused_callback:
                        # 可以定期重建菜单，但为了性能，只在状态变化时更新
                        pass

        threading.Thread(target=update_loop, daemon=True).start()
        self.icon.run()

    def _get_tooltip_text(self) -> str:
        ip_v4 = get_local_ip_v4()
        ip_v6 = get_local_ip_v6()
        uptime = self._get_uptime()
        paused = self.is_paused_callback() if self.is_paused_callback else False
        status = "⏸ 已暂停" if paused else "▶ 运行中"
        return (
            f"🖥️ 远程控制服务\n"
            f"{status}\n"
            f"📡 IPv4: {ip_v4}\n"
            f"📡 IPv6: {ip_v6 or '未获取'}\n"
            f"👤 客户端: {self._client_count} 个\n"
            f"⏱️ 运行: {uptime}"
        )

    def _show_status(self) -> None:
        ip_v4 = get_local_ip_v4()
        ip_v6 = get_local_ip_v6()
        uptime = self._get_uptime()
        paused = self.is_paused_callback() if self.is_paused_callback else False
        status = "⏸ 已暂停" if paused else "✅ 运行中"
        if self.icon:
            self.icon.notify(
                f"{status}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"📡 IPv4: {ip_v4}:9999\n"
                f"📡 IPv6: {ip_v6 or '未获取'}:9999\n"
                f"👤 客户端: {self._client_count} 个\n"
                f"⏱️ 运行时长: {uptime}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"💡 使用主控端连接",
                "远程控制服务"
            )

    def _copy_ipv4(self) -> None:
        ip = get_local_ip_v4()
        self._copy_to_clipboard(ip)
        if self.icon:
            self.icon.notify(f"✅ 已复制: {ip}", "IPv4地址")

    def _copy_ipv6(self) -> None:
        ip = get_local_ip_v6()
        if ip:
            self._copy_to_clipboard(ip)
            if self.icon:
                self.icon.notify(f"✅ 已复制: {ip}", "IPv6地址")
        else:
            if self.icon:
                self.icon.notify("❌ 未获取到IPv6地址", "提示")

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            import subprocess
            import sys
            if sys.platform == 'win32':
                subprocess.run(['clip'], input=text.encode('utf-8'), check=True)
            elif sys.platform == 'darwin':
                subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
            else:
                try:
                    subprocess.run(['xclip', '-selection', 'c'], input=text.encode('utf-8'), check=True)
                except FileNotFoundError:
                    try:
                        subprocess.run(['wl-copy'], input=text.encode('utf-8'), check=True)
                    except FileNotFoundError:
                        import tkinter as tk
                        root = tk.Tk()
                        root.withdraw()
                        root.clipboard_clear()
                        root.clipboard_append(text)
                        root.update()
                        root.destroy()
            self.logger.debug(f"已复制: {text}")
        except Exception as e:
            self.logger.error(f"复制失败: {e}")

    def _toggle_pause(self) -> None:
        self.logger.info("切换暂停/继续")
        if self.on_toggle_pause:
            self.on_toggle_pause()
        # 更新菜单
        self.update_menu()

    def _restart_service(self) -> None:
        self.logger.info("🔄 用户请求重启服务")
        if self.icon:
            self.icon.notify("🔄 服务正在重启...", "远程控制服务")
        if self.on_restart:
            def do_restart():
                try:
                    self.on_restart()
                except Exception as e:
                    self.logger.error(f"重启服务失败: {e}")
                # 重启后更新菜单
                self.update_menu()
            threading.Thread(target=do_restart, daemon=True).start()

    def _exit_app(self) -> None:
        self.logger.info("🚪 用户请求退出应用")
        self._running = False
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None

        if self.on_exit:
            try:
                self.on_exit()
            except Exception as e:
                self.logger.error(f"退出应用失败: {e}")
        else:
            if self.on_toggle_pause:
                try:
                    self.on_toggle_pause()
                except Exception:
                    pass
            import os, sys
            os._exit(0)

    def stop(self) -> None:
        self._running = False
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
"""
程序入口
"""

import sys
import os
import threading
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def resource_path(relative_path: str) -> str:
    """获取资源文件路径（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：资源在临时解压目录 _MEIPASS
        base_path = sys._MEIPASS
    else:
        # 源码运行：资源在脚本所在目录
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


from logger import setup_logger, get_logger
from config import Config
from service.daemon import RemoteService
from service.autostart import AutostartManager
from service.pid_manager import PIDManager
from utils.ip import get_local_ip_v4, get_local_ip_v6

# v4.0.0：从包 __init__ 动态导入版本号
try:
    from __init__ import __version__
except ImportError:
    __version__ = "4.0.0"

# v4.0.0：强制依赖列表
_REQUIRED_DEPS = {
    'PIL': 'pillow',
    'pyautogui': 'pyautogui',
    'mss': 'mss',
    'pyperclip': 'pyperclip',
    'bcrypt': 'bcrypt',
    'cryptography': 'cryptography',
}


def run_service(show_tray: bool = True):
    """运行服务模式（进程保活）"""
    logger = setup_logger('INFO')
    logger.info("🚀 启动远程控制服务.")
    
    # 显示IP信息
    ip_v4 = get_local_ip_v4()
    ip_v6 = get_local_ip_v6()
    logger.info(f"📡 IPv4: {ip_v4}")
    if ip_v6:
        logger.info(f"📡 IPv6: {ip_v6}")
    
    service = RemoteService(show_tray=show_tray)
    if not service.start():
        logger.error("❌ 服务启动失败")
        input("按回车键退出...")
        return

    logger.info("⏳ 服务运行中，按 Ctrl+C 停止")
    try:
        # 主循环：进程保持运行，服务可能处于运行或暂停状态
        while not service.should_exit():
            if service.is_running():
                time.sleep(1)
            else:
                # 服务已暂停，进程仍在运行，等待恢复或退出
                time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("📌 收到停止信号，正在退出...")
        service.shutdown()
    finally:
        # 确保完全退出
        if not service.should_exit():
            service.shutdown()
        logger.info("进程已退出")


def show_gui():
    """显示GUI主菜单"""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("错误: tkinter未安装")
        sys.exit(1)
    
    root = tk.Tk()
    root.title(f"远程桌面控制 v{__version__}")
    root.geometry("450x450")
    root.resizable(False, False)
    
    # 设置图标
    try:
        root.iconbitmap(default=resource_path('icon.ico'))
    except:
        pass
    
    main_frame = ttk.Frame(root, padding=20)
    main_frame.pack(fill=tk.BOTH, expand=True)
    
    # 标题
    ttk.Label(main_frame, text="🖥️ 远程桌面控制", 
              font=("Arial", 18, "bold")).pack(pady=5)
    ttk.Label(main_frame, text=f"版本 {__version__}",
              font=("Arial", 9), foreground="gray").pack(pady=2)
    ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
    
    # IP地址显示
    ip_v4 = get_local_ip_v4()
    ip_v6 = get_local_ip_v6()
    
    ip_frame = ttk.Frame(main_frame)
    ip_frame.pack(pady=5, fill=tk.X)
    ttk.Label(ip_frame, text="📍 IPv4:", font=("Arial", 10)).pack(side=tk.LEFT)
    ttk.Label(ip_frame, text=ip_v4, font=("Arial", 10), foreground="blue").pack(side=tk.LEFT, padx=5)
    
    if ip_v6:
        ip_frame2 = ttk.Frame(main_frame)
        ip_frame2.pack(pady=5, fill=tk.X)
        ttk.Label(ip_frame2, text="📍 IPv6:", font=("Arial", 10)).pack(side=tk.LEFT)
        ttk.Label(ip_frame2, text=ip_v6, font=("Arial", 9), foreground="green").pack(side=tk.LEFT, padx=5)
    
    ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
    
    # 功能按钮
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(pady=10, fill=tk.X)
    
    def start_service():
        root.destroy()
        run_service(show_tray=True)
    
    def start_service_no_tray():
        root.destroy()
        run_service(show_tray=False)
    
    def start_control():
        root.destroy()
        try:
            from gui.main_window import MainWindow
            MainWindow().run()
        except Exception as e:
            print(f"启动主控端失败: {e}")
            input("按回车键退出...")
    
    def install_autostart():
        manager = AutostartManager()
        if manager.install():
            messagebox.showinfo("成功", "✅ 开机自启动已安装")
        else:
            messagebox.showerror("失败", "❌ 安装失败，请检查权限")
    
    def uninstall_autostart():
        manager = AutostartManager()
        if manager.uninstall():
            messagebox.showinfo("成功", "✅ 开机自启动已卸载")
        else:
            messagebox.showerror("失败", "❌ 卸载失败")
    
    ttk.Button(btn_frame, text="🚀 启动被控端服务", 
               command=start_service, width=25).pack(pady=4)
    ttk.Button(btn_frame, text="🖥️ 启动主控端", 
               command=start_control, width=25).pack(pady=4)
    
    ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
    
    ttk.Button(btn_frame, text="⚙️ 安装开机自启动", 
               command=install_autostart, width=25).pack(pady=4)
    ttk.Button(btn_frame, text="🗑️ 卸载开机自启动", 
               command=uninstall_autostart, width=25).pack(pady=4)
    
    ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
    ttk.Label(main_frame, text="💡 被控端启动后在后台运行，可使用托盘管理", 
              foreground="gray", font=("Arial", 8)).pack()
    ttk.Label(main_frame, text="💡 按 Ctrl+C 可停止服务", 
              foreground="gray", font=("Arial", 8)).pack()
    
    root.mainloop()


def main():
    """主入口"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(description=f"远程桌面控制 v{__version__}")
    parser.add_argument('--service', action='store_true', help='以服务模式运行')
    parser.add_argument('--no-tray', action='store_true', help='不显示托盘图标')
    parser.add_argument('--install', action='store_true', help='安装开机自启动')
    parser.add_argument('--uninstall', action='store_true', help='卸载开机自启动')
    parser.add_argument('--set-password', type=str, default=None, metavar='PASSWORD',
                        help='设置被控端密码（Bcrypt加密）并退出')
    parser.add_argument('--version', action='store_true', help='显示版本信息')
    
    args = parser.parse_args()
    
    if args.version:
        print(f"远程桌面控制 v{__version__}")
        return

    if args.set_password is not None:
        config = Config()
        config.set_password(args.set_password)
        print("✅ 密码已设置（Bcrypt加密）")
        return
    
    if args.install:
        manager = AutostartManager()
        if manager.install():
            print("✅ 开机自启动已安装")
        else:
            print("❌ 安装失败")
        return
    
    if args.uninstall:
        manager = AutostartManager()
        if manager.uninstall():
            print("✅ 开机自启动已卸载")
        else:
            print("❌ 卸载失败")
        return
    
    if args.service:
        run_service(show_tray=not args.no_tray)
        return
    
    # v4.0.0：强化依赖检查（新增 pyperclip、bcrypt、cryptography 为强制依赖）
    missing = []
    for module, pip_name in _REQUIRED_DEPS.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"❌ 缺少依赖: {', '.join(missing)}")
        print(f"请安装: pip install {' '.join(missing)}")
        input("按回车键退出.")
        sys.exit(1)
    
    # 启动GUI
    show_gui()


if __name__ == "__main__":
    main()
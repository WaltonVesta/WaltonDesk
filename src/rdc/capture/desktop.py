"""Windows 输入桌面探针（走查第六批 P0：安全桌面检测）。

用途：被控端采集连续失败时，区分"远端切到了安全桌面（UAC 提权确认框、
Ctrl+Alt+Del、登录界面）"与"其它故障"。安全桌面由 Winlogon 持有，普通
用户态进程既截不到屏（BitBlt 失败）也注不进键鼠（SendInput 无效）——
这不是缺陷，是 Windows 的安全边界；v5.0.0 的正确行为是**如实告知 UI**
并等 UAC 结束后自愈（采集源内部已挂 MSS 句柄重建）。

实现（Win32）：``OpenInputDesktop`` 取当前接收输入的桌面名，等于
``Winlogon`` 即安全桌面。任何失败（非 Windows、句柄权限不足、API 异常）
一律返回 ``False``——探针只做锦上添花的归因，绝不引入新故障面：调用方
在 ``False`` 时仍可凭"连续采集失败"本身提示画面停滞，``True`` 时进一步
告诉用户"远端正在安全桌面（如 UAC 确认中）"。

参考：MSDN OpenInputDesktop / GetUserObjectInformationW。
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import sys

__all__ = ["is_secure_desktop"]

#: GetUserObjectInformation 的信息类：桌面名字符串
_UDESKTOP_NAME = 2
#: OpenInputDesktop 的最小访问权限（仅查询名字）
_DESKTOP_READOBJECTS = 0x0001
#: 安全桌面的固定名称（Winlogon 持有：UAC 确认框、登录界面、Ctrl+Alt+Del）
_WINLOGON = "winlogon"


def is_secure_desktop() -> bool:
    """当前输入桌面是否为安全桌面（Winlogon）。

    Returns:
        ``True`` 仅当运行于 Windows 且成功读到输入桌面名为 ``Winlogon``；
        非 Windows 平台或任何 API 失败返回 ``False``（保守：宁可不提示，
        也不把其它故障误报成"安全桌面"）。
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except AttributeError:  # pragma: no cover - 仅极端精简的 Windows
        return False
    handle = 0
    try:
        handle = int(user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS))
        if not handle:
            return False
        buf = ctypes.create_unicode_buffer(256)
        size = ctypes.wintypes.DWORD(0)
        if not kernel32.GetUserObjectInformationW(
            handle, _UDESKTOP_NAME, buf, ctypes.sizeof(buf), ctypes.byref(size)
        ):
            return False
        return str(buf.value).lower() == _WINLOGON
    except (OSError, ValueError):
        return False
    finally:
        if handle:
            with contextlib.suppress(OSError, ValueError):
                user32.CloseDesktop(handle)

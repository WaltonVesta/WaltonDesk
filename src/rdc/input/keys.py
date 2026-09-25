"""Qt 键码 → v5 规范键名映射（阶段 7 批 E，IME 支持·键名侧）。

链路定位（阶段 7 决策点 3 的"发送侧不破坏 IME"）::

    本地键盘 → ScreenView.keyEvent(int qt_key, bool press)     [Qt 原生]
             → controller.send_key(qt_key, press)              [阶段 9 装配]
             → qt_key_to_name(qt_key)                          [本模块]
             → InputBackend.key_press/release(规范名)           [批 A 契约]
    IME 候选窗提交文本 → ScreenView.textCommitted(str)          [批 E]
             → controller.send_text(text)
             → InputBackend.type_text(text)                    [KEYEVENTF_UNICODE]

即：**组合态永不进协议**（候选窗是控制端本地 UI 状态），只有"已上屏"
的文本与确定键的键名外发；键名归一口径与 :func:`rdc.input.base.normalize_key_name`
的规范值域一致。

映射规则：
- ASCII 可打印（0x20–0x7E）→ 单字符小写名（``Key_Space`` 例外 → ``"space"``）；
- Qt 功能/修饰/导航键（0x01000000 起）→ 规范名查表；
- 小键盘数字（0x30000000 段）→ ``kp0``–``kp9``（与被控端 NumLock 解耦）；
- 未知键 → ``None``（调用方决定丢弃并记日志，不抛错——键盘事件流上
  抛异常会打断 UI）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

__all__ = ["qt_key_to_name"]

#: Qt 功能键（0x01000000+）→ v5 规范名。
_QT_FUNCTION: dict[int, str] = {
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",  # Shift+Tab 的键本体仍是 tab（Shift 由 modifiers 表达）
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",  # 小键盘 Enter：Windows 上 VK 同为 0x0D
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_Print: "printscreen",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_PageUp: "pageup",
    Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Meta: "win",  # Qt 的 Meta = Win/Cmd
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_CapsLock: "capslock",
    Qt.Key.Key_NumLock: "numlock",
    Qt.Key.Key_ScrollLock: "scrolllock",
    Qt.Key.Key_Space: "space",
}

#: 功能键 F1–F24（Qt 编码 0x1000030 起连续）。
_QT_FUNCTION.update({int(Qt.Key.Key_F1) + i: f"f{i + 1}" for i in range(24)})


def qt_key_to_name(qt_key: int) -> str | None:
    """Qt 键码 → v5 规范键名（见模块 docstring 的映射规则）。

    Args:
        qt_key: :class:`PySide6.QtCore.Qt.Key` 的整数值（来自
            :class:`QKeyEvent` 的 ``key()``）。

    Returns:
        规范键名；无法识别的键返回 ``None``。
    """
    key = Qt.Key(qt_key)
    name = _QT_FUNCTION.get(qt_key)
    if name is not None:
        return name
    code = int(key)
    if 0x21 <= code <= 0x7E:  # 可打印 ASCII（不含空格——表内映射为 "space"）
        return chr(code).lower()
    if 0x30000000 <= code <= 0x30000009:  # 小键盘数字段
        return f"kp{code - 0x30000000}"
    return None

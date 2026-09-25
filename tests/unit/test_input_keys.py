"""rdc.input.keys 单元测试（阶段 7 批 E：Qt 键码 → 规范键名）。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from rdc.input.base import normalize_key_name
from rdc.input.keys import qt_key_to_name


class TestAscii:
    @pytest.mark.parametrize(
        ("qt_key", "expected"),
        [
            (Qt.Key.Key_A, "a"),
            (Qt.Key.Key_Z, "z"),
            (Qt.Key.Key_0, "0"),
            (Qt.Key.Key_9, "9"),
            (Qt.Key.Key_Semicolon, ";"),
            (Qt.Key.Key_Comma, ","),
            (Qt.Key.Key_Ampersand, "&"),
        ],
    )
    def test_printable(self, qt_key: Qt.Key, expected: str) -> None:
        assert qt_key_to_name(int(qt_key)) == expected

    def test_space_maps_to_name_not_char(self) -> None:
        """空格必须走名字通道（pyautogui/SendInput 对 ' ' 单字符支持不一致）。"""
        assert qt_key_to_name(int(Qt.Key.Key_Space)) == "space"


class TestFunctionAndNavigation:
    @pytest.mark.parametrize(
        ("qt_key", "expected"),
        [
            (Qt.Key.Key_Escape, "esc"),
            (Qt.Key.Key_Tab, "tab"),
            (Qt.Key.Key_Backspace, "backspace"),
            (Qt.Key.Key_Return, "enter"),
            (Qt.Key.Key_Enter, "enter"),  # 小键盘 Enter 同 enter（VK 0x0D）
            (Qt.Key.Key_Insert, "insert"),
            (Qt.Key.Key_Delete, "delete"),
            (Qt.Key.Key_Home, "home"),
            (Qt.Key.Key_End, "end"),
            (Qt.Key.Key_PageUp, "pageup"),
            (Qt.Key.Key_PageDown, "pagedown"),
            (Qt.Key.Key_Left, "left"),
            (Qt.Key.Key_Up, "up"),
            (Qt.Key.Key_Right, "right"),
            (Qt.Key.Key_Down, "down"),
            (Qt.Key.Key_Shift, "shift"),
            (Qt.Key.Key_Control, "ctrl"),
            (Qt.Key.Key_Alt, "alt"),
            (Qt.Key.Key_Meta, "win"),
            (Qt.Key.Key_CapsLock, "capslock"),
            (Qt.Key.Key_NumLock, "numlock"),
            (Qt.Key.Key_ScrollLock, "scrolllock"),
            (Qt.Key.Key_Pause, "pause"),
            (Qt.Key.Key_Print, "printscreen"),
        ],
    )
    def test_named_keys(self, qt_key: Qt.Key, expected: str) -> None:
        assert qt_key_to_name(int(qt_key)) == expected

    @pytest.mark.parametrize("index", [1, 5, 12, 24])
    def test_function_range(self, index: int) -> None:
        assert qt_key_to_name(int(Qt.Key.Key_F1) + index - 1) == f"f{index}"


class TestNumpad:
    @pytest.mark.parametrize("digit", range(10))
    def test_kp_digits(self, digit: int) -> None:
        assert qt_key_to_name(int(Qt.Key.Key_0) + 0x30000000 - 0x30 + digit) == f"kp{digit}"

    def test_kp1_canonical(self) -> None:
        assert qt_key_to_name(0x30000000 + 1) == "kp1"


class TestUnknown:
    def test_unrecognized_returns_none(self) -> None:
        assert qt_key_to_name(0x0DEAD) is None

    def test_zero_returns_none(self) -> None:
        assert qt_key_to_name(0) is None


class TestRoundTripIntoBackendNamespace:
    """映射结果必须是 normalize_key_name 的不动点（后端二次归一不变形）。"""

    @pytest.mark.parametrize(
        "qt_key",
        [
            Qt.Key.Key_A,
            Qt.Key.Key_Return,
            Qt.Key.Key_F7,
            Qt.Key.Key_Up,
            Qt.Key.Key_Space,
            0x30000003,
        ],
    )
    def test_normalize_stable(self, qt_key: int) -> None:
        name = qt_key_to_name(int(qt_key))
        assert name is not None
        assert normalize_key_name(name) == name

"""WindowsBackend 单元测试（阶段 7 批 B）。

核心红线（阶段 7 决策点 1）：**测试绝不触达真实系统输入**。全部用例经构造
注入把 ``send``/``bounds``/``scan_char`` 换成记录器与固定值，断言组装出的
INPUT 结构体数组（type/dx/dy/mouseData/wVk/wScan/dwFlags）。

结构布局（ctypes 复刻 Win32）单独锁定：x64 下 ``sizeof(INPUT)`` = 40，
鼠标与键盘成员的字段偏移与 C 对齐一致——偏移错了 SendInput 会静默错注入，
故本文件对 ``ctypes.sizeof`` / ``offsetof`` 直接断言。
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from rdc.exceptions import InputControlError
from rdc.input.base import MouseButton, ScreenRect
from rdc.input.windows import (
    _INPUTUNION,
    _KEYBDINPUT,
    _MOUSEINPUT,
    ABSOLUTE_MAX,
    EXPECTED_INPUT_SIZE,
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MAX_BATCH,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    WHEEL_DELTA,
    WindowsBackend,
    create_windows_backend,
)

FIXED_BOUNDS = ScreenRect(-1920, 0, 3840, 1080)  # 主屏 1920x1080 + 左副屏

MOVE_ABS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

# Windows VK 码（本文件内联定义，测试不依赖被测表的内部常量）
_VK_A = 0x41
_VK_0 = 0x30
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_RETURN = 0x0D
_VK_LEFT = 0x25
_VK_F5 = 0x74
_VK_LWIN = 0x5B
_VK_NUMPAD1 = 0x61
_VK_OEM_1 = 0xBA  # 美键盘的 ';' 键


def _scan_us(ch: str) -> int:
    """美式键盘 VkKeyScanW 替身：; → 0xBA（无需 Shift），: → 0xBA|0x0100（需 Shift）。"""
    table = {";": _VK_OEM_1, ":": _VK_OEM_1 | 0x0100}
    return table.get(ch, -1)


class Recorder:
    """注入边界记录器：保存每批 INPUT 的字段快照。"""

    def __init__(self, *, fail_at: int | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self._fail_at = fail_at

    def __call__(self, inputs: Any) -> int:
        snapshot: list[dict[str, Any]] = []
        for inp in inputs:
            if inp.type == INPUT_MOUSE:
                snapshot.append(
                    {
                        "type": "mouse",
                        "dx": inp.u.mi.dx,
                        "dy": inp.u.mi.dy,
                        "mouseData": inp.u.mi.mouseData,
                        "flags": inp.u.mi.dwFlags,
                    }
                )
            else:
                snapshot.append(
                    {
                        "type": "key",
                        "wVk": inp.u.ki.wVk,
                        "wScan": inp.u.ki.wScan,
                        "flags": inp.u.ki.dwFlags,
                    }
                )
        self.batches.append(snapshot)
        if self._fail_at is not None and self._fail_at == len(self.batches):
            return max(0, len(inputs) - 1)  # 模拟部分注入
        return len(inputs)

    @property
    def all_items(self) -> list[dict[str, Any]]:
        return [item for batch in self.batches for item in batch]

    @property
    def send_count(self) -> int:
        return len(self.batches)

    def reset(self) -> None:
        self.batches.clear()


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def backend(rec: Recorder) -> WindowsBackend:
    return WindowsBackend(send=rec, bounds=lambda: FIXED_BOUNDS, scan_char=_scan_us)


# ---------------------------------------------------------------------------
# 结构布局（错了会静默错注入，必须直接断言）
# ---------------------------------------------------------------------------


class TestStructureLayout:
    def test_input_size(self) -> None:
        assert ctypes.sizeof(INPUT) == EXPECTED_INPUT_SIZE

    def test_union_size(self) -> None:
        """联合尺寸 = 最大成员（MOUSEINPUT：8 字节指针成员把对齐抬到 8）。"""
        assert ctypes.sizeof(_INPUTUNION) == ctypes.sizeof(_MOUSEINPUT)
        assert ctypes.sizeof(_MOUSEINPUT) >= ctypes.sizeof(_KEYBDINPUT)

    def test_member_offsets(self) -> None:
        assert INPUT.type.offset == 0  # type: ignore[attr-defined]
        assert INPUT.u.offset == (8 if sys.maxsize > 2**32 else 4)  # type: ignore[attr-defined]
        assert _MOUSEINPUT.dx.offset == 0  # type: ignore[attr-defined]
        assert _KEYBDINPUT.wVk.offset == 0  # type: ignore[attr-defined]
        assert _KEYBDINPUT.wScan.offset == 2  # type: ignore[attr-defined]
        assert _KEYBDINPUT.dwFlags.offset == 4  # type: ignore[attr-defined]

    def test_hard_to_make(self) -> None:
        """组装不抛错 + type 字段区分成员。"""
        m = INPUT(type=INPUT_MOUSE)
        m.u.mi.dx = -5
        assert m.u.mi.dx == -5
        k = INPUT(type=INPUT_KEYBOARD)
        k.u.ki.wVk = 0xFFFF
        assert k.u.ki.wVk == 0xFFFF
        # 联合共享存储：写 ki 影响 mi 的位（这就是能塞进一个结构体的原因）
        assert ctypes.sizeof(m) == ctypes.sizeof(k)


# ---------------------------------------------------------------------------
# 鼠标
# ---------------------------------------------------------------------------


class TestMouse:
    def test_move_normalizes_to_virtual_desktop(self, backend: WindowsBackend, rec: Recorder) -> None:
        """主屏原点 (0,0) → 归一化 x = 1920/3839*65535（负原点副屏在左）。"""
        backend.move_mouse(0, 0)
        item = rec.all_items[0]
        assert item["type"] == "mouse"
        assert item["flags"] == MOVE_ABS
        assert item["dx"] == round(1920 * ABSOLUTE_MAX / 3839)
        assert item["dy"] == 0

    def test_move_bottom_right_is_max(self, backend: WindowsBackend, rec: Recorder) -> None:
        """虚拟桌面右下角 → 65535/65535。"""
        backend.move_mouse(FIXED_BOUNDS.right - 1, FIXED_BOUNDS.bottom - 1)
        item = rec.all_items[0]
        assert (item["dx"], item["dy"]) == (ABSOLUTE_MAX, ABSOLUTE_MAX)

    def test_move_negative_origin_secondary(self, backend: WindowsBackend, rec: Recorder) -> None:
        """副屏左上角（负原点）→ 0,0。"""
        backend.move_mouse(-1920, 0)
        item = rec.all_items[0]
        assert (item["dx"], item["dy"]) == (0, 0)

    def test_out_of_range_clamped(self, backend: WindowsBackend, rec: Recorder) -> None:
        """越界坐标 clamp 到边缘而非抛错（契约）。"""
        backend.move_mouse(99999, -99999)
        item = rec.all_items[0]
        assert (item["dx"], item["dy"]) == (ABSOLUTE_MAX, 0)

    @pytest.mark.parametrize(
        ("button", "down", "up"),
        [
            (MouseButton.LEFT, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            (MouseButton.RIGHT, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            (MouseButton.MIDDLE, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        ],
    )
    def test_click_emits_move_then_action(
        self,
        button: MouseButton,
        down: int,
        up: int,
        rec: Recorder,
    ) -> None:
        """按下 = move + down 标志；先定位再动作，且坐标同帧。"""
        backend = WindowsBackend(send=rec, bounds=lambda: FIXED_BOUNDS, scan_char=_scan_us)
        backend.click(100, 100, button, True)
        items = rec.all_items
        assert len(items) == 2
        assert items[0]["flags"] == MOVE_ABS
        assert items[1]["flags"] == down
        assert (items[1]["dx"], items[1]["dy"]) == (items[0]["dx"], items[0]["dy"])
        assert items[0]["dx"] == round((100 + 1920) * ABSOLUTE_MAX / 3839)
        # 释放路径使用配对的 up 标志
        rec.reset()
        backend.click(100, 100, button, False)
        acts = [i for i in rec.all_items if i["flags"] in (down, up)]
        assert [a["flags"] for a in acts] == [up]

    def test_release_flags(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.click(10, 10, MouseButton.LEFT, True)
        backend.click(10, 10, MouseButton.LEFT, False)
        acts = [i for i in rec.all_items if i["flags"] in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)]
        assert [a["flags"] for a in acts] == [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP]

    def test_string_button_accepted(self, backend: WindowsBackend, rec: Recorder) -> None:
        """协议允许传裸字符串（ScreenView 载荷直转），内部归一为枚举。"""
        backend.click(10, 10, "right", True)  # type: ignore[arg-type]
        assert any(i["flags"] == MOUSEEVENTF_RIGHTDOWN for i in rec.all_items)

    def test_scroll_vertical(self, backend: WindowsBackend, rec: Recorder) -> None:
        """dy=3 上滚 → WHEEL + 360；批次 = move + wheel。"""
        backend.scroll(500, 500, 0, 3)
        items = rec.all_items
        assert items[0]["flags"] == MOVE_ABS
        assert items[1]["flags"] == MOUSEEVENTF_WHEEL
        assert items[1]["mouseData"] == 3 * WHEEL_DELTA

    def test_scroll_negative_is_twos_complement(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        """dy=-2 下滚 → mouseData 为补码位型（c_ulong 拒收负 int）。"""
        backend.scroll(500, 500, 0, -2)
        wheel = [i for i in rec.all_items if i["flags"] == MOUSEEVENTF_WHEEL][0]
        assert wheel["mouseData"] == (-2 * WHEEL_DELTA) & 0xFFFFFFFF

    def test_scroll_horizontal(self, backend: WindowsBackend, rec: Recorder) -> None:
        """dx≠0 → HWHEEL（补 v4 TODO 的水平滚动）。"""
        backend.scroll(500, 500, -2, 0)
        items = rec.all_items
        assert items[-1]["flags"] == MOUSEEVENTF_HWHEEL
        assert items[-1]["mouseData"] == (-2 * WHEEL_DELTA) & 0xFFFFFFFF

    def test_scroll_both_axes(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.scroll(1, 2, 1, 1)
        flags = [i["flags"] for i in rec.all_items]
        assert flags == [MOVE_ABS, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL]


# ---------------------------------------------------------------------------
# 键盘
# ---------------------------------------------------------------------------


class TestKeyboard:
    def test_letter_vk(self, backend: WindowsBackend, rec: Recorder) -> None:
        """a-z 用自身 ASCII VK（大写形式），无扩展标志。"""
        backend.key_press("a")
        item = rec.all_items[0]
        assert item == {"type": "key", "wVk": _VK_A, "wScan": 0, "flags": 0}

    def test_digit_vk(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.key_press("7")
        assert rec.all_items[0]["wVk"] == _VK_0 + 7

    def test_modifier_from_alias_normalized(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        """X11 风格键名 Control_L 归一为 ctrl → VK_CONTROL。"""
        backend.key_press("Control_L")
        assert rec.all_items[0]["wVk"] == _VK_CONTROL

    def test_enter_and_win_and_function(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        backend.key_press("Return")
        backend.key_press("Super_L")
        backend.key_press("F5")
        vks = [i["wVk"] for i in rec.all_items]
        assert vks == [_VK_RETURN, _VK_LWIN, _VK_F5]

    def test_arrow_keys_get_extended_flag(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        backend.key_press("Up")
        assert rec.all_items[0]["flags"] == KEYEVENTF_EXTENDEDKEY

    def test_numpad_key(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.key_press("KP_3")
        assert rec.all_items[0]["wVk"] == _VK_NUMPAD1 + 2

    def test_punctuation_via_scan_char(self, backend: WindowsBackend, rec: Recorder) -> None:
        """';' 不在表内 → scan_char（VkKeyScanW）→ 0xBA。"""
        backend.key_press(";")
        assert rec.all_items[0]["wVk"] == _VK_OEM_1

    def test_shift_requiring_char_resolves_base_key(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        """:' 需 Shift（scan=0x01BA）→ 注入**基键** OEM_1（走查第六批 Bug2）。

        物理键模型：用户的 Shift 是独立的 ``shift`` 键事件先行注入，远端已持有
        修饰态，这里只补基键即由远端合成 ':'。旧版对 scan>0xFF 一律拒绝（指向
        type_text），导致标点符号键全部无法注入。
        """
        backend.key_press(":")
        item = rec.all_items[0]
        assert item["wVk"] == _VK_OEM_1  # 低字节基键，非整段 0x01BA
        assert item["flags"] == 0  # 不叠加任何修饰标志

    @pytest.mark.parametrize(
        ("ch", "expect_vk"),
        [
            # 数字行移位符号（scan 高字节 = shift 状态）
            ("!", 0x31),
            ("@", 0x32),
            ("#", 0x33),
            ("$", 0x34),
            ("%", 0x35),
            ("^", 0x36),
            ("&", 0x37),
            ("*", 0x38),
            ("(", 0x39),
            (")", 0x30),
            # 编辑区符号（含未移位/移位两档）
            ("-", 0xBD),
            ("_", 0xBD),
            ("=", 0xBB),
            ("+", 0xBB),
            ("[", 0xDB),
            ("{", 0xDB),
            ("]", 0xDD),
            ("}", 0xDD),
            ("\\", 0xDC),
            ("|", 0xDC),
            (";", 0xBA),
            (":", 0xBA),
            ("'", 0xDE),
            ('"', 0xDE),
            (",", 0xBC),
            ("<", 0xBC),
            (".", 0xBE),
            (">", 0xBE),
            ("/", 0xBF),
            ("?", 0xBF),
            ("`", 0xC0),
            ("~", 0xC0),
        ],
    )
    def test_all_punctuation_resolves_to_base_vk(self, ch: str, expect_vk: int) -> None:
        """走查第六批 Bug2 全量哨兵：用户报的每个标点都能解析到基键 VK。

        用完整美式布局 VkKeyScanW 替身（探针实测真机返回值）。病灶：旧
        ``_resolve_key`` 对 scan>0xFF 抛"请用 type_text" → !@# 等 27 个移位
        符号全部注入失败；且旧 ``_VkKeyScanW`` 未声明 restype，未移位 ;,.
        等读到高位垃圾也被误拒。修复后一律取 ``scan & 0xFF`` 基键，远端
        shift 态由独立的 shift 物理键事件维持。
        """
        full_scan = {
            "!": 0x0131, "@": 0x0132, "#": 0x0133, "$": 0x0134, "%": 0x0135,
            "^": 0x0136, "&": 0x0137, "*": 0x0138, "(": 0x0139, ")": 0x0130,
            "-": 0x00BD, "_": 0x01BD, "=": 0x00BB, "+": 0x01BB,
            "[": 0x00DB, "{": 0x01DB, "]": 0x00DD, "}": 0x01DD,
            "\\": 0x00DC, "|": 0x01DC, ";": 0x00BA, ":": 0x01BA,
            "'": 0x00DE, '"': 0x01DE, ",": 0x00BC, "<": 0x01BC,
            ".": 0x00BE, ">": 0x01BE, "/": 0x00BF, "?": 0x01BF,
            "`": 0x00C0, "~": 0x01C0,
        }
        rec = Recorder()
        be = WindowsBackend(
            send=rec, bounds=lambda: FIXED_BOUNDS, scan_char=full_scan.get
        )
        be.key_press(ch)
        item = rec.all_items[0]
        assert item["wVk"] == expect_vk, f"{ch!r} 应解析到基键 0x{expect_vk:02X}"
        assert item["flags"] == 0
        # 释放同样落基键（记账/ reset 口径一致）
        rec.reset()
        be.key_release(ch)
        assert rec.all_items[0]["wVk"] == expect_vk

    def test_vkkeyscanw_restype_declared(self) -> None:
        """走查第六批 Bug2 根因锁定：``_VkKeyScanW.restype = c_short``。

        未声明时 ctypes 按默认 c_int 读寄存器，高 16 位是垃圾 → ``; , . /`` 等
        未移位标点（本应 scan≤0xFF 直接可用）被判成 ``scan>0xFF`` 误拒。探针实测
        该缺失是本机"部分未移位标点也失效"的直接原因（移位符号失效则是
        resolve 逻辑，见上条）。非 Windows 环境该符号为 None，跳过。
        """
        import rdc.input.windows as win_mod

        if win_mod._VkKeyScanW is None:  # pragma: no cover - 非 Windows 分支
            pytest.skip("VkKeyScanW 仅在 Windows 存在")
        assert win_mod._VkKeyScanW.restype is ctypes.c_short

    def test_unmappable_char_rejected(self, backend: WindowsBackend, rec: Recorder) -> None:
        with pytest.raises(InputControlError, match="无对应 VK"):
            backend.key_press("€")

    def test_unknown_named_key_rejected(self, backend: WindowsBackend, rec: Recorder) -> None:
        with pytest.raises(InputControlError, match="无法解析键名"):
            backend.key_press("hyperspace")

    def test_release_adds_keyup_and_keeps_extended(
        self, backend: WindowsBackend, rec: Recorder
    ) -> None:
        """释放保留按下时的扩展标志（reset/正常释放口径一致）。"""
        backend.key_press("Up")
        rec.reset()
        backend.key_release("Up")
        assert rec.all_items[0]["flags"] == KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP

    def test_blank_key_raises(self, backend: WindowsBackend, rec: Recorder) -> None:
        with pytest.raises(InputControlError):
            backend.key_press("  ")


# ---------------------------------------------------------------------------
# Unicode 文本（type_text）
# ---------------------------------------------------------------------------


class TestTypeText:
    def test_empty_is_noop(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.type_text("")
        assert rec.send_count == 0

    def test_ascii_uses_unicode_flag(self, backend: WindowsBackend, rec: Recorder) -> None:
        """每字符 down+up，wVk=0、wScan=码元、KEYEVENTF_UNICODE。"""
        backend.type_text("a")
        items = rec.all_items
        assert len(items) == 2
        assert items[0] == {"type": "key", "wVk": 0, "wScan": 0x61, "flags": KEYEVENTF_UNICODE}
        assert items[1]["flags"] == KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

    def test_cjk_code_units(self, backend: WindowsBackend, rec: Recorder) -> None:
        """CJK 在 BMP 内：每字一个码元（U+4E2D）。"""
        backend.type_text("中")
        assert rec.all_items[0]["wScan"] == 0x4E2D

    def test_emoji_surrogate_pair(self, backend: WindowsBackend, rec: Recorder) -> None:
        """BMP 外字符拆 UTF-16 代理对：1 字符 → 2 码元 → 4 事件。"""
        backend.type_text("\U0001F600")  # 😀
        items = rec.all_items
        assert len(items) == 4
        scans = [i["wScan"] for i in items]
        assert scans == [0xD83D, 0xD83D, 0xDE00, 0xDE00]

    def test_newline_becomes_enter(self, backend: WindowsBackend, rec: Recorder) -> None:
        """\\n/\\r 译为 VK_RETURN 按下+释放（Unicode 标志无法表达 Enter）。"""
        backend.type_text("\n\r")
        items = rec.all_items
        assert len(items) == 4
        assert all(i["wVk"] == _VK_RETURN and i["flags"] in (0, KEYEVENTF_KEYUP) for i in items)

    def test_batching(self, backend: WindowsBackend, rec: Recorder) -> None:
        """超长文本按 MAX_BATCH 分批，不一次全塞，且不丢事件。"""
        chars = MAX_BATCH * 2 + 4  # 132 字符 × 2 事件 = 264 条 INPUT
        backend.type_text("a" * chars)
        assert rec.send_count >= 3
        assert all(len(b) <= MAX_BATCH + 1 for b in rec.batches)  # 批内允许 +1 溢出
        assert sum(len(b) for b in rec.batches) == chars * 2


# ---------------------------------------------------------------------------
# reset / 记账
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_releases_recorded_state(self, backend: WindowsBackend, rec: Recorder) -> None:
        """按下 2 键 + 1 键鼠 → reset 逆序发 KEYUP ×2 + LEFTUP ×1。"""
        backend.key_press("ctrl")
        backend.key_press("shift")
        backend.click(10, 10, MouseButton.LEFT, True)
        rec.reset()
        backend.reset()
        items = rec.all_items
        assert [i["wVk"] for i in items[:2]] == [_VK_SHIFT, _VK_CONTROL]  # 逆序
        assert items[0]["flags"] == KEYEVENTF_KEYUP
        assert items[1]["flags"] == KEYEVENTF_KEYUP
        assert items[2]["flags"] == MOUSEEVENTF_LEFTUP

    def test_reset_empty_is_noop(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.reset()
        assert rec.send_count == 0

    def test_reset_reentrant(self, backend: WindowsBackend, rec: Recorder) -> None:
        """可重入：两次 reset 第二次无注入。"""
        backend.key_press("a")
        backend.reset()
        rec.reset()
        backend.reset()
        assert rec.send_count == 0

    def test_release_clears_ledger(self, backend: WindowsBackend, rec: Recorder) -> None:
        """正常释放后 reset 不应重复发送。"""
        backend.key_press("a")
        backend.key_release("a")
        rec.reset()
        backend.reset()
        assert rec.send_count == 0

    def test_reset_preserves_extended_flag(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.key_press("Up")
        rec.reset()
        backend.reset()
        assert rec.all_items[0]["flags"] == KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP

    def test_release_unpressed_is_harmless(self, backend: WindowsBackend, rec: Recorder) -> None:
        backend.key_release("a")
        backend.reset()
        assert rec.send_count == 1  # 只有显式释放本身，reset 无追加


# ---------------------------------------------------------------------------
# 注入边界失败
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_partial_send_raises(self, rec_fail_once: Recorder) -> None:
        backend = WindowsBackend(send=rec_fail_once, bounds=lambda: FIXED_BOUNDS, scan_char=_scan_us)
        with pytest.raises(InputControlError, match="部分失败"):
            backend.move_mouse(10, 10)

    def test_supported_matches_platform(self, backend: WindowsBackend) -> None:
        assert backend.supported() is (sys.platform == "win32")

    def test_bounds_delegates(self, backend: WindowsBackend) -> None:
        assert backend.bounds() is FIXED_BOUNDS


@pytest.fixture
def rec_fail_once() -> Recorder:
    return Recorder(fail_at=1)


# ---------------------------------------------------------------------------
# 工厂入口
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_returns_backend(self) -> None:
        from rdc.input.base import InputBackend

        assert isinstance(create_windows_backend(), InputBackend)

    def test_default_backend_prefer_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """工厂 prefer=windows 走本模块，且打桩 send 后行为正常。

        create_windows_backend 用默认真实边界——这里不实际注入：monkeypatch
        成带记录器的构造。
        """
        from rdc.input import base as input_base

        rec = Recorder()
        monkeypatch.setattr(
            input_base,
            "_construct_backend",
            lambda *_a: WindowsBackend(
                send=rec, bounds=lambda: FIXED_BOUNDS, scan_char=_scan_us
            ),
        )
        backend = input_base.default_backend(prefer="windows")
        assert isinstance(backend, WindowsBackend)
        backend.move_mouse(0, 0)
        assert rec.send_count == 1


# ---------------------------------------------------------------------------
# 契约复用（批 A 的 InputBackendContractBase）
# ---------------------------------------------------------------------------

from tests.contract.test_input_backend import InputBackendContractBase  # noqa: E402


class TestWindowsContract(InputBackendContractBase):
    """批 A 契约基类跑真实 WindowsBackend（边界已打桩）。"""

    @pytest.fixture
    def events(self) -> Iterator[list[str]]:
        yield []

    @pytest.fixture
    def backend(self, events: list[str]) -> Any:
        """事件版记录器：在结构体断言之外，同时产出契约基类要消费的语义标签。"""

        def send(inputs: Any) -> int:
            batch_tags: set[str] = set()
            for inp in inputs:
                if inp.type == INPUT_MOUSE:
                    if inp.u.mi.dwFlags == MOVE_ABS:
                        batch_tags.add("move")
                    elif inp.u.mi.dwFlags in (
                        MOUSEEVENTF_LEFTDOWN,
                        MOUSEEVENTF_LEFTUP,
                        MOUSEEVENTF_RIGHTDOWN,
                        MOUSEEVENTF_RIGHTUP,
                        MOUSEEVENTF_MIDDLEDOWN,
                        MOUSEEVENTF_MIDDLEUP,
                    ):
                        batch_tags.add("click")
                    else:
                        batch_tags.add("scroll")
                else:
                    if inp.u.ki.dwFlags & KEYEVENTF_UNICODE:
                        batch_tags.add("type_text")
                    elif inp.u.ki.dwFlags & KEYEVENTF_KEYUP:
                        batch_tags.add("key_release")
                    else:
                        batch_tags.add("key_press")
            order = ["move", "click", "scroll", "key_press", "key_release", "type_text"]
            events.extend(t for t in order if t in batch_tags)
            return len(inputs)

        return WindowsBackend(send=send, bounds=lambda: FIXED_BOUNDS, scan_char=_scan_us)

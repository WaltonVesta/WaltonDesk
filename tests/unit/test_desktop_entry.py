"""打包入口（rdc.desktop_entry）与 windowed 控制台补建哨兵（阶段 10 收口批）。

锁四条会静默毁掉发布产物的语义（收口批：console=True → windowed 反转）：

1. **裁决表**：GUI/托盘形态零控制台操作（双击零黑框的全部秘密），CLI 形态
   必须补回可用终端（否则 --version/getpass 全废）；
2. **重定向不劫持**：标准流已有值（管道/文件重定向）时绝不 Attach/Alloc——
   把 `exe > out.txt` 的输出劫持进弹出来的控制台是不可接受的副作用；
3. **Attach 优先、Alloc 兜底、两条全失败不抛**：调用序列与降级路径；
4. **重连后守卫判定翻转**：补控制台后 stdin 必须是 tty（getpass 可用），
   失败则三流保持 None → console_safety 既有降级接管，绝不半接半漏。
"""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest

from rdc import desktop_entry as de


class TestKeepsConsole:
    """裁决表：哪些调用需要可用终端（纯函数，零 IO）。"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], False),  # 双击图标 → GUI
            (["--gui"], False),
            (["serve"], True),  # headless 被控端：横幅可见
            (["serve", "--tray"], False),  # 托盘常驻 = 桌面形态
            (["serve", "--tray", "--silent"], False),  # 开机自启形态
            (["serve", "--silent"], True),  # 无托盘静默仍是终端服务
            (["serve", "--host", "0.0.0.0", "--tray"], False),
            (["control", "1.2.3.4"], True),  # getpass + 指纹确认
            (["set-password"], True),
            (["--version"], True),
            (["--help"], True),
        ],
    )
    def test_decision_table(self, argv: list[str], expected: bool) -> None:
        assert de.keeps_console(argv) is expected

    def test_argv_mutation_safe(self) -> None:
        """不得就地改调用方传入的列表（日志/重试场景会看到被吃掉的首参）。"""
        argv = ["serve", "--tray"]
        de.keeps_console(argv)
        assert argv == ["serve", "--tray"]


class TestEnsureConsole:
    """windowed 让标准输出可用：句柄复用优先 → Attach → Alloc → 重连。"""

    def test_non_windows_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin", raising=False)
        assert (
            de.ensure_console(
                attach=lambda _p: 1, alloc=lambda: 1, reconnect=lambda: True, reopen=lambda: True
            )
            is False
        )

    def test_source_form_stdout_present_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """源码/venv 形态（stdout 非 None）→ 直接 True，一个 Win32 面都不碰。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", io.StringIO(), raising=False)
        calls: list[str] = []

        assert (
            de.ensure_console(
                attach=lambda _p: calls.append("attach") or 1,
                alloc=lambda: calls.append("alloc") or 1,
                reconnect=lambda: calls.append("reconnect") or True,
                reopen=lambda: calls.append("reopen") or True,
            )
            is True
        )
        assert calls == [], "标准流已可用却动了控制台/句柄 = 干扰正常终端"

    def test_inherited_handles_preferred_over_console(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重定向保真（收口批实测病灶）：继承句柄可用 → 用句柄，绝不 Attach。

        病灶：windowed 进程 sys.stdout 恒 None，但 ``exe > log.txt`` 时
        Win32 hStdOutput 就是那个文件句柄。若直接 AttachConsole，重定向被
        劫持进控制台、log.txt 零字节、首启密码静默丢失（实测复现）。
        """
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        calls: list[str] = []

        ok = de.ensure_console(
            attach=lambda _p: calls.append("attach") or 1,
            alloc=lambda: calls.append("alloc") or 1,
            reconnect=lambda: calls.append("reconnect") or True,
            reopen=lambda: calls.append("reopen") or True,
        )
        assert ok is True
        assert calls == ["reopen"], f"句柄可用却补了控制台 = 劫持重定向：{calls}"

    def test_attach_parent_preferred_over_alloc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """句柄无效 + 有父控制台 → 挂父控制台，不得新开窗口。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        calls: list[str] = []

        ok = de.ensure_console(
            attach=lambda _p: calls.append("attach") or 1,
            alloc=lambda: calls.append("alloc") or 1,
            reconnect=lambda: calls.append("reconnect") or True,
            reopen=lambda: calls.append("reopen") or False,  # 句柄不可用
        )
        assert ok is True
        assert calls == ["reopen", "attach", "reconnect"], f"调用序列错误: {calls}"

    def test_alloc_fallback_when_no_parent_console(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """双击 exe 带参数（句柄无效且无父控制台）→ AllocConsole 新建。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        calls: list[str] = []

        ok = de.ensure_console(
            attach=lambda _p: calls.append("attach") or 0,  # attach 失败
            alloc=lambda: calls.append("alloc") or 1,
            reconnect=lambda: calls.append("reconnect") or True,
            reopen=lambda: calls.append("reopen") or False,
        )
        assert ok is True
        assert calls == ["reopen", "attach", "alloc", "reconnect"]

    def test_all_paths_fail_degrade_to_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """四条路全失败 → False 且不重连（console_safety 降级路径接管）。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)

        def _reconnect() -> bool:  # pragma: no cover - 断言不该被调
            raise AssertionError("控制台没建起来却重连了标准流")

        ok = de.ensure_console(
            attach=lambda _p: 0,
            alloc=lambda: 0,
            reconnect=_reconnect,
            reopen=lambda: False,
        )
        assert ok is False
        assert sys.stdout is None, "降级必须保持 None 现状，交给 _NullStream"

    def test_api_raises_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attach/Alloc 抛异常（kernel32 被策略拦截等）只降级、绝不上抛。"""

        def _boom(_p: int) -> int:
            raise OSError("kernel32 被拦截")

        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        assert (
            de.ensure_console(
                attach=_boom, alloc=_boom, reconnect=lambda: True, reopen=lambda: False
            )
            is False
        )

    def test_reconnect_failure_keeps_streams_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """控制台建成但标准流重连失败 → False + 三流保持 None（半接半漏禁止）。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        monkeypatch.setattr(sys, "stderr", None, raising=False)
        monkeypatch.setattr(sys, "stdin", None, raising=False)
        assert (
            de.ensure_console(
                attach=lambda _p: 1,
                alloc=lambda: 1,
                reconnect=lambda: False,
                reopen=lambda: False,
            )
            is False
        )
        assert sys.stdout is None


class TestFrozenEntry:
    """frozen 入口：裁决 → （仅 CLI 形态）补控制台 → 进 CLI 的完整顺序。"""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        record: dict[str, Any] = {"ensure": 0, "main": []}

        import rdc.cli as cli

        monkeypatch.setattr(cli, "main", lambda argv: record["main"].append(list(argv)) or 0)
        monkeypatch.setattr(
            de, "ensure_console", lambda **_kw: record.__setitem__("ensure", record["ensure"] + 1) or True
        )
        return record

    def test_frozen_cli_form_ensures_console(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert de.frozen_entry(["--version"]) == 0
        assert calls["ensure"] == 1, "windowed 下 CLI 形态不补控制台 = 输出无处可去"
        assert calls["main"] == [["--version"]]

    def test_frozen_gui_form_never_touches_console(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """双击/--gui/serve --tray **零**控制台调用——黑框消除的根保证。"""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        for argv in ([], ["--gui"], ["serve", "--tray"], ["serve", "--tray", "--silent"]):
            de.frozen_entry(argv)
        assert calls["ensure"] == 0, (
            "GUI/托盘形态调用了 ensure_console = 双击可能闪黑框（本批病灶复发）"
        )
        assert calls["main"] == [[], ["--gui"], ["serve", "--tray"], ["serve", "--tray", "--silent"]]

    def test_frozen_serve_headless_ensures(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        de.frozen_entry(["serve"])
        assert calls["ensure"] == 1, "headless 不补控制台 = 首启密码横幅彻底不可见"

    def test_source_form_never_ensures(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """源码/venv 形态不裁决：本来就在真终端里跑。"""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        de.frozen_entry(["--version"])
        assert calls["ensure"] == 0

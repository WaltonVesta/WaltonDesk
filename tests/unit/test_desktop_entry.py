"""打包入口（rdc.desktop_entry）与控制台形态裁决哨兵（阶段 10.1）。

锁三条会静默毁掉发布产物的语义：

1. **控制台形态裁决**：GUI/托盘形态必须释放黑框（Feature 17"静默进被控端"），
   CLI 形态必须保留（否则 ``--version``/横幅/getpass 全废）。
2. **释放后标准流置 None**：句柄已失效还留着流对象，首启密码横幅写它即抛
   OSError 炸掉被控端（Feature 18 可见性链条的最后一环）。
3. **置 None 后打包守卫三态判定仍成立**：``stdin_is_absent()`` 必须落回
   "无控制台 → 必须起 GUI"，这是第十二批打包守卫修过的病灶（合并成
   "非终端不起 GUI" 会让窗口永不出现）。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from rdc import desktop_entry as de
from rdc.console_safety import console_stream, stdin_is_absent, stdin_is_tty


class TestKeepsConsole:
    """裁决表：哪些调用保留控制台（纯函数，零 IO）。"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], False),  # 双击图标 → GUI
            (["--gui"], False),
            (["serve"], True),  # headless 被控端：横幅 + Ctrl+C
            (["serve", "--tray"], False),  # 托盘常驻 = 桌面形态
            (["serve", "--tray", "--silent"], False),  # 开机自启形态
            (["serve", "--silent"], True),  # 无托盘静默仍是终端服务
            (["serve", "--host", "0.0.0.0", "--tray"], False),
            (["control", "1.2.3.4"], True),  # 需要 getpass 交互
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


class TestFrozenEntry:
    """frozen 入口：裁决 → 释放 → 进 CLI 的完整顺序。"""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        record: dict[str, Any] = {"detach": 0, "main": []}

        def _fake_cli_main(argv: list[str]) -> int:
            record["main"].append(list(argv))
            return 0

        import rdc.cli as cli

        monkeypatch.setattr(cli, "main", _fake_cli_main)

        def _fake_detach() -> bool:
            record["detach"] += 1
            return True

        monkeypatch.setattr(de, "detached_console", _fake_detach)
        return record

    def test_frozen_gui_form_detaches(self, calls: dict[str, Any], monkeypatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert de.frozen_entry([]) == 0
        assert calls["detach"] == 1
        assert calls["main"] == [[]]

    def test_frozen_cli_form_keeps_console(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert de.frozen_entry(["--version"]) == 0
        assert calls["detach"] == 0, "--version 释放了控制台 = 输出无处可去"

    def test_frozen_serve_headless_keeps_console(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        de.frozen_entry(["serve"])
        assert calls["detach"] == 0, "headless 被控端释放控制台 = 横幅与 Ctrl+C 全废"

    def test_frozen_tray_form_detaches(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """开机自启命令（serve --tray --silent）必须释放，否则开机弹黑框。"""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        de.frozen_entry(["serve", "--tray", "--silent"])
        assert calls["detach"] == 1

    def test_source_form_never_detaches(
        self, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """源码/venv 形态不裁决：开发期跑 --tray 本就该看到日志。"""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        de.frozen_entry([])
        assert calls["detach"] == 0


class TestDetachedConsoleStreamHygiene:
    """释放后标准流必须置 None，且打包守卫判定要跟着翻到"无控制台"。"""

    def test_non_windows_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin", raising=False)
        assert de.detached_console() is False

    def test_streams_nulled_after_successful_detach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """关键回归：句柄失效仍留流对象 → 首启密码横幅写它必抛 OSError。"""
        import ctypes
        import io

        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", io.StringIO(), raising=False)
        monkeypatch.setattr(sys, "stderr", io.StringIO(), raising=False)
        monkeypatch.setattr(sys, "stdin", io.StringIO(), raising=False)
        monkeypatch.setattr(
            ctypes.windll.kernel32, "FreeConsole", lambda: 1, raising=False
        )

        assert de.detached_console() is True
        assert sys.stdout is None and sys.stderr is None and sys.stdin is None

        # 守卫必须跟着翻：console_stream 落 _NullStream（写不炸），
        # stdin_is_absent 落 True → cli._gui_launch_expected 的"必须起 GUI"分支
        console_stream().write("初始密码：abc12345")
        assert stdin_is_absent() is True
        assert stdin_is_tty() is False

        import rdc.cli as cli

        assert cli._gui_launch_expected() is True, (  # noqa: SLF001 - 哨兵锁启动判定
            "释放控制台后不起 GUI = 双击图标只打印帮助就退出（第十二批病灶复发）"
        )

    def test_freeconsole_failure_keeps_streams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes
        import io

        out = io.StringIO()
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "stdout", out, raising=False)
        monkeypatch.setattr(sys, "stderr", out, raising=False)
        monkeypatch.setattr(sys, "stdin", out, raising=False)
        monkeypatch.setattr(
            ctypes.windll.kernel32, "FreeConsole", lambda: 0, raising=False
        )

        assert de.detached_console() is False
        assert sys.stdout is out, "释放失败却清空了流 = 把好好的终端会话弄哑"

    def test_windll_missing_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FreeConsole 根本调不到（异常）也只降级，绝不上抛炸启动。"""
        monkeypatch.setattr(sys, "platform", "win32", raising=False)

        class _Boom:
            def __getattr__(self, _name: str) -> Any:
                raise OSError("no kernel32")

        import ctypes

        monkeypatch.setattr(ctypes, "windll", _Boom(), raising=False)
        assert de.detached_console() is False

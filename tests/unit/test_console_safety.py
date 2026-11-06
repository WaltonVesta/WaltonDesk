"""控制台守卫（rdc.console_safety）单元测试。

锁定走查第十二批"PyInstaller --windowed 兼容"的全部判据：

1. ``sys.stdout``/``sys.stdin`` 为 ``None``（无控制台形态）→ isatty 判定
   不崩、日志装配不崩、无参启动**仍然起 GUI**（不得退化成打印帮助退出）；
2. 正常终端形态 → 行为与修复前**逐字节一致**（真流对象原样返回、tty 判定
   结果不变）——防止守卫把 GUI/状态机带偏（上次死逻辑事故的教训）；
3. 流存在但 isatty 自抛异常 → 保守落 False 且不外逸。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

import rdc.console_safety as cs


class _ExplodingStream:
    """isatty 自抛异常的流（模拟损坏/非常规宿主对象）。"""

    def isatty(self) -> bool:
        raise OSError("bad device")


class _NoIsattyStream:
    """无 isatty 面的对象。"""


class TestIsTty:
    def test_real_tty_stream_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = io.StringIO()
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)
        assert cs.is_tty(stream) is True

    def test_none_stream_false(self) -> None:
        """--windowed 形态：sys.stdout/stdin 是 None，判定必须落 False 不崩。"""
        assert cs.is_tty(None) is False

    def test_missing_isatty_false(self) -> None:
        assert cs.is_tty(_NoIsattyStream()) is False

    def test_isatty_raising_false_not_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 保守落 False，同时留痕（不静默吞掉异常事实）
        assert cs.is_tty(_ExplodingStream()) is False
        assert "isatty 探测失败" in capsys.readouterr().err

    def test_module_level_helpers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        monkeypatch.setattr(sys, "stdin", None, raising=False)
        assert cs.stdout_is_tty() is False
        assert cs.stdin_is_tty() is False
        assert cs.stdin_is_absent() is True


class TestConsoleStream:
    def test_returns_real_stream_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常形态：原对象身份返回，绝不包装/替换（GUI 行为零变化的硬保证）。"""
        sentinel = io.StringIO()
        monkeypatch.setattr(sys, "stdout", sentinel, raising=False)
        assert cs.console_stream() is sentinel

    def test_null_stream_when_stdout_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        stream = cs.console_stream()
        assert stream.write("hello") == 5  # 丢弃写入不抛错
        stream.flush()
        assert stream.isatty() is False
        stream.close()


class TestLoggingWithWindowedStdout:
    """configure_logging 在 stdout=None 形态下必须可装配、可发日志、不崩。"""

    def test_configure_and_emit_with_none_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import rdc.logging.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_configured", False)
        monkeypatch.setattr(setup_mod, "_root", None)
        monkeypatch.setattr(sys, "stdout", None, raising=False)
        root = setup_mod.configure_logging("INFO", log_file=tmp_path / "x.log")
        root.warning("windowed smoke")  # 走 console handler（此刻是 _NullStream）
        assert (tmp_path / "x.log").read_text(encoding="utf-8").strip() != ""

    def test_configure_with_real_stdout_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import rdc.logging.setup as setup_mod

        monkeypatch.setattr(setup_mod, "_configured", False)
        monkeypatch.setattr(setup_mod, "_root", None)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf, raising=False)
        setup_mod.configure_logging("INFO", log_file=tmp_path / "y.log")
        root = setup_mod.configure_logging("INFO", log_file=tmp_path / "y.log")
        root.error("console visible")
        assert "console visible" in buf.getvalue()  # 控制台输出目标仍是真流


class TestGuiLaunchDecision:
    """无参启动三态判定（上次回归的病灶：把"无控制台"误判成"非交互"）。"""

    @pytest.fixture()
    def _stub_launchers(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import rdc.cli as cli_mod

        calls: dict[str, Any] = {"gui": 0}

        def _fake_gui() -> int:
            calls["gui"] += 1
            return 0

        monkeypatch.setattr(cli_mod, "_run_gui", _fake_gui)
        monkeypatch.setattr(cli_mod, "_has_display", lambda: True)
        return calls

    def test_tty_stdin_launches_gui(
        self, _stub_launchers: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rdc.cli as cli_mod

        tty = io.StringIO()
        monkeypatch.setattr(tty, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys, "stdin", tty, raising=False)
        assert cli_mod.main([]) == 0
        assert _stub_launchers["gui"] == 1

    def test_windowed_none_stdin_still_launches_gui(
        self, _stub_launchers: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """核心防回归：stdin=None（--windowed）必须起 GUI，不能打印帮助退出。"""
        import rdc.cli as cli_mod

        monkeypatch.setattr(sys, "stdin", None, raising=False)
        assert cli_mod.main([]) == 0
        assert _stub_launchers["gui"] == 1

    def test_piped_stdin_prints_help(
        self, _stub_launchers: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """有控制台但 stdin 被重定向（脚本/CI）→ 维持原语义：打印帮助。"""
        import rdc.cli as cli_mod

        monkeypatch.setattr(sys, "stdin", io.StringIO("x"), raising=False)
        assert cli_mod.main([]) == 0
        assert _stub_launchers["gui"] == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_cli_password_and_fingerprint_survive_none_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 冒烟函数在 windowed 形态不得抛 AttributeError。"""
        import rdc.cli as cli_mod

        monkeypatch.setattr(sys, "stdin", None, raising=False)
        assert cli_mod._cli_password() == ""
        assert cli_mod._cli_fingerprint("AA:BB", "h1") is False

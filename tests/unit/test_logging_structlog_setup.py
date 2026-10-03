"""``rdc.logging`` structlog 改造测试（阶段 2，2.8/2.9）。

锁住三层契约：
1. 新面：``get_logger(name)`` → JSON 行落盘、logger 名带 ``rdc.`` 前缀；
2. 兼容面：``get_logger()`` → stdlib 风格 %-format 适配器（v4 代码 24 处依赖）；
3. ``configure_logging`` 幂等（重复调用不叠加 handler，只调级别）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from rdc.logging import configure_logging, get_logger
from rdc.logging.setup import strip_emoji


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    """把日志指向临时文件并复位配置状态（允许重新 configure）。"""
    import rdc.logging.setup as setup_mod

    target = tmp_path / "test.log"
    setup_mod._configured = False
    setup_mod._root = None
    configure_logging("DEBUG", log_file=target)
    yield target
    # 重新钉到临时目录（不重置 _configured），防止后续测试 auto-configure 写真实 HOME。
    configure_logging("DEBUG", log_file=tmp_path / "teardown.log")


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestStructlogPath:
    def test_named_logger_writes_json_line(self, log_file: Path) -> None:
        get_logger("config.migration").warning("测试事件")
        recs = _lines(log_file)
        assert any(r["event"] == "测试事件" and r["level"] == "warning" for r in recs)

    def test_named_logger_prefixes_rdc_namespace(self, log_file: Path) -> None:
        get_logger("secrets.dpapi").info("ns-check")
        recs = _lines(log_file)
        assert any(r.get("logger") == "rdc.secrets.dpapi" for r in recs)

    def test_exception_includes_traceback(self, log_file: Path) -> None:
        log = get_logger("t")
        try:
            raise ValueError("boom-traceback")
        except ValueError:
            log.exception("出错了")
        recs = _lines(log_file)
        assert any(
            r["event"] == "出错了" and "boom-traceback" in str(r.get("exception", ""))
            for r in recs
        )


class TestStdlibCompatPath:
    def test_compat_logger_percent_format(self, log_file: Path) -> None:
        get_logger().info("保存配置失败: %s", "disk-full")
        assert any("保存配置失败: disk-full" in r["event"] for r in _lines(log_file))

    def test_compat_logger_bad_format_args_no_raise(self, log_file: Path) -> None:
        get_logger().info("无占位符消息", "多余参数")
        assert any("无占位符消息" in r["event"] for r in _lines(log_file))

    def test_compat_logger_has_five_methods(self, log_file: Path) -> None:
        log = get_logger()
        for meth in ("debug", "info", "warning", "error", "exception"):
            getattr(log, meth)("method-%s", meth)
        events = {r["event"] for r in _lines(log_file)}
        assert {f"method-{m}" for m in ("debug", "info", "warning", "error", "exception")} <= events


class TestConfigureIdempotent:
    def test_reconfigure_does_not_duplicate_handlers(self, log_file: Path) -> None:
        root = logging.getLogger("rdc")
        count = len(root.handlers)
        configure_logging("WARNING")
        assert len(root.handlers) == count

    def test_reconfigure_updates_console_level(self, log_file: Path) -> None:
        root = configure_logging("ERROR")
        console = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
        assert console and console[0].level == logging.ERROR


class TestStripEmojiProcessor:
    def test_strip_emoji_removes_emoji(self) -> None:
        out = strip_emoji(None, "info", {"event": "完成 🎉 了"})
        assert out["event"] == "完成  了"

    def test_strip_emoji_keeps_plain_text(self) -> None:
        out = strip_emoji(None, "info", {"event": "plain 中文 ok"})
        assert out["event"] == "plain 中文 ok"

    def test_strip_emoji_non_string_event_passthrough(self) -> None:
        out = strip_emoji(None, "info", {"event": 123})
        assert out["event"] == 123

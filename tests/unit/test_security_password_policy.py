"""security/password_policy 单元测试（阶段 9.1）。"""

from __future__ import annotations

import pytest

from rdc.security.password_policy import (
    PASSWORD_MIN_LENGTH,
    PasswordIssue,
    check_password,
    is_strong_password,
    policy_error_text,
)


class TestCheckPassword:
    def test_empty_returns_all_three_issues(self) -> None:
        assert check_password("") == [
            PasswordIssue.TOO_SHORT,
            PasswordIssue.NO_LETTER,
            PasswordIssue.NO_DIGIT,
        ]

    def test_minimum_strong_password_passes(self) -> None:
        assert check_password("abcd1234") == []
        assert is_strong_password("abcd1234") is True

    @pytest.mark.parametrize("pwd", ["Abcdef12", "1234567a", "a1" + "x" * 6])
    def test_exactly_min_length_with_letter_digit_passes(self, pwd: str) -> None:
        assert len(pwd) == PASSWORD_MIN_LENGTH
        assert check_password(pwd) == []

    def test_too_short_only(self) -> None:
        assert check_password("abc1") == [PasswordIssue.TOO_SHORT]

    def test_long_without_digit(self) -> None:
        assert check_password("abcdefghijkl") == [PasswordIssue.NO_DIGIT]

    def test_long_without_letter(self) -> None:
        assert check_password("1234567890") == [PasswordIssue.NO_LETTER]

    def test_no_letter_no_digit(self) -> None:
        assert check_password("!@#$%^&*()") == [
            PasswordIssue.NO_LETTER,
            PasswordIssue.NO_DIGIT,
        ]

    def test_issue_order_is_length_letter_digit(self) -> None:
        issues = check_password("!!")
        assert issues == [
            PasswordIssue.TOO_SHORT,
            PasswordIssue.NO_LETTER,
            PasswordIssue.NO_DIGIT,
        ]

    def test_cjk_counts_as_letter(self) -> None:
        # str.isalpha 对汉字为 True：中文+数字满足"字母+数字"口径
        assert check_password("密码密码密码密码12") == []

    def test_emoji_is_neither_letter_nor_digit(self) -> None:
        assert check_password("😀" * PASSWORD_MIN_LENGTH) == [
            PasswordIssue.NO_LETTER,
            PasswordIssue.NO_DIGIT,
        ]

    def test_length_counts_codepoints(self) -> None:
        # 代理对在外层 Python str 就是 1 个码点：6 个 emoji + "a1" = 8 码点 → 通过
        pwd = "🦀" * 6 + "a1"
        assert len(pwd) == PASSWORD_MIN_LENGTH
        assert check_password(pwd) == []
        # 5 个 emoji + "a1" = 7 码点 → 仅长度违规
        assert check_password("🦀" * 5 + "a1") == [PasswordIssue.TOO_SHORT]


class TestPolicyErrorText:
    def test_empty_issues_returns_empty_text(self) -> None:
        assert policy_error_text([]) == ""

    def test_text_lists_missing_rules_in_order(self) -> None:
        text = policy_error_text([PasswordIssue.TOO_SHORT, PasswordIssue.NO_DIGIT])
        assert "长度至少 8 位" in text
        assert "至少包含一个数字" in text
        assert "字母" not in text  # 未违反的不出现在文案里

    def test_all_issues_mention_all_three_rules(self) -> None:
        text = policy_error_text(
            [PasswordIssue.TOO_SHORT, PasswordIssue.NO_LETTER, PasswordIssue.NO_DIGIT]
        )
        assert all(k in text for k in ("长度", "字母", "数字"))

    def test_unknown_code_ignored(self) -> None:
        assert policy_error_text(["not-a-code"]) == ""


class TestNoCredentialHardcoding:
    """红线自检：策略模块零 Qt / 零网络 / 零存储依赖（security 层约束）。"""

    def test_module_has_only_stdlib_imports(self) -> None:
        import inspect

        import rdc.security.password_policy as mod

        source = inspect.getsource(mod)
        for banned in ("import PySide6", "from PySide6", "import socket", "import bcrypt"):
            assert banned not in source

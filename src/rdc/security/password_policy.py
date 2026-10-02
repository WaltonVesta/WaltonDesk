"""口令强度策略（阶段 9.1）：≥8 位 + 至少一个字母 + 至少一个数字。

v4 病灶：``Config.set_password``（``_v4_config.py:410``）对口令强度**零校验**，
弱口令（如 ``1``/``admin``）直接 bcrypt 落库，配合 v4 认证限流失效构成
"弱口令爆破"渗透面（本阶段渗透清单 S3 的前置加固）。

v5 口径：
- 规则常量公开可查（UI/CLI 共用同一来源，禁止调用点复制阈值）；
- :func:`check_password` 返回**结构化违规列表**——UI 可逐条展示，
  CLI 拼成人读文本；空列表即通过；
- :func:`policy_error_text` 给对话框/终端的一行提示文案；
- 长度按 Unicode 码点数计（与 bcrypt 输入字节截断无关，只作强度门槛）。

依赖红线：本模块属 ``security`` 层，**零 Qt / 零网络导入**（UI 反过来依赖它）。
"""

from __future__ import annotations

__all__ = [
    "PASSWORD_MIN_LENGTH",
    "PasswordIssue",
    "check_password",
    "is_strong_password",
    "policy_error_text",
]

#: 最小长度（含）——任务书基线"≥8 位"。
PASSWORD_MIN_LENGTH = 8


class PasswordIssue:
    """违规项常量命名空间（结构化返回，避免调用点匹配文案）。"""

    TOO_SHORT = "too_short"
    """长度不足 :data:`PASSWORD_MIN_LENGTH`。"""
    NO_LETTER = "no_letter"
    """缺少至少一个字母（含中文等 Unicode 字母不计数，与主流策略口径一致）。"""
    NO_DIGIT = "no_digit"
    """缺少至少一个数字。"""


def check_password(plain: str) -> list[str]:
    """校验口令，返回违规代码列表；**空列表表示通过**。

    Args:
        plain: 明文口令（调用方负责不落日志）。

    Returns:
        违规代码列表（:class:`PasswordIssue` 常量值），顺序固定：
        长度 → 字母 → 数字，便于 UI 稳定展示。
    """
    issues: list[str] = []
    if len(plain) < PASSWORD_MIN_LENGTH:
        issues.append(PasswordIssue.TOO_SHORT)
    if not any(ch.isalpha() for ch in plain):
        issues.append(PasswordIssue.NO_LETTER)
    if not any(ch.isdigit() for ch in plain):
        issues.append(PasswordIssue.NO_DIGIT)
    return issues


def is_strong_password(plain: str) -> bool:
    """布尔便捷判定：口令是否满足强度策略。"""
    return not check_password(plain)


def policy_error_text(issues: list[str]) -> str:
    """把违规代码列表拼成一行中文提示（UI 标签 / CLI stderr 共用）。

    Args:
        issues: :func:`check_password` 的返回值。

    Returns:
        人读文案；``issues`` 为空时返回空串。
    """
    hints = {
        PasswordIssue.TOO_SHORT: f"长度至少 {PASSWORD_MIN_LENGTH} 位",
        PasswordIssue.NO_LETTER: "至少包含一个字母",
        PasswordIssue.NO_DIGIT: "至少包含一个数字",
    }
    missing = [hints[i] for i in issues if i in hints]
    if not missing:
        return ""
    return "口令不满足强度要求：" + "、".join(missing)

"""服务端口令哈希（bcrypt, rounds=12）+ 初始口令生成。

从 v4 ``Config.set_password`` / ``Config.verify_password`` 提取为纯函数，
新代码直接使用本模块；``_v4_config.Config`` 保持原样不再扩展（个人版策略，
2026-09-22 范围收缩决定）。

走查第十二批 Feature 18：新增 :func:`generate_initial_password`——被控端
首次运行（配置无口令）时自动生成的随机 8 位强口令（大写+小写+数字，满足
``security.password_policy`` 全部规则），密码学随机源 ``secrets``。
"""

from __future__ import annotations

import secrets

import bcrypt

from rdc.exceptions import SecretStoreError

__all__ = [
    "BCRYPT_ROUNDS",
    "generate_initial_password",
    "hash_password",
    "verify_password",
]

BCRYPT_ROUNDS = 12

#: 初始口令长度（Feature 18 规格：8 位）。
INITIAL_PASSWORD_LENGTH = 8
# 排除易混淆字形（0/O、1/I/l），降低"抄错密码"真人成本；随机源不变。
_UPPER = "ABCDEFGHJKMNPQRSTUVWXYZ"
_LOWER = "abcdefghijkmnpqrstuvwxyz"
_DIGITS = "23456789"


def generate_initial_password(length: int = INITIAL_PASSWORD_LENGTH) -> str:
    """生成随机初始口令：``length`` 位，保证至少各含一个大写、小写、数字。

    构造 = 必含三类各 1 个 + 余量从全字符表抽取，再 Fisher-Yates（secrets
    实现）打散位置；熵源全程 ``secrets``（CSPRNG），不用 ``random``。

    Raises:
        ValueError: ``length`` < 3 时无法同时容纳三类字符。
    """
    if length < 3:
        raise ValueError(f"初始口令长度至少 3 位才能同时含大小写与数字，实得 {length}")
    alphabet = _UPPER + _LOWER + _DIGITS
    chars = [
        secrets.choice(_UPPER),
        secrets.choice(_LOWER),
        secrets.choice(_DIGITS),
    ]
    chars += [secrets.choice(alphabet) for _ in range(length - 3)]
    # secrets.randbelow 版 Fisher-Yates：从尾部交换，避免 random.shuffle 的非 CSPRNG 源
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def hash_password(plain: str) -> str:
    """返回 bcrypt（rounds=12）哈希字符串；空口令返回空串（表示未设置）。"""
    if not plain:
        return ""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希是否匹配。``hashed`` 为空视为未设置密码 → True（v4 语义）。"""
    if not hashed:
        return True
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError as exc:
        # 存储的哈希格式损坏：拒绝认证并抛错，绝不静默放行（约束 6）。
        raise SecretStoreError(f"password: 存储的 bcrypt 哈希格式无效，拒绝认证: {exc}") from exc

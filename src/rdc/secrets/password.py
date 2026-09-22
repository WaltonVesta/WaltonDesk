"""服务端口令哈希（bcrypt, rounds=12）。

从 v4 ``Config.set_password`` / ``Config.verify_password`` 提取为纯函数，
新代码直接使用本模块；``_v4_config.Config`` 保持原样不再扩展（个人版策略，
2026-09-22 范围收缩决定）。
"""

from __future__ import annotations

import bcrypt

from rdc.exceptions import SecretStoreError

__all__ = ["BCRYPT_ROUNDS", "hash_password", "verify_password"]

BCRYPT_ROUNDS = 12


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

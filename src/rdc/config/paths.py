"""配置与数据目录解析（阶段 2 新增，2.1 支撑）。

v4 在 ``config.py``、``logger.py``、``utils/security.py`` 三处各自拼接
``Path.home() / ".remote_desktop"``，路径变更后容易互相不一致。本模块是
唯一的路径来源，测试可用 :func:`set_override_root` 重定向到临时目录，
彻底不触碰真实 HOME。

阶段 10 品牌更名：数据根 ``~/.remote_desktop`` → ``~/.waltondesk``，迁移由
:func:`migrate_legacy_root` 一次性完成（**只搬不删**，理由见该函数）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

__all__ = [
    "APP_DIR_NAME",
    "LEGACY_APP_DIR_NAME",
    "banlist_file",
    "cert_dir",
    "config_dir",
    "config_file",
    "ensure_dirs",
    "history_file",
    "legacy_config_dir",
    "log_dir",
    "migrate_legacy_root",
    "override_root",
    "secret_store_file",
    "set_override_root",
]

#: 阶段 10 品牌更名后的数据根目录名。
APP_DIR_NAME = ".waltondesk"

#: 更名前的旧数据根。仅服务于 :func:`migrate_legacy_root`。
#: 迁移完成前，旧目录里的 ``secrets.dpapi.json`` 仍持有可解密的口令密文，
#: 因此迁移**只搬不删**——宁可留一个空壳旧目录，也不能让口令静默丢失。
LEGACY_APP_DIR_NAME = ".remote_desktop"

#: 复制兜底路径写下的完成标记（见 :func:`migrate_legacy_root`）。
_MIGRATION_MARKER = ".migrated-from-remote-desktop"

_override_root: Path | None = None
_legacy_migration_enabled = True


def set_legacy_migration_enabled(enable: bool) -> None:
    """开/关旧目录迁移（仅供测试关闭，**生产代码不应调用**）。

    测试套件红线是"绝不触碰真实 HOME"（见 tests/conftest.py），而本迁移读写
    的正是真实 ``~/.remote_desktop`` / ``~/.waltondesk``。故 conftest 全局关闭，
    迁移单测再用 override 根 + 显式打开来覆盖两条路径。
    """
    global _legacy_migration_enabled
    _legacy_migration_enabled = bool(enable)


def legacy_migration_enabled() -> bool:
    """当前是否允许执行旧目录迁移。"""
    return _legacy_migration_enabled


def set_override_root(path: Path | str | None) -> None:
    """把应用数据根目录重定向到 ``path``（None 恢复默认 HOME 下路径）。

    仅供测试与便携模式使用；生产代码不应调用。
    """
    global _override_root
    _override_root = Path(path) if path is not None else None


def override_root() -> Path | None:
    """返回当前重定向根目录（未重定向时为 None）。"""
    return _override_root


def config_dir() -> Path:
    """应用数据根目录（阶段 10 起：``~/.waltondesk``）。"""
    if _override_root is not None:
        return _override_root
    return Path.home() / APP_DIR_NAME


def legacy_config_dir() -> Path:
    """更名前的旧数据根 ``~/.remote_desktop``（仅迁移用）。

    取**新根的兄弟目录**而非写死 ``Path.home()``：生产下两者同为 ``~`` 下同级
    （语义一致），便携/测试模式（``set_override_root``）下则落在同一个可写
    沙箱内，不会越界去碰真实 HOME。
    """
    return config_dir().parent / LEGACY_APP_DIR_NAME


def migrate_legacy_root(
    *,
    legacy: Path | None = None,
    new: Path | None = None,
) -> bool:
    """把旧 ``~/.remote_desktop`` 数据搬到新 ``~/.waltondesk``（幂等，只搬不删）。

    为什么必须迁移：DPAPI 密文索引（``secrets.dpapi.json``）与 ``config.json``
    里的口令哈希都躺在旧目录，目录名一改就读不到——用户表现为"密码框明明
    存过却说没设密码"，属静默数据丢失。

    为什么**不删**旧目录：跨目录搬迁不是原子的，"复制成功→删除"中间任何一步
    出错都会留下两边都不完整的状态；而留着旧目录最坏结果只是一个几百 KB 的空壳。
    删除留给用户自行决定。

    幂等与安全性：
      - 新目录已有内容（``config.json`` 存在，或完成标记已写）→ 直接返回，
        绝不覆盖新数据（回滚/重装场景下新目录可能已是权威数据源）；
      - 旧目录不存在 → 返回 False（无需迁移）；
      - 优先整目录 ``rename``（同盘原子）；跨盘或占用失败 → 退回逐项
        ``copy2``，**已存在的目标文件一律跳过不覆盖**；
      - copy2 路径全部成功后写完成标记，避免每次启动重复扫描；
      - 任何异常都不上抛：迁移失败时新目录照常可用，错误经 logger 留痕
        （本函数被日志系统早期调用，故用标准库 logging 而非 structlog，
        避免在日志装配完成前触发重复装配）。

    Args:
        legacy: 旧数据根（默认 :func:`legacy_config_dir`；测试注入临时目录）。
        new: 新数据根（默认 :func:`config_dir`；测试注入临时目录）。

    Returns:
        本次是否实际搬迁了数据（False = 无需迁移 / 已迁过 / 迁移失败）。
    """
    import logging

    log = logging.getLogger("rdc.config")
    if not _legacy_migration_enabled:
        return False
    auto = legacy is None and new is None
    src_root = Path(legacy) if legacy is not None else legacy_config_dir()
    dst_root = Path(new) if new is not None else config_dir()
    if auto and _override_root is not None:
        # 便携/测试模式（override 根）：数据本就在用户指定的新位置，旧数据
        # 概念不成立。更要紧的是——若 override 根恰好被设在 ``~`` 之下，自动
        # 路径会把**真实的** ``~/.remote_desktop`` 整目录 rename 走，属破坏性
        # 副作用。自动迁移只在默认根下执行；显式传参（单测）不受此限。
        return False
    if not src_root.is_dir() or src_root == dst_root:
        return False
    if (dst_root / _MIGRATION_MARKER).exists() or (dst_root / "config.json").exists():
        return False  # 已迁过，或新目录已是权威数据源

    try:
        dst_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            src_root.rename(dst_root)  # 同盘原子整目录搬迁
            log.info("数据根目录已重命名至 %s", dst_root)
            return True
        except OSError:
            pass  # 跨盘/被占用/权限：退化为逐项复制

        dst_root.mkdir(parents=True, exist_ok=True)
        moved = 0
        skipped = 0
        for src in sorted(src_root.rglob("*")):
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            if dst.exists():
                skipped += 1  # 绝不覆盖新目录既有文件
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            moved += 1
        (dst_root / _MIGRATION_MARKER).write_text(
            f"copied={moved} skipped={skipped}\n", encoding="utf-8"
        )
        log.info(
            "数据根目录已复制迁移至 %s：moved=%d skipped=%d", dst_root, moved, skipped
        )
        return moved > 0
    except Exception as exc:  # noqa: BLE001 - 迁移失败不得阻断启动
        log.warning("数据根目录迁移失败（继续使用新目录）: %r", exc)
        return False


def config_file() -> Path:
    """主配置文件路径 ``config.json``。"""
    return config_dir() / "config.json"


def history_file() -> Path:
    """连接历史文件路径 ``history.json``。"""
    return config_dir() / "history.json"


def banlist_file() -> Path:
    """认证封禁持久化文件路径 ``banlist.json``（阶段 9 ``server/banlist`` 落盘处）。"""
    return config_dir() / "banlist.json"


def log_dir() -> Path:
    """日志目录 ``logs/``。"""
    return config_dir() / "logs"


def cert_dir() -> Path:
    """证书目录 ``certs/``（阶段 9 的自签/用户证书落盘处）。"""
    return config_dir() / "certs"


def secret_store_file(name: str) -> Path:
    """文件落盘型 SecretStore 的存储文件（如 DPAPI/Fernet 密文索引）。"""
    return config_dir() / f"secrets.{name}.json"


def ensure_dirs() -> None:
    """创建配置/日志/证书目录（幂等）。"""
    for d in (config_dir(), log_dir(), cert_dir()):
        d.mkdir(parents=True, exist_ok=True)

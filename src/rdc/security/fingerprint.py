"""证书指纹信任列表管理（TOFU，阶段 9 加固版）。

首次连接自签名证书时，记录 SHA-256 指纹供用户确认；确认后加入信任列表，
后续连接自动信任；指纹不匹配拒绝连接，防中间人攻击。

v4 → v5 加固差异（任务书 B 组第 7 项"证书"范畴，与 :mod:`rdc.security.certs`
同属证书信任面）：

======================  =========================================  ==================================
维度                    v4                                        v5
======================  =========================================  ==================================
异常处理                ``except Exception`` 宽泛捕获（约束 6 违例）  收窄为 ``OSError``/``json.JSONDecodeError``/
                                                                ``UnicodeDecodeError``，失败可观测
落盘                    直接 ``open(w)``（进程中断可撕裂信任列表，
                        损坏即 MITM 面）                            **原子写**（tmp + ``os.replace``）
路径来源                ``_v4_config.CONFIG_DIR``                   :func:`rdc.config.paths.config_dir`
                                                                （模块级常量保留名，测试可 monkeypatch）
logger                  ``rdc.logger`` shim                       :func:`rdc.logging.get_logger`（structlog）
类型                    无注解（mypy 宽松清单 TODO(phase-9)）        全量注解，**已移出宽松清单**
======================  =========================================  ==================================

公开面不变（``compute_fingerprint`` / ``is_trusted`` / ``trust`` /
``get_host_for_fingerprint`` / ``remove`` / ``get_fingerprint_manager``），
:mod:`rdc.transport.tls` 与既有 TLS 测试零改动。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from rdc.config.paths import config_dir
from rdc.logging import get_logger

__all__ = [
    "FingerprintManager",
    "KNOWN_FINGERPRINTS_PATH",
    "compute_fingerprint",
    "get_fingerprint_manager",
]

_logger = get_logger("security.fingerprint")

#: 信任列表文件路径（模块级常量：测试以 monkeypatch 重定向到 tmp，勿改语义）。
KNOWN_FINGERPRINTS_PATH: Path = config_dir() / "known_fingerprints.json"


def compute_fingerprint(der_cert: bytes) -> str:
    """计算证书 DER 的 SHA-256 指纹（大写十六进制，冒号分隔）。"""
    digest = hashlib.sha256(der_cert).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class FingerprintManager:
    """TOFU 信任列表管理器（文件持久化，原子写）。"""

    def __init__(self, *, path: Path | None = None) -> None:
        """
        Args:
            path: 信任列表文件；``None`` 取模块级 :data:`KNOWN_FINGERPRINTS_PATH`
                （保持既有测试的 monkeypatch 面）。
        """
        self._path = path
        self._trusted: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ IO

    @property
    def path(self) -> Path:
        """当前生效的信任列表路径。"""
        return self._path if self._path is not None else KNOWN_FINGERPRINTS_PATH

    def _load(self) -> None:
        """加载信任列表；缺失=空表，损坏/不可读=warning+空表（不静默放行任何指纹）。"""
        target = self.path
        data: Any = None
        try:
            if target.exists():
                data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            _logger.warning("fingerprint.load_failed", path=str(target), error=str(exc))
            self._trusted = {}
            return
        entries = data.get("fingerprints") if isinstance(data, dict) else None
        self._trusted = entries if isinstance(entries, dict) else {}

    def _save(self) -> None:
        """原子写：tmp 文件 + ``os.replace``，进程中断不会撕裂信任列表。"""
        target = self.path
        tmp = target.with_name(target.name + ".tmp")
        payload = {"fingerprints": self._trusted, "updated_at": time.time()}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, target)
        except OSError as exc:
            # 内存态仍是本次会话的权威；落盘失败可观测并在下次写重试。
            _logger.warning("fingerprint.save_failed", path=str(target), error=str(exc))
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup_exc:  # pragma: no cover - 清理失败仅记录
                _logger.warning(
                    "fingerprint.tmp_cleanup_failed", path=str(tmp), error=str(cleanup_exc)
                )

    # ------------------------------------------------------------------ 信任面

    @staticmethod
    def compute_fingerprint(der_cert: bytes) -> str:
        """（类方法形式保留 v4 调用面）转发模块级 :func:`compute_fingerprint`。"""
        return compute_fingerprint(der_cert)

    def is_trusted(self, fingerprint: str, host: str = "") -> bool:
        """指纹是否已被信任。

        Args:
            fingerprint: SHA-256 指纹。
            host: 主机地址（保留参数以对齐 v4 调用面；当前仅按指纹判定）。
        """
        return fingerprint in self._trusted

    def trust(self, fingerprint: str, host: str = "") -> None:
        """加入信任列表并落盘（TOFU 用户确认后调用）。"""
        self._trusted[fingerprint] = {"host": host, "trusted_at": time.time()}
        self._save()
        _logger.info("fingerprint.trusted", fingerprint=fingerprint[:23], host=host)

    def get_host_for_fingerprint(self, fingerprint: str) -> str | None:
        """返回指纹关联的主机；未记录返回 ``None``。"""
        entry = self._trusted.get(fingerprint)
        if entry is not None and isinstance(entry, dict):
            return str(entry.get("host", ""))
        return None

    def remove(self, fingerprint: str) -> None:
        """从信任列表移除指纹（存在才落盘）。"""
        if self._trusted.pop(fingerprint, None) is not None:
            self._save()
            _logger.info("fingerprint.removed", fingerprint=fingerprint[:23])

    def fingerprints(self) -> tuple[str, ...]:
        """当前信任的全部指纹（管理接口/测试观测）。"""
        return tuple(self._trusted)


# 全局单例（TLS 路径默认使用；测试 monkeypatch 本名重置）
_fingerprint_manager: FingerprintManager | None = None


def get_fingerprint_manager() -> FingerprintManager:
    """获取指纹管理器单例。"""
    global _fingerprint_manager
    if _fingerprint_manager is None:
        _fingerprint_manager = FingerprintManager()
    return _fingerprint_manager

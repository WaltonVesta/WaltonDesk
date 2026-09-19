"""
安全工具模块（v4.0.1 新增）
证书指纹信任列表管理，用于自签名证书的 TOFU（Trust On First Use）机制。
"""

import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Optional

from config import CONFIG_DIR
from logger import get_logger

KNOWN_FINGERPRINTS_PATH = CONFIG_DIR / "known_fingerprints.json"


class FingerprintManager:
    """证书指纹信任管理器（TOFU 模式）

    首次连接自签名证书时，记录 SHA-256 指纹供用户确认。
    用户确认后加入信任列表，后续连接自动信任。
    指纹不匹配时拒绝连接，防止中间人攻击。
    """

    def __init__(self):
        self.logger = get_logger()
        self._trusted: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """加载信任列表"""
        try:
            if KNOWN_FINGERPRINTS_PATH.exists():
                with open(KNOWN_FINGERPRINTS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._trusted = data.get('fingerprints', {})
        except Exception as e:
            self.logger.warning(f"加载证书指纹信任列表失败: {e}")
            self._trusted = {}

    def _save(self) -> None:
        """保存信任列表"""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(KNOWN_FINGERPRINTS_PATH, 'w', encoding='utf-8') as f:
                json.dump({
                    'fingerprints': self._trusted,
                    'updated_at': time.time(),
                }, f, indent=2)
        except Exception as e:
            self.logger.warning(f"保存证书指纹信任列表失败: {e}")

    @staticmethod
    def compute_fingerprint(der_cert: bytes) -> str:
        """计算证书的 SHA-256 指纹（大写十六进制，冒号分隔）"""
        digest = hashlib.sha256(der_cert).hexdigest().upper()
        return ':'.join(digest[i:i+2] for i in range(0, len(digest), 2))

    def is_trusted(self, fingerprint: str, host: str = "") -> bool:
        """检查指纹是否已被信任

        Args:
            fingerprint: SHA-256 指纹
            host: 主机地址（用于记录关联）

        Returns:
            True 如果指纹在信任列表中
        """
        if fingerprint in self._trusted:
            return True
        return False

    def trust(self, fingerprint: str, host: str = "") -> None:
        """将指纹加入信任列表

        Args:
            fingerprint: SHA-256 指纹
            host: 主机地址
        """
        self._trusted[fingerprint] = {
            'host': host,
            'trusted_at': time.time(),
        }
        self._save()
        self.logger.info(f"已信任证书指纹: {fingerprint[:23]}... ({host})")

    def get_host_for_fingerprint(self, fingerprint: str) -> Optional[str]:
        """获取指纹关联的主机"""
        entry = self._trusted.get(fingerprint)
        if entry:
            return entry.get('host', '')
        return None

    def remove(self, fingerprint: str) -> None:
        """从信任列表中移除指纹"""
        if fingerprint in self._trusted:
            del self._trusted[fingerprint]
            self._save()
            self.logger.info(f"已移除证书指纹: {fingerprint[:23]}...")


# 全局单例
_fingerprint_manager: Optional[FingerprintManager] = None


def get_fingerprint_manager() -> FingerprintManager:
    """获取指纹管理器单例"""
    global _fingerprint_manager
    if _fingerprint_manager is None:
        _fingerprint_manager = FingerprintManager()
    return _fingerprint_manager

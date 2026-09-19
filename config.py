"""
配置管理模块
支持JSON持久化，自动加载保存，版本管理
"""

import base64
import json
import os
import sys
import ctypes
import time
from pathlib import Path
from typing import Any, Optional

from exceptions import ConfigError

# 配置文件路径
CONFIG_DIR = Path.home() / ".remote_desktop"
CONFIG_PATH = CONFIG_DIR / "config.json"
HISTORY_PATH = CONFIG_DIR / "history.json"

# 配置版本
CONFIG_VERSION = 5

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "port": 9999,
    "control_port": 9998,
    "jpeg_quality": 60,
    "min_quality": 30,
    "max_quality": 95,
    "min_fps": 5,
    "max_fps": 30,
    "use_diff": True,
    "heartbeat_interval": 3.0,
    "heartbeat_timeout": 10.0,
    "auto_reconnect": True,
    "reconnect_delay": 2.0,
    "max_reconnect_attempts": 10,
    "enable_clipboard": True,
    "enable_file_transfer": True,
    "language": "zh_CN",
    "hosts": {},
    "recent_hosts": [],
    "max_recent_hosts": 10,
    "bandwidth_limit": 0,  # 0 = 无限制，单位 KB/s
    "enable_compression": True,
    "max_connections": 10,
    # ===== 认证配置 =====
    "enable_auth": True,            # 是否启用认证（默认开启）
    "password_hash": "",            # Bcrypt 加盐哈希后的密码（空表示无密码）
    "auth_timeout": 10,             # 认证超时（秒）
    "max_auth_attempts": 3,         # 最大认证尝试次数
    "auth_ban_duration": 300,       # 认证失败后封禁时间（秒）
    # ===== TLS 证书（v4.0.0 自动生成） =====
    "tls_cert_generated": 0,        # 自签名证书生成时间戳（0=未生成）
    # ===== 安全配置 =====
    "enable_whitelist": False,      # 是否启用IP白名单
    "whitelist_ips": [],            # 白名单IP列表
    "enable_blacklist": False,      # 是否启用IP黑名单
    "blacklist_ips": [],            # 黑名单IP列表
    # ===== 记住密码 =====
    "remember_password": False,     # 是否记住密码
    "saved_password": "",           # 记住的密码（DPAPI加密存储，DPAPI1:前缀）
}

# 记住密码密文前缀（区分旧版base64明文存储）
_PASSWORD_PREFIX = "DPAPI1:"
# DPAPI附加熵（将密文绑定到本应用）
_DPAPI_ENTROPY = b"RemoteDesktop-Control-v3"
# v4.0.1：密钥环服务名（macOS Keychain / Linux libsecret）
_KEYRING_SERVICE = "RemoteDesktop"
_KEYRING_ACCOUNT = "default"


def _dpapi_protect(data: bytes) -> bytes:
    """使用Windows DPAPI加密数据"""
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), buf_in)
    buf_ent = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    blob_ent = DATA_BLOB(len(_DPAPI_ENTROPY), buf_ent)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "RemoteDesktop saved password",
        ctypes.byref(blob_ent), None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("DPAPI加密失败")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _dpapi_unprotect(data: bytes) -> bytes:
    """使用Windows DPAPI解密数据"""
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), buf_in)
    buf_ent = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    blob_ent = DATA_BLOB(len(_DPAPI_ENTROPY), buf_ent)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, ctypes.byref(blob_ent),
        None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("DPAPI解密失败")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


# ===== v4.0.1：非 Windows 平台密钥环支持 =====

def _macos_keychain_save(password: str) -> bool:
    """macOS：使用 Keychain 保存密码（需要 pyobjc）"""
    try:
        import Security
        service = _KEYRING_SERVICE.encode('utf-8')
        account = _KEYRING_ACCOUNT.encode('utf-8')
        pwd = password.encode('utf-8')
        Security.SecKeychainDeleteGenericPassword(
            None, len(service), service, len(account), account, None, None
        )
        status = Security.SecKeychainAddGenericPassword(
            None, len(service), service, len(account), account,
            len(pwd), pwd, None
        )
        return status == 0
    except Exception:
        return False


def _macos_keychain_load() -> Optional[str]:
    """macOS：从 Keychain 读取密码"""
    try:
        import Security
        import ctypes
        service = _KEYRING_SERVICE.encode('utf-8')
        account = _KEYRING_ACCOUNT.encode('utf-8')
        status, item = Security.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account,
            None, None, None
        )
        if status != 0 or item is None:
            return None
        password_length = ctypes.c_uint32(0)
        password_data = ctypes.c_void_p(0)
        status = Security.SecKeychainItemCopyAttributesAndData(
            item, None, None, None,
            ctypes.byref(password_length), ctypes.byref(password_data)
        )
        if status == 0 and password_data.value:
            data = ctypes.string_at(password_data.value, password_length.value)
            Security.SecKeychainItemFreeContent(None, password_data)
            return data.decode('utf-8')
        return None
    except Exception:
        return None


def _linux_libsecret_save(password: str) -> bool:
    """Linux：使用 libsecret (secretstorage) 保存密码"""
    try:
        import secretstorage
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            collection.unlock()
        collection.create_item(
            _KEYRING_SERVICE,
            {"application": _KEYRING_SERVICE, "user": _KEYRING_ACCOUNT},
            password.encode('utf-8'),
            replace=True
        )
        return True
    except Exception:
        return False


def _linux_libsecret_load() -> Optional[str]:
    """Linux：从 libsecret 读取密码"""
    try:
        import secretstorage
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        items = collection.search_items(
            {"application": _KEYRING_SERVICE, "user": _KEYRING_ACCOUNT}
        )
        for item in items:
            return item.get_secret().decode('utf-8')
        return None
    except Exception:
        return None


def is_keyring_available() -> bool:
    """v4.0.1：检查当前平台是否有可用的安全密码存储"""
    if sys.platform == 'win32':
        return True
    if sys.platform == 'darwin':
        try:
            import Security  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import secretstorage  # noqa: F401
        return True
    except ImportError:
        return False


class Config:
    """配置管理单例"""
    
    _instance: Optional['Config'] = None
    
    def __new__(cls) -> 'Config':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self) -> None:
        """初始化配置"""
        self._data: dict = {}
        self._ensure_config_dir()
        self.load()
    
    def _ensure_config_dir(self) -> None:
        """确保配置目录存在"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> None:
        """加载配置文件"""
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                
                # 版本迁移
                loaded_version = loaded.get("version", 0)
                if loaded_version < CONFIG_VERSION:
                    loaded = self._migrate_config(loaded, loaded_version)
                
                # 合并默认值
                self._data = DEFAULT_CONFIG.copy()
                self._data.update(loaded)
                
            except (json.JSONDecodeError, IOError):
                self._data = DEFAULT_CONFIG.copy()
                self.save()
        else:
            self._data = DEFAULT_CONFIG.copy()
            self.save()
        
        # 验证配置
        self._validate()
    
    def _migrate_config(self, old_config: dict, from_version: int) -> dict:
        """
        配置版本迁移
        
        Args:
            old_config: 旧配置
            from_version: 旧版本号
        
        Returns:
            迁移后的配置
        """
        # v1 -> v2: 新增字段
        if from_version < 2:
            old_config["enable_encryption"] = False
            old_config["tls_cert"] = ""
            old_config["tls_key"] = ""
            old_config["bandwidth_limit"] = 0
            old_config["enable_compression"] = True
            old_config["max_connections"] = 10
            old_config["version"] = 2
        
        # v2 -> v3: 新增认证和安全配置
        if from_version < 3:
            old_config["enable_auth"] = False
            old_config["password_hash"] = ""
            old_config["auth_timeout"] = 10
            old_config["max_auth_attempts"] = 3
            old_config["auth_ban_duration"] = 300
            old_config["enable_whitelist"] = False
            old_config["whitelist_ips"] = []
            old_config["enable_blacklist"] = False
            old_config["blacklist_ips"] = []
            old_config["version"] = 3
        
        # v3 -> v4: 默认开启认证，消除无密码暴露风险
        if from_version < 4:
            old_config["enable_auth"] = True
            old_config["version"] = 4

        # v4 -> v5: 密码哈希从SHA256升级为Bcrypt，强制重置密码；TLS证书自动生成
        if from_version < 5:
            old_password_hash = old_config.get("password_hash", "")
            if old_password_hash:
                # 旧版SHA256哈希失效，强制清空并提示用户重置
                old_config["password_hash"] = ""
                from logger import get_logger
                get_logger().warning(
                    "[MIGRATE] 检测到旧版 SHA256 密码哈希，已清除。"
                    "请使用 --set-password 重新设置密码。"
                )
            # 移除旧的加密相关配置
            old_config.pop("enable_encryption", None)
            old_config.pop("tls_cert", None)
            old_config.pop("tls_key", None)
            old_config["tls_cert_generated"] = 0
            old_config["version"] = 5

        return old_config
    
    def _validate(self) -> None:
        """验证配置有效性"""
        # 端口范围
        port = self.get('port', 9999)
        if not (1024 <= port <= 65535):
            raise ConfigError(f"端口 {port} 超出有效范围 (1024-65535)")
        
        # 画质范围
        quality = self.get('jpeg_quality', 60)
        if not (1 <= quality <= 100):
            raise ConfigError(f"画质 {quality} 超出有效范围 (1-100)")
        
        min_q = self.get('min_quality', 30)
        max_q = self.get('max_quality', 95)
        if not (1 <= min_q <= max_q <= 100):
            raise ConfigError(f"画质范围无效: min={min_q}, max={max_q}")
        
        # 帧率范围
        min_fps = self.get('min_fps', 5)
        max_fps = self.get('max_fps', 30)
        if not (1 <= min_fps <= max_fps <= 120):
            raise ConfigError(f"帧率范围无效: min={min_fps}, max={max_fps}")
        
        # 最大连接数
        max_conn = self.get('max_connections', 10)
        if not (1 <= max_conn <= 100):
            raise ConfigError(f"最大连接数 {max_conn} 超出有效范围 (1-100)")
    
    def save(self) -> None:
        """保存配置"""
        try:
            # 确保版本号正确
            self._data["version"] = CONFIG_VERSION
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            from logger import get_logger
            get_logger().error(f"保存配置失败: {e}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return self._data.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """设置配置项"""
        self._data[key] = value
        self.save()
    
    def update(self, values: dict) -> None:
        """批量更新"""
        self._data.update(values)
        self.save()
    
    def get_hosts(self) -> dict:
        """获取主机列表"""
        return self._data.get("hosts", {})
    
    def add_recent_host(self, host: str) -> None:
        """添加最近连接"""
        recent = self._data.get("recent_hosts", [])
        # 去重 - 移除所有相同项
        if host in recent:
            recent = [h for h in recent if h != host]
        recent.insert(0, host)
        max_count = self._data.get("max_recent_hosts", 10)
        self._data["recent_hosts"] = recent[:max_count]
        self.save()
    
    def get_recent_hosts(self) -> list:
        """获取最近连接列表"""
        return self._data.get("recent_hosts", [])
    
    def validate(self) -> bool:
        """验证配置（外部调用）"""
        try:
            self._validate()
            return True
        except ConfigError as e:
            from logger import get_logger
            get_logger().error(f"配置验证失败: {e}")
            return False

    def set_password(self, plain_password: str) -> None:
        """设置密码（Bcrypt加盐哈希，rounds=12）"""
        import bcrypt
        if plain_password:
            hashed = bcrypt.hashpw(
                plain_password.encode('utf-8'),
                bcrypt.gensalt(rounds=12)
            )
            self._data["password_hash"] = hashed.decode('utf-8')
        else:
            self._data["password_hash"] = ""
        self.save()

    def verify_password(self, plain_password: str) -> bool:
        """验证密码（Bcrypt）"""
        import bcrypt
        stored = self._data.get("password_hash", "")
        if not stored:
            return True  # 未设置密码
        try:
            return bcrypt.checkpw(
                plain_password.encode('utf-8'),
                stored.encode('utf-8')
            )
        except Exception:
            from logger import get_logger
            get_logger().warning("密码哈希校验异常，拒绝认证")
            return False

    def save_password(self, password: str, remember: bool = False) -> None:
        """保存密码（remember为True时使用平台密钥环/DPAPI加密存储，不再明文落盘）"""
        if remember and password:
            if sys.platform == 'win32':
                try:
                    encrypted = _dpapi_protect(password.encode('utf-8'))
                    encoded = base64.b64encode(encrypted).decode('ascii')
                    self._data["saved_password"] = _PASSWORD_PREFIX + encoded
                    self._data["remember_password"] = True
                except Exception as e:
                    from logger import get_logger
                    get_logger().error(f"DPAPI加密失败，放弃记住密码: {e}")
                    self._data["saved_password"] = ""
                    self._data["remember_password"] = False
            elif sys.platform == 'darwin':
                # v4.0.1：macOS Keychain
                if _macos_keychain_save(password):
                    self._data["saved_password"] = "KEYCHAIN:"
                    self._data["remember_password"] = True
                else:
                    from logger import get_logger
                    get_logger().warning("macOS Keychain不可用，不记住密码（请安装pyobjc）")
                    self._data["saved_password"] = ""
                    self._data["remember_password"] = False
            else:
                # v4.0.1：Linux libsecret
                if _linux_libsecret_save(password):
                    self._data["saved_password"] = "LIBSECRET:"
                    self._data["remember_password"] = True
                else:
                    from logger import get_logger
                    get_logger().warning("Linux密钥环不可用，不记住密码（请安装secretstorage）")
                    self._data["saved_password"] = ""
                    self._data["remember_password"] = False
        else:
            self._data["saved_password"] = ""
            self._data["remember_password"] = False
        self.save()

    def get_saved_password(self) -> str:
        """获取保存的密码（DPAPI/Keychain/libsecret解密；旧版base64明文自动升级加密）"""
        stored = self._data.get("saved_password", "")
        if not stored:
            return ""

        if stored.startswith(_PASSWORD_PREFIX):
            try:
                encrypted = base64.b64decode(stored[len(_PASSWORD_PREFIX):].encode('ascii'))
                return _dpapi_unprotect(encrypted).decode('utf-8')
            except Exception:
                from logger import get_logger
                get_logger().warning("解密已保存密码失败，已清除")
                self._data["saved_password"] = ""
                self._data["remember_password"] = False
                self.save()
                return ""

        # v4.0.1：macOS Keychain
        if stored == "KEYCHAIN:":
            pwd = _macos_keychain_load()
            if pwd:
                return pwd
            self._data["saved_password"] = ""
            self._data["remember_password"] = False
            self.save()
            return ""

        # v4.0.1：Linux libsecret
        if stored == "LIBSECRET:":
            pwd = _linux_libsecret_load()
            if pwd:
                return pwd
            self._data["saved_password"] = ""
            self._data["remember_password"] = False
            self.save()
            return ""

        # 旧版base64明文存储：解码后升级为DPAPI加密
        try:
            plain = base64.b64decode(stored.encode()).decode()
        except Exception:
            self._data["saved_password"] = ""
            self.save()
            return ""
        if plain:
            self.save_password(plain, True)
        return plain

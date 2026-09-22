"""macOS Keychain 后端 mock 测试（阶段 2，2.5/2.9）。

⚠️ 本机为 Windows，无法真机验证（R6）。本测试的价值完全取决于
**mock 面与 pyobjc 真实 API 面一致**，因此 ``FakeSecurity`` 严格镜像：

1. pyobjc 对"带出参的 C 函数"返回 ``(status, *outs)`` **元组**（非标量）：
   - AddGenericPassword    -> (status, item)
   - FindGenericPassword   -> (status, passwordLength, passwordData, item)
   - DeleteGenericPassword -> (status, foundClass, item)
2. ``passwordData`` 是 NSData 形态，可 ``bytes()`` 取内容；
3. ``SecKeychainItemFreeContent(attrList, data)`` 返回 None；
4. 未找到条目的 OSStatus 是 ``errSecItemNotFound = -25300``。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from rdc.exceptions import SecretStoreError
from rdc.secrets.keychain import SERVICE, KeychainStore, keychain_available

ERR_ITEM_NOT_FOUND = -25300


class FakeSecurity:
    """镜像 pyobjc Security 框架调用面的内存实现。"""

    def __init__(self) -> None:
        # (service, account) -> password bytes
        self.store: dict[tuple[bytes, bytes], bytes] = {}
        self.free_calls: int = 0
        self.fail_add: int | None = None

    # --- 真实签名：(keychain, sLen, service, aLen, account, pLen, password, itemRef)
    def SecKeychainAddGenericPassword(
        self, keychain, s_len, service, a_len, account, p_len, password, item_ref
    ):
        # 真机：重复条目返回 errSecDuplicateItem(-25299)，模拟该约束。
        if (service, account) in self.store:
            return (-25299, None)
        if self.fail_add is not None:
            return (self.fail_add, None)
        assert len(service) == s_len and len(account) == a_len and len(password) == p_len
        self.store[(service, account)] = bytes(password)
        return (0, object())

    def SecKeychainFindGenericPassword(
        self, keychain, s_len, service, a_len, account, pwd_len, pwd_data, item_ref
    ):
        data = self.store.get((service, account))
        if data is None:
            # 真机：未找到时 outs 为 None。
            return (ERR_ITEM_NOT_FOUND, None, None, None)
        # 真机：passwordData 是 NSData；这里用 bytes 替身（bytes(data) 可用）。
        return (0, len(data), data, object())

    def SecKeychainDeleteGenericPassword(
        self, keychain, s_len, service, a_len, account, found_class, item_ref
    ):
        if self.store.pop((service, account), None) is None:
            return (ERR_ITEM_NOT_FOUND, None, None)
        return (0, None, None)

    def SecKeychainItemFreeContent(self, attr_list, data):
        self.free_calls += 1
        return None


@pytest.fixture
def fake_sec(monkeypatch):
    """注入 FakeSecurity 并把平台伪装成 darwin。"""
    mod = FakeSecurity()
    monkeypatch.setitem(sys.modules, "Security", mod)
    monkeypatch.setattr(sys, "platform", "darwin")
    return mod


class TestPlatformGuard:
    def test_keychain_available_false_off_macos(self, monkeypatch, fake_sec) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert keychain_available() is False

    def test_keychain_available_true_on_macos_with_module(self, fake_sec) -> None:
        assert keychain_available() is True

    def test_keychain_available_false_without_pyobjc(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setitem(sys.modules, "Security", None)  # import Security -> ImportError
        assert keychain_available() is False

    def test_store_ops_raise_when_pyobjc_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setitem(sys.modules, "Security", None)
        with pytest.raises(SecretStoreError, match="Security 框架不可用"):
            KeychainStore().save("k", "v")


class TestStoreContract:
    def test_name_is_keychain(self, fake_sec) -> None:
        assert KeychainStore().name == "keychain"

    def test_save_then_load_roundtrip(self, fake_sec) -> None:
        store = KeychainStore()
        store.save("saved_password", "桌面密码")
        assert store.load("saved_password") == "桌面密码"
        assert fake_sec.store[(SERVICE.encode(), b"saved_password")] == "桌面密码".encode()

    def test_save_overwrites_existing_key(self, fake_sec) -> None:
        """save 必须先删后加，重复保存不报 errSecDuplicateItem。"""
        store = KeychainStore()
        store.save("k", "v1")
        store.save("k", "v2")
        assert store.load("k") == "v2"

    def test_load_missing_returns_none(self, fake_sec) -> None:
        assert KeychainStore().load("absent") is None

    def test_load_frees_data_buffer(self, fake_sec) -> None:
        store = KeychainStore()
        store.save("k", "v")
        store.load("k")
        assert fake_sec.free_calls == 1

    def test_save_failure_raises_with_status(self, fake_sec) -> None:
        fake_sec.fail_add = -25291  # errSecReadOnly 之类
        with pytest.raises(SecretStoreError, match="保存键.*-25291"):
            KeychainStore().save("k", "v")

    def test_load_unexpected_status_raises(self, fake_sec) -> None:
        """非 0 非 not-found 的 status（如 -25291 权限）必须抛错，不得当成"无值"。"""

        def boom(*_a, **_k):
            return (-25291, None, None, None)

        fake_sec.SecKeychainFindGenericPassword = boom  # type: ignore[method-assign]
        with pytest.raises(SecretStoreError, match="-25291"):
            KeychainStore().load("k")

    def test_delete_removes_entry(self, fake_sec) -> None:
        store = KeychainStore()
        store.save("k", "v")
        store.delete("k")
        assert store.load("k") is None

    def test_delete_missing_is_idempotent(self, fake_sec) -> None:
        KeychainStore().delete("absent")  # not-found 视为成功，不抛

    def test_delete_error_status_raises(self, fake_sec) -> None:
        def boom(*_a, **_k):
            return (-4, None, None)  # errSecUnimplemented

        fake_sec.SecKeychainDeleteGenericPassword = boom  # type: ignore[method-assign]
        with pytest.raises(SecretStoreError, match="OSStatus=-4"):
            KeychainStore().delete("k")

    def test_load_undecodable_bytes_raises(self, fake_sec) -> None:
        fake_sec.store[(SERVICE.encode(), b"k")] = b"\xff\xfe\xfa"  # 非法 UTF-8
        with pytest.raises(SecretStoreError, match="解码失败"):
            KeychainStore().load("k")

    def test_multiple_keys_isolated(self, fake_sec) -> None:
        store = KeychainStore()
        store.save("a", "1")
        store.save("b", "2")
        assert store.load("a") == "1"
        assert store.load("b") == "2"
        store.delete("a")
        assert store.load("a") is None and store.load("b") == "2"


class TestRuntimeTupleShape:
    """锁定 mock 与真机一致的返回形态（叮嘱 e 的回归防线）。"""

    def test_find_returns_4tuple(self, fake_sec: Any) -> None:
        res = fake_sec.SecKeychainFindGenericPassword(
            None, len(SERVICE), SERVICE.encode(), 1, b"k", None, None, None
        )
        assert isinstance(res, tuple) and len(res) == 4

    def test_add_returns_2tuple(self, fake_sec: Any) -> None:
        res = fake_sec.SecKeychainAddGenericPassword(
            None, len(SERVICE), SERVICE.encode(), 1, b"k", 1, b"v", None
        )
        assert isinstance(res, tuple) and len(res) == 2

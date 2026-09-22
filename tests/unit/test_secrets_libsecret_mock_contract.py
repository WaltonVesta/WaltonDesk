"""Linux libsecret 后端 mock 测试（阶段 2，2.5/2.9）。

本机为 Windows 无法真机验证（R6）。``FakeSecretStorage`` 严格镜像
``secretstorage`` 库的真实调用面：

- ``dbus_init() -> Connection``
- ``get_default_collection(conn) -> Collection``
- ``collection.is_locked() -> bool`` / ``collection.unlock()``
- ``collection.create_item(label, attributes, secret: bytes, replace=True)``
- ``collection.search_items(attributes) -> Iterable[Item]``
- ``item.get_secret() -> bytes`` / ``item.delete()``
"""

from __future__ import annotations

import sys

import pytest

from rdc.exceptions import SecretStoreError
from rdc.secrets.libsecret import APP_NAME, LibsecretStore, libsecret_available


class FakeItem:
    def __init__(self, session: FakeSecretStorage, label: str, attrs: dict, secret: bytes) -> None:
        self._session = session
        self.label = label
        self.attrs = attrs
        self._secret = secret

    def get_secret(self) -> bytes:
        if self._session.fail_get_secret:
            raise RuntimeError("io-error")
        return self._secret

    def delete(self) -> None:
        self._session.items.remove(self)


class FakeCollection:
    def __init__(self, session: FakeSecretStorage) -> None:
        self._session = session

    def is_locked(self) -> bool:
        return self._session.locked

    def unlock(self) -> None:
        if self._session.fail_unlock:
            raise RuntimeError("user-dismissed")
        self._session.locked = False

    def create_item(self, label, attributes, secret, replace=False):  # noqa: ANN001
        if self._session.fail_create:
            raise RuntimeError("dbus-denied")
        for item in self._session.items:
            if item.attrs == attributes:
                if replace:
                    item._secret = secret
                    return item
                raise RuntimeError("exists")
        item = FakeItem(self._session, label, dict(attributes), secret)
        self._session.items.append(item)
        return item

    def search_items(self, attributes):  # noqa: ANN001
        if self._session.fail_search:
            raise RuntimeError("dbus-io")
        return [i for i in self._session.items if all(i.attrs.get(k) == v for k, v in attributes.items())]


class FakeSecretStorage:
    """镜像 secretstorage 模块面的替身。"""

    def __init__(self) -> None:
        self.items: list[FakeItem] = []
        self.locked = False
        self.fail_unlock = False
        self.fail_create = False
        self.fail_search = False
        self.fail_get_secret = False
        self.dbus_init_calls = 0

    def dbus_init(self):
        self.dbus_init_calls += 1
        if self.locked and self.fail_unlock:
            pass  # 允许锁住 + 解锁失败组合
        return object()

    def get_default_collection(self, conn):  # noqa: ANN001
        return FakeCollection(self)


@pytest.fixture
def fake_ss(monkeypatch):
    """注入替身模块并把平台伪装为 linux。"""
    mod = FakeSecretStorage()
    monkeypatch.setitem(sys.modules, "secretstorage", mod)
    monkeypatch.setattr(sys, "platform", "linux")
    return mod


class TestPlatformGuard:
    def test_available_false_off_linux(self, monkeypatch, fake_ss) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert libsecret_available() is False

    def test_available_true_on_linux(self, fake_ss) -> None:
        assert libsecret_available() is True

    def test_available_false_when_locked_unlock_fails(self, fake_ss) -> None:
        fake_ss.locked = True
        fake_ss.fail_unlock = True
        assert libsecret_available() is False

    def test_available_false_without_secretstorage(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "secretstorage", None)
        assert libsecret_available() is False


class TestStoreContract:
    def test_name(self, fake_ss) -> None:
        assert LibsecretStore().name == "libsecret"

    def test_save_then_load_roundtrip(self, fake_ss) -> None:
        store = LibsecretStore()
        store.save("saved_password", "p@ss-桌面")
        assert store.load("saved_password") == "p@ss-桌面"
        only = fake_ss.items
        assert len(only) == 1
        assert only[0].attrs == {"application": APP_NAME, "rdc_key": "saved_password"}
        assert only[0].label == f"{APP_NAME}:saved_password"

    def test_locked_collection_unlocked_on_demand(self, fake_ss) -> None:
        fake_ss.locked = True
        store = LibsecretStore()
        store.save("k", "v")
        assert fake_ss.locked is False
        assert store.load("k") == "v"

    def test_save_twice_replaces(self, fake_ss) -> None:
        store = LibsecretStore()
        store.save("k", "v1")
        store.save("k", "v2")
        assert store.load("k") == "v2"
        assert len(fake_ss.items) == 1

    def test_load_missing_returns_none(self, fake_ss) -> None:
        assert LibsecretStore().load("absent") is None

    def test_delete_removes_item(self, fake_ss) -> None:
        store = LibsecretStore()
        store.save("k", "v")
        store.delete("k")
        assert store.load("k") is None

    def test_delete_missing_noop(self, fake_ss) -> None:
        LibsecretStore().delete("absent")

    def test_save_error_wraps_cause_with_platform(self, fake_ss) -> None:
        fake_ss.fail_create = True
        with pytest.raises(SecretStoreError, match="libsecret: 保存键"):
            LibsecretStore().save("k", "v")

    def test_load_error_wraps(self, fake_ss) -> None:
        fake_ss.fail_search = True
        with pytest.raises(SecretStoreError, match="libsecret: 读取键"):
            LibsecretStore().load("k")

    def test_load_decoding_failure_wraps(self, fake_ss) -> None:
        LibsecretStore().save("k", "v")
        fake_ss.items[0]._secret = b"\xff\xfa"
        with pytest.raises(SecretStoreError, match="读取键"):
            LibsecretStore().load("k")

    def test_delete_error_wraps(self, fake_ss) -> None:
        fake_ss.fail_search = True
        with pytest.raises(SecretStoreError, match="libsecret: 删除键"):
            LibsecretStore().delete("k")

    def test_connection_failure_wraps(self, fake_ss) -> None:
        def boom():
            raise ConnectionError("no dbus")

        fake_ss.dbus_init = boom  # type: ignore[method-assign]
        with pytest.raises(SecretStoreError, match="连接密钥环失败"):
            LibsecretStore().save("k", "v")

    def test_missing_module_raises_on_save(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "secretstorage", None)
        with pytest.raises(SecretStoreError, match="secretstorage 不可用"):
            LibsecretStore().save("k", "v")

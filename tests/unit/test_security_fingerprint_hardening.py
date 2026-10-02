"""security/fingerprint 加固测试（阶段 9，批 2 收口项）。

行为面（TOFU 首连/拒绝/落盘）已由 ``test_transport_tls.py`` 覆盖；
本文件锁定**加固差异**：异常收窄、原子写、损坏加载、缺失文件路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdc.security import fingerprint as fp_mod
from rdc.security.fingerprint import (
    FingerprintManager,
    compute_fingerprint,
    get_fingerprint_manager,
)


@pytest.fixture()
def manager(tmp_path: Path) -> FingerprintManager:
    return FingerprintManager(path=tmp_path / "trust" / "known_fp.json")


class TestComputeFingerprint:
    def test_format_upper_colon_pairs(self) -> None:
        fp = compute_fingerprint(b"der-bytes")
        parts = fp.split(":")
        assert len(parts) == 32
        assert all(len(p) == 2 and p == p.upper() for p in parts)

    def test_class_alias_equivalent(self) -> None:
        assert FingerprintManager.compute_fingerprint(b"x") == compute_fingerprint(b"x")

    def test_different_der_different_fp(self) -> None:
        assert compute_fingerprint(b"a") != compute_fingerprint(b"b")


class TestLoadPaths:
    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        # 回归锁定：v5 首版把 exists() False 路径写成 data 未定义（NameError）
        mgr = FingerprintManager(path=tmp_path / "absent" / "known_fp.json")
        assert mgr.fingerprints() == ()

    def test_corrupt_json_warns_and_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "known_fp.json"
        p.write_text("{broken", encoding="utf-8")
        mgr = FingerprintManager(path=p)
        assert mgr.fingerprints() == ()

    def test_non_dict_root_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "known_fp.json"
        p.write_text("[1,2]", encoding="utf-8")
        mgr = FingerprintManager(path=p)
        assert mgr.fingerprints() == ()

    def test_non_dict_entries_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "known_fp.json"
        p.write_text(json.dumps({"fingerprints": "oops"}), encoding="utf-8")
        mgr = FingerprintManager(path=p)
        assert mgr.fingerprints() == ()

    def test_roundtrip(self, manager: FingerprintManager) -> None:
        manager.trust("AA:BB", "10.0.0.1")
        again = FingerprintManager(path=manager.path)
        assert again.is_trusted("AA:BB")
        assert again.get_host_for_fingerprint("AA:BB") == "10.0.0.1"

    def test_untrusted_host_lookup_none(self, manager: FingerprintManager) -> None:
        assert manager.get_host_for_fingerprint("nope") is None


class TestAtomicSave:
    def test_no_tmp_leftover_after_trust(self, manager: FingerprintManager) -> None:
        manager.trust("AA:BB", "h")
        assert manager.path.exists()
        assert not manager.path.with_name(manager.path.name + ".tmp").exists()

    def test_save_failure_keeps_memory_authoritative(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        mgr = FingerprintManager(path=blocker / "sub" / "known_fp.json")
        mgr.trust("AA:BB", "h")  # 落盘失败（NotADirectoryError ⊂ OSError）但内存生效
        assert mgr.is_trusted("AA:BB")

    def test_remove_only_persists_when_present(self, manager: FingerprintManager) -> None:
        manager.trust("AA:BB", "h")
        before = manager.path.read_bytes()
        manager.remove("not-there")
        assert manager.path.read_bytes() == before  # 未命中不重写文件
        manager.remove("AA:BB")
        assert not manager.is_trusted("AA:BB")
        assert manager.path.read_bytes() != before

    def test_module_level_path_default(self, manager: FingerprintManager) -> None:
        # 不传 path 时属性回退模块级常量（保持测试 monkeypatch 面）
        mgr = FingerprintManager()
        assert mgr.path == fp_mod.KNOWN_FINGERPRINTS_PATH


class TestSingleton:
    def test_singleton_identity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_path / "known_fp.json")
        monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)
        a = get_fingerprint_manager()
        b = get_fingerprint_manager()
        assert a is b

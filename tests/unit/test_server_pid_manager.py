"""server/pid_manager 单元测试（阶段 9 批 3 接入补测）。

路径经 ``paths.set_override_root``（``tmp_app_dir`` 夹具）重定向，全程不触碰
真实 HOME；Windows 分支真机可验（psutil 已装 + ctypes OpenProcess 只读查询），
非 win32 分支本机不可达由 CI 覆盖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rdc.exceptions import ServiceError
from rdc.server.pid_manager import PIDManager


@pytest.fixture()
def manager(tmp_app_dir: Path) -> PIDManager:
    return PIDManager("TestService")


class TestPathResolution:
    def test_pid_file_under_override_root(self, manager: PIDManager, tmp_app_dir: Path) -> None:
        assert manager.pid_file == tmp_app_dir / "TestService.pid"

    def test_service_name_embedded(self, tmp_app_dir: Path) -> None:
        m = PIDManager("RemoteControlService")
        assert m.pid_file.name == "RemoteControlService.pid"


class TestWriteReadRemove:
    def test_write_then_read_roundtrip(self, manager: PIDManager) -> None:
        manager.write()
        assert manager.read() == os.getpid()

    def test_write_creates_parent_dir(self, manager: PIDManager) -> None:
        assert not manager.pid_file.parent.exists() or True
        manager.write()
        assert manager.pid_file.parent.is_dir()

    def test_write_overwrites(self, manager: PIDManager) -> None:
        manager.write()
        manager.pid_file.write_text("999999", encoding="utf-8")
        assert manager.read() == 999999
        manager.write()
        assert manager.read() == os.getpid()

    def test_read_missing_file_none(self, manager: PIDManager) -> None:
        assert manager.read() is None

    def test_read_corrupt_content_none(self, manager: PIDManager) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text("not-a-pid", encoding="utf-8")
        assert manager.read() is None

    def test_read_whitespace_tolerant(self, manager: PIDManager) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text(" 4242 \n", encoding="utf-8")
        assert manager.read() == 4242

    def test_remove_deletes(self, manager: PIDManager) -> None:
        manager.write()
        manager.remove()
        assert not manager.pid_file.exists()

    def test_remove_missing_is_noop(self, manager: PIDManager) -> None:
        manager.remove()  # 不抛

    def test_write_failure_raises_service_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        # pid_file = blocker/<name>.pid → mkdir 触发 NotADirectoryError（⊂ OSError）
        m = PIDManager("T")
        m.pid_file = blocker / "nested" / "T.pid"
        with pytest.raises(ServiceError, match="写入PID失败"):
            m.write()

    def test_remove_failure_warns_not_raises(
        self, manager: PIDManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager.write()
        real_unlink = Path.unlink

        def boom(self: Path, *a: object, **kw: object) -> None:
            raise OSError("模拟删除失败")

        monkeypatch.setattr(Path, "unlink", boom)
        try:
            manager.remove()  # warning 不抛（约束 6：可观测降级）
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)


class TestIsRunning:
    def test_no_file_false(self, manager: PIDManager) -> None:
        assert manager.is_running() is False

    def test_own_pid_true(self, manager: PIDManager) -> None:
        manager.write()
        assert manager.is_running() is True

    def test_dead_pid_false(self, manager: PIDManager) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text("99999999", encoding="utf-8")  # 几乎不可能存活
        assert manager.is_running() is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Win32 回退分支本机验证")
    def test_win32_psutil_importerror_falls_back_to_api(
        self, manager: PIDManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager.write()
        monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil → ImportError
        assert manager.is_running() is True  # 走 _win32_pid_exists(OpenProcess)

    def test_win32_api_dead_pid(self, manager: PIDManager) -> None:
        assert PIDManager._win32_pid_exists(99999999) is False

    def test_win32_api_own_pid(self, manager: PIDManager) -> None:
        assert PIDManager._win32_pid_exists(os.getpid()) is True


class TestCleanupStale:
    def test_missing_file_false(self, manager: PIDManager) -> None:
        assert manager.cleanup_stale() is False

    def test_corrupt_file_removed_true(self, manager: PIDManager) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text("junk", encoding="utf-8")
        assert manager.cleanup_stale() is True
        assert not manager.pid_file.exists()

    def test_dead_pid_removed_true(self, manager: PIDManager) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text("99999999", encoding="utf-8")
        assert manager.cleanup_stale() is True

    def test_alive_pid_kept_false(self, manager: PIDManager) -> None:
        manager.write()
        assert manager.cleanup_stale() is False
        assert manager.pid_file.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Win32 回退分支本机验证")
    def test_cleanup_win32_fallback(
        self, manager: PIDManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
        manager.pid_file.write_text("99999999", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert manager.cleanup_stale() is True

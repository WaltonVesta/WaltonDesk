"""阶段 10 品牌更名哨兵：数据根迁移 + 品牌名单点 + 自启注册表迁移。

三组哨兵各自锁一条更名可能引入的静默回归：

1. **数据根迁移**（:func:`migrate_legacy_root`）：漏搬 = 用户"存过的密码突然
   没了"；多搬 = 覆盖新目录权威数据。两条都要红得住。
2. **品牌名单点**：更名不彻底最典型的形态就是"改了窗口标题漏了托盘 tooltip"。
3. **自启注册表值名**：旧值名残留 = 开机拉起失效路径 + 托盘勾选态失真。

⚠️ 本文件**绝不触碰真实 HOME**：迁移用例全部经 ``legacy=`` / ``new=`` 显式注入
tmp 路径（另有一条用例专门锁定"旧根取新根同级、绝不越界"的安全语义）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rdc.brand import BRAND_NAME, brand_title
from rdc.config import paths


class TestBrandSingleSource:
    """品牌名单点：所有用户可见位置都必须含 WaltonDesk。"""

    def test_app_dir_renamed(self) -> None:
        assert paths.APP_DIR_NAME == ".waltondesk"
        assert paths.LEGACY_APP_DIR_NAME == ".remote_desktop"

    def test_cli_banner_uses_brand(self) -> None:
        from rdc.cli import build_parser

        parser = build_parser()
        assert parser.prog == "waltondesk"
        assert BRAND_NAME in (parser.description or "")

    def test_brand_title_forms(self) -> None:
        assert brand_title().startswith(BRAND_NAME)
        assert "v" in brand_title()  # 带版本号
        assert brand_title("主控端").endswith("主控端")
        assert brand_title(with_version=False) == BRAND_NAME

    def test_main_window_title_uses_brand(self, qapp: Any) -> None:
        from rdc.ui.main_window import MainWindow

        win = MainWindow()
        try:
            assert win.windowTitle().startswith(BRAND_NAME)
            assert "主控端" in win.windowTitle()
        finally:
            win.deleteLater()

    def test_tray_tooltip_uses_brand(self, qapp: Any) -> None:
        """托盘 tooltip 是更名最易漏的角落（历史上同类问题反复出现）。"""
        from rdc.ui.tray import TrayController

        ctrl = TrayController(autostart_get=lambda: False, autostart_set=lambda _e: True)
        try:
            ctrl.update_client_count(0)
            tip = ctrl._icon.toolTip()  # noqa: SLF001 - 哨兵锁展示面
            assert tip.startswith(BRAND_NAME)
            assert "远程桌面控制" not in tip
        finally:
            ctrl.hide()

    def test_app_icon_resource_loads(self) -> None:
        """图标资源必须真的在包内且可解码（漏生成 = 打包产物没图标且无人发现）。"""
        from rdc.ui.icons import ICON_RESOURCE, app_icon_path, build_app_icon

        p = Path(app_icon_path())
        assert p.name == ICON_RESOURCE
        assert p.is_file() and p.stat().st_size > 1000
        assert build_app_icon() is not None
        assert not build_app_icon().isNull()  # type: ignore[union-attr]


class TestLegacyRootPathSafety:
    """旧根解析：必须是新根的同级目录，绝不越界碰真实 HOME。"""

    def test_legacy_is_sibling_of_new(self, tmp_app_dir: Path) -> None:
        """override 根下，旧根 = 新根的兄弟目录（同沙箱，不越界）。"""
        legacy = paths.legacy_config_dir()
        new = paths.config_dir()
        assert legacy.name == paths.LEGACY_APP_DIR_NAME
        assert legacy.parent == new.parent
        assert legacy != new

    def test_legacy_uses_sibling_not_hardcoded_home(self, tmp_app_dir: Path) -> None:
        """旧根 = 新根同级（可被 override 根带走）。

        这是"绝不越界"的真正保证：一旦有人改回写死 ``Path.home()``，本用例
        在 override 模式下立即转红。
        """
        root = paths.override_root()
        assert root is not None
        assert paths.config_dir() == root
        assert paths.legacy_config_dir() == root.parent / paths.LEGACY_APP_DIR_NAME

    def test_auto_migration_skipped_under_override_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_enabled: Any
    ) -> None:
        """override 根（便携/测试）下**自动**迁移必须跳过。

        危险场景：pytest 的 tmp_path 位于真实 ``~`` 之下。若自动迁移在
        override 模式下照跑，源会按"同级"推导指到**真实的**
        ``~/.remote_desktop`` 并整目录 rename 走 —— 破坏用户数据。守卫必须
        挡在任何 IO 之前。

        用假 HOME（monkeypatch ``Path.home``）复刻该几何关系，全程不落真实目录。
        """
        fake_home = tmp_path / "fakehome"
        real_legacy = fake_home / paths.LEGACY_APP_DIR_NAME
        real_legacy.mkdir(parents=True)
        marker = real_legacy / "MUST-NOT-TOUCH.txt"
        marker.write_text("sentinel", encoding="utf-8")
        monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: fake_home))

        # override 根设成假 HOME 下的新目录名（复刻 tmp_path ⊂ ~ 的危险几何）
        paths.set_override_root(fake_home / paths.APP_DIR_NAME)
        try:
            assert paths.migrate_legacy_root() is False  # 不传参 = 自动路径
            assert marker.exists(), "真实旧目录被自动迁移动了——守卫失效"
            assert not paths.config_dir().exists()

            # 对照：回到默认根（override=None）→ 同一路径下确实该迁，且迁成功
            paths.set_override_root(None)
            assert paths.migrate_legacy_root() is True
            assert not marker.exists()
        finally:
            paths.set_override_root(None)


class TestMigrateLegacyRoot:
    """旧数据根 → 新数据根：搬对、幂等、绝不覆盖、失败不炸。"""

    @pytest.fixture
    def roots(self, tmp_path: Path, migration_enabled: Any) -> tuple[Path, Path]:
        """返回 (旧根, 新根)——两者都**不存在**，由用例按需创建。"""
        home = tmp_path / "home"
        home.mkdir()
        return home / paths.LEGACY_APP_DIR_NAME, home / paths.APP_DIR_NAME

    def test_no_legacy_is_noop(self, roots: tuple[Path, Path]) -> None:
        legacy, new = roots
        assert not legacy.exists()
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is False
        assert not new.exists()

    def test_same_root_is_noop(self, roots: tuple[Path, Path]) -> None:
        """两个根相同（配置错位）→ 直接放弃，绝不自己搬自己。"""
        legacy, _new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("x", encoding="utf-8")
        assert paths.migrate_legacy_root(legacy=legacy, new=legacy) is False
        assert (legacy / "config.json").exists()

    def test_moves_files_via_rename(self, roots: tuple[Path, Path]) -> None:
        """同盘 rename 路径：内容整体出现在新根，旧根消失（rename 语义）。"""
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text('{"version": 6}', encoding="utf-8")
        (legacy / "logs").mkdir()
        (legacy / "logs" / "rdc.log").write_text("x", encoding="utf-8")

        assert paths.migrate_legacy_root(legacy=legacy, new=new) is True
        assert (new / "config.json").read_text(encoding="utf-8") == '{"version": 6}'
        assert (new / "logs" / "rdc.log").exists()
        assert not legacy.exists()

    def test_second_run_is_idempotent(self, roots: tuple[Path, Path]) -> None:
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("a", encoding="utf-8")
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is True
        # rename 后旧根已不存在 → 第二次直接 False，不再做任何 IO
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is False

    def test_never_overwrites_existing_new_root(self, roots: tuple[Path, Path]) -> None:
        """新根已有 config.json（重装/回滚场景）→ 整个迁移放弃，绝不动它。"""
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("OLD", encoding="utf-8")
        new.mkdir(parents=True)
        (new / "config.json").write_text("NEW", encoding="utf-8")

        assert paths.migrate_legacy_root(legacy=legacy, new=new) is False
        assert (new / "config.json").read_text(encoding="utf-8") == "NEW"
        assert (legacy / "config.json").exists()  # 旧根也完好无损

    def test_copy_fallback_skips_existing_and_writes_marker(
        self, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rename 失败（跨盘/占用）→ copy2 兜底：补齐缺失、跳过已存在、写标记。

        新根刻意预置 banlist.json 但**不放 config.json**——有 config.json 会
        触发"新根已是权威数据源"守卫、整个迁移在 rename 之前就放弃了，根本
        走不到兜底路径（那由 :meth:`test_never_overwrites_existing_new_root` 覆盖）。
        """
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("OLD", encoding="utf-8")
        (legacy / "banlist.json").write_text("OLD-BAN", encoding="utf-8")
        new.mkdir(parents=True)
        (new / "banlist.json").write_text("NEW-BAN", encoding="utf-8")

        real_rename = Path.rename

        def _fake_rename(self: Path, target: Path) -> Path:
            if self == legacy:
                raise OSError("跨设备或文件被占用")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", _fake_rename)

        assert paths.migrate_legacy_root(legacy=legacy, new=new) is True
        assert (new / "config.json").read_text(encoding="utf-8") == "OLD"  # 补齐缺失
        assert (new / "banlist.json").read_text(encoding="utf-8") == "NEW-BAN", (
            "已存在的目标文件绝不覆盖"
        )
        assert (new / paths._MIGRATION_MARKER).exists()  # noqa: SLF001 - 哨兵锁标记
        assert (legacy / "config.json").exists()  # 只搬不删（copy 路径保留旧根）

    def test_third_run_skipped_by_marker(
        self, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """copy 兜底后第三次调用靠标记短路，不重复扫描。"""
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "banlist.json").write_text("{}", encoding="utf-8")

        def _boom(self: Path, _t: Path) -> Path:
            raise OSError("跨设备")

        monkeypatch.setattr(Path, "rename", _boom)
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is True

        (legacy / "history.json").write_text("[]", encoding="utf-8")
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is False
        assert not (new / "history.json").exists()

    def test_failure_never_raises(
        self, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """迁移全程异常只留痕、绝不上抛——迁移失败不该阻止被控端启动。"""
        legacy, _new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("x", encoding="utf-8")

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("磁盘炸了")

        monkeypatch.setattr(Path, "rename", _boom)
        monkeypatch.setattr(paths.shutil, "copy2", _boom)
        assert paths.migrate_legacy_root(legacy=legacy, new=_new) is False  # 不抛

    def test_disabled_flag_blocks_migration(self, roots: tuple[Path, Path]) -> None:
        """全局开关关闭（测试会话默认态）→ 即使显式传路径也绝不搬迁。"""
        legacy, new = roots
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("x", encoding="utf-8")
        paths.set_legacy_migration_enabled(False)  # conftest 默认即 False，这里显式重申
        assert paths.migrate_legacy_root(legacy=legacy, new=new) is False
        assert not new.exists()
        assert (legacy / "config.json").exists()

    def test_cli_main_triggers_migration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """主入口必须真的调用迁移（漏挂 = 老用户的口令与证书永远读不到）。

        打桩 :func:`rdc.cli.migrate_legacy_root` 记录调用（本会话全局开关为关，
        真迁移不会发生），用 ``--version`` 走"解析即退出"分支——迁移排在
        :func:`build_parser` 之前，故 SystemExit 前必已调用。
        """
        import rdc.cli as cli

        calls: list[str] = []
        monkeypatch.setattr(cli, "migrate_legacy_root", lambda: calls.append("migrate"))
        with pytest.raises(SystemExit):
            cli.main(["--version"])
        assert calls == ["migrate"]


class TestAutostartRegistryRename:
    """自启注册表值名更名：新名生效、旧名清理、回读不失真。"""

    @pytest.fixture
    def fake_winreg(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        import sys
        import types

        store: dict[str, str] = {}

        class _Key:
            def __enter__(self) -> _Key:
                return self

            def __exit__(self, *_a: object) -> bool:
                return False

        def _query(_k: object, name: str) -> tuple[str, int]:
            if name not in store:
                raise FileNotFoundError(name)
            return store[name], 1

        def _set(_k: object, name: str, _r: int, _t: int, value: str) -> None:
            store[name] = value

        def _delete(_k: object, name: str) -> None:
            if name not in store:
                raise FileNotFoundError(name)
            del store[name]

        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            KEY_SET_VALUE=2,
            REG_SZ=1,
            OpenKey=lambda *_a, **_k: _Key(),
            SetValueEx=_set,
            QueryValueEx=_query,
            DeleteValue=_delete,
        )
        fake._store = store  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "winreg", fake)
        monkeypatch.setattr("platform.system", lambda: "Windows")
        return fake

    def test_default_service_name_is_brand(self) -> None:
        from rdc.host import autostart as am

        assert am.SERVICE_NAME == "WaltonDesk"
        assert am.LEGACY_SERVICE_NAME == "RemoteControlService"
        assert am.AutostartManager().service_name == "WaltonDesk"

    def test_install_removes_legacy_value(self, fake_winreg: Any) -> None:
        from rdc.host import autostart as am

        fake_winreg._store[am.LEGACY_SERVICE_NAME] = r'"C:\old\pythonw.exe" -m rdc serve'  # noqa: SLF001
        assert am.AutostartManager(frozen=True, exe_path=r"C:\d\WaltonDesk.exe").install()

        store = fake_winreg._store  # noqa: SLF001
        assert am.SERVICE_NAME in store
        assert "WaltonDesk.exe" in store[am.SERVICE_NAME]
        assert am.LEGACY_SERVICE_NAME not in store, (
            "旧自启项必须清掉，否则开机拉起失效路径、且与新项并存"
        )

    def test_uninstall_removes_legacy_too(self, fake_winreg: Any) -> None:
        from rdc.host import autostart as am

        store = fake_winreg._store  # noqa: SLF001
        store[am.SERVICE_NAME] = "x"
        store[am.LEGACY_SERVICE_NAME] = "y"
        assert am.AutostartManager(frozen=True, exe_path="X").uninstall()
        assert store == {}, "取消勾选必须把新旧两条都清干净"

    def test_query_falls_back_to_legacy(self, fake_winreg: Any) -> None:
        """只有旧项时仍报"已开启"——否则托盘显示未勾选，用户点了却没变化。"""
        from rdc.host import autostart as am

        fake_winreg._store[am.LEGACY_SERVICE_NAME] = "old-cmd"  # noqa: SLF001
        assert am.AutostartManager(frozen=True, exe_path="X").is_enabled() is True

    def test_query_false_when_neither(self, fake_winreg: Any) -> None:
        from rdc.host import autostart as am

        assert am.AutostartManager(frozen=True, exe_path="X").is_enabled() is False

    def test_custom_service_name_does_not_touch_legacy(self, fake_winreg: Any) -> None:
        """显式自定义服务名（多实例场景）不得越界删别人的旧条目。"""
        from rdc.host import autostart as am

        store = fake_winreg._store  # noqa: SLF001
        store[am.LEGACY_SERVICE_NAME] = "someone-else"
        assert am.AutostartManager("OtherSvc", frozen=True, exe_path="X").install()
        assert store[am.LEGACY_SERVICE_NAME] == "someone-else"

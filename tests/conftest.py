"""pytest 全局夹具。

阶段 1 建立测试骨架；后续阶段在此追加各层夹具（假屏幕源、事件循环、
双进程服务、qtbot 配置等）。

所有夹具遵循依赖注入原则：被测代码通过参数接收外部依赖，测试提供替身，
从而无需真实屏幕 / socket / 注册表即可单元测试（见 ASSESSMENT.md §5.7）。
"""

from __future__ import annotations

import pytest

from rdc.config import paths
from rdc.config import settings as settings_mod


@pytest.fixture(scope="session", autouse=True)
def isolate_logging(tmp_path_factory):
    """会话级：日志文件写进 pytest 临时目录，绝不触碰真实 ~/.waltondesk/logs。

    ``get_logger()`` 首次调用会自动 ``configure_logging()``；若不先注入，
    任何被测代码（如 migration 的告警）都会向真实 HOME 建目录写日志。
    """
    from rdc.logging import configure_logging

    log_dir = tmp_path_factory.mktemp("rdc-logs")
    configure_logging("DEBUG", log_file=log_dir / "rdc.log")


@pytest.fixture(scope="session", autouse=True)
def isolate_legacy_root_migration():
    """会话级：关闭 ``~/.remote_desktop`` → ``~/.waltondesk`` 的真实迁移。

    :func:`rdc.config.paths.migrate_legacy_root` 读写的是**真实 HOME**——它是
    唯一不能被 ``set_override_root`` 天然接管的路径语义（"从真实旧目录搬到
    真实新目录"），CLI 主入口每次调用都会触发。测试套件红线是不碰真实 HOME，
    故全局关闭；迁移行为由 tests/unit/test_stage10_branding.py 用 override 根
    + 下方 ``migration_enabled`` 夹具显式覆盖。
    """
    paths.set_legacy_migration_enabled(False)
    yield
    paths.set_legacy_migration_enabled(True)


@pytest.fixture
def migration_enabled():
    """迁移单测专用：临时打开迁移开关（配合 set_override_root 使用）。"""
    paths.set_legacy_migration_enabled(True)
    yield
    paths.set_legacy_migration_enabled(False)


@pytest.fixture
def tmp_app_dir(tmp_path, monkeypatch):
    """把应用数据根目录重定向到 pytest 临时目录（autouse 级隔离）。

    阶段 2 的 config/secrets/logging 全部经 ``rdc.config.paths`` 取路径，
    本夹具让测试完全不触碰真实 ``~/.waltondesk``。
    """
    paths.set_override_root(tmp_path)
    settings_mod.reset_for_test()
    yield tmp_path
    paths.set_override_root(None)
    settings_mod.reset_for_test()


@pytest.fixture
def config_file(tmp_app_dir):
    """临时目录内的 config.json 路径。"""
    return tmp_app_dir / "config.json"


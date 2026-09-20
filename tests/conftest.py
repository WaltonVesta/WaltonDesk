"""pytest 全局夹具。

阶段 1 建立测试骨架；后续阶段在此追加各层夹具（假屏幕源、事件循环、
双进程服务、qtbot 配置等）。

所有夹具遵循依赖注入原则：被测代码通过参数接收外部依赖，测试提供替身，
从而无需真实屏幕 / socket / 注册表即可单元测试（见 ASSESSMENT.md §5.7）。
"""

from __future__ import annotations

import pytest

from rdc.config import _v4_config


@pytest.fixture
def uninitialized_config() -> _v4_config.Config:
    """返回一个**未初始化**的 Config 实例（不触碰真实 home 目录）。

    ``Config.__new__`` 被重写为单例，调用 ``Config()`` 会触发 ``_initialize``
    并向 ``~/.remote_desktop/config.json`` 写文件——单元测试绝不能污染真实
    home。这里用 ``object.__new__`` 绕过自定义 ``__new__``，得到裸实例。

    仅用于测试 ``_migrate_config`` 这类**不依赖实例状态**的纯逻辑方法。
    需要真实加载/保存行为的测试请用 ``tmp_config``（阶段 2 补充）。
    """
    return object.__new__(_v4_config.Config)

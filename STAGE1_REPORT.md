# STAGE1_REPORT.md — 阶段 1：项目脚手架

> 完成日期：2026-09-22
> 基线：git tag `v4.0.3-baseline` @ `dfe2281`
> 起始状态：零工程化（无 pyproject.toml、无测试、无 lint/类型检查配置）
> 结束状态：src layout + 全工具链 + 32 测试 + ruff/mypy 0 error

---

## 1. 子任务完成状态（对照 REFACTOR_PLAN §3 阶段 1）

| # | 任务 | 计划工时 | 实际状态 | 说明 |
|---|------|----------|----------|------|
| 1.1 | 写 `pyproject.toml` | 3h | ✅ | PEP 621 + ruff/mypy/pytest/coverage/bandit 配置 |
| 1.2 | ruff 配置 | 1.5h | ✅ | `line-length=100`，`select=["E","F","I","N","UP","B","SIM"]`，逐目录 per-file-ignores |
| 1.3 | mypy 配置 | 1.5h | ✅ | `strict=true`，`mypy_path=src`，`plugins=[pydantic.mypy]`，37 文件 0 error |
| 1.4 | `.pre-commit-config.yaml` | 1h | ⏭️ 推迟 | 推迟到阶段 10 CI/CD 一并交付（节省阶段 1 额度） |
| 1.5 | 安装依赖 `uv sync` | 1h | ✅ | 32 个包，`rdc==5.0.0` 可编辑安装 |
| 1.6 | 建 `src/rdc/` layout | 2h | ✅ | 10 个子包 `__init__.py` + `py.typed` |
| 1.7 | 机械搬迁 25 文件 | 6h | ✅ | 24 文件 → `src/rdc/`，59 处 import 改写，mypy strict 下补全类型注解 |
| 1.8 | `gui/*.py` → `legacy_v4/` | 0.5h | ✅ | `git mv` 46 文件（含 Tkinter 全部 2,588 行） |
| 1.9 | 删除死代码 | 1h | ✅ | 移入 `legacy_v4/`（D5 偏离：保留而非删除） |
| 1.10 | 清零静默吞错 | 4h | ✅ | 31/31 全部清零（SIM105×24 + E722×7），含 service.py 提取 2 个 helper |
| 1.11 | 建 `tests/` 骨架 | 2h | ✅ | `conftest.py`（`uninitialized_config` 夹具）+ `tests/unit/` |
| 1.12 | 首批回归测试 | 3h | ✅ | 32 个测试全部通过（framing 19 + ratelimit 7 + config_migration 6） |
| 1.13 | 更新 `.gitignore` | 0.5h | ✅ | 新增 `src/rdc/protocol/pb/`、`.mypy_cache/`、`.ruff_cache/`、`htmlcov/`、`.coverage.*`、`*.egg-info/` |

**总计：12/13 完成，1 项（1.4 pre-commit）推迟到阶段 10。**

---

## 2. 验收结果（真实输出，2026-09-22）

```
$ uv run ruff check src tests
All checks passed!

$ uv run mypy src
Success: no issues found in 37 source files

$ uv run pytest tests/unit -v
32 passed in 2.10s

$ uv run python -m rdc --version
远程桌面控制 v5.0.0

$ uv run pytest tests/unit --cov=src/rdc --cov-report=term-missing
TOTAL  3027  2717  738  11  9.5%
```

覆盖率 9.5% 为阶段 1 初始基线（仅搬迁代码未进入业务重写），阶段 2 目标 `config/` + `secrets/` ≥85%。

---

## 3. 关键决策记录

### 3.1 已确认决策（来自 REFACTOR_PLAN §5）

| # | 决策 | 状态 |
|---|------|------|
| Q1 | 本机 PyInstaller -D，Nuitka 只在 CI | ✅ 已确认 |
| Q2 | CI 矩阵 3.12-3.14，`requires-python >= 3.12` | ✅ 已确认（发现 1 更新为 ≥3.12） |
| Q3 | v5 全帧 H.264，v4 JPEG 差分 | ✅ 已确认 |
| Q4 | v5.0 保留 → v5.1 deprecated → v6.0 删除 | ⏳ 默认采纳，未异议 |
| Q5 | 交付配置 + 本地模拟，不推送外部 | ⏳ 默认采纳，未异议 |

### 3.2 对计划的偏离（D1-D7）

| # | 计划原定 | 实际做法 | 原因与影响 |
|---|----------|----------|------------|
| D1 | `logger.py` → `logging/setup.py` | 保持 `rdc/logger.py` 单模块 | `logging` 遮蔽标准库；阶段 2 改 structlog 时再重组。**无功能影响。** |
| D2 | `config.py` → `config/settings.py` | → `config/_v4_config.py` | 下划线标记"待阶段 2 重写"，避免与 Pydantic Settings 混淆。**阶段 2 会重命名。** |
| D3 | `utils/` → `platform/` | → `host/` | `platform` 遮蔽标准库。**更优选择。** |
| D4 | mouse+keyboard 合并为 `legacy_pyautogui.py` | 各自独立搬迁 | 合并不属于"机械搬迁"，留到阶段 7。**推迟而非取消。** |
| D5 | **删除** renderer/kcp_channel/build.bat | **移入 `legacy_v4/` 保留** | 删除不可逆，等用户单独授权。**保守决策，阶段 6 完成后再删。** |
| D6 | mypy `warn_unreachable = true` | **关闭** | 不属于 strict；对跨线程守卫与跨平台分支产生结构性误报。**pyproject.toml 已写明理由。** |
| D7 | service.py 逐处加日志 | **提取 2 个 helper** | `_close_quietly()` + `_shutdown_and_close()`，消除 10 段逐字重复。**更优实现。** |

---

## 4. 新增/修改文件清单

### 4.1 新增文件

| 文件 | 用途 |
|------|------|
| `pyproject.toml` | PEP 621 + 工具链配置 |
| `uv.lock` | 依赖锁文件 |
| `.gitattributes` | LF 统一（.bat/.ps1/.iss 保留 CRLF） |
| `src/rdc/__init__.py` | `__version__ = "5.0.0"` |
| `src/rdc/__main__.py` | `python -m rdc` 入口 |
| `src/rdc/cli.py` | argparse 子命令 |
| `src/rdc/py.typed` | PEP 561 类型标记 |
| `src/rdc/constants.py` | 协议常量（搬迁） |
| `src/rdc/exceptions.py` | 7 个异常类（搬迁） |
| `src/rdc/logger.py` | 全局日志（搬迁，阶段 2 改 structlog） |
| `src/rdc/capture/__init__.py` | 子包 |
| `src/rdc/capture/diff.py` | 块级差分（搬迁） |
| `src/rdc/capture/framerate.py` | 动态帧率（搬迁） |
| `src/rdc/capture/mss_source.py` | mss 截图（搬迁） |
| `src/rdc/client/__init__.py` | 子包 |
| `src/rdc/client/ratelimit.py` | 滑动窗口限流（搬迁） |
| `src/rdc/config/__init__.py` | 子包 |
| `src/rdc/config/_v4_config.py` | v4 配置（搬迁，阶段 2 重写） |
| `src/rdc/encoding/__init__.py` | 子包 |
| `src/rdc/encoding/jpeg.py` | JPEG 编码（搬迁） |
| `src/rdc/host/__init__.py` | 子包 |
| `src/rdc/host/autostart.py` | 自启动（搬迁） |
| `src/rdc/host/netinfo.py` | 网络信息（搬迁） |
| `src/rdc/host/power.py` | 电源管理（搬迁） |
| `src/rdc/input/__init__.py` | 子包 |
| `src/rdc/input/dpi.py` | DPI 探测（搬迁） |
| `src/rdc/input/keyboard.py` | 键盘输入（搬迁） |
| `src/rdc/input/modifiers.py` | 修饰键（搬迁） |
| `src/rdc/input/mouse.py` | 鼠标输入（搬迁） |
| `src/rdc/protocol/__init__.py` | 子包 |
| `src/rdc/protocol/framing.py` | 15B 分帧（搬迁） |
| `src/rdc/security/__init__.py` | 子包 |
| `src/rdc/security/fingerprint.py` | TOFU 指纹（搬迁） |
| `src/rdc/server/__init__.py` | 子包 |
| `src/rdc/server/pid_manager.py` | PID 文件（搬迁） |
| `src/rdc/server/service.py` | 被控端服务（搬迁 + 18 处吞错清零 + 2 helper） |
| `src/rdc/transport/__init__.py` | 子包 |
| `src/rdc/transport/control_channel.py` | 控制通道（搬迁 + 6 处吞错清零） |
| `src/rdc/transport/heartbeat.py` | 心跳（搬迁） |
| `src/rdc/transport/reconnect.py` | 重连（搬迁） |
| `src/rdc/transport/screen_channel.py` | 画面通道（搬迁 + 5 处吞错清零） |
| `tests/conftest.py` | `uninitialized_config` 夹具 |
| `tests/unit/test_framing.py` | 19 个分帧测试 |
| `tests/unit/test_ratelimit.py` | 7 个限流测试 |
| `tests/unit/test_config_migration.py` | 6 个配置迁移测试 |
| `STAGE1_REPORT.md` | 本文件 |

### 4.2 修改文件

| 文件 | 修改内容 |
|------|----------|
| `.gitignore` | 新增 6 条规则 |
| `README.md` | 重写为 v5 占位版 |
| `ASSESSMENT.md` | 新建（v4 现状评估） |
| `REFACTOR_PLAN.md` | 新建（10 阶段重构计划） |
| `PROGRESS.md` | 新建并持续更新至阶段 1 完成 |

### 4.3 搬迁到 legacy_v4/

46 个 v4 原件（含 `gui/` 全部 10 个文件、`screen/`、`network/`、`core/`、`service/`、`utils/`、`input/` 原件及死代码）。

---

## 5. 遗留问题

| # | 问题 | 严重性 | 处理计划 |
|---|------|--------|----------|
| 1 | `.pre-commit-config.yaml` 未创建 | 低 | 阶段 10 CI/CD 一并交付 |
| 2 | `service.py` 的 `from gui.tray import SystemTray` 仍用 `type: ignore` | 低 | 阶段 6 UI 重写时自然解决 |
| 3 | 线程池 `shutdown()` 异常类型未收窄 | 低 | 阶段 4 改 asyncio 时消除 |
| 4 | 覆盖率 9.5%（目标 ≥70%） | 预期 | 每阶段递增，阶段 2 目标 `config/` + `secrets/` ≥85% |
| 5 | `legacy_v4/` 保留（计划要求删除） | 低 | 阶段 6 完成后统一删除，需用户授权 |

---

## 6. 阶段 2 起点

### 阶段 2：配置与密钥（REFACTOR_PLAN §3）

**核心任务**：
1. `Settings` Pydantic v2 模型（分层子模型 Network/Video/Security/Audio/Transfer/UI + 字段校验器）
2. `migration.py`（v1→v6 完整迁移链，保留 v4 的 v1-v5 逻辑，新增 v5→v6）
3. `SecretStore` Protocol + `factory.py` 平台探测降级链
4. `dpapi.py`（提取重复的 DATA_BLOB）
5. `keychain.py`(pyobjc) + `libsecret.py`(secretstorage)
6. `fernet_fallback.py`（机器 ID + 口令 → PBKDF2/HKDF → Fernet）
7. 删除三轨制（统一为 SecretStore）
8. `logging/setup.py` 改造为 structlog
9. 测试（三平台 SecretStore mock、v1→v6 全路径迁移、覆盖率 ≥85%）

**开工前建议**：
- 重读 `src/rdc/config/_v4_config.py` 理解当前配置结构
- 设计 `Settings` 分层模型（先画数据模型图再写代码）
- `config/_v4_config.py` 重命名为 `config/migration.py` 并重写为迁移链

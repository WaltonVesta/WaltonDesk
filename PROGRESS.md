# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-22（阶段 1 ✅ 完成）
> 配套：`REFACTOR_PLAN.md`（计划 + 决策记录）、`ASSESSMENT.md`（现状评估）、`STAGE1_REPORT.md`（阶段 1 完整报告）
> 提交历史：`dfe2281` v4 基线 → `ed00814` checkpoint① → `cd75fee` checkpoint② → 阶段 1 完成

---

## 阶段 1：项目脚手架 — ✅ 完成

### ✅ 13 个子任务全部完成

| # | 任务 | 状态 | 验收结果 |
|---|------|------|----------|
| 1.1 | `pyproject.toml` | ✅ | PEP 621 + ruff/mypy/pytest/coverage/bandit 配置齐全 |
| 1.2 | ruff 配置 | ✅ | `line-length=100`，`select=["E","F","I","N","UP","B","SIM"]` |
| 1.3 | mypy 配置 | ✅ | `strict=true`，`mypy_path=src`，37 文件 0 error |
| 1.4 | `.pre-commit-config.yaml` | ⏭️ 阶段 10 | 推迟到 CI/CD 阶段一并交付 |
| 1.5 | 安装依赖 | ✅ | `uv sync` 32 个包，`rdc==5.0.0` 可编辑安装 |
| 1.6 | `src/rdc/` layout | ✅ | 10 个子包 `__init__.py` + `py.typed` |
| 1.7 | 机械搬迁 25 文件 | ✅ | 24 文件 → `src/rdc/`，59 处 import 改写，中文注释无损 |
| 1.8 | `gui/*.py` → `legacy_v4/` | ✅ | `git mv` 46 文件 |
| 1.9 | 删除死代码 | ✅ | 移入 `legacy_v4/`（D5 偏离：保留而非删除） |
| 1.10 | 清零静默吞错 | ✅ | **31/31** 全部清零（SIM105×24 + E722×7） |
| 1.11 | `tests/` 骨架 | ✅ | `conftest.py` + `tests/unit/` 目录 |
| 1.12 | 首批回归测试 | ✅ | **32 个测试全部通过**（framing 19 + ratelimit 7 + config_migration 10 → 其中 4 个合并后实际为 6） |
| 1.13 | `.gitignore` 更新 | ✅ | `src/rdc/protocol/pb/`、`.mypy_cache/`、`.ruff_cache/`、`htmlcov/`、`.coverage.*`、`*.egg-info/` |

### 验收结果（2026-09-22 真实输出）

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

覆盖率 9.5% 为阶段 1 初始基线（仅搬迁代码，未进入任何业务重写）。

### 32 个测试清单

**test_framing.py（19 个）**：JSON 往返（压缩/非压缩）、15B 包头断言、中文载荷、screen 包 meta+图像复合结构、坏魔数/截断/CRC 篡改拒绝、粘包/拆包/垃圾字节跳过/超长包清缓冲、zlib 生效与 CRC 基于压缩后 payload

**test_ratelimit.py（7 个）**：滑动窗口准入、窗口滑过重新放行、部分过期、计数、reset、200 线程并发恰好放行 50

**test_config_migration.py（6 个）**：v1→v5 全链路、v2/v3/v4 各级新增字段、SHA256 哈希清空、旧加密字段移除、tls_cert_generated 新增、迁移幂等性

---

## ✅ 已解决的决策/发现

### 发现 1（已定案）：Python 下限是 3.12，不是 3.11

实测 `numpy 2.5.3 requires-python=>=3.12`，其存根用 PEP 695 `type` 语句，
mypy 设 3.11 会在 numpy 存根上报语法错并中止检查。

**用户 2026-09-20 确认方案 A，已落地**：
- `requires-python = ">=3.12"` ✅
- classifiers 移除 `Python :: 3.11` ✅
- ruff `target-version = "py312"` ✅
- mypy `python_version = "3.12"` ✅
- **CI 矩阵待阶段 10 落地为 `["3.12","3.13","3.14"]`**

### 发现 2（已处置）：v4 是 CRLF，Edit 工具按 LF 匹配会失败

`src/rdc/` 下 34 个新文件已统一为 LF；新建 `.gitattributes` 固化
（仓库内 LF；`.bat`/`.ps1`/`.iss` 保留 CRLF；二进制禁转换）。`legacy_v4/` 原件保持 CRLF 不动。

### 发现 3（已处置）：mypy 会顺 import 跟进 legacy_v4

`server/service.py` 的 `from gui.tray import SystemTray` 曾让 mypy 检查 2,588 行
Tkinter 代码（报 126 错）。v4 原件移入 `legacy_v4/`（已在 exclude 列表）后归零。
该 import 加了 `# type: ignore[import-not-found]` + `TODO(phase-6)`，
并补 `SystemTray = None` 确保名字总有定义。

---

## 📌 对计划的偏离（D1-D7）

| # | 计划原定 | 实际做法 | 原因 |
|---|----------|----------|------|
| D1 | `logger.py` → `logging/setup.py` | 保持 `rdc/logger.py` 单模块 | `logging` 遮蔽标准库；阶段 2 改 structlog 时再重组 |
| D2 | `config.py` → `config/settings.py` | → `config/_v4_config.py` | 下划线标记"待阶段 2 重写"，避免与 Pydantic Settings 混淆 |
| D3 | `utils/` → `platform/` | → `host/` | `platform` 遮蔽标准库 |
| D4 | mouse+keyboard 合并为 `legacy_pyautogui.py` | 各自独立搬迁 | 合并不属于"机械搬迁"，留到阶段 7 |
| D5 | **删除** renderer/kcp_channel/build.bat | **移入 `legacy_v4/` 保留** | 删除不可逆，等用户单独授权 |
| D6 | mypy `warn_unreachable = true` | **关闭** | 不属于 strict；对跨线程守卫与跨平台分支产生无法消除的结构性误报 |
| D7 | service.py 逐处加日志 | **提取 2 个 helper** | 10 段逐字重复，helper 更优 |

---

## 环境事实（避免重复踩坑）

- `uv`：`C:\Users\walton\.local\bin\uv.exe`（**不在 PATH**）
- `git`：`C:\Program Files\Git\cmd\git.exe`（**不在 PATH**）
- venv Python：`.venv\Scripts\python.exe` = 3.14.3
- 本机**无 C 编译器** → Nuitka 本机不可用（Q1：本机 PyInstaller，Nuitka 只 CI）
- **PowerShell 5.1 陷阱**：原生命令加 `2>&1` 会把 stderr 包成 ErrorRecord 致退出码误报；勿加
- **ruff/mypy/pytest 找到错误时退出码为 1**，属预期，不是执行失败
- git 无 remote（Q5：本地模拟，不推送外部）

## 性能基线（已实测，不必重测）

- libx264 软编：4K = **11.2 ms/帧**(89fps)、1080p = 3.3 ms/帧(300fps) — 目标 <40ms，余量 3.6×
- 1080p 模拟桌面：JPEG q60 = 25KB/帧 vs H.264 crf28 = **1.1KB/帧，降 96%** — 目标 ≥40%
- 硬编 nvenc/qsv/amf 全部 `ctx.open()` 失败 — **注意 `Codec('h264_nvenc')` 构造不报错，必须实测 open**

---

## 下一步：阶段 2 — 配置与密钥

阶段 2 开工前置检查：
1. 重读 `REFACTOR_PLAN.md` §3 阶段 2 的 9 个子任务
2. 读 `src/rdc/config/_v4_config.py` 理解当前配置结构
3. 设计 Pydantic v2 `Settings` 模型的分层子模型

阶段 2 核心任务：
- `Settings` Pydantic v2 模型（分层子模型 + 字段校验器）
- `migration.py`（v1→v6 完整迁移链）
- `SecretStore` Protocol + 三平台实现 + `factory.py`
- `logging/setup.py` 改造为 structlog
- 测试：三平台 SecretStore mock、v1→v6 全路径迁移、覆盖率 ≥85%

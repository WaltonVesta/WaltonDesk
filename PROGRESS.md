# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-19（阶段 1 进行中，因额度耗尽暂停）
> 配套：`REFACTOR_PLAN.md`（计划 + 决策记录）、`ASSESSMENT.md`（现状评估）
> 恢复方式：用户说"继续"后，从下方【下一步】接续

---

## 阶段 1：项目脚手架 — 🟡 进行中

### ✅ 已完成

| 子步骤 | 内容 | 验收结果 |
|--------|------|----------|
| 1 | 写 `pyproject.toml` | PEP 621 元数据 + ruff/mypy/pytest/coverage/bandit 配置齐全 |
| 2 | `uv sync` | ✅ 成功装入 32 个包，`rdc==5.0.0` 可编辑安装，原有运行时依赖零移除 |
| 3a | 机械搬迁 | ✅ 24 个文件搬到 `src/rdc/`，59 处 import 改写，中文注释无损 |
| 3b | v4 原件移入 `legacy_v4/` | ✅ `git mv` 46 个文件，消除与 src/ 的代码重复 |
| 3c | **ruff 非吞错类清零** | ✅ UP035/SIM102/SIM103/B904/F841 共 15 个已修，`ruff --select` 全过 |
| 3d | **mypy strict 清零** | ✅ **433 → 0 error**（37 文件全过，无裸 `# type: ignore`，风险 R3 化解） |
| 3e | 全部 src 语法编译 | ✅ `py_compile` 0 错误 |

**质量里程碑**：
- mypy：433 → **0**（其中 337 个"缺注解"类错误由 §5 的模块级降级配置精准吸收；96 个真实类型错误逐个修复，无一处糊弄）
- ruff：221 → 19（剩余 19 个全是子步骤 4 的静默吞错）
- 工具版本实测：ruff 0.16.8 / mypy 2.3.1 / pytest 9.1.1

### 🟡 进行中：子步骤 4 — 清零静默吞错（硬性约束 6）

**起始 31 处**（SIM105×24 + E722×7）→ **已修 12 处 → 剩余 19 处**

已修文件（3 个，均已收窄异常类型 + 加日志，清理失败仍不中断但不静默）：
- ✅ `transport/control_channel.py` — 6 处（连接失败清理 / TLS 降级重建 / `_disconnect` / `close`）
- ✅ `transport/screen_channel.py` — 5 处（令牌认证失败清理 / `_disconnect` / `close`）
- ✅ `host/power.py` — 1 处（读 `/etc/os-release` 失败降级）

**剩余 19 处**（下次继续）：
- ⏳ `server/service.py` — **18 处**（`pause`/`shutdown` 里的 socket/threadpool 清理、`_accept_loop` TLS 握手失败清理、`_send_auth_response` 等，多为成对的 `try: close() except: pass`）
- ⏳ `input/dpi.py` — **1 处**（line 47，DPI 感知 API 探测降级链的最内层 `pass`；此处属"明确处理"的探测链，SIM105 是形式告警，改法：加注释说明或转 `contextlib.suppress` + 日志）

**修复模式（已确立，剩余照此办理）**：
```python
# v4 原样（违反约束 6）：
try:
    self.sock.close()
except Exception:   # 或裸 except:
    pass
# v5 改法：
try:
    self.sock.close()
except OSError:     # 收窄到真实可能的异常类型
    self.logger.debug("...关闭 socket 异常", exc_info=True)  # 记录但不中断清理
```

### ⏳ 待做（子步骤 4 完成后）

- 子步骤 5：建 `tests/` 骨架 + `conftest.py` + 首批回归测试（`test_framing.py` 分帧往返 / `test_ratelimit.py` / `test_config_migration.py`）
- 更新 `.gitignore`（加 `.mypy_cache/`、`.ruff_cache/`、`htmlcov/`、`src/rdc/protocol/pb/`）——**计划 1.13，尚未做**
- 跑阶段 1 完整验收命令并贴输出
- `git commit`（约定式：`refactor: 阶段1 迁移到 src layout + 工程化脚手架`）
- **停下等用户确认**，再进阶段 2

---

## 🔴 新发现的问题（需用户决策或知晓）

### 发现 1：numpy 2.5.3 要求 Python ≥3.12 —— 与 Q2 决策冲突

实测 `importlib.metadata`：`numpy 2.5.3 requires-python=>=3.12`。其类型存根使用 PEP 695 `type` 语句（3.12+ 语法），mypy 若设 `python_version=3.11` 会在 numpy 存根上报语法错并中止。

**已临时处置**：把 mypy `python_version` 从 3.11 调到 **3.12**（否则 mypy 跑不起来）。

**但遗留冲突**：Q2 你确认的 CI 矩阵是"3.11-3.14"，而 **3.11 实际装不上 numpy 2.5.3**。真实最低版本是 3.12。

**需你定夺**（下次继续时）：
- 方案 A：CI 矩阵改为 **3.12-3.14**，`requires-python = ">=3.12"`（推荐，与实际依赖一致）
- 方案 B：降 numpy 到 2.0.x（支持 3.11），但可能影响阶段 5 的向量化差分性能
- 我倾向 A。

### 发现 2：v4 文件是 CRLF，Edit 工具按 LF 匹配会失败

v4 原件行尾是 CRLF。搬迁脚本按字节忠实保留（正确），但导致 Edit 工具无法匹配整块文本。**已处置**：把 `src/rdc/` 下 34 个新文件统一规范为 LF，并新建 `.gitattributes` 固化（仓库内 LF，`.bat`/`.ps1`/`.iss` 保留 CRLF，二进制文件禁转换）。`legacy_v4/` 原件保持 CRLF 不动。

### 发现 3：mypy 会顺着跨包 import 跟进 legacy_v4

`server/service.py` 的 `from gui.tray import SystemTray` 让 mypy 跟进到 v4 树，把 2,588 行 Tkinter 也纳入检查（曾报 126 个错）。**已处置**：v4 原件移入 `legacy_v4/`（在 ruff/mypy 的 exclude 列表内）后，这类错误归零。该 import 加了带错误码的 `# type: ignore[import-not-found]` + `TODO(phase-6)` 标注。

---

## 📌 对计划的偏离（已做，需你知晓认可）

| # | 计划原定 | 实际做法 | 原因 |
|---|----------|----------|------|
| D1 | `logger.py` → `logging/setup.py` | 保持单模块 `rdc/logger.py` | `logging` 会遮蔽标准库；阶段 2 改 structlog 时再重组为包 |
| D2 | `config.py` → `config/settings.py` | → `config/_v4_config.py` | 下划线前缀明确标记"待阶段 2 重写"，避免与未来 Pydantic Settings 混淆 |
| D3 | `utils/` → `platform/` | → `host/` | `platform` 会遮蔽标准库 |
| D4 | `input/mouse.py`+`keyboard.py` 合并为 `legacy_pyautogui.py` | 各自独立搬迁 | 合并两文件不属于"机械搬迁"，留到阶段 7 引入 InputBackend 时重组 |
| D5 | **删除** `renderer.py`/`kcp_channel.py`/`build.bat` | **移入 `legacy_v4/` 保留** | 删除不可逆；已承诺等你单独授权再删。基线 tag 也在，双保险 |
| D6 | mypy `warn_unreachable = true` | **关闭** | 它不属于 strict（约束 7 要的是 strict）；实测对"跨线程取消守卫"和"跨平台分支"产生无法消除的结构性误报。已在 pyproject 写明理由 |

**新增模块目录**（阶段 1 已建，含说明性 `__init__.py`，均**不做 eager re-export** 以打破 v4 循环加载链）：
`config/ protocol/ transport/ capture/ encoding/ client/ server/ host/ input/ security/`

---

## 环境事实（避免重复踩坑）

- `uv` 全路径：`C:\Users\walton\.local\bin\uv.exe`（不在 PATH）
- `git` 全路径：`C:\Program Files\Git\cmd\git.exe`（不在 PATH）
- venv Python：`.venv\Scripts\python.exe` = **3.14.3**
- 本机**无 C 编译器**（cl/gcc/clang/link 全缺）→ Nuitka 本机不可用（Q1 已定：本机 PyInstaller，Nuitka 只 CI）
- **PowerShell 5.1 陷阱**：对原生命令加 `2>&1` 会把 stderr 包成 ErrorRecord 致退出码误报；stderr 已单独捕获，勿加 `2>&1`
- git 无 remote（Q5 已定：本地模拟，不推送外部）

---

## 下一步（恢复后从此处接续）

1. **续做子步骤 4**：修 `server/service.py` 剩余 18 处 + `input/dpi.py` 1 处吞错（照上方"修复模式"）
2. 每修一个文件立即 `ruff check` + `mypy` 复查，防止再引入缩进类错误（子步骤 3 曾误引入一次 `IndentationError`，已修正 —— 教训：合并嵌套 if 必须带完整块体）
3. 子步骤 4 清零后 → 建 `tests/` 骨架 + 首批回归测试
4. 更新 `.gitignore`
5. 跑阶段 1 完整验收命令，贴真实输出
6. `git commit`（约定式），**停下等确认**
7. 就【发现 1】的 Python 版本冲突请你定夺（A/B）

# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-20（阶段 1 约 90%，因额度紧张第二次 checkpoint）
> 配套：`REFACTOR_PLAN.md`（计划 + 决策记录）、`ASSESSMENT.md`（现状评估）
> 提交历史：`dfe2281` v4 基线 → `ed00814` checkpoint①(12/31) → 本次 checkpoint②

---

## 阶段 1：项目脚手架 — 🟡 约 90%

### ✅ 已完成并验证

| 子步骤 | 内容 | 验收结果 |
|--------|------|----------|
| 1 | `pyproject.toml` | PEP 621 + ruff/mypy/pytest/coverage/bandit 配置齐全 |
| 2 | `uv sync` | 32 个包，`rdc==5.0.0` 可编辑安装，原有运行时依赖零移除 |
| 3a | 机械搬迁 | 24 文件 → `src/rdc/`，59 处 import 改写，中文注释无损 |
| 3b | v4 原件移入 `legacy_v4/` | `git mv` 46 文件，消除代码重复 |
| 3c | **ruff 全过** | `All checks passed!`（起始 221 → 0） |
| 3d | **mypy strict 0 error** | `Success: no issues found in 37 source files`（起始 433 → 0，无裸 ignore） |
| 3e | 语法编译 | 全部 src `py_compile` **0 错误** |
| **4** | **清零静默吞错（约束 6）** | **31/31 全部清零**（SIM105×24 + E722×7） |

### 子步骤 4 详情（本轮完成剩余 19 处）

修复模式统一为：**收窄异常类型 + 记录日志**，清理失败仍不中断流程但不再静默。

- ✅ `transport/control_channel.py` — 6 处
- ✅ `transport/screen_channel.py` — 5 处
- ✅ `host/power.py` — 1 处（裸 except 收窄为 OSError）
- ✅ `input/dpi.py` — 1 处（DPI 感知 API 探测降级链，加 debug 日志）
- ✅ `server/service.py` — **18 处**（本轮重点）

**service.py 的结构性改进**：18 处中有 10 段是逐字重复的 `try: close() except: pass`。
新增两个私有 helper 消除重复：
- `_close_quietly(sock, context)` — 关闭监听 socket
- `_shutdown_and_close(conn, context)` — 先 `shutdown(SHUT_RDWR)` 中断对端阻塞 recv，再 close

调用点：`pause()` 6 处、`_start_server` 4 处、`_accept_loop` 1 处、
`_handle_client` finally 2 处、`_send_auth_response` 1 处、`shutdown()` 5 处。

**一处保守决策**：线程池 `shutdown()` 的异常面不确定，未收窄为 RuntimeError，
保持 `except Exception` + `logger.debug`（无测试兜底前不改变 v4 运行时行为）。

### 🟡 进行中：子步骤 5 — tests/ 骨架（已建文件，**尚未运行过**）

已创建 4 个文件：
- `tests/conftest.py` — `uninitialized_config` 夹具（用 `object.__new__` 绕过 Config 单例的
  `_initialize`，**不污染真实 `~/.remote_desktop`**）
- `tests/unit/test_framing.py` — **19 个用例**：JSON 往返（压缩/非压缩）、15B 包头断言、
  中文载荷、screen 包 meta+图像复合结构、坏魔数/截断/CRC 篡改拒绝、粘包/拆包/
  垃圾字节跳过/超长包清缓冲、zlib 生效与 CRC 基于压缩后 payload
- `tests/unit/test_ratelimit.py` — 滑动窗口准入、窗口滑过重新放行、部分过期、计数、reset、
  **200 线程并发恰好放行 50**（用 monkeypatch 控制时钟，不依赖真实 sleep）
- `tests/unit/test_config_migration.py` — v1→v5 全链路：v2/v3/v4 各级新增字段、
  SHA256 哈希清空、旧加密字段移除、tls_cert_generated 新增、**迁移幂等性**

> ⚠️ **这三个测试文件一次都没跑过**。`test_framing.py` 里对 `HEADER_SIZE`、
> `Packet.from_bytes` 返回值、`PacketBuffer.buffer` 属性名等的假设需实际运行验证。
> `test_ratelimit.py` 的 `clock` 夹具用 `monkeypatch.setattr(ratelimit.time, "time", ...)`
> —— 这会 patch **全局 time 模块**，可能影响同批其他测试；若出问题改为 patch 模块内引用。

### ⏳ 剩余待做（恢复后按序执行）

1. **跑测试**：`uv run pytest tests/unit -v` — 预期需修若干断言/夹具细节
2. 跑覆盖率：`uv run pytest tests/unit --cov=src/rdc --cov-report=html`
3. `.gitignore` 补两条（正要做时被打断）：coverage `parallel=true` 产生的
   `.coverage.*` 分片文件、`*.egg-info/`
4. 跑阶段 1 完整验收命令，贴真实输出
5. `git commit`（约定式：`refactor: 阶段1 迁移到 src layout + 工程化脚手架`）
6. **停下等用户确认**，再进阶段 2

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

## 📌 对计划的偏离（已做，待用户认可）

| # | 计划原定 | 实际做法 | 原因 |
|---|----------|----------|------|
| D1 | `logger.py` → `logging/setup.py` | 保持 `rdc/logger.py` 单模块 | `logging` 遮蔽标准库；阶段 2 改 structlog 时再重组 |
| D2 | `config.py` → `config/settings.py` | → `config/_v4_config.py` | 下划线标记"待阶段 2 重写"，避免与 Pydantic Settings 混淆 |
| D3 | `utils/` → `platform/` | → `host/` | `platform` 遮蔽标准库 |
| D4 | mouse+keyboard 合并为 `legacy_pyautogui.py` | 各自独立搬迁 | 合并不属于"机械搬迁"，留到阶段 7 |
| D5 | **删除** renderer/kcp_channel/build.bat | **移入 `legacy_v4/` 保留** | 删除不可逆，等用户单独授权 |
| D6 | mypy `warn_unreachable = true` | **关闭** | 不属于 strict；对跨线程守卫与跨平台分支产生无法消除的结构性误报（pyproject 已写明理由） |
| D7 | service.py 逐处加日志 | **提取 2 个 helper** | 10 段逐字重复，helper 更优（本轮新增偏离） |

已建包目录（`__init__.py` 均**不做 eager re-export**，打破 v4 的
`service/__init__ → daemon → gui.tray → tkinter` 加载链）：
`config/ protocol/ transport/ capture/ encoding/ client/ server/ host/ input/ security/`

---

## 环境事实（避免重复踩坑）

- `uv`：`C:\Users\walton\.local\bin\uv.exe`（**不在 PATH**）
- `git`：`C:\Program Files\Git\cmd\git.exe`（**不在 PATH**）
- venv Python：`.venv\Scripts\python.exe` = 3.14.3
- 本机**无 C 编译器**（cl/gcc/clang/link 全缺）→ Nuitka 本机不可用（Q1：本机 PyInstaller，Nuitka 只 CI）
- **PowerShell 5.1 陷阱**：原生命令加 `2>&1` 会把 stderr 包成 ErrorRecord 致退出码误报；勿加
- **ruff/mypy/pytest 找到错误时退出码为 1**，属预期，不是执行失败（曾误判为循环）
- git 无 remote（Q5：本地模拟，不推送外部）

## 性能基线（已实测，不必重测）

- libx264 软编：4K = **11.2 ms/帧**(89fps)、1080p = 3.3 ms/帧(300fps) — 目标 <40ms，余量 3.6×
- 1080p 模拟桌面：JPEG q60 = 25KB/帧 vs H.264 crf28 = **1.1KB/帧，降 96%** — 目标 ≥40%
- 硬编 nvenc/qsv/amf 全部 `ctx.open()` 失败 — **注意 `Codec('h264_nvenc')` 构造不报错，必须实测 open**

---

## 下一步（恢复后从此处接续）

**第一件事：`uv run pytest tests/unit -v`** —— 三个测试文件从未运行过，
先修通再谈覆盖率。然后按【⏳ 剩余待做】3→6 收尾阶段 1。

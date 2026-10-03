# STAGE9 进行中状态（2026-10-03 中断保护快照）

> 触发原因：上下文超长，按额度节省策略末条执行「PROGRESS + 本文件 + commit + 停下」。
> 新会话只需读本文件 + `PROGRESS.md` 阶段 9 章节即可续跑，**不必重读 11 份历史文档**。

## 一句话现状

阶段 9 批 0→4 全部完成并验收通过，**仅剩批 5（端到端 + 渗透）与收口（报告）两步**。

## 基线与 HEAD

- 任务书基线：`efd38a0`（1137 passed）。
- 当前 HEAD：**`e7b6d9c`**（工作区应干净，本文件与 `STAGE9_PLAN.md` 随收口提交一并入库）。
- 阶段 9 提交链：
  - `188479d` 批 0 bandit + pip-audit 基线扫描
  - `44c3b2b` 批 1 password_policy + banlist
  - `59b807c` 批 2 certs + audit
  - `f8ea292` 批 3 service.py 五件拆分 + asyncio 传输
  - `f2ca14e` 批 4 controller + input/pipeline 装配
  - `e7b6d9c` 批 4 收尾：v4 遗留面清理 + 拆环期协程泄漏修复

## 批 4 验收数据（最新，可直接复用）

- pytest：**1382 passed / 退出码 0**（全量）；ruff `All checks passed!`；mypy `Success: no issues found in 98 source files`。
- 覆盖率：`client/controller.py` 90.0%；input：base/coalesce/keys 100%、windows 88.3%、legacy_pyautogui 94.3%、平台空实现 70~77%；`client/ratelimit.py` 100%。
- 红线核验：controller 源码 `PySide6|QtWidgets|QtCore|QtGui` grep **零命中**。
- 遗留面清理：已删 `config/_v4_config.py`、`constants.py`、`logger.py`(shim)、`ScreenCapturer` 外壳、`Packet.create_json/to_json/create_screen/parse_screen`；删除前 `git grep` 核验零消费者；`test_framing.py` / `test_config_migration.py` 删除，其核心能力（CRC 篡改、粘包、半帧）迁至 `test_protocol_framing_v5.py`；`pyproject.toml` 的 mypy 宽松清单与 coverage omit 同步收紧扣。

## 批 4 重要经验（批 5 排查同类问题参照）

症状：测试全绿但 pytest 进程**退出码 1**，且 `test_protocol_codec.py` 两个 hypothesis 用例在全量下间歇失败（单跑通过）。

根因两条，都已修复：
1. `_start_reconnect_loop` 在解释器/GC 拆环阶段被收尾回调触发，`ensure_future` 在无运行循环时抛错并留下从未 await 的协程 → `RuntimeWarning: coroutine ... was never awaited`，被 `filterwarnings=["error"]` 计入失败。修法：函数开头 `asyncio.get_running_loop()` 守卫，`RuntimeError` 时直接 `_set_state("disconnected")` 返回。
2. 测试用例直接构造 `ClientController` 未登记 `_LIVE_CTLS`，recv/flush/heartbeat 4 个任务存活到退出期，在已关闭循环上销毁 → `closing generator` + 1 个 uncollectable。修法：构造后立即 `_LIVE_CTLS.append(ctl)`。

排查手法（有效，值得复用）：
- `gc.set_debug(gc.DEBUG_UNCOLLECTABLE)` 打出垃圾对象类型（曾据此定位到 `PySide6.QtCore.Property`）。
- 按测试类**累加前缀**二分定位泄漏组；夹具收尾打印 `asyncio.all_tasks()` 残留清单直接指认泄漏任务名。
- 注意 PowerShell 会把 pytest 的 stderr 包成 NativeCommandError，别把这类噪声当成命令失败。

## 下一步：批 5 待办（验收口径已用户确认）

1. **loopback 端到端**（同机双进程）：被控端 `rdc serve` + 主控端连入，自动判定三项——认证通过、收到 H.264 帧、发送输入事件被受理。
2. **渗透四项实测报告**：
   - 重放：复用阶段 4 三层测试，不重写；
   - MITM：TLS 指纹校验绕过尝试；
   - 弱口令爆破：password_policy + banlist 联合验证；
   - 路径穿越：对 `banlist.json`、审计日志、证书、配置四类写入点做规范化防护并实测；报告须声明「文件传输面待阶段 8 补测」。
3. **bandit 0 high + pip-audit 0 vuln** 复跑（批 0 若留有 HIGH 项，按叮嘱 a 需先汇报再决定，不要自行修）。
4. 全量覆盖率收口（总体 ≥70%、核心 ≥85%）。
5. **交付真机走查清单**给用户（STAGE7 §6 口径：鼠标落点、中文 IME、剪贴板等）。
6. commit：`test(e2e): add loopback e2e and penetration checks`。

## 收口待办

- `STAGE9_REPORT.md` 必含 7 块（叮嘱 e）：6 批完成情况 / 装配架构 / 安全加固 / 渗透清单（含补测声明）/ bandit+pip-audit 结果 / 端到端（自动 + 走查清单）/ 阶段 10 起点。
- `README.md` 状态修正（目前仍写「阶段 4」）。
- `PROGRESS.md` 阶段 9 章节标记完成。
- commit：`docs(stage9): add stage 9 report and update PROGRESS`。

## 环境要点（新会话直接用，别重新摸索）

- 项目根：`C:\Users\walton\Desktop\remote_desktop\control_v4\control_v4.0\control_v4.0.3`
- `uv` 需全路径 `C:\Users\walton\.local\bin\uv.exe`，且**必须带 `run --no-sync`** + 显式 cwd 到项目根（每条 Bash 是独立 shell）。
- GUI/Qt 测试必须 `$env:QT_QPA_PLATFORM="offscreen"`。
- shell 是 Windows PowerShell 5.1：无 `&&`/`||`，用 `;` + `if ($?)`；stderr 已由工具单独捕获，**不要写 `2>&1` 重定向原生命令**。
- Python 3.14.3 + qasync 0.28.0（官方声明 <3.14，本机四项判据实测通过，见 `scripts/poc`）；3.16 需切换保留方案乙。
- 覆盖率采集：pytest 内建 `--cov` 一次跑完最稳；`coverage combine` 会因 `[paths]` 映射对 `src\rdc` 与绝对路径不匹配报「No data to report」。

## 关键约束回顾（不可违背）

- controller **零 Qt/Widgets 导入**。
- 1350+ 基线**不回归**；ruff / mypy / pytest 三绿且**退出码 0**。
- 删除任何遗留面前，先 `git grep` 核验零消费者。
- 每批完成立即 commit，中间不产出冗余报告文件；过程回复只报批名 + 验收数字。
- 遇 HIGH 级安全问题或拆分异常：**停下汇报，不硬拆、不自行修**。

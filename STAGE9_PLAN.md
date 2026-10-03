# 阶段 9 执行方案（用户已确认，2026-10-02）

> 本文件原为空占位，2026-10-03 中断保护收口时按会话内确认方案补全，作为批 5 / 收口的验收依据。

## 范围

v4.0.3 → v5.0.0 重构的阶段 9：**安全加固 + 装配**。阶段 8（文件传输 + 音频）经用户决定延后。

任务书基线：HEAD `efd38a0`；ruff 0 / mypy 91 files / pytest 1137 passed / input 95.5% / UI 90.3% / 冷启动 375-441ms。

## A. 装配

| 任务 | 内容 | 接缝依据 |
|---|---|---|
| A1 | `client/controller.py` 主控会话状态机 | UI 侧 `ClientController`/`ClientEvents` Protocol 已定稿（`main_window.py:69-139`，含 `send_mouse`/`send_key`/`send_text`/`clipboard_*`）；传输侧 `ControlChannel.connect` / `perform_control_handshake` / `perform_screen_join` 齐备；**controller 零 Widgets 导入红线** |
| A2 | `server/service.py` 拆分（D14） | 原 1213 行 v4 线程模型（accept 线程 + ThreadPoolExecutor + 裸 4B 握手 + 同步 broadcaster B3 病灶）→ 拆 service/auth/banlist/broadcaster/audit 五件，接 `transport/server.py`（TransportServer 含 `on_control`/`on_screen` 钩子）+ qasync |
| A3 | `default_backend` 装配 | `input/__init__.py` 刻意不 eager re-export（防 v4 循环链），工厂在 `base.py`；Windows 接真实现，macOS/Linux 空实现抛错 |
| A4 | coalesce 挂 pipeline 节拍 | `ScreenPipeline.run_once()` 返回 `FrameDecision`（fps/delay_s）；`InputCoalescer.set_fps`/`poll`/`drain` 就绪（D34 只提前不推迟） |

## B. 安全加固

| 任务 | 内容 |
|---|---|
| B5 | `security/password_policy.py`：≥8 位 + 字母 + 数字校验器 + UI 提示（bcrypt 工具已有） |
| B6 | `server/banlist.py`：复用 `client/ratelimit.py` 滑动窗口；service 内联封禁迁出 + `banlist.json` 持久化 + 1000 条裁剪 |
| B7 | `security/certs.py`：用户证书加载 + 轮换 + 过期检测 API（自签名/临期续期已在 `transport/tls.py`） |
| B8 | `server/audit.py` + `security/audit_events.py`：认证 / 连接 / 控制操作三类 structlog 结构化事件 |
| B9 | MD5 清除：阶段 3 已改 SHA-256，本阶段仅确认 bandit 无 B324 即闭环 |
| B10 | 渗透清单四项：重放（复用阶段 4 三层测试）/ MITM / 弱口令爆破 / 路径穿越 |
| B11 | bandit + pip-audit + 协议模糊复跑（阶段 3 已有 2×10k 基础） |

## 分批计划与 commit message（英文，用户指定）

| 批 | 内容 | commit message |
|---|---|---|
| 批 0 | 装 bandit + pip-audit（dev/security 组入 pyproject），首轮基线扫描，产出问题清单 | `chore(security): add bandit and pip-audit with baseline scan` |
| 批 1 | B5 + B6 + B9 确认 + 对应测试（目标区覆盖率 ≥85%） | `feat(security): add password policy and banlist` |
| 批 2 | B7 + B8 + 测试 | `feat(security): add certs management and audit logging` |
| 批 3 | A2 拆分，删 v4 线程模型与同步 broadcaster（B3 根治），CLI serve 落地 | `refactor(server): split service.py and switch to asyncio transport` |
| 批 4 | A1 + A3 + A4 + CLI control / GUI 入口装配 + 遗留面清理 | `feat(client): add controller and wire input/pipeline` |
| 批 5 | loopback 端到端 + 渗透四项 + bandit/pip-audit 复跑 + 全量覆盖率收口 | `test(e2e): add loopback e2e and penetration checks` |
| 收口 | STAGE9_REPORT.md + PROGRESS + README 状态修正 | `docs(stage9): add stage 9 report and update PROGRESS` |

## 用户确认的 2 个疑点

1. **路径穿越渗透项**：阶段 8 未做，文件传输无真实落地面。处理＝对现有全部"外部输入→本地路径"写入点（`banlist.json`、审计日志、证书、配置）做穿越防护测试，报告声明"文件传输面待阶段 8 补测"，不阻塞阶段 9。
2. **端到端"跑起来"**：同机双进程 loopback 自动验证到"认证通过 + 收到 H.264 帧 + 输入事件被受理"；真机手感走查（STAGE7 §6：鼠标落点、中文 IME、剪贴板）由用户亲自体验，批 5 完成后交付走查清单。

## 用户叮嘱（a–f）要点

- **a** 批 0：`uv pip install` / `uv sync --group security`；**HIGH 级问题先汇报不直接修**（可能要决策）。
- **b** 批 3（最大风险）：拆分前"标记不删"→ 拆分后"验证零回归"→ 最后"删旧"；拆分后**真机起服务**验证；发现问题**停下汇报，不硬拆**。
- **c** 批 4：controller **零 Widgets 导入**红线；装配顺序 transport → input → pipeline → controller；删 `_v4_config` / `constants` 双份等遗留面。
- **d** 批 5：loopback 自动验证 + 渗透四项 + bandit 0 high + pip-audit 0 vuln + 全量覆盖率收口 + 交付真机走查清单。
- **e** STAGE9_REPORT.md 必含：6 批完成情况、装配架构（controller + service 拆分 + default_backend + coalesce）、安全加固（password_policy/banlist/certs/audit）、渗透清单（含"文件传输面待阶段 8 补测"声明）、bandit/pip-audit 结果、端到端验证（自动 + 走查清单）、阶段 10 起点。
- **f** commit message 按上表英文模板。

## 额度节省策略（自定，执行中遵守）

11 份文档已读完不重读；源码按需 Grep 提取片段不整读大文件；ruff+mypy+pytest 合并单条链跑；每批完成立即 commit 不产出冗余中间报告；过程回复只报批名 + 验收数字；复用阶段 4 渗透测试只补新项；额度近耗尽阈值则立即 PROGRESS + INPROGRESS + commit + 停下。

## 验收总口径

- 1350+ 测试无回归、ruff / mypy / pytest 全绿（含**退出码 0**）。
- `security/` + `server/auth` 覆盖率达标（目标区 ≥85%）。
- bandit 0 high、pip-audit 0 vuln。
- 端到端 loopback 三项判据达成。

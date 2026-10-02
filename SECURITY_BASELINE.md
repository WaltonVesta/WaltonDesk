# SECURITY_BASELINE.md — 阶段 9 批 0：安全扫描基线

> 日期：2026-10-02 · bandit 1.9.4 · pip-audit 2.10.1 · Python 3.14.3
> 命令：`uv run bandit -r src/ -c pyproject.toml` · `uv run pip-audit --progress-spinner off`
> 工具已在 pyproject `[dependency-groups] security` 声明，本批 `uv pip install` 落 venv（版本与 lock 一致）。

## 结果速览

| 工具 | High | Medium | Low | 判定 |
|------|------|--------|-----|------|
| bandit | **0** | 2 | 53 | **High 达标**（验收线 0 high）；Medium/Low 按下方计划在批 1-4 收敛 |
| pip-audit | — | — | — | **0 漏洞**（`rdc` 自身不在 PyPI 被 Skip，属预期） |

## bandit 明细与处置计划

### Medium（2）— 批 3 处置

| 位置 | 规则 | 处置 |
|------|------|------|
| `server/service.py:338,346` `bind('0.0.0.0', port)` | B104 | 被控端监听全部网卡属产品语义（远程桌面）。重写后绑定地址由 Settings 提供 + `# nosec B104` 注释说明 |

### Low（53）— 分组处置

| 组 | 数量 | 位置 | 处置 |
|----|------|------|------|
| B105 硬编码"密码"串 | 19 | `_v4_config.py`×17 / `config/migration.py`×1 / `secrets/base.py`×1 | 均为字段名/存储前缀常量/清空赋值，**非凭据**。`_v4_config.py` 批 4 删除自然消除 17 条；`migration.py:59`（v6 清空旧哈希）与 `secrets/base.py:18`（键名常量）加 `# nosec B105` + 理由 |
| B404/B603/B607 subprocess | 31 | `host/autostart.py`×10 / `host/power.py`×21 | argv 全部为**固定列表**（不经 shell），目标为系统管理命令（launchctl/systemctl/pmset/shutdown），无外部输入注入面。`# nosec` + 注释；power 的关机/重启属产品功能，UI 调用点二次确认兜底 |
| B101 assert | 2 | `encoding/h264.py:213` / `ui/toolbar.py:67` | 内部不变量断言（非外部输入校验），`# nosec B101` 保留断言 |
| B112 try/except/continue | 1 | `host/netinfo.py:45` | 多网关轮询探测；批 4 顺带收窄异常类型（`OSError`）后加 `# nosec B112` |

### 已确认项

- **B324（MD5）**：src/ 全树已无 `hashlib.md5` 调用点（阶段 3 起 `create_screen` 改 SHA-256，pyproject `skips=[]`）——任务书第 9 项只需回归确认，批 5 终扫复验 ✅
- 协议模糊：阶段 3 已有 2×10,000 例模糊测试（`test_protocol_framing_fuzz.py`），批 5 复跑即闭环。

## 收尾判定口径

- 验收硬线：`bandit -r src/` **0 high**（基线已满足，终扫须保持）+ `pip-audit` **0 vuln**。
- Medium/Low 目标：批 3/4 删除与 nosec 收敛后**全部清零**（误报留注释证据，非静默跳过）。

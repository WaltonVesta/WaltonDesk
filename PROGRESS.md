# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-22（阶段 2 ✅ 完成）
> 配套：`REFACTOR_PLAN.md`（计划）、`ASSESSMENT.md`（现状评估）、`STAGE1_REPORT.md` / `STAGE2_REPORT.md`（阶段报告）
> 提交历史：`dfe2281` v4 基线 → `5082762` 阶段 1 完成 → 本次阶段 2 完成

---

## ⚡ 范围重大调整（2026-09-22，用户决定，后续阶段持续生效）

**砍掉全部"v4 客户端兼容"要求**（理由：v1-v4 从未稳定可用、无存量用户、纯个人自用；
"兼容从未存在过的用户"是伪需求）：

- 阶段 2：不做三轨制存量迁移（`migrate_stored_secrets` 方案作废），旧密码/旧配置字段
  在 v5→v6 迁移时**直接清空**；`Config` 兼容门面**不做**，`_v4_config.py` 原样保留
  供旧代码调用、**不再扩展**，新代码一律用 `Settings`。
- 阶段 3：**不写 `legacy_v4.py`**，协议双版本降级为 v5 单版本（Protobuf + HMAC），
  15B 分帧头保留（v5 自己用）。
- 相关决策 Q3（v4 JPEG 差分双轨）、Q4（兼容保留期）失去存在前提 → 一并作废：
  画面管线只留 v5 全帧 H.264 + JPEG 编码回退（同一套 v5 协议内），无 v4 协商。
- REFACTOR_PLAN.md 原文**不改**（保留决策历史），本备注为其效力覆盖。

---

## 阶段 1：项目脚手架 — ✅ 完成（详见 STAGE1_REPORT.md）

## 阶段 2：配置与密钥 — ✅ 完成（详见 STAGE2_REPORT.md）

### 子任务状态（对照 REFACTOR_PLAN §3，含范围调整）

| # | 任务 | 状态 |
|---|------|------|
| 2.1 | `config/settings.py` Pydantic v2 模型（扁平 v6 schema）+ `config/paths.py` | ✅ |
| 2.2 | `config/migration.py` v1→v6 迁移链（纯函数+注册表；v6 清空旧密码） | ✅ |
| 2.3 | `secrets/base.py` Protocol + `secrets/factory.py` 探测降级链 | ✅ |
| 2.4 | `secrets/dpapi.py`（DATA_BLOB 单一实现，本机真机往返验证） | ✅ |
| 2.5 | `secrets/keychain.py` + `secrets/libsecret.py`（mock 契约测试） | ✅ |
| 2.6 | `secrets/fernet_fallback.py`（PBKDF2 480k + 机器绑定） | ✅ |
| 2.7 | ~~三轨制存量迁移~~ → 范围调整后简化为"清空 + bcrypt 工具分离"（`secrets/password.py`） | ✅ |
| 2.8 | structlog 改造（JSON+console 双 renderer，轮转保留，D1 落地） | ✅ |
| 2.9 | 测试 131 个新增（全套见 STAGE2_REPORT） | ✅ |

### 验收结果（2026-09-22 真实输出）

```
$ uv run ruff check src tests
All checks passed!

$ uv run mypy src
Success: no issues found in 50 source files

$ uv run pytest tests/unit -q
163 passed in 2.64s

$ uv run pytest --cov=src/rdc/config --cov=src/rdc/secrets
config/ + secrets/ TOTAL: 93.9%（验收线 ≥85%）

$ uv run python -m rdc --version
远程桌面控制 v5.0.0
```

全局覆盖率：阶段 1 9.5% → 阶段 2 **24.9%**。

---

## ✅ 已解决的决策/发现（阶段 1 存量，保持有效）

- **发现 1**：Python 下限 3.12（numpy 2.5 存根），CI 矩阵 ["3.12","3.13","3.14"]
- **发现 2**：CRLF/LF — `.gitattributes` 已固化，仓库 LF
- **发现 3**：mypy 顺 import 跟进 legacy_v4 — exclude 已处理
- **偏离 D1-D7**：见 STAGE1_REPORT.md §3.2；其中 D1（logging 包重组）已在阶段 2 落地

## 📌 阶段 2 新增偏离/决定

| # | 内容 |
|---|------|
| D8 | Settings 用**扁平 v6 schema**（非计划的嵌套分组）：与 Config 共读写同一 config.json，避免双 schema 互相覆盖；嵌套分组留给阶段 4/6 调用点全切后引入 |
| D9 | 旧 `rdc/logger.py` 保留为 re-export shim（24 处 v4 调用点零改动），阶段 4/6 逐步删除 |
| D10 | v6 迁移**直接清空** password_hash/saved_password（用户指令，替代三轨迁移） |

## pyproject 维护

- mypy 宽松清单移除 `rdc.logger`（已是 shim）、新增 `rdc.config._v4_config` 保留（遗留层）
- coverage omit 增加 `src/rdc/config/_v4_config.py`（TODO(phase-4/6) 删除时移除 omit）

---

## 环境事实（不变，见阶段 1 记录）

- uv / git 不在 PATH（全路径调用）；PowerShell 5.1 勿加 `2>&1`；lint 工具退出码 1 属预期
- 本机无 C 编译器；git 无 remote

## 性能基线（不变）

- libx264 4K=11.2ms/帧、1080p=3.3ms/帧；桌面场景 H.264 比 JPEG 降 96%
- 硬编全部不可用（探测必须实测 ctx.open()）

---

## 下一步：阶段 3 — 协议层（范围调整后）

| # | 任务 | 说明 |
|---|------|------|
| 3.1 | `proto/rdc.proto` | 8 个 message + PayloadType/StreamCmd 枚举 |
| 3.2 | `scripts/gen_proto.py` | grpcio-tools 生成到 `protocol/pb/`（已 gitignore） |
| 3.3 | `protocol/messages.py` | 领域 dataclass（wire 无关） |
| 3.4 | `protocol/codec.py` | v5 Protobuf 编解码（~~双版本分派~~ 已砍） |
| 3.5 | `protocol/framing.py` 扩展 | Version 字段启用（v5=2）、`PKT_FLAG_HMAC=0x08` |
| 3.6 | `protocol/hmac_integrity.py` | HMAC-SHA256 + TLS exporter 派生密钥 |
| ~~3.7~~ | ~~legacy_v4.py~~ | **砍除**（范围调整） |
| 3.8 | hypothesis property-based 往返测试 | |
| 3.9 | 分帧模糊测试（10,000 例不崩溃） | |
| ~~3.10~~ | ~~v4 golden bytes~~ | **降级**为 v5 自身的字节级快照测试（防序列化回归，非兼容目的） |

**风险 R1/R2（v4 握手陷阱/配置错位地雷）随范围调整直接消失。**
开工前置：`uv sync --group proto`（grpcio-tools 已在锁文件，确认 venv 内可用）。

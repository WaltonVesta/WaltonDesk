# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-23（阶段 3 ✅ 完成）
> 配套：`REFACTOR_PLAN.md`（计划）、`ASSESSMENT.md`（现状评估）、`STAGE1_REPORT.md` / `STAGE2_REPORT.md` / `STAGE3_REPORT.md`（阶段报告）
> 提交历史：`dfe2281` v4 基线 → `5082762` 阶段 1 → 阶段 2 → 本次阶段 3

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

## 阶段 3：协议层（简化版）— ✅ 完成（详见 STAGE3_REPORT.md）

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
- 阶段 3 起：grpcio-tools 1.84.0 / protobuf 7.36.2 已装（`uv sync --group proto`）

## 性能基线（不变）

- libx264 4K=11.2ms/帧、1080p=3.3ms/帧；桌面场景 H.264 比 JPEG 降 96%
- 硬编全部不可用（探测必须实测 ctx.open()）

---

## 阶段 3：协议层（简化版）— ✅ 完成（详见 STAGE3_REPORT.md）

### 子任务状态（对照任务书 §五）

| # | 任务 | 状态 |
|---|------|------|
| 3.1 | `proto/rdc.proto`：8 message + 8 枚举 | ✅ |
| 3.2 | `scripts/gen_proto.py` → `protocol/pb/`（含 .pyi 存根，已 gitignore） | ✅ |
| 3.3 | `protocol/messages.py`：8 frozen dataclass（构造期语义校验）+ 7 IntEnum | ✅ |
| 3.4 | `protocol/codec.py`：v5 Protobuf 单路径门面（错误统一 ProtocolError） | ✅ |
| 3.5 | `protocol/framing.py` v5 重写：Version=2 启用、HMAC=0x08、**PRE_COMPRESSED=0x10 显式跳过 zlib**（互斥校验） | ✅ |
| 3.6 | `protocol/hmac_integrity.py`：HMAC-SHA256 + TLS exporter（Protocol 适配器，⚠️F1）+ HKDF 兜底 | ✅ |
| ~~3.7~~ | ~~legacy_v4.py~~ | ❌ 砍除（范围调整） |
| 3.8 | hypothesis 往返（8 类全覆盖 + 边界 + 枚举穷举） | ✅ |
| 3.9 | 模糊 2×10,000 例不崩溃 + 恶意长度分配前拒绝（叮嘱 c） | ✅ |
| 3.10 | v5 字节快照 ×13（替代 v4 golden-bytes） | ✅ |

### 验收结果（2026-09-23 真实输出）

```
ruff:   All checks passed!
mypy:   Success: no issues found in 54 source files
pytest: 299 passed in 10.41s（新增 136）
protocol/ 覆盖率: 99.8%（验收线 ≥85%）
全局覆盖率: 33.6%（阶段 2 为 24.9%）
python -m rdc --version → 远程桌面控制 v5.0.0
```

### 📌 阶段 3 新增发现/决定

| # | 内容 |
|---|------|
| **F1** | **`ssl.SSLSocket.export_keying_material` 在 Python 中不存在**（实测 3.14.3：SSLSocket/SSLObject/SSLSession 全无；CPython 未暴露 OpenSSL `SSL_export_keying_material`）。阶段 3 按 Protocol 适配器落地；**真实 TLS 栈选型为阶段 4 前置决策**：A=pyOpenSSL Connection（RFC 5705，自 17.4.0，Apache-2.0）/ B=TLS 通道内 nonce + HKDF（零依赖）。有测试锁定 stdlib 缺失事实，API 出现时提醒复审 |
| D11 | `PKT_TYPE_KEY_EVENT=0x12`：Protobuf 按包头 Type 静态分派，鼠标/键盘拆型 |
| D12 | ScreenFrame 恒置 PRE_COMPRESSED（载荷语义即压缩字节）；zlib 仅用于非画面大载荷显式 compress=True |
| D13 | codec→pb 枚举字段用 `cast` 弥合存根要求（运行时 protobuf 接受等值 int，零成本） |
| — | MD5 清零（create_screen→SHA-256），bandit B324 跳过项移除；mypy 宽松清单兑现 TODO(phase-3) 移除 framing/constants/exceptions；README"v4 向后兼容"改写为 v5 不兼容声明；R1/R2 风险随 v4 兼容砍除正式解除 |

---

## 下一步：阶段 4 — 传输层（asyncio + TLS）

1. **先决策 F1**（exporter 路径 A/B）→ `transport/tls.py`（SSLContext 强制 TLS1.2+、TOFU、exporter 接入）
2. `transport/base.py` Channel Protocol → control/screen channel asyncio 重写
3. **v5 握手改造**：AuthRequest/AuthResponse 即普通帧（协议自描述）；会话建立后 `require_hmac=True` 强制完整性
4. `session_token.py` + `backpressure.py` + 修复 B3（head-of-line blocking）+ heartbeat/reconnect asyncio 化
5. 调用点从 `framing.create_json/create_screen` 切 `codec.encode_frame`，删 v4 遗留面
6. 验收：双进程集成全绿 · 100 并发无泄漏 · 令牌重放必失败 · transport/ ≥85%

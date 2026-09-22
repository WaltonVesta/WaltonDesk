# STAGE2_REPORT.md — 阶段 2：配置与密钥

> 完成日期：2026-09-22
> 前置：阶段 1 完成 @ `5082762`
> ⚠️ 本阶段执行中收到**范围重大调整**（见 §0），验收标准随之更新。

---

## 0. 范围调整（用户 2026-09-22 决定，持续生效到阶段 10）

项目实际状态：v1-v4 从未稳定可用、纯个人自用、无存量用户。
**"v4 客户端兼容"判定为伪需求，全部砍除。** 对本阶段的影响：

| REFACTOR_PLAN 原计划 | 实际执行 |
|---------------------|----------|
| 2.7 三轨制存量迁移（读旧值→写新→校验→删，失败保留） | **作废**。`migrate_stored_secrets()` 不实现；v5→v6 迁移直接清空 `password_hash`/`saved_password`（migration `_to_v6`），用户重设 |
| Config 兼容门面（原计划含在我的执行方案里） | **作废**。`_v4_config.py` 原样保留供旧代码调用，**一字不改、不再扩展**；新代码一律 `Settings` |
| 阶段 3 legacy_v4.py / 协议双版本 | **一并作废**（前瞻记录）：阶段 3 只做 v5 协议（Protobuf + HMAC + 15B 分帧），风险 R1/R2 消失 |
| Q3 v4 JPEG 双轨、Q4 兼容保留期 | **失去前提，作废**：画面管线单轨（全帧 H.264 + JPEG 编码回退，同一 v5 协议内） |

---

## 1. 子任务完成状态（对照 REFACTOR_PLAN §3 阶段 2，2.1-2.9）

| # | 任务 | 计划 | 实际 | 说明 |
|---|------|------|------|------|
| 2.1 | `Settings` Pydantic v2 模型 + 字段校验器 | 6h | ✅ | 扁平 v6 schema（D8），校验器完整迁移 v4 `_validate` 的端口/画质/帧率/连接数规则；`paths.py` 一并交付（收敛 3 处重复路径拼接） |
| 2.2 | `migration.py` v1→v6 迁移链 | 6h | ✅ | 纯函数+注册表；v1→v5 逻辑与 v4 逐条等价；**新增 v5→v6 按 §0 直接清空旧密码** |
| 2.3 | `SecretStore` Protocol + `factory.py` | 2h | ✅ | `runtime_checkable` Protocol；工厂带 `degraded/reason`，降级不静默 |
| 2.4 | `dpapi.py`（DATA_BLOB 单一实现） | 3h | ✅ | v4 两份逐字重复结构体收敛为 `_DataBlob`+`_crypt_call`；熵锁死 v4 原值 |
| 2.5 | `keychain.py` + `libsecret.py` | 4h | ✅ | pyobjc/secretstorage 延迟导入；mock 契约测试镜像真实调用面（§4） |
| 2.6 | `fernet_fallback.py` | 3h | ✅ | 机器 ID(uuid.getnode) + salt(落盘) + 口令 → PBKDF2-HMAC-SHA256(480k) → Fernet；解密失败抛错不返回空值 |
| 2.7 | 删除三轨制 | 4h | ✅**改** | 按 §0：v6 迁移清空字段 + `password.py`（bcrypt rounds=12 纯函数，损坏哈希抛错拒绝认证） |
| 2.8 | logging 改 structlog（D1 落地） | 3h | ✅ | `rdc/logging/` 包：JSON 文件 + console 双 renderer、RotatingFileHandler 保留、emoji 清理保留、`rdc.` 命名空间；`logger.py` 成 4 行 shim（D9），**24 处 v4 调用点零改动** |
| 2.9 | 测试 | 5h | ✅ | **新增 131 个测试**（163 总-32 存量），清单见 §5 |

**9/9 完成（2.7 按调整后口径）。**

---

## 2. 验收结果（2026-09-22 真实输出）

```
$ uv run ruff check src tests
All checks passed!

$ uv run mypy src
Success: no issues found in 50 source files

$ uv run pytest tests/unit -q
163 passed in 2.64s

$ uv run python -m rdc --version
远程桌面控制 v5.0.0
```

### 覆盖率（验收线：config/ + secrets/ ≥85%）

```
name                            Stmts   Miss  Branch  Cover
src\rdc\config\migration.py        53      0      14  100.0%
src\rdc\config\paths.py            26      1       4   96.7%
src\rdc\config\settings.py        193     19      36   90.4%
src\rdc\secrets\base.py            12      0       0  100.0%
src\rdc\secrets\dpapi.py           95     10      14   90.8%
src\rdc\secrets\factory.py         37      0      10  100.0%
src\rdc\secrets\fernet_fallback.py 81      3      10   95.6%
src\rdc\secrets\keychain.py        69      3      14   94.0%
src\rdc\secrets\libsecret.py       68      5       8   93.4%
src\rdc\secrets\password.py        16      0       4  100.0%
（logging/setup.py 93.9%，不计入验收线但同过 85%）
-----------------------------------------------------------
config/ + secrets/ TOTAL          659     41     114   93.9%   ✅ ≥85%
全局 TOTAL (src/rdc)             3390   2504     776   24.9%（阶段 1 为 9.5%）
```

### 三平台 SecretStore 测试方式与覆盖率

| 平台后端 | 测试方式 | 分支覆盖率 | 真机验证 |
|----------|----------|-----------|----------|
| dpapi（Windows） | **本机真实 crypt32 往返** + 文件 vault + 损坏/非平台分支 | 90.8% | ✅ 本机 |
| keychain（macOS） | FakeSecurity 内存替身，镜像 pyobjc 元组返回契约 | 94.0% | ⏳ CI mac runner |
| libsecret（Linux） | FakeSecretStorage 替身，镜像 secretstorage 调用链 | 93.4% | ⏳ CI ubuntu runner |
| fernet（跨平台兜底） | 真实 cryptography 往返 + 错误口令/损坏 vault | 95.6% | ✅ 本机 |

---

## 3. 三个关键决策点（最终确认）

| # | 决策 | 结果 |
|---|------|------|
| 1 | 调用面策略 | **原方案作废（§0）**：不做 Config 门面，`_v4_config.py` 冻结保留，新旧并行，阶段 4/6 逐个替换调用点后删除 |
| 2 | DPAPI 熵字符串 | ✅ 保持 v4 原值 `b"RemoteDesktop-Control-v3"`，并有测试 `test_dpapi_entropy_matches_v4_literal` 锁死（历史怪值不得"顺手修正"） |
| 3 | v5→v6 与依赖 | ✅ 版本 6：删 `saved_password`、清 `password_hash`、留 `remember_password`（UI 偏好）；零新增依赖（pydantic/cryptography/bcrypt 均已在装） |

---

## 4. mock 面真实性（叮嘱 e 的落实）

`FakeSecurity` 与真实 pyobjc 的契约锁定：
1. **带出参的 C 函数在 pyobjc 返回 `(status, *outs)` 元组而非标量** —— 实现用
   `_status()` 兼容双形态，mock 用元组形态，并有专门测试类
   `TestRuntimeTupleShape` 锁定返回结构（阶段 1 曾按标量假设写过实现，本次发现并重写）；
2. Find 返回 4 元组 `(status, passwordLength, passwordData(NSData), item)`；
3. `errSecItemNotFound = -25300` 视为"无此条目"返回 None，其余非零 status 一律抛错；
4. save 先删后加（真机重复条目报 errSecDuplicateItem=-25299，mock 同样拒绝）。

`FakeSecretStorage` 镜像 `dbus_init → get_default_collection → is_locked/unlock →
create_item(attrs, secret, replace=True) / search_items → get_secret/delete` 全链，
并测锁定、解锁失败、dbus IO 失败等环境分支。

**残余风险**：mock 面若与真机 pyobjc/secretstorage 版本行为有出入，CI 首跑会暴露；
缓解：调用面极简（各 4 个函数）且已按官方文档逐参数核对。

---

## 5. 测试清单（新增 131 个）

| 文件 | 数量 | 要点 |
|------|------|------|
| `test_config_migration_v1_to_v6.py` | 13 | 全链/逐级/v6 清空/幂等/未来版本拒绝/输入不可变 |
| `test_config_settings_roundtrip.py` | 24 | 校验器边界、修复模式、原子写无残留、损坏备份、v4 文件实测迁移、单例、paths |
| `test_secrets_password_bcrypt_roundtrip.py` | 7 | 往返/错误口令/损坏哈希拒绝/rounds=12 |
| `test_secrets_fernet_derive_and_vault.py` | 14 | 派生确定性×3 维度、salt 复用、错误口令抛错、vault 损坏 |
| `test_secrets_dpapi_roundtrip.py` | 16 | 真实 crypt32（含中文+emoji）、密文随机性、熵锁、base64 损坏 |
| `test_secrets_keychain_mock_contract.py` | 20 | §4 契约 + 状态分支 + 多键隔离 |
| `test_secrets_libsecret_mock_contract.py` | 18 | 调用链契约 + 锁定/失败包装 |
| `test_secrets_factory_degradation.py` | 12 | 候选顺序×3 平台、降级 reason、Fernet 口令透传（回归）、frozen |
| `test_logging_structlog_setup.py` | 12 | JSON 落盘/命名空间/traceback/shim/幂等/emoji |

命名遵循 `test_<module>_<function>_<scenario>`（叮嘱 b）。

---

## 6. 新增/修改文件清单

**新增源码（11）**：`config/{paths,migration,settings}.py`、`secrets/{__init__,base,dpapi,keychain,libsecret,fernet_fallback,factory,password}.py`（共 12 个文件，`secrets/__init__` 计源码）、`logging/{__init__,setup}.py`

**新增测试（9）**：见 §5。`conftest.py` 增 `tmp_app_dir`/`config_file`/session 级 `isolate_logging` 夹具。

**修改（4）**：`exceptions.py`（+`SecretStoreError`）、`logger.py`（→shim）、`pyproject.toml`（mypy 清单移除 `rdc.logger`；coverage omit `config/_v4_config.py`，标 TODO(phase-4/6)）、`PROGRESS.md`。

**未动（刻意）**：`_v4_config.py`、`server/service.py`、`transport/control_channel.py`、`encoding/jpeg.py`（旧调用面零改动）。

---

## 7. 遗留问题

| # | 问题 | 处理 |
|---|------|------|
| 1 | `migrate()` 不校验"字段缺失"只补默认（与 v4 语义一致，Pydantic 层兜底） | 无需处理 |
| 2 | keychain/libsecret 的 CI 真机行为未验证（R6 固有） | 阶段 10 CI 首跑验证 |
| 3 | `logger.py` shim + `_StdlibLoggerAdapter` 是过渡层 | TODO(phase-4/6) 调用点切换后删除 |
| 4 | `NetworkConfig/VideoConfig/SecurityConfig` 嵌套分组未建（D8 决策） | 阶段 4/6 全切 Settings 后引入 |
| 5 | 全局覆盖率 24.9%（目标 70%） | 预期内，随阶段递增 |
| 6 | `remember_password=True` 但 SecretStore 无值时（用户清空口令）返回 None | 调用方（阶段 6 UI）需处理重输 |

---

## 8. 阶段 3 起点（按 §0 调整后）

1. `uv sync --group proto` 确认 grpcio-tools 在 venv（锁文件已有 1.84.0）
2. `proto/rdc.proto`：8 message（Auth/AuthResponse/ScreenFrame/Mouse/Key/Clipboard/StreamControl/Heartbeat）+ PayloadType/StreamCmd 枚举 —— 先写 `.proto` 再生成
3. `scripts/gen_proto.py` → `src/rdc/protocol/pb/`（gitignore 已备）
4. `protocol/messages.py`（dataclass）→ `protocol/codec.py`（v5 单路径，无 JSON 分派）
5. `framing.py` 扩展：Version 真启用（v5=2）、新增 `PKT_FLAG_HMAC=0x08`、对已压缩载荷跳过 zlib（ASSESSMENT §5.3 热点）
6. `hmac_integrity.py`：TLS exporter 派生密钥 + HMAC-SHA256
7. 测试：hypothesis 往返、10k 例分帧模糊、v5 字节级快照（替代原 v4 golden-bytes）
8. **不再需要**：`legacy_v4.py`、双版本 codec 分派、握手探测逻辑、R1/R2 风险应对

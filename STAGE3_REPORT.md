# STAGE3_REPORT.md — 阶段 3：协议层（简化版，Protobuf + HMAC）

> 完成日期：2026-09-23
> 前置：阶段 2 完成 @ `83f2b6e`
> 范围：按 2026-09-22 用户决定，v4 兼容全部砍除（见 §0）；任务书 §五 简化版口径执行。

---

## 0. 范围与"明确不做"清单

**明确不做（v4 兼容相关，全部作废）**：

| 原计划项 | 处置 |
|---|---|
| 3.7 `legacy_v4.py`（裸 4B 握手复现 + JSON 字段全量复刻） | **不做**（范围调整） |
| 协议双版本分派（codec 按版本选 JSON/PB） | **不做**，v5 单路径 Protobuf |
| v4 golden-bytes 测试（用 v4 原代码生成期望字节） | **不做**，降级为 3.10 v5 自身字节快照（防序列化回归，见 §2） |
| REFACTOR_PLAN §4.3 配置错位地雷的协议自描述探测 | **不做**，v5 握手统一走分帧协议（AuthRequest 即普通帧），两端配置错位问题从结构上消失 |
| Q3 v4 JPEG 差分双轨 / Q4 兼容保留期 | 失去前提，作废（画面管线单轨在阶段 5 落地） |

**R1 / R2 风险解除说明**：

- **R1（v4 兼容层出错，致命）**：兼容层本体已删除 → 风险不复存在。
- **R2（v4 客户端 enable_auth 配置错位导致字节流错位，致命）**：v5 不存在"握手是否发生取决于本地配置"的双形态——认证请求本身就是一个 `PKT_TYPE_AUTH` 帧，与业务数据共用同一分帧/校验/重同步路径 → 风险从协议结构上消除。
- 连带：`MAX_PACKET_SIZE=32MB` 超长包**清空整个缓冲区**的 v4 行为在 v5 保留（超限即流不可信），但恶意长度防护加强为"分配前拒绝"（10k 模糊 + 参数化用例锁定，见 §4）。

---

## 1. 子任务完成状态（对照任务书 §五 / PROGRESS 3.1-3.10）

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| 3.1 | `proto/rdc.proto` | ✅ | 8 个 message（AuthRequest/AuthResponse/ScreenFrame/MouseEvent/KeyEvent/ClipboardEvent/StreamControl/Heartbeat）+ PayloadType/StreamCmd/MouseAction/MouseButton/KeyAction/ModifierKey/Codec/HeartbeatKind 枚举；enum 0 值一律 `*_UNSPECIFIED`（proto3 规范） |
| 3.2 | `scripts/gen_proto.py` | ✅ | 调 `grpc_tools.protoc`（免装系统 protoc）生成 `pb/rdc_pb2.py` + **`.pyi` 存根**（使 codec.py 走 mypy strict 无 Any）；生成后自检 import；pb/ 已 gitignore |
| 3.3 | `protocol/messages.py` | ✅ | 8 个 frozen dataclass + 7 个 IntEnum；构造期语义校验（尺寸/按键/分块/修饰键组合非法即抛 `ProtocolError`）；枚举数值与 proto enum 逐一对齐并有锁定测试 |
| 3.4 | `protocol/codec.py` | ✅ | 用户确认口径：领域 dataclass 为上层唯一接口，pb 生成类只出现在 codec 内部；`encode_packet/decode_packet/encode_frame/decode_frame/to_proto/from_payload` 门面；错误契约统一 `ProtocolError`（未知 Type/protobuf 损坏/枚举越界/语义非法），无静默 `None` |
| 3.5 | `protocol/framing.py` 扩展 | ✅ | Version 真启用（恒发 2，接收端**拒绝**其他版本，含 PacketBuffer 层）；`PKT_FLAG_HMAC=0x08`；**叮嘱 a 落实**：跳过 zlib 用显式 `PKT_FLAG_PRE_COMPRESSED=0x10`（不猜测载荷内容），且与 COMPRESSED 互斥（构造与解析双侧校验）；0x02 ENCRYPTED 保留为废弃忽略位 |
| 3.6 | `protocol/hmac_integrity.py` | ✅ | HMAC-SHA256 覆盖**头部+线上载荷**（补 CRC32 保护不到的头部字段）；TLS exporter 派生（Protocol 适配器，⚠️ 关键发现见 §3）+ HKDF 兜底；恒时比较；`HmacVerificationError(ProtocolError)` |
| 3.7 | ~~legacy_v4.py~~ | ❌ 不做 | §0 范围调整 |
| 3.8 | hypothesis 往返 | ✅ | 9 个消息策略（8 类全覆盖 + CONFIG 变体）× 200 例 Packet/帧双路径往返；边界显式用例：空字符串、128KB 二进制帧、8 万字文本、NUL/emoji/组合字符、int32/uint32 极值、全枚举穷举（叮嘱 b：语义往返，非字节级假设） |
| 3.9 | 分帧模糊 | ✅ | 2×10,000 例固定种子随机字节（含 4GB 声明、随机切分、粘垃圾）+ hypothesis 1,300 例随机头部；**叮嘱 c 落实**：恶意长度分配前拒绝、不 OOM、不死循环、半魔数保留重同步 |
| 3.10 | v5 字节快照 | ✅ | 7 个 proto 载荷快照 + 6 个完整帧快照（真实 protoc 输出逐字节锁定，防未来改 proto 字段号/序列化库无感回归）；非 v4 兼容目的 |

**9/9 完成（3.7 按计划砍除）。**

### 附带完成（不在任务书但被范围调整直接解锁）

- `protocol/constants.py`：wire 层常量拆分独立严格模块（Version=2、HMAC=0x08、PRE_COMPRESSED=0x10、HMAC_SIZE、TLS_EXPORTER_CONTEXT、`PKT_FLAG_UNKNOWN_STRICT_MASK`）
- mypy 宽松清单兑现 `TODO(phase-3)`：移除 `rdc.protocol.framing`、`rdc.constants`、`rdc.exceptions`（阶段 4/5 再移除 `rdc.constants` 残留根级常量本体）
- **MD5 清零**：`create_screen` 校验字段改 SHA-256（`usedforsecurity=False`），bandit `B324` 跳过项移除（提前兑现 TODO(phase-9) 该条）
- `HmacVerificationError` 加入 `exceptions.py`
- 修复 v4 存量 bug：`PacketBuffer` 半截魔数到达时清空整个缓冲区丢帧（v5 保留可能前缀）；新增未知 Type `PKT_TYPE_KEY_EVENT=0x12`（v4 鼠标/键盘共用 CONTROL 无法静态分派）

---

## 2. 验收结果（2026-09-23 真实输出）

```
$ uv run ruff check src tests scripts
All checks passed!

$ uv run mypy src
Success: no issues found in 54 source files

$ uv run pytest tests/unit -q
299 passed in 10.41s        # 阶段 2 为 163 → 本阶段新增 136

$ uv run python -m rdc --version
远程桌面控制 v5.0.0
```

### protocol/ 覆盖率（验收线 ≥85%）

```
name                            Stmts   Miss Branch BrPart  Cover
----------------------------------------------------------------
src\rdc\protocol\__init__.py        0      0      0      0  100.0%
src\rdc\protocol\codec.py         112      0     40      0  100.0%
src\rdc\protocol\constants.py      28      0      0      0  100.0%
src\rdc\protocol\framing.py       174      0     64      1   99.6%
src\rdc\protocol\hmac_integrity.py  34      0     12      0  100.0%
src\rdc\protocol\messages.py      158      0     18      0  100.0%
----------------------------------------------------------------
TOTAL protocol/                   506      0    134      1   99.8%   ✅ ≥85%
全局 TOTAL (src/rdc)             3779   2478    876      7   33.6%（阶段 2 为 24.9%）
```

唯一部分分支：`framing.py 119->124`（zlib 失败降级后"flags 含 HMAC 且密钥为 None"的组合——已由 `test_flags_hmac_without_key_raises_on_encode` 覆盖该分支自身）。

### 验收标准逐项

- ✅ hypothesis 往返 100% 通过（9 策略 × 200 例，Packet 与帧双路径）
- ✅ 模糊 10,000 例不崩溃（实际 2×10,000 固定种子 + 1,300 hypothesis 随机头部）
- ✅ HMAC 篡改必被检测（载荷/头部 Type/头部 Length/错误密钥/截断 trailer 五路全部有拒收测试；`require_hmac=True` 封堵剥标志降级攻击）
- ✅ protocol/ 覆盖率 99.8% ≥85%
- ✅ ruff / mypy / pytest 全绿

---

## 3. 关键决策与发现

| # | 内容 |
|---|------|
| **F1（阶段 4 阻塞性发现）** | **任务书引用的 `ssl.SSLSocket.export_keying_material` 在 Python 中不存在**。实测本机 Python 3.14.3：`ssl.SSLSocket` / `ssl.SSLObject` / `ssl.SSLSession` 均无该方法（CPython 至今未暴露 OpenSSL `SSL_export_keying_material`，`ssl.SSLSession` 只有 `has_ticket/id/time/timeout` 等 5 个属性）。阶段 3 按 **Protocol 适配器**落地 `derive_key_from_tls`（签名与 pyOpenSSL `Connection.export_keying_material` 一致，后者自 17.4.0 起支持 RFC 5705，许可证 Apache-2.0 合规约束 9）。**真实 TLS 栈选型是阶段 4 `transport/tls.py` 的架构决策**：候选 A=pyOpenSSL（多一个运行时依赖）、B=阶段 4 用 TLS 通道内交换的 nonce 走 HKDF（零依赖，会话绑定弱化）。测试 `test_stdlib_ssl_lacks_exporter_documented_assumption` 锁定实测事实，CPython 补上 API 时该测试失败提醒复审。 |
| D11 | `PKT_TYPE_KEY_EVENT=0x12` 新增：v4 鼠标/键盘共用 CONTROL(0x02) 靠 JSON `type` 字段软分派；v5 Protobuf 按包头 Type 静态解析，拆出独立类型（0x12 避开所有已用值）。 |
| D12 | ScreenFrame 载荷**恒置** `PRE_COMPRESSED`（data 字段语义即"编码后的压缩字节流"，显式 flag 声明，与叮嘱 a 一致）；`compress=True` 仅对非画面消息（大剪贴板文本）启用 zlib。 |
| D13 | codec 对 pb 枚举字段用 `cast` 弥合：运行时 protobuf 接受等值 int（零成本），生成的 .pyi 要求枚举类型；不做运行时转换。 |
| — | README"v4 向后兼容"节改写为"协议兼容性（v5 不兼容 v4）"（该节原文与范围调整直接冲突）。 |

---

## 4. 测试清单（新增 136 个）

| 文件 | 数量 | 要点 |
|------|------|------|
| `test_protocol_messages.py` | 32 | 枚举×proto/wire 常量三方对齐、frozen、8 类 payload_type 派发、12 个语义校验拒收 |
| `test_protocol_codec.py` | 55 | 9 策略 hypothesis 往返（Packet/帧双路径）、类型保持、PRE_COMPRESSED 规则、compress 路径、边界 9 例（空/128KB/8万字符/NUL/emoji/int32/uint32 极值/全枚举）、字节快照 13 例、错误契约 7 例、decode_frame None 3 例 |
| `test_protocol_framing_v5.py` | 30 | Version 启用、HMAC 全路径（往返/篡改头/篡改载荷/剥标志降级/无密钥拒收/半包等待）、PRE_COMPRESSED 互斥、恶意长度参数化、半魔数重同步、CRC 覆盖面对齐、eq/hash、遗留面防御 |
| `test_protocol_hmac_integrity.py` | 24 | exporter Protocol 契约（参数/长度/RFC 5705 label 前缀/双端同钥/会话绑定/stdlib 缺失锁定）、HKDF 真实往返、篡改五路必拒、恒时比较面 |
| `test_protocol_framing_fuzz.py` | 17 | 2×10k 固定种子随机字节 + 4GB/huge 声明 + zip bomb + 尾部多余数据 + hypothesis 随机头部 |

命名遵循 `test_<module>_<function>_<scenario>`（沿用阶段 2 约定）。

---

## 5. 新增/修改文件清单

**新增源码（4）**：`protocol/constants.py`、`protocol/messages.py`、`protocol/codec.py`、`protocol/hmac_integrity.py`；`proto/rdc.proto`、`scripts/gen_proto.py`；生成物 `protocol/pb/{__init__,rdc_pb2.py,rdc_pb2.pyi}`（gitignore，构建时生成）。

**重写（1）**：`protocol/framing.py`（15B 头/粘包/CRC/zlib 语义保留，Version 启用、HMAC、PRE_COMPRESSED、降级防线、半魔数修复、MD5→SHA-256、全量类型注解+docstring、structlog 面 `get_logger("protocol.framing")`）。

**修改（4）**：`exceptions.py`（+`HmacVerificationError`）、`pyproject.toml`（mypy 宽松清单移除 3 项 / ruff N812 协议别名豁免 / bandit B324 移除 / proto 组安装说明沿用）、`README.md`（协议节 + 开发环境补 gen_proto 步骤）、`PROGRESS.md`。

**未动（刻意）**：`rdc/constants.py` 根级旧常量（v4 调用方仍用，阶段 4/5 切换后整体删除）；`transport/*.py`、`server/service.py` 的 `create_json/create_screen` 调用点（v4 遗留面已适配新 flags 语义，行为兼容）。

---

## 6. 遗留问题

| # | 问题 | 处理 |
|---|------|------|
| 1 | **F1：TLS exporter 无 stdlib 实现**，真实"双端派生同钥"端到端测试缺失 | 阶段 4 决策（候选 A=pyOpenSSL / B=nonce+HKDF），届时补集成测试并可能调整 hmac_integrity 公开面 |
| 2 | `framing.py` v4 遗留面（`create_json/create_screen/to_json/parse_screen`）仍被 transport/server 调用 | TODO(phase-4/5)：调用点切 codec 后删除；当前带 SHA-256 checksum 与新 flags 语义运行 |
| 3 | `PKT_FLAG_FILE=0x04` 载荷类型尚无 message 定义（阶段 8 文件传输再扩 proto） | 阶段 8 加 `FileChunk` message；扩展时保持字段号只增不改 |
| 4 | hypothesis 快照值依赖 protobuf 实现细节（字段顺序 canonical 化依赖 `deterministic=True`） | 已用 deterministic 编码 + 快照锁定；升级 protobuf 大版本时预期需重生成快照 |
| 5 | 全局覆盖率 33.6%（目标 70%） | 预期内，随阶段递增 |
| 6 | `rdc.constants` 根级协议常量与 `protocol/constants.py` 暂存双份 | 阶段 4/5 删根级副本（mypy 清单已标注） |

---

## 7. 阶段 4 起点

1. **先决策 F1**（TLS exporter 路径）：pyOpenSSL vs nonce+HKDF —— 影响 `transport/tls.py` 与 `hmac_integrity` 集成方式。
2. `transport/base.py` Channel Protocol（asyncio）→ `tls.py`（SSLContext 强制 TLS1.2+、TOFU 迁移、exporter 接入）→ `control_channel.py`/`screen_channel.py` asyncio 重写。
3. **v5 握手改造落地**：AuthRequest/AuthResponse 作为首帧（协议自描述，无裸 4B 握手），`session_token.py`（token_urlsafe(32) + 绑定 + 60s TTL）；会话建立后 `PacketBuffer(require_hmac=True)` 强制完整性。
4. `backpressure.py`（满队列丢旧帧+计数）+ **修复 B3**（head-of-line blocking）+ heartbeat/reconnect 改 asyncio。
5. 调用点全部从 `framing.create_json/create_screen` 切到 `codec.encode_frame`，删除 v4 遗留面（§6-2）。
6. `uv sync` 已含 protobuf；阶段 4 需评估是否引入 pyOpenSSL（决策 1 后）。
7. 测试：双进程集成（TLS+认证+帧流）、100 并发无泄漏、令牌重放必失败、HMAC 强制会话端到端。

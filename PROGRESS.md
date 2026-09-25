# PROGRESS.md — remote_desktop v5.0.0 重构进度

> 最后更新：2026-09-25（阶段 6 ✅ 完成）
> 配套：`REFACTOR_PLAN.md`（计划）、`ASSESSMENT.md`（现状评估）、`STAGE1_REPORT.md` / `STAGE2_REPORT.md` / `STAGE3_REPORT.md` / `STAGE4_REPORT.md` / `STAGE5_REPORT.md` / `STAGE6_REPORT.md`（阶段报告）
> 提交历史：`dfe2281` v4 基线 → 阶段 1 → 阶段 2 → 阶段 3 → 阶段 4 → 阶段 5 → 阶段 6（`1752a08` PoC → … → `103d2e3`）

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

## 阶段 4：传输层（asyncio + TLS + HMAC 会话绑定）— ✅ 完成（详见 STAGE4_REPORT.md）

### 子任务状态（对照任务书 §五）

| # | 任务 | 状态 |
|---|------|------|
| 4.1 | `transport/base.py`：Channel Protocol + `FrameStream` + `StreamChannel` | ✅ |
| 4.2 | `control_channel.py` asyncio 重写（TLS + 握手 + 收发协程） | ✅ |
| 4.3 | `screen_channel.py` asyncio 重写（`ScreenJoin` 帧加入，替代裸 4B 握手） | ✅ |
| 4.4 | `backpressure.py`：`DropOldestQueue` 满丢最旧 + dropped 计数 | ✅ |
| 4.5 | `tls.py`：SSLContext 强制 TLS1.2+ + TOFU + 证书生成（自 service 提取） | ✅ |
| 4.6 | `session_token.py`：`token_urlsafe(32)` + IP/connection_id 绑定 + 60s TTL + 单次使用 | ✅ |
| 4.7 | `hmac_nonce.py`：**F1 方案 B 落地**（HKDF + nonce 捎带认证帧交换） | ✅ |
| 4.8 | `reconnect.py` + `heartbeat.py` asyncio 化 | ✅ |
| 4.9 | 修复 B3：`broadcaster.py` 每客户端独立队列 + 独立发送 task | ✅ |
| 4.10 | 测试：真实 TLS 端到端、100 并发压测、令牌重放、慢客户端不阻塞 | ✅ |
| — | `server.py`（TransportServer/会话）：任务书未列但集成与压测必需 | ✅ 补交付 |

### 验收结果（2026-09-23 真实输出）

```
ruff:   All checks passed!
mypy:   Success: no issues found in 62 source files
pytest: 439 passed in 23.54s（新增 140；阶段 3 为 299）
transport/ 覆盖率: 93.0%（验收线 ≥85%）
全局覆盖率: 53.0%（阶段 3 为 33.6%）
100 并发压测: 句柄 234→234（Δ0，两次独立复跑）| RSS Δ3.0/Δ4.5MB（阈值 220MB）
令牌重放: 集成层第二次 ScreenJoin 必 AuthError ✅
慢客户端: 20 帧 publish <1s 不被阻塞；快端收 [11..20]；慢端 dropped=10 各计各的 ✅
python -m rdc --version → 远程桌面控制 v5.0.0
```

### 📌 阶段 4 关键发现与决定

| # | 内容 |
|---|------|
| **D14（扫雷级）** | **`service.py` 不 import transport**（全树 grep 实测；transport 四文件是阶段 1 机械搬迁的孤立副本，唯一消费方在 `legacy_v4/`）。→ 任务书叮嘱 a 担心的"sync→async 桥接 / service 线程模型牵连"**不存在**；`service.py` 本阶段**零改动**。v5 新传输栈自包含，接入真实运行时的点为阶段 9 服务端拆分（用户已确认） |
| **F1 落地（方案 B）** | HKDF-SHA256(`ikm=client_nonce‖server_nonce`, `salt=connection_id`, `info=b"rdc-hmac-v1"`, 32B)。nonce **捎带在认证帧**（`AuthRequest.client_nonce` 新字段 4 / `AuthResponse.server_nonce=5, connection_id=6`）+ 新 `ScreenJoin` message（`Type=0x13`）——握手仍 1 RTT；TLS 建立失败则握手帧不执行、nonce 交换不启动（叮嘱 c 由调用顺序天然保证）。画面通道独立 nonce 集 → 与控制通道**派生不同密钥**，一条泄露不波及另一条。认证失败路径不发 nonce/不签令牌 |
| **B3 修复架构** | `transport/broadcaster.py` 独立 asyncio 组件：每订阅者 `DropOldestQueue` + 独立发送 task；慢客户端只丢自己的旧帧。**代价**：每连接独立 HMAC 密钥 ⇒ 帧无法再字节级 zero-copy，每帧每客户端一次序列化+签名（SHA-256 ~GB/s 吞吐，实测开销可忽略；权衡记录 STAGE4 §4）。v4 同步 broadcaster 留阶段 9 删除（真实运行时修复延后，单客户端场景可接受） |
| **S1-S4 安全收紧** | 随实现落地的额外加固：①降级判定移除 `certificate_verify_failed` 公共错误串匹配（v4 实为"任何校验失败都降级"，与自身注释矛盾）——过期/未知 CA/主机名不符一律不降级；②TOFU 无确认回调时 v4 默认信任 → v5 拒绝；③令牌绑定 `(ip, connection_id)`，绑定不符连原记录作废；④uuid4 令牌 → `secrets.token_urlsafe(32)` |
| 协议最小扩展 | proto 字段号只增不改 + 重新生成 pb；阶段 3 的 299 测试全保持通过。`exceptions.py` +`TlsError` |
| mypy 收紧兑现 | 宽松清单移除 transport 四条目（叮嘱 c 已核验：移除后 62 文件 0 error，无隐藏问题暴露） |
| 测试抓出的实现缺陷 ×2 | ①`recv_message` "坏帧+好帧同段到达"流停摆（pop 消费字节未产出帧时未继续排空缓冲）→ 修复+回归测试；②TLS 降级重建未关旧 socket（句柄泄漏）+ TOFU 拒绝未 `wait_closed` → 两通道修复 |

### 遗留（详见 STAGE4_REPORT §8）

v4 运行时 B3 延阶段 9 · TLS 异常分支覆盖率缺口（control 82.9/screen 80.9，缺口均为
MITM/断连构造）· 双进程集成以同事件循环双协程+真实 loopback socket 等价替代 ·
`rdc.constants` 双份与 `create_json/create_screen` 遗留面（阶段 5/9 删）

---

## 阶段 5：采集与编码（全帧 H.264）— ✅ 完成（详见 STAGE5_REPORT.md）

11 项任务全部交付：`capture/{source,mss_source,diff,monitor}.py` 重写/新建 +
`encoding/{base,h264,hw_probe,raters,bandwidth}.py` + `jpeg.py` 协议化适配（决策 A
构造注入）+ 2 个 bench 脚本 + 11 个测试文件 + 契约测试套件（H264/JPEG 同跑）。
2026-09-24 因额度暂停于批 A（source/monitor/diff 已验证），本次按
STAGE5_INPROGRESS.md §6 恢复续完，全程未重测探针数据。

### 验收结果（2026-09-25 真实输出）

```
ruff:   All checks passed!（src + tests + scripts）
mypy:   Success: no issues found in 69 source files（宽松清单移除
        rdc.capture.{diff,mss_source} + rdc.encoding.jpeg，仅留 framerate）
pytest: 695 passed（基线 439 → 新增 256，零回归；v4 兼容面实测未破）
覆盖率: capture/+encoding/ TOTAL 93.0%（验收线 ≥85%；剔除未重写的 v4
        framerate.py 后阶段 5 新文件约 96%，source/bandwidth/hw_probe 100%）

bench_diff.py（生产路径 双缓冲+adopt=True，iters=50）：
  4K high-change 3.59ms / 4K desktop 3.54ms / 1080p 1.57ms（验收线 <5ms）✅
bench_encode.py（ultrafast+zerolatency+crf28，iters=24）：
  4K static/moving 19.99/19.90ms · 1080p 5.5-6.6ms · 真实屏幕 4.53ms（<40ms）✅
  （最坏参考·信息项：4K 纯随机噪声 47.8ms 超线；断言口径为真实屏幕内容，
    高熵内容的正解是 raters 降分辨率档——详见 STAGE5_REPORT §3.2）
  带宽降幅 vs JPEG q60：94.9/95.8/97.2/94.0/97.3% + 噪声最坏 64.1% +
  真实屏幕 86.5%（验收线 ≥40%）✅
```

### 三条硬约束落地（mss_source.py）

① `mss.MSS()`（绝不用废弃 `mss.mss()`，测试锁死）；② 双缓冲交替 +
`adopt=True`（否则每帧多付 6.19ms；测试断言第 3 帧不覆盖第 2 帧缓冲）；
③ 失败一律 `ScreenCaptureError`（删 v4 兜底；v4 行为由同文件兼容外壳
`ScreenCapturer` 保留，阶段 9 删）。

### 三个致命坑（数据固化在 STAGE5_REPORT §4）

`thread_count=1` → 4K 编码 65.8ms 破线（已钳 ≥4）；mss 废弃 API ×
pytest filterwarnings=error；测量陷阱（内容合成混计时→假 71.67ms、
BGRA 当 RGB 比→假 PSNR 15dB）。

---

## 阶段 6：UI 迁移 PySide6 + 广播管线 — ✅ 完成（详见 STAGE6_REPORT.md）

五批连续执行（用户预授权）：批 A 高风险地基（PoC 桥接双轨实测 → framerate 重写 →
pipeline 串联）/ 批 B UI 骨架+低风险件 / 批 C 可视件 / 批 D 主窗口+对话框 / 批 E 收口。

### 验收结果（2026-09-25 真实输出）

```
ruff:   All checks passed!（src + tests + scripts）
mypy:   Success: no issues found in 87 source files（宽松清单 capture 条目清零：
        兑现 framerate 重写 TODO；qasync 入 ignore_missing_imports，无 py.typed 实测）
pytest: 821 passed in 32.72s（基线 695 → 新增 126，零回归）
UI 覆盖率: 90.3%（--cov=src/rdc/ui，验收线 ≥70%；framerate 100%、pipeline 92.5%）
冷启动: median 441.5ms <1500ms ✅（独立子进程×5，lazy-import 未触发）
跨线程红线: ensure_gui_thread 守卫 + 测试断言（worker 触达必抛 RuntimeError；
        pipeline publish==主线程 / encode==worker 线程且单一）✅
```

### 关键结论

- **桥接选型 = qasync 0.28.0（BSD-2-Clause 合规，PoC 四判据全过，scripts/poc 固化）**；
  核心发现：loop-in-thread **不防 GIL 争用**（忙等下 Qt 定时器 p95 11–16ms vs
  qasync 1.15ms）。⚠️ 3.16 风险：qasync 用 asyncio.iscoroutinefunction（已弃用），
  届时切方案乙（PoC 代码现成），测试豁免已注释登记。
- **GIL 释放实测成立 → executor 无需进程池**（bench_looplag.py：worker 连续编码/差分
  时主线程 1ms 定时器 p95 2–3.8ms）。测量陷阱：Windows 时钟粒度 15.6ms 地板，
  须 timeBeginPeriod(1) + 每用例独立进程。
- **B2 根治**：非模态对话框 + asyncio.Future（v4 after+Event 缝合整类拆除）；
  密码超时→取消、指纹超时→拒绝。
- **h264 新增 codec_name 注入**（hw_probe 落地路径）；硬编选项空映射不发明参数（D24）。
- **legacy_v4/ 删除**（独立提交 `05cf5da`）：45 文件 7,505 行，零引用核查后删除，
  git 历史可恢复。service.py 的 gui.tray 降级段按 D14 归阶段 9（本阶段不碰）。

---

## 下一步：阶段 7 — 输入层

1. `input/base.py` InputBackend Protocol（mouse/key/text/多显示器）+ `windows.py`
   SendInput(ctypes，本机可验) + macos/linux（CI/用户验）；v4 `input/*.py` 四件重写
   补注解后移出 mypy 宽松清单；
2. `coalesce.py` 事件流合并 + 帧对齐：消费 pipeline `FrameDecision`（fps/delay_s）与
   ScreenView `mouseMoved` 载荷（`move / press:btn / release:btn / wheel:dir` 远端绝对坐标）；
3. UI→controller 的 `send_mouse/send_key` 契约已定（MainWindow 走 Protocol，阶段 9
   装配层实现——UI 零改动）；
4. IME/pynput 回调边界：回调只进队列，触控件一律 Signal。

# PROGRESS.md — WaltonDesk v5.0.0 重构进度（原 remote_desktop / rdc）

> 最后更新：2026-11-14 —— **阶段 10 ✅ 完成收口（品牌更名 + 图标接入 + 打包发布 + Windows 安装包），v4.0.3 → v5.0.0 重构彻底完工**，见文末"阶段 10"段与 [`STAGE10_REPORT.md`](STAGE10_REPORT.md)；以下长引文为阶段 9 历史记录：2026-11-06（阶段 9 ✅ 完成收口；真机走查进行中——**十三批** ❌ 项均已修复：六批 + 第六批复测回归 `333ab13`〔VkKeyScanW argtypes〕+ 第九批 `8fd2ab1`〔Win 键释放被系统吞掉 → 按住超 2s 自动补发释放；完整钩子方案归 v5.1.0，README 新增已知限制章节；复测通过：标点/Win/其余键盘全 ✅〕+ 第十批 `fd6d1ad`〔Tab/Backtab 被 Qt 焦点链在 keyPressEvent 之前消费致控制态泄漏 → ScreenView.focusNextPrevChild 覆写 + MainWindow 应用级守卫成对转发，清单新增 3.10 项〕+ 第十一批 `6f58c46`〔被控端运行中改分辨率画面不跟随：MonitorSelector/mss 双层几何缓存致 region 过期 → 采集层 SM 包络漂移检测自动重建采集源，全链无感跟随无需手动刷新，清单新增 4.7 项〕+ **第十二批（2026-11-06，接手收尾批）：Bug 12/14/15 暂停无提示/恢复画面卡死/重启画面卡死（同一病灶链三处：`SubscriptionRegistry` 每运行周期重建修复 broadcaster `_stopped` 永久化 + `MssScreenSource.reopen()` 修复源关闭后复用 + `HostStatus(PAUSED)` 下行修复零提示，`cb68ce3`）+ Bug 19 初次连接死循环（首连网络失败绝不进重连环 + 弹窗"对方未开启被控端"，重连仅服务会话期判死，`b95fad4`）+ Bug 16 双击退化（Qt 把双击第二次按下走 `mouseDoubleClickEvent` 致漏发 press，ScreenView 覆写补成对，`f7aa867`）+ **打包守卫防御式重写**（`rdc.console_safety`：`sys.stdout/stdin is None` 形态 isatty/日志装配不崩，GUI 启动三态判定 stdin None=打包必须起窗、重定向=打印帮助，`0367dca`）+ **Feature 17 开机自启**（`host/autostart` 重写统一 `serve --tray --silent` 静默进被控端不弹 Launcher，Launcher/托盘勾选项注册失败回滚）+ **Feature 18 初始密码策略**（首启 CSPRNG 随机 8 位强口令 bcrypt 落库、明文进 DPAPI 绝不进 config，控制台/Launcher/托盘气泡显眼打印含"不再提示"持久化、托盘与 CLI 改密入口，`60ae150`——两项共享 UI 装配文件协同至全绿合并一条 commit），新增哨兵 70 项（Bug19 双向 2 + 边界对照 1 + UI 1 + Bug16 2 + 画面闭环 9〔集成 3+源 2+UI 1+controller 2+适配 1〕… 实数见 STAGE9_REPORT §6 第十二批段末：+70 = Bug19 2 + 会话期对照 1 + UI 1 + Bug16 2 + 画面闭环 9 + 守卫 13 + F17/F18 46〔14+8+10+14〕）；mypy 源文件 99→102、bandit 复扫 High/Medium/Low 全 0、`rdc.host.autostart` 移出 mypy 宽松清单兑现、`setup.py` 未动（守卫全落 `src/`），终态 1605 passed / 2 skipped 三绿退出码 0；**未测项提醒：清单 7.1 弱口令 / 7.3 日志无明文**——F18 已核验口令明文绝不入 `_LOG`（正面支撑 7.3）但需真机走查确认，7.1 待用户回报）+ **第十三批（2026-11-06）：Bug 1 托盘暂停/恢复文案不同步 `410b70c`**（探针实测根因在 transport stop 收尾顺序——3.12+ 的 wait_closed 等在途 handler 跑完，有连接时 pause 被 10s 空闲窗口拖死、is_paused 恒 False、文案不动且重复下发；修复=stop 重排 close→关会话→收任务→wait_closed + pause 入口置位 + _toggle_pause 装配层防抖锁按实际态刷新；哨兵 5）+ **Bug 2 首启密码不显示 & messageClicked 警告 `1386da0`**（明文被丢弃后横幅/气泡走 DPAPI 回读、回读失败双双静默 + 气泡同步 showMessage 被 Windows 注册时序吞 + 无连接 disconnect 实抛 SystemError；修复=first_run_notice_text 内存明文一路透传 print+气泡双路展示、QTimer.singleShot(0) 延迟投递、构建期一次性 connect 守卫，disconnect 彻底移除；哨兵 8）+ **IPv6 连接超时→v5.0.0 决策支持 `cec2bee`+`890b855`**（0.0.0.0 实测仅监听 v4；全接口语义改 AF_INET6+IPV6_V6ONLY=0 双栈监听、探测失败回退 v4 留痕 + _normalize_peer_ip 归一 ::ffff: 防封禁/白名单失配；客户端零改动；README 补 IPv6 口径；bandit B104/B107 各 1 已 nosec 附理由抑制；哨兵 6），本批哨兵 +19（1605→**1624**），三处病灶 stash 双红逐项复验转红，ruff/mypy/bandit 全零、controller 零 Qt 红线复验通过；**下一步：用户真机复测本批 3 项预期（托盘暂停文案 1s 内切换且再点不重复下发 / 删配置首启控制台显眼打印明文+托盘气泡+终端无 disconnect 警告 / 主控端填被控端 IPv6 地址直连不超时）通过后即进阶段 10（打包发布）**）
> 配套：`README.md`（面向用户的安装/使用/构建手册）、`CHANGELOG.md`（版本变更与破坏性变更）、`REFACTOR_PLAN.md`（计划）、`ASSESSMENT.md`（现状评估）、`STAGE1_REPORT.md` / `STAGE2_REPORT.md` / `STAGE3_REPORT.md` / `STAGE4_REPORT.md` / `STAGE5_REPORT.md` / `STAGE6_REPORT.md` / `STAGE7_REPORT.md` / `STAGE9_REPORT.md` / `STAGE10_REPORT.md`（阶段报告；阶段 8 延后未开）
> 提交历史：`dfe2281` v4 基线 → 阶段 1 → … → 阶段 6（`103d2e3`/`bd7c7d1`）→ 阶段 7（`9b43461` 批A → `66093ca` 批B → `2a0cb86` 批C → `818da92` 批D → `80347c7` 批E → `1f94e5e` 批F 收口 → `dc466d0` 批F 补测）→ 阶段 9（`efd38a0` 任务书基线 → `188479d` 批0 → `44c3b2b` 批1 → `59b807c` 批2 → `f8ea292` 批3 → `f2ca14e`+`e7b6d9c` 批4 → `ab95509`/`12f6b7a`/`d07a79a`/`bfea86a`/`dde0b8a` 批5 → 收口 → 走查第十四批 `14d4d98`/`3a2112c`/`4cf3232` + 文档 `6d84ce2`）→ 阶段 10（`0904fb9` 更名+图标 → `c4e3c06` 打包链路+冒烟修复 → 收口）

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

## 阶段 7：输入层（Windows 优先）— ✅ 完成（详见 STAGE7_REPORT.md）

**范围调整（2026-09-26 用户决定）**：只做 Windows 输入层；macOS/Linux
（`macos.py`/`linux_x11.py`/`linux_wayland.py`）留**空实现**——全方法抛
`NotImplementedError` + `supported()=False`，各配契约测试；将来补实现只换
方法体，`InputBackend` Protocol 与上层零改动。理由：主用 Windows、非 Windows
本机不可测、发布目标未定、保留扩展性、Windows 路径 100% 本机可验（无需 mock）。

### 子任务状态（对照任务书 §六，含范围调整）

| # | 任务 | 状态 |
|---|------|------|
| 7.1 | `input/base.py`：InputBackend Protocol + `MouseButton`/`ScreenRect`（虚拟桌面负原点）+ `normalize_key_name`（X11/Qt 三风格归一，值域=v4 KEY_MAP）+ `default_backend` 工厂（空实现/依赖缺失自动回退 legacy） | ✅ |
| 7.2 | `input/windows.py`：ctypes SendInput（结构体布局测试锁定 x64=40B）、绝对坐标 0–65535 归一化+VIRTUALDESK、KEYEVENTF_UNICODE（代理对、`\n`→VK_RETURN）、VK 表+扩展标志、VkKeyScanW 兜底（需 Shift 拒绝→type_text）、reset 记账 | ✅ |
| 7.3 | `macos.py` / `linux_x11.py` / `linux_wayland.py` 空实现（+Wayland 会话探测；docstring 固化未来实现要点与许可证注意） | ✅ |
| 7.4 | `input/legacy_pyautogui.py`：v4 mouse+keyboard 注入合并为协议实现（剔监听侧与 50ms 防抖；吞错→InputControlError；补 reset） | ✅ |
| 7.5 | `input/coalesce.py`：移动末值合并、节拍 `max(画面fps, 30)` 消费 FrameDecision（鸭子协议）、ScreenView 载荷解析、排队不丢+max_pending 背压、文本拼接、poll/drain 分离、单调 seq | ✅ |
| 7.6 | 剪贴板（Windows）：扩展 `ui/clipboard_sync.py`（决策点 2）——image/files 载荷、两级检测（廉价轮询+dataChanged 全量确认）、urls>image>text、读回基线防回环；MainWindow `clipboard_{image,files}_from_remote` 协议扩展 | ✅ |
| 7.7 | IME：ScreenView `textCommitted`（仅候选窗提交串，物理键一律 keyEvent 保阶段 6 基线）+ viewport eventFilter（InputMethod 事件无冒泡，探针实证）+ `input/keys.py`（Qt 键码→规范名）+ `send_text` 契约（决策点 3） | ✅ |
| 7.8 | 测试：Windows 输入单测（SendInput 边界打桩断言结构体，决策点 1 不打真机）+ base 契约 + coalesce 假时钟全确定 + 空实现 NotImplementedError 逐方法断言 + legacy 契约复用 | ✅ |
| 7.9 | 收口：v4 `input/{mouse,keyboard,dpi,modifiers}.py` 删除（零引用核验）、mypy 宽松清单 `rdc.input.*` 清零 | ✅ |

### 验收结果（2026-09-26 真实输出）

```
ruff:   All checks passed!（src + tests）
mypy:   Success: no issues found in 91 source files
        （宽松清单兑现 TODO(phase-7)：rdc.input.* 四条目随文件删除清零；
        新 input/* 与批 E UI 件全 strict 零豁免）
pytest: 1137 passed in 36.95s（基线 821 → +316，零回归）
input/ 覆盖率: TOTAL 95.5%（验收线 ≥70%）
  base/coalesce/keys/macos/linux_x11/linux_wayland 100% · legacy 94.3%
  · windows 88.3%（缺口=真实 Win32 边界 send/bounds/scan，按决策点 1 不打桩）
真机手动验证清单：STAGE7_REPORT §6（装配完成后一次性走查）
```

### 关键发现（详见 STAGE7_REPORT §4）

- PySide6 6.11 绑定四陷阱（探针实测固化）：QInputMethodEvent 两参构造填
  preedit 非 commit；InputMethod 事件无冒泡必须 viewport 过滤器；
  QImage.save/bytes(QByteArray) 存根与运行时错位；offscreen 空剪贴板
  mimeData() 返 None。Qt 剪贴板进程级全局——测试 fixture 必先 clear()。
- 批 E 初稿曾把可打印键改走文本通道，被阶段 6 基线测试抓住回滚（D35：
  物理键一律 keyEvent，仅 IME 提交串走 textCommitted）。

---

## 下一步：阶段 8 — 文件传输 + 音频（可选，20-26h）

> 📌 2026-10-02 用户决定：**阶段 8 延后**，先执行阶段 9（安全加固 + 装配）；
> 阶段 9 已于 2026-10-03 收口完成，当前下一步 = **阶段 10（打包发布）**，见文末。
> 本章节内容仍有效（其第 3 项"路径穿越防护"的阶段 9 前置部分已落地并复跑实测，
> 文件传输面留待阶段 8 补测——STAGE9_REPORT §4 S4 / 遗留 (b)）。

1. `file_transfer.py`：1MB 分块 + SHA-256 + 断点续传（协议 `ClipboardEvent`
   分帧已就绪，剪贴板图片/文件字节的出站通道即接此处）；
2. `transfer_dialog.py`：拖拽 UI + 进度 + 暂停/续传（复用 D26 非模态+Future 模式）；
3. **路径穿越防护**：目标路径规范化 + 沙箱根校验（阶段 9 渗透清单前置）；
4. `audio.py`：sounddevice + Opus（实测 libopus 可用）+ 播放 + 开关
   （本机无声卡 → mock 回环 + 用户手动验证）；
5. 阶段 7 遗留归阶段 9 装配：controller 实现 `send_mouse/send_key/send_text`、
   剪贴板三类双向接线、coalesce 挂 pipeline 节拍（`sync_to_frame`）、
   `default_backend` 构造与 `reset` 挂断连路径、Windows 真机走查（STAGE7 §6）。

---

# 阶段 9：安全加固 + 装配 — ✅ 完成（详见 STAGE9_REPORT.md，2026-10-03 收口）

任务书基线：`efd38a0`，1137 passed。执行方案：批 0→5 + 收口，每批 commit（用户已确认全案与 2 疑点方案）。

## 批次状态

| 批 | 内容 | commit | 状态 |
|---|---|---|---|
| 批 0 | bandit + pip-audit 入 dev 组，首轮基线扫描 | `188479d` | ✅ |
| 批 1 | B5 password_policy + B6 banlist（限流迁移/持久化/1000 裁剪）+ B9 MD5 确认 | `44c3b2b` | ✅ |
| 批 2 | B7 certs（用户证书加载/轮换/过期检测接 tls.py）+ B8 audit（认证/连接/控制三类事件） | `59b807c` | ✅ |
| 批 3 | A2 service.py → {service,auth,banlist,broadcaster,audit} 五件；v4 线程模型删除，转 asyncio TransportServer + qasync；CLI serve 落地 | `f8ea292` | ✅ |
| 批 4 | A1 client/controller.py 状态机 + A3 default_backend + A4 coalesce 挂帧节拍 + CLI control/GUI 装配 + Task 5 遗留面清理（_v4_config/constants/logger shim/ScreenCapturer/Packet v4 辅助） | `f2ca14e` + `e7b6d9c` | ✅ |
| 批 5 | 同机双进程 loopback 端到端 + 渗透四项实测 + bandit/pip-audit 复跑 + 全量覆盖率收口 + 真机走查清单交付 + 用户批准的 nosec 补齐（收口前增项） | `ab95509`（e2e）/ `12f6b7a`（渗透+证书穿越防护）/ `d07a79a`（覆盖率补测）/ `bfea86a`（走查清单）/ `dde0b8a`（nosec 补齐） | ✅ |
| 收口 | STAGE9_REPORT.md + PROGRESS/README 修正 | 本次提交 | ✅ |

## 批 4 验收数据（`e7b6d9c` 时点，已被批 5 超越）

- **pytest：1382 passed，退出码 0**（全量套件）；ruff 0 错、mypy 98 files 无问题。
- 覆盖率：`client/controller.py` 90.0%；input 核心 base/coalesce/keys 100%、windows 88.3%；`client/ratelimit.py` 100%。
- controller **零 Qt/Widgets 导入**红线核验通过。
- 遗留面清理：`git grep` 核验零消费者后删除；`test_framing.py`/`test_config_migration.py` 的 v4 基线能力（CRC 篡改、粘包、半帧）已迁至 `test_protocol_framing_v5.py`。

## 批 4 关键修复（"测试全绿但 exit 1"类问题排查法，批 5 全程未复现、备后续参照）

1. **退出码 1 污染**：pytest 全绿但进程 exit 1，且 `test_protocol_codec` 两个 hypothesis 用例间歇失败。根因：①controller 在解释器/GC 拆环阶段的收尾回调中 `ensure_future` 起重连环 → 协程 never-awaited 告警被 `filterwarnings=["error"]` 计入失败；②某测试用例直接构造 controller 未登记 `_LIVE_CTLS`，4 个后台任务存活到退出期。修复：`_start_reconnect_loop` 加"无运行循环则直接落定 disconnected"守卫 + 用例补登记；补守卫测试 `test_reconnect_guard_when_no_running_loop`。
2. 排查手法备查：`gc.set_debug(DEBUG_UNCOLLECTABLE)` 定位垃圾对象；按测试类累加前缀二分泄漏源（脚本模式见 git 史，临时文件已清理）。

## 批 5 验收数据（收口终态，真实输出）

```
ruff:   All checks passed!（全仓）
mypy:   Success: no issues found in 98 source files
pytest: 1417 passed, 2 skipped，退出码 0
        （任务书基线 1137 → 批 4 1382 → 批 5 +35：e2e 1 + 渗透 19(+2 skip) + 覆盖率补测 15）
bandit: High 0 / Medium 0 / Low 0（46 处误报全部 nosec + 逐处理由注释，口径见报告 §5）
pip-audit: No known vulnerabilities found（rdc 自身不在 PyPI 被 Skip，属预期）
覆盖率: 总体 89.7%（线 ≥70%）；核心清单全部 ≥85%——批 5 补齐两处缺口：
        transport/screen_channel 80.3→100%、transport/server 81.3→95.7%
        （新增 test_transport_screen_channel_tls.py / test_transport_server_error_paths.py）
loopback 判据: 认证通过 + H.264 帧（真实编码+真实解码 64×64×3 rgb24）+ 输入受理，三项 ✅
真机走查: 清单已交付（STAGE9_CHECKLIST.md，8 组 45 项），**结果待用户回报**（1-2 天）
```

## 批 5 偏差与发现（如实记录，详见 STAGE9_REPORT §1/§4/§5）

- **nosec 未随批落地**：批 1-4 计划的抑制补齐当时未做（收口前全树仅 1 条 nosec），
  bandit Medium/Low 未清零；经用户决定收口前统一补齐（`dde0b8a`），复扫归零。
  纪律固化：新写 assert 当场带 `# nosec` + 理由。
- **B101 +9 系新代码引入**（批 3 service 重写 +6、批 4 controller +3），非遗留面残留。
- **渗透 S1 发现**：同会话内原样重投已签名帧会被再次受理（帧 HMAC 覆盖头部+载荷但**无序号**）；
  当前业务面后果为幂等噪声级 → 遗留 (a)。
- **渗透 S4**：证书路径穿越防护 `require_within_data_root` 落地并实测（`12f6b7a` 含）；
  文件传输面 skip 声明 → 遗留 (b)。符号链接逃逸用例本机非管理员 skip（判定逻辑其余用例已覆盖）。

## 阶段 10 起点（下一步：打包发布）

进场携带（详见 STAGE9_REPORT §8/§9）：
- **真机走查已回报十四批**（第一~六批 + 复测回归 + 第九~十四批，累计哨兵 189 项入仓，终态 **1650 passed / 2 skipped**、mypy 103 files、bandit 全零；第十三批 2026-11-06 修托盘暂停文案/首启密码显示/IPv6 双栈，第十四批 2026-11-08 修 IPv6 实时展示/暂停软保活映射/首启密码三通道，`14d4d98`+`3a2112c`+`4cf3232`）；发现 ❌ 项单独提交 bug fix + 更新 STAGE9_REPORT，不混入收口。
- **遗留 (a)**：帧 HMAC 无序号——阶段 8/10 引入有状态操作（文件传输/指令序列）前须补帧序号/滑动窗口重放防护。
- **遗留 (b)**：文件传输面渗透待阶段 8 补测（目标目录沙箱根校验 + 穿越/符号链接/名规范化实测，属阶段 8 验收项）。
- 阶段 10 本体：本机 PyInstaller -D + 冷启动 <2s 验收、Nuitka CI、更新通道本地模拟、macOS/Linux CI 补验（R6）、性能压测（B3 修复后多客户端 broadcaster 实测）、硬编选项标定、qasync 3.16 切换预案。

> STAGE9_INPROGRESS.md 为批 0-4 中断保护快照（保留备查，其"批 5 待办"已全部落地于本节与 STAGE9_REPORT）。

---

# 阶段 10：品牌更名 + 图标接入 + 打包发布 — ✅ 完成（详见 STAGE10_REPORT.md，2026-11-14 收口）

**范围按用户下发的四组清单执行**（一 全局品牌更名 / 二 图标接入 + PyInstaller
配置 / 三 Windows 安装包 / 四 本地验证与文档收口）。原 REFACTOR_PLAN 阶段 10
的 10.2 Nuitka、10.4 macOS dmg、10.5 Linux deb、10.6 更新通道、10.7 CI **不在
本次范围**（本机物理限制或需单独授权），已在 STAGE10_REPORT §7.1 如实列出。

提交：`0904fb9`（更名 + 图标）→ `c4e3c06`（打包链路 + 冒烟修复）→ 收口批。

## 交付要点

- **更名**：发行名 `rdc` → `waltondesk`、入口 `waltondesk`、全部用户可见文案、
  数据目录 `~/.remote_desktop` → `~/.waltondesk`（一次性迁移，**只搬不删**）、
  自启注册表值名与 PID 文件 → `WaltonDesk`。**导入包名保持 `rdc`**（上千处
  import 与开机自启命令依赖它）。新增 `rdc/brand.py` 品牌单点。
  **刻意不改**三类会静默破坏存量状态的标识：DPAPI 熵串、密钥环条目归属键、
  自签证书 CN（`brand.py` 顶部登记为契约）。
- **图标**：`icon.png`（实测 1183×1220 非正方形）→ 9 档 `.ico`，双写根目录
  （构建期需磁盘路径）与包内 `theme/`（运行期唯一加载点，已随 datas 进包）；
  应用级默认图标让全部窗口自动继承；托盘改品牌底图 + 状态角标，资源缺失回退旧画法。
- **打包**：`waltondesk.spec`（`.gitignore` 加 `!waltondesk.spec`——原 `*.spec`
  是 v4 时代规则，不加例外则克隆后无法构建）。**windowed（`console=False`，
  收口批反转）**：真机验收发现 console=True 产物双击必闪黑框（bootloader 先建
  控制台、代码再销毁，"先有再关"物理上关不掉那一瞬）。windowed 下双击/自启
  零黑框（进程物理上没有控制台）；CLI 场景由 `desktop_entry.ensure_console()`
  按需补：先复用继承的 Win32 标准句柄（重定向/管道保真，实测上一版直接
  AttachConsole 会劫持 `> log.txt` 令日志文件零字节），句柄无效才
  AttachConsole 父控制台 → AllocConsole 新建。**体积 899 MB → 355 MB**
  （`collect_all("PySide6")` 是元凶：Qt6WebEngineCore 194 MB + devtools .pak
  83 MB；实测源码只用三个 Qt 模块，Qt6 DLL 144 → 13 个；收口批另挡掉
  pydantic.mypy 连带拖入的 mypy 包）。
- **安装包**：`waltondesk_installer.iss` 本机 `iscc`（Inno Setup 7.1.1）编译，
  **向导全界面简体中文**（官方 `ChineseSimplified.isl`，收口批改造——初版只挂
  Default.isl + 零星自译，中英混杂被用户打回）。四个 Inno 真实报错教训见
  STAGE10_REPORT §5（ISPP 把 `#13#10` 当指令 / 段内不支持续行 /
  `PrivilegesRequiredOverridesAllowed` 无 `output` 值 / `MsgBox` 只有 3 参）。
  配套新增 `scripts/check_iss_syntax.py` 把迭代成本从几十分钟降到 1.6 秒。

## 打包冒烟与真机走查发现并修复的六项

收口前三项（真跑 frozen exe，非看日志）：

1. **配置含 UTF-8 BOM 被误判损坏并静默重置**：记事本（Win10 1903+ 默认存带 BOM
   的 UTF-8）另存过 `config.json` → `json.loads` 抛 "Unexpected UTF-8 BOM" →
   **整份配置改名 `.corrupt` 并用默认值重建**，用户表现为"设置和口令突然全没了"。
   改 `utf-8-sig`。
2. **frozen 下回退输入后端静默失效**：`legacy_pyautogui` 延迟 import 分析器收不到
   → spec 补 pyautogui 及依赖到 hiddenimports（源码能跑、exe 不行，最难查的形态）。
3. 服务标识残留旧品牌 + 包 docstring 旧中文名（新增 AST 级残留哨兵）。

用户装机验收后追加三项（v5.0.0 发布前修复，详见 STAGE10_REPORT §8）：

4. **🔴 致命：输入模块漏包致控制断联**（`No module named 'rdc.input.windows'`）。
   第 2 项当时只补了 pyautogui 三方包，**没补 rdc 自己的平台后端**——
   `input/base.py` 用 `importlib.import_module` 加载后端，静态分析同样看不见，
   且 `rdc.input.windows` 在 PYZ 清单里确实缺席（取证：8 个 input 子模块只入了
   3 个被静态引用的）。修复：`collect_submodules("rdc")` 全量收集 + 用户点名的
   5 个后端显式登记；**新增 `scripts/check_frozen_modules.py` 用"源码模块 vs
   PYZ 清单"差集做机器核验**——首轮就抓出 11 个漏网（不止报障的 5 个，还含
   `encoding.jpeg`、`input.keys`、`ui.status_panel` 等），这类"想到哪个补哪个"
   的坑必须用差集比对而非人脑。
5. **双击闪黑框**：console=True 物理缺陷，反转 windowed + 按需补控制台（含句柄
   复用防重定向劫持），PE 子系统实测 `subsystem=2`，重定向 stdout 19 B /
   stderr 140 B 保真。
6. **安装包中英混杂**：换官方简体中文语言包 + Tasks 全中文字面量，编译日志
   实测读到 `Languages\ChineseSimplified.isl`。

## 验收（真实输出，收口批终值）

```
pytest: 1703 passed / 2 skipped，退出码 0（进场基线 1650，+53 哨兵：品牌 29 + 打包入口 24）
ruff All checks passed · mypy 106 files（进场 103）· bandit High/Medium/Low 全 0
双红哨兵 13/13 注入病灶转红（品牌 6/6 + 打包入口 7/7，含"句柄复用被忽略=重定向
被劫持"新病灶），恢复后全绿
产物 dist/WaltonDesk/ 355.1 MB；PE subsystem=2（GUI/windowed，双击零黑框的物理证据）
动态导入差集核验 check_frozen_modules.py：源码 106 个 rdc.* 模块全部入包
  （首轮差集抓出 11 个漏网，含报障的 rdc.input.windows / legacy_pyautogui）
输入后端依赖闭合：windows.py 纯 stdlib（ctypes/sys）；pyautogui 及 4 个依赖全入包
WaltonDesk.exe --version → "WaltonDesk v5.0.0"（exit 0）；重定向保真 stdout 19 B /
  stderr 140 B（上一版直接 AttachConsole 时被劫持为 0 字节）
冷启动 median 0.418s（验收线 <2s）
pb/ + dark.qss + icon.ico + platforms/qwindows.dll + av/ + shiboken6/ 全部入包；
  开发工具 mypy/mypyc/pydantic.mypy 已挡在产物之外（收口批核验发现并排除）
安装包 dist/WaltonDesk_setup.exe 98.4 MB，版本信息读回全对；编译日志实测读取
  Languages\ChineseSimplified.isl（向导全中文）
frozen 被控端冒烟（隔离数据根）：初始密码生成并打印 → TLS 自签 → 双栈监听 →
  libx264 AVAILABLE → pipeline 就绪；stderr 零输出
QT_QPA_PLATFORM=offscreen 跑 --gui 存活 6s 不闪退（证明 frozen 资源真实可用）
```

哨兵锁的是"漏了就静默坏掉"的语义而非覆盖率数字，例：override 根下自动迁移必须
跳过（否则把用户真实 `~/.remote_desktop` 整目录搬走）、`main()` 必须在任何配置
读写前迁移（晚一步被新建空配置堵死）、新根已有 config.json 则整体放弃、
GUI/托盘形态对控制台**零操作**（黑框病灶复发即红）而 `--version` 必须补控制台、
继承句柄可用时绝不 AttachConsole（否则劫持用户的 `> log.txt`）。

## 下一步

- **阶段 8**（文件传输 + 音频，延后未开）：进入前**必须先做遗留 (a) 帧序号/
  滑动窗口重放防护**——文件传输是有状态操作，帧 HMAC 无序号在此之前只是幂等
  噪声级，之后就是真风险。遗留 (b) 文件传输面渗透随该阶段补测。
- 发布前补 Authenticode 代码签名（否则 SmartScreen 告警；EV 证书同时是 v5.1.0
  UIAccess 的前置）。
- v5.1.0 特权操作范围（安全桌面 + 完整 Win 键）与体积优化（cv2 占 111 MB）、
  密钥库命名空间迁移。


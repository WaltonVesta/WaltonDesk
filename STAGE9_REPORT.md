# STAGE9_REPORT.md — 阶段 9：安全加固 + 装配 ✅ 完成

> 日期：2026-10-03 收口 · 2026-10-04 走查第一轮修复 GUI 启动 bug · 2026-10-05 走查第二轮修复 launcher→主窗切换崩溃 bug · 2026-10-06 走查第三轮修复奇数分辨率编码器崩溃 bug · 2026-10-07 走查第四批修复认证流程三项缺陷 · 2026-10-07 走查第五批修复画面缩放策略 · 2026-10-08 走查第六批修复 UAC 安全桌面画面永久停摆（P0）+ 键盘标点/Tab·Win 泄漏 + 封禁告知竞态 + 异常断连卡键 + 断连回调已删窗 + 封禁倒计时（六批详见 §6 走查记录） · 2026-10-17 第六批复测回归修复 VkKeyScanW argtypes 缺失（§6） · 2026-10-17 走查第九批修复 Win 键释放被系统吞掉致被控端卡键（§6，v5.0.0 应急兜底 + README 已知限制） · 2026-10-17 走查第十批修复 Tab/Backtab 被 Qt 焦点链提前消费致控制态泄漏（§6，视图覆写 + 应用级守卫双层） · 任务书基线 `efd38a0`（1137 passed）→ 收口 1417 passed / 2 skipped → 走查修复后终态 1508 passed / 2 skipped → 复测回归修复后 1511 passed / 2 skipped → 第九批后 1518 passed / 2 skipped → 第十批后 1523 passed / 2 skipped（三绿退出码 0）
> 范围：v4.0.3 → v5.0.0 重构的阶段 9（**阶段 8 文件传输+音频经用户决定延后**）
> 配套：`STAGE9_PLAN.md`（执行方案）、`STAGE9_CHECKLIST.md`（真机走查清单）、`SECURITY_BASELINE.md`（批 0 扫描基线）

## 1. 六批完成情况

| 批 | 内容 | commit | 验收（当时实测） |
|---|---|---|---|
| 批 0 | bandit + pip-audit 入 security 组，基线扫描 | `188479d` | High **0** / Medium 2 / Low 53；pip-audit 0 漏洞 |
| 批 1 | B5 `security/password_policy` + B6 `server/banlist`（滑动窗口/持久化/1000 裁剪）+ B9 MD5 确认 | `44c3b2b` | 目标区覆盖率 ≥85% |
| 批 2 | B7 `security/certs`（用户证书加载/轮换/过期）+ B8 审计（认证/连接/控制三类事件） | `59b807c` | 同上 |
| 批 3 | A2 `service.py`（1,213 行 v4 线程模型）拆五件 + asyncio 传输接入 + B3 根治 + CLI serve | `f8ea292` | 全量绿 + 真机起服务验证 |
| 批 4 | A1 `client/controller` + A3 `default_backend` + A4 coalesce 挂帧节拍 + CLI control/GUI 装配 + 遗留面清理 + 协程泄漏修复 | `f2ca14e` + `e7b6d9c` | **1382 passed** 退出码 0；ruff 0；mypy 98 files 0；controller 零 Qt 红线 grep 零命中 |
| 批 5 | loopback 双进程端到端 + 渗透四项 + bandit/pip-audit 复跑 + 覆盖率收口 + 走查清单 + nosec 补齐 | `ab95509` / `12f6b7a` / `d07a79a` / `bfea86a` / `dde0b8a` | 见 §2/§5/§6/§7；收口终态 **1417 passed / 2 skipped**，三绿退出码 0 |
| 收口 | 本报告 + README/PROGRESS 状态修正 | 本提交 | — |

**提交口径说明**：计划表原定批 5 单条 commit message（`test(e2e): add loopback e2e and penetration checks`），实际按"每完成一小项立即 commit"纪律拆为 5 条（语义不超出计划范围）；nosec 补齐为用户批准的收口前增项（`chore(security)`）。

**批 5 后 bandit 口径澄清（用户问询，如实记录）**：批 0 基线 Medium 2 / Low 53 → 批 5 复扫 Medium 1 / Low 45。差额构成：
- B105 19→2（**−17**）：批 4 删除 `_v4_config.py` 连带消除（与计划一致）；
- B101 2→11（**+9**）：批 3 重写 `service.py`（+6）、批 4 新增 `controller.py`（+3）引入的内部不变量断言——**新代码净增**，非遗留；
- B104 2→1（−1）：旧 service 内联 `bind('0.0.0.0')` ×2 随批 3 拆除，剩余 1 处为 `settings.py` 默认值；
- subprocess 组 31 与 B112 组 1：未动，与基线一致。
- 基线批 1-4 计划中的 nosec 抑制**未随批落地**（当时全树仅 1 条 nosec），经用户决定在收口前统一补齐（§5）。

## 2. 装配架构（A1–A4）

### A2 被控端：service.py 五件拆分（D14）

```
HostService（server/service.py，479 行，纯生命周期编排，零 Qt）
  ├─ TransportServer        transport/server.py     TLS 双端口监听/握手/check_client 准入/on_auth_failed
  ├─ HostAuth               server/auth.py          口令哈希视图 + 准入顺序（封禁→白→黑→限流）+ 失败接线
  ├─ SubscriptionRegistry   server/broadcaster.py   画面订阅/回收/关键帧请求
  │    └─ ScreenBroadcaster transport/broadcaster.py（B3 修复件：每订阅者 DropOldestQueue）
  ├─ ScreenPipeline         server/pipeline.py      采集→差分→门控→编码→publish（单 worker executor）
  ├─ InputBackend           input/base.default_backend（懒构造，测试可注入）
  └─ AuditLogger / Banlist  server/audit.py / server/banlist.py
```

v4 行为差异（线程模型→单事件循环协程、裸 4B 握手→15B 分帧 Protobuf+HMAC、welcome 帧取消、`tk.Tk()` 剪贴板 B2 消除→pyperclip、Semaphore 计数对象"客户端→会话"、口令热更走 restart）逐条记录于类 docstring 与 git 史。

### A1 主控端：`client/controller.py`（774 行，**零 Qt/Widgets 导入**红线达成）

- UI 侧 `ClientController`/`ClientEvents` Protocol 结构化实现（不显式继承，UI 零改动）；
- 口令/指纹**异步直通**：`get_password`/`confirm_fingerprint` 回调 awaitable 感知直通 `verify_peer_trust_async`；批 4 评审否决"预检探测"（会在被控端握手期制造伪认证失败、污染封禁窗口）；会话口令明文驻内存、断开即清、不落日志；
- 状态机 idle→connecting→authenticating→connected→reconnecting→disconnected；重连失败分型：网络类续环、认证/TLS/用户取消类**终止**（不烧对端封禁窗口）；
- RTT=PING 回显时间戳差；指标 1s 窗口（fps/带宽/分辨率/累计帧）；
- 教训固化（批 4）：`_start_reconnect_loop` 无运行循环守卫 + 测试构造 controller 必须登记活体清单——否则解释器拆环期 `coroutine never awaited` 被 `filterwarnings=error` 计入失败（"测试全绿但 exit 1"类问题排查法见 `STAGE9_INPROGRESS.md`）。

### A3/A4 输入装配

- `input/__init__.py` 刻意不 eager re-export（防 v4 循环链），工厂在 `base.default_backend`（Windows=SendInput，macOS/Linux 空实现按阶段 7 范围回退）；
- coalesce 挂画面节拍：controller `_handle_screen_frame → sync_to_frame`（帧间隔 EMA 估实测 fps），`_flush_loop` 按 `next_delay_s` 出批——画面静止 idle 档输入不饿死（D34 只提前不推迟）。

### CLI/GUI 入口

`rdc serve [--tray]` / `rdc control HOST [--frames N]` / `rdc set-password` / 无参 GUI；serve 纯 asyncio headless（托盘走 qasync 装配），control 冒烟即 §6 的 CLI 前身。

## 3. 安全加固（B5–B9）

| 件 | 要点 | 覆盖 |
|---|---|---|
| `security/password_policy.py` | ≥8 位 + 字母 + 数字，结构化违规列表；UI/CLI 单一来源 | 100% |
| `server/banlist.py` | 滑动窗口（仅窗口内失败计数）、`banlist.json` 原子写持久化、1000 条裁剪、损坏=warning+空表 | 97.2% |
| `security/certs.py` | 用户证书加载校验（PEM/公私钥匹配/未过期，失败**不回落自签名** S1 口径）、过期三态分类、轮换建议；批 5 新增 `require_within_data_root` 路径穿越防护 | 91.5% |
| `server/audit.py` + `security/audit_events.py` | 认证/连接/控制三类 structlog 事件；控制类首条+计数聚合（防 30fps 冲垮轮转）；接口从签名上不接受口令/私钥 | 100% |
| B9 MD5 | src/ 零 `hashlib.md5` 调用点，bandit 无 B324——阶段 3 改 SHA-256 的闭环确认 | — |

接线：`HostAuth`（口令视图/准入/失败→封禁→审计）+ `TransportServer.check_client`（握手**前**拒绝，被封 IP 不消耗握手帧）+ `on_auth_failed`（窗口语义：TLS/网络/超时类失败**不计数**）。

## 4. 渗透清单四项实测（B10，`tests/e2e/test_penetration_stage9.py`，21 用例：19 passed / 2 skip）

### S1 重放（复用阶段 4 三层，不重写 + 3 条运行时抓帧实测）

| 层 | 既有锁定测试 | 本批补实测 |
|---|---|---|
| ① 令牌单次使用 | `test_transport_session_token.py` | — |
| ② 加入层拒绝 | `test_transport_integration.py::test_token_replay_second_join_fails`、`test_host_service_integration.py::test_replay_join_rejected` | service 全栈下复验 ② |
| ③ 密钥会话绑定 | `test_transport_handshake.py`（每连接一次 nonce、双通道密钥隔离） | **跨会话重放=HMAC 验签失败丢帧且不断流**（真实帧字节换连接重投）；篡改一字节重投同样丢弃、紧随合法帧照常 |
| 威胁面如实记录 | — | **同会话内**原样重投已签名帧会被再次受理（帧 HMAC 覆盖头部+载荷但**无序号**）。当前业务面（鼠标/按键/画面）后果为幂等噪声级 → 遗留 (a) 见 §8 |

### S2 MITM（TOFU 指纹校验绕过尝试）

- 真实 TLS **双证书**接入实测：预信任证书 A，服务端换证书 B → **必触发确认回调**（无静默放行）、用户拒绝 → `TlsError` 且 B **不落信任列表**；同证书重连免确认（证明检出非误杀）；
- 纯函数判定面既有锁定：无证书拒、无 confirm 回调拒（v5 禁静默默认信任）、过期/主机名不符**不降级**（v4.0.2 病灶——匹配公共错误串 `certificate_verify_failed` 等于对一切失败降级——的收窄规则本批复验）。

### S3 弱口令爆破（password_policy + banlist 联合）

- 策略源头：7 个常见弱口令字典全部无法设进系统；
- 运行时：真实 TLS 连打 5 个弱口令变体 → 第 3 次失败**滑动窗口触发封禁** → 封禁期内**正确口令也被握手前拒绝**；
- 自愈：封禁到期自动解封（假时钟零等待，防封禁机制反被用来 DoS 自己）；
- 吞吐天花板：每秒连接限流在握手前生效（默认 5/s）；bcrypt rounds=12 单次校验 ≥50ms 成本锚定；
- 取证：爆破全程审计事件齐全（`auth.failure`×N → `auth.banned` → `conn.rejected(reason=banned)`），替换计数替身逐项断言。

### S4 路径穿越（"外部输入→本地路径"写入点排查 + 规范化防护 + 实测）

| 写入点 | 路径来源 | 结论 |
|---|---|---|
| `banlist.json` | `paths.banlist_file()`（数据根派生） | **无注入面**；恶意"IP"串（`../`、`\`、`/`）只作 JSON 键存活、零路径副作用；被投毒的落盘文件重新 load 时非字符串键/畸形值一律丢弃（值→路径通道封堵） |
| 审计日志 | `configure_logging(log_file)`（数据根派生 logs/rdc.log） | 无注入面 |
| 用户证书 | `Settings.tls_cert_path/key_path`（**config.json 外部可写**） | **批 5 新增防护**：`require_within_data_root`（resolve 规范化 `..`/展开符号链接 + 数据根前缀判定 + 跨盘符拒绝），接入 `HostService._prepare_certificates`；实测 `..` 逃逸/绝对路径越界/根内合法路径三态正确，**越界文件读取副作用不发生**、错误串不回显文件内容 |
| 配置写回 | CLI `--config` 显式给定（设计如此） | 原子写（tmp+`os.replace`）实测无撕裂残留 |
| **文件传输** | — | ⚠️ **阶段 8 未实现，无真实落地面——待阶段 8 补测**（用户确认的疑点 1 处理口径；测试以显式 skip 声明，不算通过） |

已知限制：符号链接逃逸用例在 Windows 非管理员环境 skip（系统不允许创建 symlink），其判定逻辑（resolve 后前缀校验）由其余穿越用例覆盖。

## 5. bandit + pip-audit 结果（B11）

| 时点 | High | Medium | Low | 说明 |
|---|---|---|---|---|
| 批 0 基线 | 0 | 2 | 53 | 计划：批 1-4 以删除 + nosec 收敛清零 |
| 批 5 复跑（抑制前） | 0 | 1 | 45 | 差额构成与偏差如实记录见 §1 澄清段 |
| **批 5 收口（nosec 补齐后，`dde0b8a`）** | **0** | **0** | **0** | 46 处全部 `# nosec + 逐处理由注释`：B104×1（产品语义监听全网卡）、B101×11（内部不变量断言）、B105×2（清空赋值/键名常量非凭据）、B112×1（多网关轮询设计）、subprocess 组×31（固定 argv 不经 shell，launchctl/systemctl/pmset/shutdown，关机/重启调用点 UI 二次确认兜底） |

- **验收硬线**：`bandit -r src/` High 0 ✅（全绿抑制、非静默——误报留注释证据，兑现 SECURITY_BASELINE"收尾判定口径"）。
- **pip-audit**：`No known vulnerabilities found` ✅（本机包 `rdc` 不在 PyPI 的 skip 属预期）。
- 协议模糊回归：阶段 3 的 2×10,000 例（`test_protocol_framing_fuzz.py`）随全量套件复跑 ✅。
- **过程披露**：批 1-4 未随批落地 nosec（当时全树仅 1 条），收口前发现并汇报，用户决定"现在补齐再收口"——已补齐并复扫归零。遗留提醒见 §8。

## 6. 端到端验证（d 项：自动 + 走查清单）

### 自动部分（loopback 双进程，`tests/e2e/test_loopback_e2e.py`）✅ 三项判据已达成

架构：被控端=**真实子进程**（`_host_runner.py`，与 `rdc serve` 同一 HostService 装配；采集源/输入后端注入替身——用户工作机上绝不截真屏/动真光标；stdout JSON 行协议暴露 published/encoder/inputs/calls 观测面；数据根重定向 pytest tmp）；主控端=**本进程 ClientController**（与 GUI/CLI 同一实现，image_factory 冒烟替身）。

| 判据 | 实测 |
|---|---|
| ① 认证通过 | bcrypt + nonce/HMAC 握手 → `connected`；被控端统计出现控制会话（`on_auth_success` 触发） |
| ② 收到 H.264 帧 | pipeline 真实编码器（hw_probe 选型，本机 libx264）→ 主控端 ≥2 帧、`frame_arrived` 像素 64×64×3 **真实解码**（PyAV rgb24）、被控端 `published>0`、订阅登记=1 |
| ③ 输入被受理 | `send_mouse/send_key/send_text + flush_now` → 被控端替身后端记录 `move(33,44)`、`click×2`、`press(enter)`、`text(hi)` |

### 真机手感走查：**清单已交付，结果持续回报中**

用户执行 45 项走查（`STAGE9_CHECKLIST.md`，STAGE7 §6 口径：鼠标落点/跨屏负原点/中文 IME/剪贴板/卡键兜底/断线重连/托盘/安全体验抽查等）。按约定，走查发现的 ❌ 项单独提交 bug fix 并更新本报告，不混入收口。

**已发现并修复 1 项（2026-10-04，批 4 引入）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1.1 启动 GUI | `rdc` / `rdc --gui` 窗口不出现；asyncio debug 模式抛 `RuntimeError: loop <QIOCPEventLoop running=False ...> is not the running loop`（source_traceback 指 `ui/session.py:151` create_task） | `run_gui` 与 `cli._run_gui_with`（批 4 同批引入，共 2 处同病灶）用**裸 `app.exec()`** 驱动 qasync——跳过 asyncio running-loop 登记。Python 3.14 的 Task 步进 `_check_allowed` 随即抛错，主编排协程（launcher 对话框）第一步即死 → 窗口永不出现。报错时机在 task 首次被 `_SimpleTimer` 回调调度时，**非 create_task 创建时**（故"挪到 QTimer.singleShot(0) 里创建"不解决问题，根因是驱动方式） | 改 `loop.run_forever()`（qasync 内部先 `asyncio.events._set_running_loop(self)` 再进 `app.exec()`，见 `qasync/__init__.py` run_forever）；口径与 PoC `mode_live` 一致。`_run_gui_with` 同时补 `asyncio.set_event_loop(loop)`（原缺失） |

**测试缺口根因**（用户分析确认）：批 4 的 `test_ui_session.py` 只测 `qimage_factory`/`build_client_controller` 纯函数，其 docstring 自注"launcher 主编排端到端由批 5 真机走查覆盖"——即 `run_gui` 的**循环驱动路径从未被自动化覆盖**；既有 GUI 测试均直接构造 widget 不经 `run_gui`。批 5 的 loopback 端到端走 `HostService`/`ClientController` 编程装配面，同样不经 GUI 入口，故双进程测试全绿也漏掉了这个病灶。

**哨兵测试补齐**（`tests/integration/test_gui_startup_smoke.py`，2 项）：offscreen 下走 `cli.main(["--gui"])` 与 `cli.main(["serve","--tray"])` 完整路径，打桩交互点（`launch_choice`/`host_session`）后断言**协程真被调度**。已验证：病灶代码上双红（复现同款 RuntimeError + `calls==[]`），修复后双绿（<1s，5 秒 QTimer 看门狗防挂死兜底）。

**已发现并修复第 2 项（2026-10-05，批 4 引入）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| launcher→主窗切换 | `rdc --gui` launcher 窗口正常出现，点"启动主控端"**瞬间进程崩溃**，traceback 三段：`RuntimeError: libshiboken: Internal C++ object (_SimpleTimer) already deleted` + `Task was destroyed but it is pending! (Task-1, _gui_main)` + `RuntimeError: no running event loop` | 三段互证的根因链：① launcher QDialog 是当时**唯一可见窗口**，accept 关闭 → Qt `quitOnLastWindowClosed` → `app.quit()` → `run_forever` 提前返回，`_gui_main` 正 await `client_session`（`session.py:138`）半路夭折；② 解释器/shiboken 拆除期删 `_SimpleTimer` C++ 对象 → 主窗 `destroyed → stop.set → call_soon` 撞已删 timer（第一炸，唤醒回调丢失）；③ GC 注入 GeneratorExit 拆协程 → finally `await disconnect` → `_teardown` 的 `current_task()` 抛 "no running event loop"（第三炸）。病灶①是第一桶；另注：普通 `QMainWindow.close` 只隐藏不销毁，`destroyed` 唤醒源本就不可靠 | 修复分三点：**(A 根因)** 装配两处（`run_gui` / `cli._run_gui_with`，`host_session` 路径经由同一 `run_gui`）`app.setQuitOnLastWindowClosed(False)`——退出完全由协程链显式控制（stop Event → `app.quit`），对齐 PoC `mode_live` 显式收尾思路；**(B 唤醒源)** `client_session` 主窗接 `WA_DeleteOnClose`，close 即销毁 → `destroyed → stop` 可靠触发（否则 A 修掉后关窗挂死）；**(C 纵深防御)** `controller._teardown` 开头加无运行环守卫（对齐 `_start_reconnect_loop` 的 `e7b6d9c` 修复法），拆环期直接落定不死炸 |

**哨兵测试补齐·第二轮**（`tests/integration/test_gui_handoff_smoke.py`，1 项）：走 `cli.main(["--gui"])` 完整路径——真 launcher 程序化点击"启动主控端" → 断言主窗真实出现 → 程序化关窗 → 断言 `disconnect` 链走完且结束时**运行环仍在**（`_SpyController` 探针显式锁病灶 3）→ 5 秒看门狗防挂死回归。已验证：病灶代码上双红（复现三段 traceback），修复后双绿。

**已发现并修复第 3 项（2026-10-06，阶段 5/6 引入）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1.2 画面显示（窗口模式） | 连接全绿（认证+通道+订阅），但**画面不显示**；被控端 serve 日志 `Task exception was never retrieved`：`pipeline.run()` 抛 `RuntimeError('libx264 打开失败 (955x910)')`（`avcodec_open2` ExternalError）。用户双测锁定：全屏 1920×1080（偶数）正常 ✅ / 窗口 955×910（奇数）无画面 ❌ | H.264 yuv420p 要求宽高均为偶数，奇数尺寸 `avcodec_open2` 直接拒绝。`pipeline._ensure_encoder` 把 mss 抓到的 `frame.width/height` 原样传给 `H264Encoder`，编码链无对齐兜底；首帧即炸 → pipeline 协程终止 → 画面永久不推，但连接层独立存活 → 表现为"连上但无画面"。**非边缘 case**：多显示器单屏、改分辨率、缩放窗口都可能产生奇数尺寸（955×910 即虚拟机窗口模式实测常态） | 落点选**编码层自兜底**（覆盖默认工厂/bench 脚本/硬件编码器全部构造路径，优于仅改 pipeline:295）：`h264.py` 新增 `_align_even`（向下取偶；对齐后 <2x2 抛 ValueError——1x1 无意义），接入 `__init__` / `set_resolution`（先对齐再幂等比较）/ `encode` 自动跟随分支（**先对齐再比较尺寸**——否则奇数源持续进来会每帧误判重建）；`_bgra_to_frame` 对奇数源帧裁掉多余 1 行/1 列后做 cv2 I420 转换（cv2 同样要求偶数）；`EncodedFrame` 改报对齐后尺寸（954×910），与解码端实际输出一致。pipeline 侧零改动，`set_quality` 惰性重建路径天然继承对齐 |

**哨兵测试补齐·第三轮**（8 项）：`tests/unit/test_encoding_h264.py::TestOddSizeAlignment`（7 项）——奇数构造 ctx 尺寸断言、奇数流解码往返、**跟随重建只发生一次**（81×61 持续进来不得每帧重建）、`set_resolution` 对齐+幂等、单边奇数、极小尺寸（2×2 可建可编 / 1×1 拒绝）；`tests/unit/test_server_pipeline.py::TestOddResolutionEndToEnd`（1 项）——真实默认工厂（不经替身）跑 955×911 源帧，断言不再崩且发布对齐尺寸 954×910。已验证：stash 回退修复后病灶代码上 **8 项全红**（复现生产同款 `libx264 打开失败 (955x910)` traceback），修复后全绿；2×2 下限经临时探针实测本机 x264 接受后方入测试。

**已发现并修复第 4 项（2026-10-07，认证流程三项 UX/逻辑缺陷）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1.3 错口令提示 | 服务端审计 `auth.failure reason=bad_password`、协议层 `AuthError('认证失败: 口令校验失败')` 齐全，但 GUI 只显示状态机流转（connecting/reconnecting/disconnected），用户看不到"口令错误" | `main_window._handle_connect_failure` 只看 `auto_reconnect` 开关分流，把一切异常归入 RECONNECTING/泛化"连接失败"，异常携带的原因串被吞 | 失败按异常类型分派：认证/TLS 被拒 → 落定 DISCONNECTED + 状态栏红字直显具体原因；新增 `AuthCancelledError`（AuthError 子类，controller 取消点原抛裸 AuthError）区分"用户主动取消" → 中性"已取消连接"不误报失败；网络类保持原重连策略不变 |
| 1.4 封禁无感知 | 服务端 `auth.ban attempts=3` 后持续 `conn.rejected reason=banned`，但客户端每次点连接仍弹口令框、输入后连接被重置——用户不知被封，反复白试 | 准入拒绝发生在**握手前**：`check_client=False → writer.close()`（批 3 基线设计：被封 IP 不消耗 bcrypt/不发握手帧），客户端只能看到 OSError | 向后兼容的**接线开关**：协议 `AuthResponse` 只增字段 `banned=7` / `ban_remaining_seconds=8`（proto3 默认值不上线，字节快照不变；pb2 经 `scripts/gen_proto.py` 离线重生成）；装配点 `ban_query=self._auth.ban_remaining_seconds` 接线后，命中封禁时服务端先消费对端 AuthRequest 再回一帧 banned 响应（限 `BAN_NOTICE_READ_TIMEOUT=5s`，洪水连接不发帧即静默关，开销与裸关同量级；未接线/非封禁拒绝维持裸关基线）；客户端 `AuthBannedError` 携剩余秒，controller 按主机缓存到期时刻（注入时钟），二次 connect **不发网络包、不弹口令框**直接提示"已被服务端封禁，请约 N 分钟后重试" |
| 1.3 一次点击双重认证 | 同秒日志 `connect_failed → reconnect_attempt(wait=2.0) → reconnect_auth_class_abort → new=disconnected → reconnect_success`；服务端同刻两条 `bad_password`。用户"点 2 次就触发封禁"（阈值 3） | 首连 `AuthError` 在 `auto_reconnect` 下也起环；环内 attempt 立即复用 `_password_holder` 里同一份错误口令再认证一次（计数 ×2，且**不等退避**——`wait` 在 `ReconnectController.run` 里是"失败后 sleep"，attempt 返回 True 直接短路，所以日志 wait=2.0 从未生效）；更糟：环把认证类中止 `return True` 当连接成功，记 `reconnect_success`（日志自相矛盾即此） | 认证/TLS/rdc-ConnectionError 统一为 `_AUTH_CLASS_ERRORS`：首连失败**绝不进重连环**，直接落定 disconnected 上抛（一次点击=恰好一次服务端认证尝试）；环内此类失败改为异常上抛、由环收尾落定，消除假 success；语义统一为"网络类才重试、认证类一律终止"（不烧对端封禁窗口） |

**哨兵测试补齐·第四批**（9 项 + 2 UI 项）：`TestBanCache`（controller 4 项：首响应缓存/二次 connect 弹框前拦截且不建通道/换主机不误拦/假时钟过期放行）、`TestBanNotice`（服务端 3 项三态：接线命中封禁回帧含剩余秒/非封禁拒绝静默/**未接线维持裸关基线**）、`test_transport_handshake.py` 封禁分支 2 项（banned→AuthBannedError 分钟折算文案 + 子类断言）；UI 层缺陷 1 两分支各 1 项（认证被拒红字含原因/取消中性文案）在 `test_ui_main_window.py`。stash 回退行为层三文件验证：病灶上相关哨兵转红（含 `ban_query` 参数不存在、`_ban_host` 缺失、裸 AuthError 文案三种失败形态），修复后全绿。

**已发现并修复第 5 项（2026-10-07，阶段 6 引入；含三项显示观察）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1.2 画面缩放（主） | 主控端 1920×1080 连 954×910 被控端：画面未等比缩放、卡窗口左上、超出部分被原始像素填满（用户截图：右半屏"被照片平铺"观感） | `ScreenView` 默认 `_fit_mode=False`（阶段 6 刻意"远桌面 1:1 左上对齐"语义），且 `fitInView` 只在手动开 fit/全屏时触发——**无 resizeEvent 重算**，拖窗/全屏/分辨率变化都不会更新缩放；背景 brush 走主题深灰非纯黑，黑边观感差 | 默认策略改为**适应窗口**（VNC/TeamViewer 同语义）：`_fit_mode=True` + `AlignCenter` + 纯黑 `setBackgroundBrush` + `SmoothPixmapTransform`；新增 `resizeEvent` 在视口变化时重算 `fitInView(KeepAspectRatio)`；`present_image` 仅在**分辨率变化**时重算（每帧 fitInView 会致矩阵抖动），1:1 模式保留（显式 `set_fit_mode(False)` 回左上+滚动条，对齐方式随模式切换）；全屏切换不再动 fit（旧版退出全屏强切 1:1 会把窗口态带回老 bug） |
| 显示观察（次） | ① 静止画面状态栏"0.0 FPS"观感差；② 画质指示"较差"红点但画面清晰；③ 状态栏无远端分辨率 | ①② framerate 控制器 idle 降载（画面静止 0 帧属正常），但 UI 把 0 帧/0fps 当"质量差"渲染；③ 指标链路有 `resolution` 字段、状态栏无展示位 | ① `StatusBar._tick_fps` 静止时显示 `idle`；② `QualityIndicator.update_quality` fps≤0 判为 idle 档（等级只看延迟/丢包，详情 `Nms \| idle`，v4 阈值表其余不动）；③ 状态栏新增分辨率标签（`metrics_updated` 接线、断开清除） |

**哨兵测试补齐·第五批**（1 项新增 + 3 项适配）：`test_fit_mode_black_borders_out_of_bounds`——默认 fit 断言 + 100×100 帧进 400×300 视口缩放因子 =3（撑满高度维）+ 中心点坐标映射正确 + 左右黑边 `remote_pos` 返回 None（病灶上默认 1:1 直接红）；既有 2 个 1:1 坐标用例显式 `set_fit_mode(False)` 保留原语义验证；`test_fullscreen_toggle_pair` 重写为"fit 常驻、切换只动窗口模式"。

**已发现并修复第 6 项（2026-10-08，阶段 5 采集层引入；P0 可用性）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1.2 UAC 后画面永久停 | 被控端弹 UAC 点"是"后，主控端画面永久停在提权前最后一帧（控制同步失效），服务端持续报"屏幕采集失败 BitBlt" + "SendInput 部分失败"，直至会话结束不恢复 | **三层病灶**：① mss MSS() 的 GDI 句柄在安全桌面切换后**永久失效**，而 v5 采集源从不重建（阶段 5 未覆盖）；② pipeline `_produce` 对 ScreenCaptureError 零处理 → **采集 task 直接崩溃**（"Task exception was never retrieved"），画面流永久停摆；③ 安全桌面期间 SendInput 必然失败（Windows 安全边界，非缺陷），但每事件刷一条 error 日志 | **自愈闭环 + 状态可观测**：① `mss_source._grab_bgra` 失败路径挂 `_rebuild_backend`（close+重建 MSS()，保留选屏索引；重建失败置空句柄、下一帧入口继续重试自愈）；② `pipeline._produce` 捕获采集异常返回 IDLE+指数退避（0.25s→2s 封顶），**不再崩 task**；连续 3 次失败沿边触发 `capture_stall_hook`、恢复时通知（事件循环线程消费边沿，executor 线程零回调红线）；③ 控制端口新增 `HostStatus`（payload 0x14，proto 只增）下行：service 把停滞/恢复 + `OpenInputDesktop` 桌面探针判定（`capture/desktop.py` 新模块，非 Windows/任何 API 失败一律 False）广播到全部控制会话；④ 注入失败降级 warning + 同类 5s 节流（被抑制条数计入下条日志）；⑤ UI：controller `_on_control_message` 解析 → `host_status` 事件 → 状态栏 warn 直显"远端处于安全桌面（UAC 确认中），画面暂停——操作完成后自动恢复"，恢复回常态 |

**哨兵测试补齐·第六批 P0**（21 项）：`TestHandleRebuild` 采集重建 3（失败即重建+旧句柄关闭/选屏索引保留/重建失败下帧自愈）+ `TestCaptureBackoffAndStall` 退避回调 4（失败返回退避不崩/翻倍封顶 2s/阈值 3 触发一次+恢复通知/回调在事件循环线程红线）+ `test_capture_desktop.py` 探针 8（平台门/windll 缺失/句柄 0/查询失败/CloseDesktop 抛错吞掉/winlogon 与 Default 检出）+ `TestHostStatusBroadcast` 集成 2（真实控制会话收到下行含 secure_desktop 归因与恢复 OK/无会话不抛）+ controller 入站 2 + UI 提示 2。stash 回退行为层四文件双红验证：**8 项在病灶上转红**（含 task 直接崩、`capture_stall_hook` 参数不存在、`HostStatus` 被 `control_unexpected_inbound` 吞三种失败形态），恢复后目标 108 项全绿。

**v5.1.0 特权操作范围（本批记入文档，不在 v5.0.0 实现）**〔2026-10-17 走查第九批扩充第 ⑥ 项"完整 Win 键处理"，最终 6 项清单见本节第九批段末〕：v5.0.0 对安全桌面的正确行为止于"如实提示 + UAC 结束后自愈"——进入安全桌面（看/操作 UAC 弹窗本身）受 Windows 安全边界限制，需要：① Windows 服务化（SYSTEM 权限，Session 0 常驻）；② UIAccess=true 声明（EV 代码签名证书 + 受保护安装目录）；③ 安全桌面内画面采集与键鼠注入（TeamViewer "支持 UAC" 同级能力）；④ 驱动级采集兜底（如 BitBlt 长期不可用场景，按需）；⑤ 服务 ↔ 用户态会话通信通道。

**第六批其余五项（P1/P2，逐 bug 独立 commit）**：

| bug | 现象 | 根因 | 修复 | commit |
|---|---|---|---|---|
| 2（P1）键盘标点无效 + Tab/Win 泄漏 | 标点 `!@#$%^&*()_+-={}[]\|;:'",.<>?~\`` 全部注入失败；Tab 走本地焦点导航、Win 触发主控端开始菜单 | 两层：① `_VkKeyScanW` 未声明 `restype=c_short` → ctypes 读全 32 位寄存器，高 16 位垃圾使未移位标点 `; , . /` 也被判 `scan>0xFF`；且 `_resolve_key` 对 `scan>0xFF`（含 shift 状态位）一律拒绝——但物理键模型下 Shift 是**独立键事件先行注入**，远端已持修饰态，只需基键 VK；② 控制态点击画面未抓键盘焦点，Tab/Win 落工具栏/窗口 | ① `_VkKeyScanW.restype=c_short`；`_resolve_key` 取 `scan & 0xFF` 基键（不再拒 shift 符号）；② `mousePressEvent` 控制态 `setFocus()`，配合 `keyPressEvent` 已有 `accept()`；③ 未映射键日志 debug→warning（可观测） | `71cf4a5` |
| 3（P1）封禁告知未生效 | 服务端封禁生效，客户端得 `ConnectionResetError('Connection lost')` 而非 `AuthBannedError`，反复白弹口令框 | **真机竞态**（集成测试未暴露）：客户端 `connect()` 是 TLS→**弹框等真人打字**→才发 AuthRequest；服务端 `_reject_control` 回帧读窗口旧固定 5s < 真人打字耗时 → 超时裸关 → 客户端把 AuthRequest 发进已关连接得 RST，banned 帧从未送达、本地缓存永不建立 | `BAN_NOTICE_READ_TIMEOUT` 对齐 `HANDSHAKE_TIMEOUT`(10s)；回帧成功走 `FrameStream.close()+wait_closed()` 优雅冲刷 TLS（对齐正常握手拒绝路径），未回帧仍裸关维持零响应基线 | `bb9655d` |
| 4（P1）异常断连修饰键残留 | 按住 Win→拔网线→远端 Win 未释放（Win+D 触发显示桌面）；正常"点断开"无卡键 | `_serve_control` 的 `await next_message()` 无超时：静默断网无 FIN/RST（TCP 半开），阻塞到 OS keepalive（Windows ~2 小时），`finally: _reset_input()` 迟迟不触发 | 控制环加读空闲上限 = `heartbeat_timeout`(默认 10s，客户端每 3s 必发 PING，静默即真死不误伤)；静默超时 `break` → finally reset，卡键窗口从 ~2h 收敛到 ~10s；`conn_close` 加 `reason=idle_timeout` | `cdf5dd1` |
| 5（P2）断连 `ScreenView already deleted` | 断连日志 `event_hook_failed ... Internal C++ object (ScreenView) already deleted` hook=state_changed | `client_session` 给主窗挂 `WA_DeleteOnClose`：关窗销毁子控件树，controller 仍持窗口 Python 引用，随后 disconnect 收尾/画面 EOF 的回调直达已删对象 | 两层防御：① 全部 controller→UI 事件入口加 `_ui_alive()`（`shiboken6.isValid`）守卫，销毁后 no-op；② `closeEvent` 先 `set_events(None)` 解绑再调度断开（协议签名放开 None） | `5e85dff` |
| 6（P2）封禁期无倒计时 | 封禁期间连接按钮不禁用、无倒计时，用户反复点入被拒 | UI 收到 `AuthBannedError` 只落红字，未压制按钮/未倒数 | `_handle_connect_failure` 先于 AuthError 分支拦截 `AuthBannedError`（其子类）→ 墙钟 QTimer 倒计时（起点=服务端回传 `remaining_seconds`）：禁用连接按钮 + 状态栏"X 分 Y 秒后可重试"每秒刷新，归零自动恢复；重复被拒复用同一定时器取最新剩余秒；连接成功/关窗清残留；toolbar 加 `set_connect_enabled()` | `9c09e48` |

**哨兵测试补齐·第六批 Bug2-6**（每项 stash 双红验证后转绿）：Bug2 全量标点参数化 32 + restype 锁定 + UI 点击抓焦（病灶 23 项转红）；Bug3 窗口不变式 + 窗口内/外慢客户端竞态 3（病灶上不变式 `assert 5.0==10.0` 转红）；Bug4 静默半开链路 idle→reset + 活链路不误伤 2（病灶上以 5s 断言超时收场，测试自身已改为 finally 先关客户端防挂死套件）；Bug5 销毁后 state/frame/metrics/host_status 四回调 no-op + closeEvent 解绑 3；Bug6 倒计时禁用/每秒刷新/归零恢复/重复触发复用/连接成功清表 4。全量 **1508 passed / 2 skipped**（P0 后 1462 → Bug2 +34 → Bug3 +3 → Bug4 +2 → Bug5 +3 → Bug6 +4）。

**第六批复测回归（2026-10-17，Bug2 修复的配套遗漏；commit `333ab13`）**：

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 3.1/2.7 键盘标点（复测） | `71cf4a5` 修复后重测，标点**全部**报"键 '!' 在当前键盘布局无对应 VK"（比修复前更彻底地失败：修复前未移位标点可用） | `71cf4a5` 给 `_VkKeyScanW` 补了 `restype = c_short` 但**漏了 `argtypes = [c_wchar]`**：ctypes 缺省把 `str` 当 `char*`（ANSI 指针）编组，而 VkKeyScanW 期望**按值** WCHAR（UTF-16）→ 参数错位 → 恒返回 -1 → `_resolve_key` 的 `scan < 0` 分支拒绝一切非表内字符。用户手动 ctypes 对比实锤：无 argtypes 得 -1，声明 `c_wchar` 后 `'!'` 得 0x0131 | ① `windows.py:155` 补 `_VkKeyScanW.argtypes = [ctypes.c_wchar]`（一行解锁全部标点）；② 顺带补齐 `_GetSystemMetrics` 的 `argtypes=[c_int]`/`restype=c_int`（审计发现其原本两者皆缺——低 16 位返回值恰好不被垃圾污染故未爆雷，但属同类隐患）；③ `_SendInput` 声明经审计本就完整，无需动 |

**为什么单测没抓到（决策点 1 的代价，如实记录）**：`71cf4a5` 的 restype 锁定测试只断言了 `restype`；全量标点参数化测试打桩了 `scan_char`（假时钟替身表），真实 ctypes 参数编组路径在决策点 1（"单测不打真机"）下从未被自动化覆盖——这正是打桩测试测不到的维度。

**哨兵测试补齐·复测回归**（3 项，均 stash/临时回退双红验证后转绿）：`test_vkkeyscanw_argtypes_declared`（声明锁定，病灶上 `None != [c_wchar]` 红）；`test_win32_api_declarations_complete`（装载区**全部三个** windll 入口 argtypes+restype 完整性审计哨兵——任何新增入口"声明一半"都转红）；`test_scan_char_real_call_resolves_punctuation`（**真机 ctypes 调用**、不打桩：`_win_scan_char(';')`/`('!')` 断言 >0 且 `'!'` 与 `'1'` 基键一致；病灶上精确复现 `assert -1 > 0`。VkKeyScanW 为只读查询 API，不动光标不注入输入，不违反决策点 1 的"不打真机 SendInput"本意——此为本决策点首个登记在案的例外）。全量 **1511 passed / 2 skipped**（1508 + 3），三绿退出码 0。

### 第九批：Win 键释放被系统吞掉 → 被控端永久按住（2026-10-17，P1；commit `8fd2ab1`）

| 项 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 3.7 组合热键（Win 单键/组合） | 主机按两下 Win：主机侧弹菜单→关菜单（正常两次 Win 行为），**被控端 Win 进入永久"按下"状态且不弹菜单**。用户归纳的全部 Win 现象同源：① 单按 Win → 按下转发/释放丢失 → 永久按住 → 不是"短按"故不弹菜单；② Win+V → 主机弹剪贴板（系统处理 V）、被控端弹开始菜单（按住态被后续机制/超时松开的表象）；③ Win+X/Q/W → 整个组合被 Windows 抢走，rdc 收不到任何事件 → 被控端无反应。**影响严重**：按一次 Win 后，被控端后续每个按键都变成 Win+组合，会话基本不可用 | Windows 对 Win 键的**系统级处理**：Shell 接管该键用于开始菜单/热键，Qt 应用层**收不到 WM_KEYUP**（`keyReleaseEvent` 不投递）→ ScreenView 只有按下被转发（`keyPressEvent` → `send_key(win, True)`），释放永远不到 → 被控端 `press(win)` 无对应 `release(win)`，修饰键状态常驻。**非 rdc 代码缺陷**，是应用层键盘事件的固有限制（低级键盘钩子 WH_KEYBOARD_LL 才能拿到完整按下+释放） | **v5.0.0 应急兜底（controller 层，用户建议 A+B 合并为确定性机制；C 复用既有断连 reset 链路）**：① `send_key` 对"监视热键"记按下时刻（`_sticky_held[name]=clock()`），真实释放到达即销账；② `_reconcile_sticky_keys()` 在**每次键出批前**（`send_key`，= 建议 B"收到其他键时先释放可疑的 Win"）与**冲刷环每拍**（`_flush_loop`，= 建议 A"N 秒内没收到释放即主动释放"的周期形态，覆盖用户不再敲任何键的静置场景）对账：按住超 `_STICKY_HELD_TIMEOUT_S=2.0s` → 喂一条 `KEY_RELEASE` 并清账（每次按下至多补发一次，防重复注入；真实释放若迟于补发到达，对被控端幂等无害）；③ **监视集合只含 `win`**——Shift/Alt/Ctrl 长按是合法用法（Shift 连打大写、AltGr 打字、Ctrl 游戏组合键），纳入必然误伤，且用户实测确认丢释放的仅 Win；④ 记账在 `_teardown`/`set_control_enabled(False)` 清空（防陈旧释放跨重连残留、向新会话补发无来源释放）；⑤ **配套不对称病灶**（同批发现）：`MainWindow` 只覆写 `keyPressEvent`（一律 press=True）却无 `keyReleaseEvent`——焦点不在 ScreenView（如停在工具栏）时**每个键**都是"按下已转发、释放被 Qt 默认实现丢弃"→ 同样卡键；补对称覆写（`_controller_send_key(event, press)` 参数化） |

**落点取舍（为何 controller 层）**：兜底要同时覆盖"敲下一个键即解锁"（建议 B）与"静置 2s 自动解锁"（建议 A），前者需在下一次键事件的**出批顺序**上严格前置、后者需要**周期检查点**——controller 是唯一同时持有注入时钟（假时钟可测）与 coalescer 出批通道的层；UI 层无时钟注入点、`WindowsBackend` 在**被控端**，拿不到主控端的按键时序。

**为何不在 v5.0.0 做低级钩子**：`WH_KEYBOARD_LL` 需在主控端常驻全局钩子线程（影响本机输入延迟、须处理钩子超时与线程消息泵），且会把"rdc 未连接时也拦截全机 Win 键"的副作用引入用户桌面——超出走查应急范围，按用户决定归 **v5.1.0**。

**哨兵测试补齐·第九批**（8 个用例=控制器 6 + 主窗 2；主窗第 2 项由既有 `test_key_press_ignored_when_not_connected` 改名扩写，故全量净增 **+7**；病灶回退双红验证：禁用兜底后"注入/解锁"3 项转红、stash 窗口层修复后释放成对哨兵转红，恢复后全绿）：
- `TestStickyKeyReconcile::test_win_press_then_fast_release_no_injection`——未超阈值的正常短按**不得**注入（防误伤）；
- `::test_stuck_win_unlocked_by_next_key`——超阈值后下一个键之前严格先补发 `RELEASE win`（建议 B，顺序断言）；
- `::test_stuck_win_unlocked_by_flush_loop_when_idle`——静置场景按环节拍解锁 + 第二次对账不重复注入（建议 A + "至多一次"语义）；
- `::test_shift_long_hold_not_injected`——Shift 长按不注入（监视集合收窄口径锁定）；
- `::test_teardown_clears_sticky_bookkeeping` / `::test_control_off_clears_sticky_bookkeeping`——两处清账（防跨会话残留）；
- `test_connected_control_key_release_forwards`——`QTest.keyClick` 天然发出 press+release 对，断言**成对**转发（病灶态 release 半边缺失即红）；`test_key_release_ignored_when_not_connected`——未连接态不转发。

全量 **1518 passed / 2 skipped**（1511 + 7），三绿退出码 0。文档：README 新增"已知限制（v5.0.0）"章节（Win 键限制 + 2s 兜底行为 + 单键 Win 语义不可实现 + Win+X/Q/W/V 被系统消费）与安全桌面限制。

**v5.1.0 特权操作范围（本批扩充，用户指定记入）**：在第 6 批 P0 段所列 5 项（服务化 / UIAccess / 安全桌面采集与注入 / 驱动级采集兜底 / 服务↔用户态通道）之外，本批新增第 ⑥ 项：**完整 Win 键处理**——低级键盘钩子 `WH_KEYBOARD_LL` 拦截 Win 的**按下与释放**并转发，实现单键 Win（开开始菜单）与 `Win+组合` 的真正远程语义；钩子须限定"仅 rdc 会话激活时生效"，避免未连接时拦截本机 Win。

### 第十批：Tab/Backtab 被 Qt 焦点链提前消费 → 控制模式下 Tab 泄漏（2026-10-17，P2；commit `fd6d1ad`）

**复测结论（第九批后）**：标点全部正常 ✅、Win 卡键已解决（2s 自动松开）✅、其余键盘正常 ✅——第六批 `71cf4a5`/复测回归 `333ab13`/第九批 `8fd2ab1` 三连修复生效。残留：**Tab 按下虚拟机无反应，主机 GUI 焦点跳转**。

**根因（探针实测复现）**：Qt 在 `QWidget::event()` 内部把无修饰 Tab/Backtab **直接转成 `focusNextPrevChild()` 焦点导航，发生在 `keyPressEvent` 之前**。因此：
- 第六批在 `ScreenView.keyPressEvent` 里 `event.accept()` 并注释"Tab 不再跳转"——**误判**（焦点在画面时事件根本走不到 keyPressEvent；裸视图单测焦点链内无可跳转目标，`focusNextPrevChild` 天然失败才回落 keyPress，测试假绿——与第六批 argtypes 同类教训：**哨兵必须跑在病灶场景的装配层**）；
- 第九批补的 `MainWindow.keyPress/keyRelease` 覆写同样拦不住（焦点在按钮时按钮先消费）。
- 探针双场景证据：①焦点在画面 → 按 Tab 焦点跳走、零转发；②焦点在工具栏 → 同样跳走、零转发。

**修复（两层）**：
1. `ScreenView.focusNextPrevChild` 控制态返回 `False`（焦点在画面时导航被拒，按键按普通键继续投递 → keyPressEvent 正常转发）；
2. 应用级 `MainWindow.eventFilter`：拦本窗控件树（含自身）内 Tab/Backtab 的 KeyPress/KeyRelease 成对转发并吞掉——覆盖焦点在**任意**子控件的场景；三条件门控（已连接 + 控制开启 + 属本窗树），口令/设置/指纹对话框是独立顶层窗不受影响，控制关闭时本窗导航如常；`closeEvent` 卸载过滤器（同第六批 Bug5"先解绑回调源"纵深）。

**哨兵测试补齐·第十批**（5 项，`test_ui_main_window.py::TestTabFocusGuard`；病灶回退双红验证：stash 两 UI 文件 → 3 转发类转红 + stash 仅 screen_view → 覆写契约项转红，恢复后全绿）：
- `::test_tab_forwards_pair_screen_focus`——焦点在画面：Tab 成对转发 + 焦点不跳（病灶态 `c.keys == []` 且焦点跳走）；
- `::test_tab_forwards_pair_toolbar_focus`——焦点在工具栏：同样成对转发、焦点保持（应用级过滤器覆盖证明）；
- `::test_shift_tab_forwards_pair`——Shift+Tab（offscreen 平台发 Key_Tab+Shift 形态）Tab 自身成对转发、无反向导航；
- `::test_tab_navigates_normally_when_control_off`——控制关闭：零转发 + 本地导航恢复（不误伤）；
- `::test_view_refuses_navigation_only_when_control_on`——视图覆写本体两分支直调探询（组装场景过滤器先吞事件，覆写是唯一独立防线，需单独锁定）。

全量 **1523 passed / 2 skipped**（1518 + 5），三绿退出码 0（mypy 中途抓到 `QApplication.instance()` 返回 Optional 的 1 个类型错误，已补 None 守卫复绿）。controller 零 Qt 红线不受影响（本批改动全在 UI 层）。

**走查其余项**：第五批修复后 1.2 画面显示完整闭环（缩放策略 + 奇数分辨率 + 认证体验）；第六批修复后 **UAC 场景**（1.2）、**键盘标点/Tab/Win**（2.7/3.x）、**封禁体验**（1.4）、**异常断开卡键**（1.7）待重测（窗口模式 + UAC → 状态栏提示"安全桌面（UAC 确认中），画面暂停" → 结束后自动恢复；封禁期连接按钮禁用带倒计时）。**键盘标点已复测一次**：检出 `71cf4a5` 配套遗漏（`_VkKeyScanW` argtypes 缺失）致标点全灭，已二次修复（`333ab13`，见上"复测回归"段）。**Win 键同批复测另立第九批**：第六批的抓焦修复解决的是"Win 触发**主控端**开始菜单"（泄漏），复测确认**被控端**仍卡按住——根因是系统吞掉释放事件（焦点修复救不了，事件根本不投递），已按第九批落 2s 超时补发兜底（`8fd2ab1`）。**第九批修复后 Win 键重测预期**：按一次 Win → 被控端约 2s 内自动松开（后续按键不再变 Win+组合），但**开始菜单不会在被控端弹出**、`Win+X/Q/W/V` 仍被主控端系统消费不转发（v5.0.0 已知限制，README 同口径；完整支持归 v5.1.0）。**第九批复测通过（标点/Win/其余键盘全 ✅），Tab 残留另立本第十批**（`fd6d1ad`）：**第十批修复后 Tab 重测预期**：控制开启时按 Tab/Shift+Tab → 虚拟机焦点移动（表单/窗口内切焦点），主机 GUI 焦点**不再**跳转；控制关闭时主机 Tab 导航如常。封禁阈值维持 3 次/5 分钟不变（缺陷 3 修复后计数语义已恢复"点击数=失败数"，且阈值已是 Settings 字段 `max_auth_attempts`/`auth_ban_duration`，用户如需放宽改配置即可）；再发现 ❌ 项按同一方式处理。性能"卡"问题已确认属虚拟机瓶颈，不进走查阻塞项，留阶段 10 物理机验证。

## 7. 验收数据收口

```
ruff:   All checks passed!（全仓）
mypy:   Success: no issues found in 99 source files
pytest: 1417 passed, 2 skipped, exit 0
        （任务书基线 1137 → 批 4 1382 → 批 5 +35：e2e 1 + 渗透 19(+2 skip) + 覆盖率补测 15）
bandit: High 0 / Medium 0 / Low 0（全部 nosec 抑制，附理由）
pip-audit: 0 vulnerabilities
（收口后走查修复终态：pytest 1508 passed / 2 skipped，+47 为六批走查哨兵（第一轮 2 + 第二轮 1 + 第三轮 8 + 第四批 12 + 第五批 1 + 第六批 23〔P0 21 项含新增 desktop.py 探针模块 + Bug2-6 逐项〕，另有若干既有用例按新语义适配改写）；mypy 源文件数 98→99（第六批 P0 新增 `capture/desktop.py` 桌面探针）。第六批 Bug2 复测回归修复后再 +3（argtypes 锁定 + 装载区声明完整性审计 + 真机 VkKeyScanW 调用哨兵）达 1511 passed / 2 skipped；**走查第九批（Win 释放丢失）再 +7**（粘滞热键对账 6 + 主窗释放成对转发 1，另 1 项既有用例改名扩写），终态 **1518 passed / 2 skipped**、三绿退出码 0。**走查第十批（Tab 焦点链泄漏）再 +5**（`TestTabFocusGuard`：画面/工具栏两焦点场景成对转发 + Shift+Tab + 控制关闭不伤导航 + 视图覆写直调契约），终态 **1523 passed / 2 skipped**、三绿退出码 0。controller 零 Qt/Widgets 导入红线**第九批改动后复验通过**（新增逻辑仅用本地整型键码表与注入时钟，grep `PySide6|QtWidgets|QtCore|QtGui` 仍零命中；第十批改动全在 UI 层不触及）。详见 §6）
覆盖率: 总体 89.7%（验收线 ≥70%）
  核心清单（protocol/transport/encoding/server auth+banlist/secrets/config migration）全部 ≥85%；
  批 5 补齐两处缺口：transport/screen_channel 80.3→100%、transport/server 81.3→95.7%
  （tests/unit/test_transport_screen_channel_tls.py / test_transport_server_error_paths.py）
  目标区：security/* + server/auth 达标（password_policy/audit 100%，certs 91.5%，auth 97.3%，banlist 97.2%）
红线:  controller 零 Qt（grep 零命中）；删除遗留面前 git grep 零消费者核验；三绿退出码 0
```

## 8. 遗留清单（明确记入）

**用户指定必记两条：**

- **(a) 帧 HMAC 无序号**：v5 帧签名覆盖头部+载荷但**不含 seq/时间窗**，同会话原样重放不可检测（§4 S1 实测）。当前业务面后果为幂等噪声级；**阶段 8/10 引入任何有状态操作（文件传输、指令序列、转账式语义）前，必须先补帧序号/滑动窗口重放防护**。
- **(b) 文件传输面渗透待阶段 8 补测**：路径穿越四项中"文件传输"面（外部文件名/路径→本地落地）无实现载体，`test_file_transfer_surface_pending_stage8` 显式 skip 声明；**阶段 8 实现文件传输时必须补目标目录沙箱根校验 + 穿越/符号链接/名规范化实测**，属该阶段验收项。

**其余跟进（非阻塞）：**

| # | 项 | 归属 |
|---|---|---|
| 1 | 真机走查 45 项：**已回报七批**（第一~六批 + 第六批复测回归 + 第九批）——第一轮 1.1 GUI 启动、第二轮 launcher→主窗切换崩溃、第三轮 1.2 奇数分辨率编码器崩溃、第四批认证三缺陷（错口令无提示/封禁无感知/一次点击双重认证）、第五批 1.2 画面缩放策略、第六批 P0 UAC 安全桌面画面永久停 + Bug2-6（键盘标点/Tab·Win 泄漏、封禁告知竞态、异常断连卡键、断连回调已删窗、封禁倒计时）均已修复（§6）；第六批 Bug2 复测检出配套遗漏（argtypes 缺失）已二次修复 `333ab13`；**第九批 3.7 Win 键释放被系统吞掉致被控端永久按住（会话基本不可用）——v5.0.0 已落应急兜底（按住超 2s 自动补发释放，`8fd2ab1`），完整支持（低级键盘钩子）归 v5.1.0，README 已记已知限制**；哨兵累计 **57 项入仓**（六批 47 + 复测 3 + 第九批 7；第九批主窗第 2 项系既有用例改名扩写，不计新增）；1.2 窗口模式/UAC、1.3/1.4 认证与封禁体验、1.7 异常断开、2.7/3.x 键盘（标点需二次重测）、**3.7 Win 键（需验证 2s 兜底解锁生效，并确认"按下+超时补发释放"的实际观感）**待用户重测确认，4.x/5.x/6.x/7.x 走查结果待继续回报 | 走查进行中 |
| 2 | 符号链接逃逸实测用例在本机 skip（非管理员无 symlink 权限），CI/开发者模式环境自动生效 | 阶段 10 CI |
| 3 | 硬编选项映射为空（`_hardware_options` 不发明参数），硬件编码器真正启用需逐卡标定 | 阶段 10 |
| 4 | qasync 官方声明 <3.14；本机 3.14.3 四判据实测通过，3.16 时切换 PoC 保留方案乙 | 阶段 10 / 环境升级 |
| 5 | `host/netinfo.py` 宽异常批 5 仅加抑制注释未收窄（行为变更不进收口），v4 遗留面重构挂起 | 阶段 10 |
| 6 | **新代码纪律**：批 3/4 教训——新写的内部不变量 assert 请当场 `# nosec B101 + 理由`，避免再次堆积 | 持续 |
| 7 | 剪贴板图片/文件**字节**传输、分块重组（协议字段已就绪） | 阶段 8 |
| 8 | 性能"卡"：虚拟机瓶颈所致（用户已确认，走查中不阻塞），留阶段 10 物理机复测 | 阶段 10 |
| 9 | **Win 键完整支持**：v5.0.0 仅有"按住超 2s 自动补发释放"应急兜底（§6 第九批 `8fd2ab1`）——单键 Win（开开始菜单）语义与 `Win+组合` 转发不可用；正解=低级键盘钩子 `WH_KEYBOARD_LL` 拦截按下+释放（限定 rdc 会话激活时生效，防未连接时拦本机 Win），与安全桌面能力同批归 **v5.1.0 特权操作范围**；README"已知限制（v5.0.0）"已向用户声明 | v5.1.0 |

## 9. 阶段 10 起点（打包发布，30-40h）

按 `REFACTOR_PLAN.md` §阶段 10：本机 PyInstaller -D 打包并验证冷启动（<2s 验收线，无 C 编译器约束 Q1 方案 A）、Nuitka 配置交付 CI、更新通道（git 无 remote → 本地 HTTP 模拟 Release 验收 Q5）、GUI 安装物与卸载、macOS/Linux 平台代码 CI 补验（R6）。

进场携带：§8 遗留 1（走查结果）、遗留 (a)(b) 的归属阶段决策确认；性能基线沿用（libx264 4K 11.2ms/帧、桌面场景 H.264 比 JPEG 降 96%、硬编不可用探测必须实测）。

---

*本文档由阶段 9 六批 + 收口过程实测数据写成；各批详细设计取舍见 `STAGE9_INPROGRESS.md`（批 0-4 快照）与 git 提交史。*

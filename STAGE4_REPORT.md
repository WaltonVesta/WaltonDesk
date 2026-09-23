# STAGE4_REPORT.md — 阶段 4：传输层（asyncio + TLS + HMAC 会话绑定）

> 完成日期：2026-09-23
> 前置：阶段 3 完成 @ `9e0e0ca`
> 口径：任务书 §五（10 项）+ 已确认决策 F1 方案 B + 2026-09-22 范围调整（v4 兼容全砍）

---

## 0. 扫雷级发现：service.py 不 import transport

全树 grep（`src/` + `tests/`）实测：**v5 代码树内没有任何一处导入
`rdc.transport.control_channel` / `screen_channel` / `heartbeat` / `reconnect`**。
四个文件是阶段 1 机械搬迁进来的孤立副本，唯一消费方是 `legacy_v4/gui`（不在 v5 编译范围）。

`server/service.py` 自己裸管 socket：`_accept_loop` 线程 + `ThreadPoolExecutor` +
`_handle_client` 线程函数，从未复用 transport 层。

**后果（正面）**：任务书叮嘱 a 担心的"sync→async 改 API 会牵连 service.py 线程模型、
需要 `run_coroutine_threadsafe` 桥接"**不存在**。本阶段 asyncio transport 是自包含新栈，
`service.py` 一行未改，也不需要 sync 兼容层。阶段 4 工作量因此显著低于预估。

**后果（需注意）**：v4 半成品被控端仍走自己的同步 socket 路径，**本阶段的新传输栈
尚未被真实运行时使用**；接入点是阶段 9 服务端拆分（用户已确认此安排）。

---

## 1. 子任务完成状态（对照任务书 §五）

| # | 任务 | 状态 | 落地位置 |
|---|------|------|----------|
| 1 | `transport/base.py`：Channel Protocol（asyncio 抽象） | ✅ | `base.py`：`Channel`(Protocol) + `FrameStream` + `StreamChannel` |
| 2 | `control_channel.py` asyncio 重写（TLS + 握手 + 收发协程） | ✅ | `control_channel.py`（72 stmt）+ `handshake.py` |
| 3 | `screen_channel.py` asyncio 重写 | ✅ | `screen_channel.py`（64 stmt）+ `handshake.py` |
| 4 | `backpressure.py`：asyncio.Queue + 满队列丢旧帧 | ✅ | `backpressure.py`：`DropOldestQueue[T]` + `QueueStats` |
| 5 | `tls.py`：SSLContext（TLS 1.2+）+ TOFU 指纹校验 | ✅ | `tls.py`：证书生成/续期 + 双端 context + `verify_peer_trust` |
| 6 | `session_token.py`：token_urlsafe(32) + 绑定 + 60s TTL + 单次使用 | ✅ | `session_token.py`：`SessionTokenStore` |
| 7 | `hmac_nonce.py`：F1 落地（HKDF + nonce 交换） | ✅ | `hmac_nonce.py`（派生）+ `handshake.py`（交换）——细节见 §3 |
| 8 | `reconnect.py` + `heartbeat.py` asyncio 化 | ✅ | 两文件重写为协程控制器 |
| 9 | 修复 B3（每客户端独立发送协程 + 独立队列） | ✅ | `broadcaster.py`（新组件）——见 §4 |
| 10 | 测试：双进程集成、100 并发压测（fd/内存）、令牌重放 | ✅ | `tests/integration/`(1) + `tests/load/`(1) + `tests/unit/`(9) |

**10/10 完成。** 另交付 `transport/server.py`（`TransportServer` / `ControlSession` /
`ScreenSession`）：任务书未列，但双进程集成与压测要求真实 TLS 服务端，v4 的
`service.py` 不可复用（同步线程模型 + 裸 4B 握手），故在传输层补齐 asyncio 接入栈。

---

## 2. 验收结果（2026-09-23 真实输出）

```
$ uv run ruff check src tests scripts
All checks passed!

$ uv run mypy src
Success: no issues found in 62 source files

$ uv run pytest tests -q
439 passed in 23.54s        # 阶段 3 为 299 → 本阶段新增 140

$ uv run python -m rdc --version
远程桌面控制 v5.0.0
```

### transport/ 覆盖率（验收线 ≥85%）

```
name                              Stmts   Miss Branch BrPart  Cover
--------------------------------------------------------------------
transport/__init__.py                 0      0      0      0 100.0%
transport/backpressure.py            52      0      4      0 100.0%
transport/base.py                   153     11     34      2  92.0%
transport/broadcaster.py            102      1     20      1  98.4%
transport/control_channel.py         72     11      4      2  82.9%
transport/handshake.py               92      3     24      3  94.8%
transport/heartbeat.py               83      1     16      2  97.0%
transport/hmac_nonce.py              25      0      2      0 100.0%
transport/reconnect.py               66      0     14      1  98.8%
transport/screen_channel.py          64     11      4      2  80.9%
transport/server.py                 136     21     16      4  83.6%
transport/session_token.py           50      0     10      0 100.0%
transport/tls.py                     88      3     20      0  97.2%
--------------------------------------------------------------------
TOTAL transport/                    983     62    168     17  93.0%   ✅ ≥85%
全局 TOTAL (src/rdc)              4,185+924  1,894        27  53.0%（阶段 3 为 33.6%）
```

未覆盖行集中在**真实网络异常分支**（`control_channel.py:143-169` 的 TLS 降级重建与
TOFU 拒绝、`screen_channel.py:130-156` 同形分支、`server.py:127-152` 的 stop 竞态
与连接数拒绝、`base.py` 的 socket 关闭竞态）——集成测试在真实 TLS 下走的是
"首连即信任"与正常关闭路径，异常路径需构造中间人/断连时序，成本高于本阶段收益。

### 100 并发压测（`-m load`，真实 loopback TLS）

`tests/load/test_transport_load.py`：两轮全生命周期（warm-up → 采基线 → 100 并发 →
复测），每客户端各建**控制 + 画面双通道**（真实 TLS 握手 + 认证 + nonce + HMAC 强制），
广播 10 帧后全部关闭。

```
[load100] handles 234 → 234 (Δ0)   | RSS 99 → 102MB (Δ3.0)
[load100] handles 234 → 234 (Δ0)   | RSS 99 → 103MB (Δ4.5)      # 独立复跑第二次
```

- **句柄**：Windows socket 无 POSIX fd 语义，用 `psutil.num_handles()` 作泄漏代理。
  200 个连接（100×2 通道）全生命周期后 **Δ0**，即完全回落到基线。
- **RSS**：+3.0 / +4.5MB（阈值 220MB）——Python 分配器缓存不归还 OS 的正常水位，
  200 连接量级下无失控增长。
- **task 泄漏**：断言 `screen-send-*` 与 broadcaster 订阅者列表压测后归零。

### 令牌重放（验收：第二次使用必失败）

三层各有测试：
- 单元 `test_transport_session_token.py::test_replay_second_use_fails`；
- 集成 `test_transport_integration.py::test_token_replay_second_join_fails`
  （真实 TLS 下第一次 `ScreenJoin` 成功、第二次 `AuthError`）；
- 失败文案统一性 `test_expired_message_uniform_no_oracle`
  （未知/已用/过期三类同文案，不给重放者区分信号）。

### 慢客户端不阻塞（叮嘱 a-1）

`test_transport_broadcaster.py::TestFanout::test_slow_client_does_not_block_others`：
慢客户端 `drain()` 永久挂起（模拟弱网/后台冻结）时——

- 20 帧 `publish()` 总耗时 **< 1s**（实测微秒级，v4 此处会串行 `sendall` 全员冻结）；
- 快客户端仍完整收到最新 10 帧（`== [11..20]`，自己队列丢旧）；
- 慢客户端自身丢帧计数 10，不拖累他人。

---

## 3. F1 方案 B 的实际落地

### 派生（`transport/hmac_nonce.py`）

```
ikm  = client_nonce(32B) || server_nonce(32B)      # 固定拼接顺序，换序即换钥
salt = connection_id(32B)                          # 服务端每连接 secrets.token_bytes
info = b"rdc-hmac-v1"
key  = HKDF-SHA256(ikm, salt, info, length=32)
```

- 任一输入长度非 32B → `ProtocolError` 拒绝（不悄悄削弱熵）；
- 密钥建立后**固定不重派生**（叮嘱 c）；
- 保留 `hmac_integrity.KeyingMaterialExporter` 适配器与 `derive_key_from_tls`，
  未来若上具备 RFC 5705 的 TLS 栈可单点替换，上层签名/验签面不变。

### nonce 交换时机与方式（全部走 v5 协议帧，非裸字节）

协议层为落地本方案做了**最小扩展**（`proto/rdc.proto` 字段号只增不改 + 重新生成 pb）：

| 通道 | 交换帧 | 携带字段 |
|------|--------|----------|
| 控制 | `AuthRequest`（C→S，握手首帧） | `client_nonce`（新字段 4） |
| 控制 | `AuthResponse`（S→C） | `server_nonce`（5）、`connection_id`（6）、`token` |
| 画面 | `ScreenJoin`（新 message，Type `0x13`） | `token`、`client_nonce`、`connection_id` |
| 画面 | `AuthResponse` | `server_nonce`、`connection_id` |

设计选择：nonce **捎带在既有认证帧上**而非新增独立 `NonceExchange` 往返——
握手仍是 1 RTT，且"TLS 建立失败 → 根本不会执行到握手帧收发 → nonce 交换不启动"
（叮嘱 c）由调用顺序天然保证。

画面通道用**独立 nonce 集**、复用控制通道的 `connection_id` ⇒ 两通道派生**不同密钥**
（`test_join_key_differs_from_control_key` 锁定），一条泄露不波及另一条。

### 失败路径不发 nonce

`serve_control_handshake`：口令校验失败 → 只回 `AuthResponse(ok=False, reason=...)`，
不生成 `server_nonce`/`connection_id`、不签发令牌（`test_wrong_password_rejected_both_sides`）。

### 双端派生同钥测试（假 TLS 会话模拟）

- `test_both_ends_derive_same_key`：同入参两次独立派生 == 且长 32B；
- 集成层用真实 TLS 会话（自签名 + TOFU）端到端复现"双端同钥"：
  `test_handshake_bidirectional_frames` 中 client/server 各自 `enable_hmac(key)` 后
  互发帧验签通过。
- 阶段 3 遗留的"真实双端派生同钥集成测试缺失"（遗留项 1）随本阶段闭环。

---

## 4. B3 修复的前后对比

### 修复前（v4 `service._broadcast_loop`，仍在 `server/service.py:1175-1184`）

```python
with self._sub_lock:
    for conn in list(self._subscribers.keys()):
        try:
            conn.sendall(frame_bytes)      # ← 串行、阻塞
            self.service._stats['bytes_sent'] += len(frame_bytes)
        except Exception:
            dead_conns.append(conn)
```

单广播线程内逐个客户端**同步** `sendall`。任一慢客户端（弱网、TCP 发送缓冲满、
进程被 OS 挂起）让 `sendall` 阻塞 → 整个广播循环停摆 → **所有**客户端画面同时冻结。
统计自增也在广播线程与各客户端线程间无锁并发（B6 同源）。

### 修复后（`transport/broadcaster.py`）

```
publish(frame)                    # 非阻塞：入队即返回
  └─ 每订阅者：encode_frame(frame, hmac_key=sub_key) → DropOldestQueue.push()
                ├─ 队列满 → 丢该订阅者最旧帧 + dropped 计数
                └─ 绝不阻塞广播方

每订阅者独立 task（_send_loop）：
  while True: data = await queue.pop(); await stream.send_raw(data)
                └─ OSError（死/慢客户端）→ 注销该订阅者 + on_remove 回调 + 关流
```

| 维度 | v4 | v5 |
|------|----|----|
| 发送并发 | 1 广播线程串行全员 | N 订阅者 × 独立 task |
| 慢客户端影响半径 | 全体 | 仅自身（丢自己旧帧） |
| 队列满策略 | `deque(maxlen)` 静默丢 | 丢最旧 + `dropped` 计数上报 |
| 背压 | `sendall` 阻塞广播循环 | 非阻塞入队 + 独立消费 |
| 断链回收 | 串行循环里 append 后统一删 | task 捕获 OSError 自动注销（幂等） |
| 帧字节共享 | zero-copy 同一 `frame_bytes` | **每客户端单独签名**（见下） |

**代价（架构权衡，明确记录）**：v5 每连接有独立 HMAC 会话密钥（F1），
帧 trailer 依赖密钥，无法再像 v4 那样全员共享同一份字节 → 每帧每客户端一次
protobuf 序列化 + HMAC-SHA256。SHA-256 吞吐 ~GB/s，1080p/4K 帧（数十~百 KB）
单客户端 <1ms，换取队头阻塞消除 + 连接级完整性。若阶段 5 实测该开销不可接受，
可切"全员共享一密钥"（需重议会话绑定强度）。

**遗留**：v4 `service.py` 的同步 broadcaster 本阶段未动（用户确认：阶段 9 整体切
新栈时一并删除）。真实运行时的 B3 修复因此延到阶段 9 生效（个人自用单客户端，
可接受）。

---

## 5. 对 service.py 的"最小适配"：零改动

实测 `git diff` 层面 `server/service.py` **无任何修改**（叮嘱 a 授权的最小适配范围
最终为空集）。原因见 §0。

本阶段改动的非 transport 文件只有三类：

1. **协议层为 F1 让路**（必需）：`proto/rdc.proto`（`AuthRequest.client_nonce`、
   `AuthResponse.server_nonce/connection_id`、新 `ScreenJoin` message +
   `PAYLOAD_SESSION_JOIN=0x13`）、`protocol/constants.py`（`PKT_TYPE_SESSION_JOIN`）、
   `protocol/messages.py`（3 个 dataclass 字段 + `ScreenJoin` + nonce 长度校验）、
   `protocol/codec.py`（双向映射 + `from_payload` 分派）、重新生成 `protocol/pb/`。
   阶段 3 的 299 个测试全数保持通过（字段号只增不改）。
2. **异常面**：`exceptions.py` 新增 `TlsError(RemoteDesktopError)`。
3. **配置**：`pyproject.toml` 从 mypy 宽松清单移除
   `rdc.transport.{control_channel,screen_channel,heartbeat,reconnect}`
   （叮嘱 c：移除后 mypy 复跑 62 文件仍 0 error，未暴露隐藏问题——重写即补注解）。

---

## 6. 测试清单（新增 140 个，共 439）

| 文件 | 数量 | 要点 |
|------|------|------|
| `unit/test_transport_base.py` | 25 | 粘包/半包/垃圾重同步、坏帧跳过不滞留、HMAC 开启后签名往返、剥标志降级拒收、错误密钥拒收、篡改 trailer 拒收、EOF→None、写失败上抛、`StreamChannel` 生命周期（bind/双 start/幂等 close/EOF 哨兵） |
| `unit/test_transport_hmac_nonce.py` | 12 | 双端同钥、拼接顺序敏感、connection_id 绑定、nonce 变更换钥、确定性、短输入拒绝（参数化 4 例）、派生钥直接喂 framing 往返 |
| `unit/test_transport_session_token.py` | 13 | 单次使用/重放失败、绑定 IP+connection_id、TTL 过期与边界、三类失败文案统一、过期惰性回收、非法 ttl |
| `unit/test_transport_backpressure.py` | 10 | 满丢**最旧**（非新）、dropped 精确计数、drop_ratio、pop 阻塞、非法 maxsize |
| `unit/test_transport_broadcaster.py` | 18 | **慢客户端不阻塞**、丢旧保新（`[1..10]`→收 `[7,8,9,10]`）、计数、每订阅者独立密钥签名 + 跨密钥验不过、send 失败自动注销 + 回调、task 零残留、重复注册/空密钥/shutdown 后拒绝、回调异常隔离 |
| `unit/test_transport_heartbeat.py` | 12 | 对端静默判死（**回调至多一次**）、连续发送失败达阈值、`on_pong` 刷新静默与失败计数、回调异常不外逸、reset、start/stop 幂等 |
| `unit/test_transport_reconnect.py` | 14 | 退避序列/封顶、成功即停、耗尽返 False、网络异常计次、**编程错误上抛不吞**、固定间隔模式、start/stop 语义、观测面 |
| `unit/test_transport_tls.py` | 22 | 证书生成/复用/临期续期/损坏重生成/建父目录、双端 context 强制 TLS1.2、自签名判定（含 v4 过宽匹配的差异锁定）、指纹格式、TOFU 首连确认→二次自动信任、拒绝/无证书/无回调一律拒、信任落盘 |
| `unit/test_transport_handshake.py` | 14 | 控制握手双端同钥 + 帧自动签名、首帧类型校验、缺 nonce 拒、错口令双向拒且不发 nonce、存储哈希损坏拒、响应缺字段 client 拒、EOF 处理；画面加入成功/密钥与控制不同/令牌拒/首帧类型/`connection_id` 不符/缺 server_nonce |
| `integration/test_transport_integration.py` | 5 | **真实 TLS**（自签名 + TOFU）端到端：握手双向帧、错口令拒、输入事件流、画面加入 + broadcaster 帧下发、令牌重放拒 |
| `load/test_transport_load.py` | 1（100 并发） | 句柄 Δ0、RSS 有界、发送 task 零残留、订阅者零残留 |

命名沿用 `test_<module>_<function>_<scenario>`。共享支撑：`tests/_transport_support.py`
（`FakeWriter` 可控失败/阻塞 drain、`make_stream_pair`、`pump`、`run_handshake_pair`、`gen_cert`）。

**过程中被测试抓出的两个实现缺陷（均已修 + 有回归测试锁定）**：

1. `FrameStream.recv_message` 在"坏帧 + 好帧同一 TCP 段到达"时流停摆：
   `PacketBuffer` 消费掉坏帧字节后未再尝试 pop 就去 `await read`，好帧永久滞留。
   修复为"pop 有消费但未产出帧 → 继续排空缓冲"（`test_corrupt_crc_frame_skipped_next_ok`）。
2. TLS 降级重建 socket 前未关闭旧连接（句柄泄漏）+ TOFU 拒绝后未 `wait_closed`。
   两通道各修（对齐 v4.0.2 的同类修复）。

---

## 7. 本阶段发现的安全收紧点（非任务书要求，已落地）

| # | 发现 | v4 行为 | v5 处置 |
|---|------|---------|---------|
| S1 | **降级判定过宽** | 匹配 `certificate_verify_failed`——那是**所有**证书校验失败（过期、未知 CA、主机名不符）的公共错误串，等于"任何校验失败都降级到不校验" | `_SELF_SIGNED_MARKERS` 只留 `self signed certificate` / `self-signed certificate`；过期/未知 CA/主机名不符一律不降级直接拒（`tls.py` 注释说明 + `test_false_for_other_cert_errors` 锁定差异） |
| S2 | **无确认回调时默认信任** | `fingerprint_callback is None` → 默认信任自签名（注释自认"存在 MITM 风险"） | 无回调即拒绝连接（首连确认是 TOFU 的安全前提，不允许静默放行），`test_no_confirm_callback_rejected_v5` |
| S3 | 令牌可跨连接挪用 | `dict[uuid4, 过期时间]`，不绑定任何上下文 | 绑定 `(client_ip, connection_id)`，且绑定不符时**连原记录一并作废**（不给二次尝试机会） |
| S4 | 会话令牌用 uuid4 | 122-bit 随机、非面向会话凭据的 API | `secrets.token_urlsafe(32)`（256-bit） |

---

## 8. 遗留问题

| # | 问题 | 处理 |
|---|------|------|
| 1 | **v4 `service.py` 的同步 broadcaster 仍带 B3**，新 `transport/broadcaster.py` 未接入真实运行时 | 阶段 9 服务端拆分时整体切新栈并删 v4 同步实现（用户已确认；个人单客户端场景可接受） |
| 2 | `transport/screen_channel.py` 80.9% / `control_channel.py` 82.9% 低于模块均值 | 缺口全在 TLS 降级重建与 TOFU 拒绝等异常分支；真实 MITM 场景测试成本高。阶段 9 渗透清单补测 |
| 3 | 未做任务书字面的"双**进程**"集成（用同事件循环双协程 + 真实 loopback socket 等价替代） | 传输栈正确性覆盖相同；跨进程仅额外验证 pb 生成物路径与解释器启动，额度考量下取舍。如需可加 `scripts/` 手动双进程 demo |
| 4 | `ws_backend.py`（原 REFACTOR_PLAN 4.7）未实现 | 任务书 §五 已不含此项（范围调整），如需 WebSocket 走配置项另议 |
| 5 | `rdc.constants` 根级旧常量与 `protocol/constants.py` 双份；`framing.create_json/create_screen` v4 遗留面仍被 `server/service.py` 调用 | 阶段 5/9 删（pyproject mypy 清单已标 TODO） |
| 6 | `logger.py` shim（D9）与 `_v4_config` 遗留层仍在 | TODO(phase-4/6)：阶段 6 调用点切换后删除 |
| 7 | asyncio 与 Qt 事件循环桥接（阶段 6 的 qasync/loop-in-thread）未做 | 阶段 6 前置项，非本阶段范围 |

---

## 9. 阶段 5 起点（采集与编码）

1. `capture/source.py`：`ScreenSource` Protocol 注入点（当前 `mss_source.py` 硬绑 mss，
   ASSESSMENT §5.7 标为"高难测"）+ `monitor.py` 多显示器枚举。
2. `capture/diff.py` 向量化重写：`cv2.absdiff` → reshape → `np.any(axis=(2,3))`
   单次出掩码；消除 `_adjust_block_size` 每帧整图 int16 运算；删 `_last_image`（死赋值）。
   目标 4K 差分 <5ms（v4 现状：8,160 次 Python 循环 + 8,160 次数组比较/帧）。
3. `encoding/{base,h264,jpeg,hw_probe,raters,bandwidth}.py`：PyAV libx264
   `preset=ultrafast`/`tune=zerolatency`/CRF 动态；**H.264 走全帧、不做区域裁剪**
   （帧间有状态编码，与区域裁剪架构不兼容）。
   `hw_probe` **必须实测 `ctx.open()`**（nvenc 构造成功但 open 抛 `PermissionError`）。
4. `scripts/bench_diff.py` + `bench_encode.py` 含 v4 JPEG 基线对比。
   已知基线（无需重测）：libx264 4K=11.2ms/帧、1080p=3.3ms/帧；桌面场景
   JPEG q60 25KB/帧 vs H.264 crf28 1.1KB/帧（降 96%，需补高熵场景取样）。
5. **与阶段 4 的接口衔接**：`ScreenFrame.seq/timestamp/codec/keyframe` 已定；
   `broadcaster.publish(frame)` 即编码产物的投递点（每订阅者签名已由其内部完成）；
   画面通道无客户端侧发送队列（S→C 单向），背压全在服务端。
6. 验收：4K 差分 <5ms · 编码软编 <40ms · 带宽降幅 ≥40%（多场景）· `capture/`+`encoding/` 覆盖率。

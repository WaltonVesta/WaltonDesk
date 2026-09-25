# STAGE5_REPORT.md — 阶段 5：采集与编码（全帧 H.264）✅ 完成

> 完成日期：2026-09-25（2026-09-24 因 7 天额度暂停，批 A 已验证后落盘 STAGE5_INPROGRESS.md，本次恢复续完）
> 基线 HEAD：`545bdfa`（批 A 验证提交）→ 本报告对应 `feat(stage5): add vectorized diff and PyAV H.264 encoding`
> 取代：`STAGE5_INPROGRESS.md`（其数据已全部并入本报告，原文件删除）

---

## 1. 任务逐项状态（对照任务书 §五 的 11 项）

| # | 任务 | 状态 | 交付物与验证 |
|---|------|------|--------------|
| 1 | `capture/source.py` ScreenSource Protocol | ✅ | 9 方法协议 + `ArrayScreenSource`（100% 覆盖，21 测试） |
| 2 | `capture/mss_source.py` mss 实现（多显示器） | ✅ | `MssScreenSource`（95.2% 覆盖，30 测试）+ v4 兼容外壳 `ScreenCapturer`（service.py import 面零破坏） |
| 3 | `capture/diff.py` 向量化 + 修 B4/B5 | ✅ | **4K 生产路径 3.54–3.59ms < 5ms**；vs 朴素 oracle 多配置逐块全等（95.2% 覆盖，40 测试） |
| 4 | `capture/monitor.py` 多显示器枚举/切换 | ✅ | 92.2% 覆盖，25 测试 |
| 5 | `encoding/base.py` VideoEncoder Protocol | ✅ | 7 方法协议 + `EncodedFrame`/`EncoderStats`；**契约测试基类** `tests/contract/test_encoding_base.py` 同跑 H264+JPEG（30 用例） |
| 6 | `encoding/h264.py` PyAV libx264 | ✅ | **4K 真实内容 19.9ms、1080p 5.5–6.6ms < 40ms**（97.9% 覆盖，26 测试） |
| 7 | `encoding/hw_probe.py` 硬编探测 | ✅ | 三层结论 + **必须实测 `ctx.open()`**（100% 覆盖，13 测试） |
| 8 | `encoding/raters.py` 自适应码率 | ✅ | CRF∈[18,45] 标定表 + 快降慢升迟滞 + 振荡冻结（99.1% 覆盖，38 测试） |
| 9 | `encoding/bandwidth.py` BandwidthProbe 迁出 | ✅ | `time.monotonic()` 替换 `time.time()`（100% 覆盖，11 测试）；jpeg.py re-export 保持 v4 import 面 |
| 10 | `scripts/bench_diff.py` / `bench_encode.py` | ✅ | 内置 §2.6 三大防坑；输出对齐全部验收项（真实数据见 §4） |
| 11 | 测试（差分正确性/编码往返/码率收敛） | ✅ | 11 个新测试文件 + 契约套件；**695 passed 零回归**（基线 439） |

**阶段验收对照**

| 验收项 | 目标 | 实测（bench 正式取样） | 判定 |
|--------|------|------------------------|------|
| 4K 差分 | <5ms | 3.54–3.59ms（双缓冲+adopt=True 生产路径，iters=50） | ✅ |
| 编码延迟 | <40ms | 4K static/moving 中位 19.99/19.90ms；1080p 5.5–6.6ms | ✅ |
| 带宽降幅 | ≥40% | 6 场景 64.1%–97.3%（含高熵噪声最坏场景） | ✅ |
| capture/+encoding/ 覆盖率 | ≥85% | **93.0%**（阶段 5 新文件剔除 v4 `framerate.py` 后约 96%） | ✅ |
| ruff / mypy | 0 error | All checks passed! / Success: no issues found in **69** source files | ✅ |
| pytest 全绿零回归 | — | **695 passed**（v4 兼容面 `get_changed_blocks/get_changed_region` 与 `JPEGEncoder()` 裸构造、`ScreenCapturer` 均实测未破） | ✅ |

---

## 2. 三条硬约束（mss_source.py，违反即验收失败）

1. **`mss.MSS()`，绝不用 `mss.mss()`**——后者在 mss 10.2 已废弃，调用即抛
   `DeprecationWarning`；本项目 pytest `filterwarnings=["error",…]` 只豁免
   PySide6/protobuf，测试触达即炸。`tests/unit/test_capture_mss.py` 用假后端把
   `mss.mss()` 调用直接做成 `AssertionError` 锁死。
2. **双缓冲交替 + 差分 `adopt=True`**——`adopt=False` 每帧多付 6.19ms（33MB 拷贝）。
   `MssScreenSource.capture()` 维护 2 块持久缓冲乒乓写入；测试以"第 3 帧不得覆盖
   第 2 帧缓冲 + 乒乓对象身份"锁死（`test_double_buffer_alternation`）。
   `ScreenDiffer` 侧的违约示范测试（同一缓冲改写→差分致盲）从反面论证了该设计。
3. **失败一律抛 `ScreenCaptureError`**——删除 v4 的 `except Exception: return 1920,1080`
   兜底与 `HAS_MSS` 静默降级；构造失败/枚举失败/grab 失败/关闭后采集四类路径
   均有测试。"mss 未装"场景：mss 为硬依赖，ImportError 在包加载期即显式爆出的行为
   由依赖声明保证（用户叮嘱 a 的补充项已落实为"故障必抛且计数"测试）。

v4 兼容外壳 `ScreenCapturer`（PIL 输出、`get_screen_size` 兜底、越界索引静默）**原样
保留行为语义**——它是 `server/service.py`（叮嘱 B 不碰）的既有 import 面，阶段 9 随
v4 运行时一并删除。

---

## 3. 关键实测数据

### 3.1 差分（`scripts/bench_diff.py` iters=50，生产路径 双缓冲+adopt=True）

| 场景 | 中位 | 最小 | p95 | 最大 | 判定 |
|------|------|------|-----|------|------|
| 4K high-change（230 块/帧） | **3.59ms** | 2.75 | 4.32 | 4.73 | ✅ |
| 4K desktop（2 脏区） | **3.54ms** | 3.03 | 4.09 | 4.77 | ✅ |
| 1080p high-change | 1.57ms | 1.46 | 2.68 | 4.39 | ✅ |
| 1080p desktop | 1.57ms | 1.38 | 2.83 | 3.34 | ✅ |
| 1440p | 2.70ms | 2.37 | 4.80 | 5.24 | ✅ |
| 720p | 0.92ms | 0.63 | 1.28 | 1.56 | ✅ |

- v4 对照：双重 Python 循环 24.5–64.7ms/帧（4 次测量，内容相关）→ **提速 ~7×**；
- 内存带宽物理下限标定：本机 ~32GB/s，4K `a!=b` 读 66MB 写 8.3MB ≈ 3.15–3.44ms，
  故分带线程池（x8、预分配缓冲）是唯一达标路径；
- 正确性 oracle：vs 朴素逐块 `np.array_equal` 全等，覆盖 4K/1080p/**1928×1080 非整除**/
  65×63/130×100@16/1080p@8/1×1/1×4000/4000×1；
- 任务书建议的 `cv2.absdiff` 路径实测 27.5ms **比 v4 还慢**（写 33MB 差值数组是瓶颈），
  已按实测改走 `np.not_equal`(uint32 打包)。

### 3.2 编码（`scripts/bench_encode.py` iters=24，ultrafast+zerolatency+crf28）

| 场景 | 中位 | 最小 | 最大 | 判定 |
|------|------|------|------|------|
| 4K static | **19.99ms** | 18.23 | 36.97 | ✅ <40ms |
| 4K moving | **19.90ms** | 16.31 | 25.36 | ✅ |
| 1080p static | 5.51ms | 4.33 | 6.93 | ✅ |
| 1080p moving | 6.09ms | 5.04 | 7.26 | ✅ |
| 1080p scroll | 6.55ms | 4.98 | 8.72 | ✅ |
| 1080p noise（高熵） | 10.41ms | 9.34 | 13.54 | ✅ |
| 真实屏幕 1080p（拖窗动） | 4.53ms | — | — | ✅ |

⚠️ **口径边界（诚实声明）**：40ms 验收线的口径是**真实屏幕内容**。脚本另测了一个
"最坏参考"信息项（不参与 PASS/FAIL 断言）：4K **纯随机噪声**（熵拉满、x264 完全无法
帧间预测的人工最坏场景）中位 **47.8ms，超线**。真实桌面（视频播放、滚动、UI 动画）
实测均 ≤20ms；若阶段 6 集成发现高熵内容（如全屏游戏）成为瓶颈，解法是走 raters 的
`resolution_scale` 降分辨率档，而非改编码参数。

- P2 色彩路径（`cv2.cvtColor(BGRA2YUV_I420)` 预分配 dst + 直写 yuv420p 平面，
  按 `line_size` 分支）比 `from_ndarray` 省 ~16ms/帧（4K）；
- 往返 PSNR（crf28，bgra 对齐比较）：min 27.7 / avg 28.5 dB（合成高动内容）；
  真实 1080p 内容历史探针 min 38.4 dB；`gop=60`（默认 2s@30fps）；
- 硬编本机结论：nvenc/qsv/amf 全部 `open()` 失败（PermissionError/ArgumentError/
  UnknownError），videotoolbox/vaapi/v4l2m2m 未编译 → 落 `libx264`。

### 3.3 带宽降幅（v4 JPEG q60 → v5 H.264 crf28，验收线 ≥40%）

| 场景 | JPEG | H.264 | 降幅 |
|------|------|-------|------|
| 4K static | 245.02KB | 12.50KB | **94.9%** |
| 4K moving | 570.00KB | 23.84KB | **95.8%** |
| 1080p static | 60.83KB | 1.73KB | **97.2%** |
| 1080p moving | 145.09KB | 8.72KB | **94.0%** |
| 1080p scroll | 138.67KB | 3.81KB | **97.3%** |
| 1080p noise（最坏） | 1817.36KB | 652.58KB | **64.1%** |
| 真实屏幕 1080p | 95.9KB | 13.0KB | **86.5%** |

全部 ≥40%；高熵随机噪声的最坏场景仍有 64.1%，桌面场景 94–97% 与文档基线 96% 吻合。

### 3.4 CRF↔码率标定（raters.py 数据基础，4K 真实屏幕内容 @30fps）

| CRF | 平均包 | 码率 | | CRF | 平均包 | 码率 |
|-----|--------|------|-|-----|--------|------|
| 18 | 41.93KB | 10.30Mbps | | 32 | 13.75KB | 3.38Mbps |
| 23 | 23.41KB | 5.75Mbps | | 38 | 9.48KB | 2.33Mbps |
| 28 | 16.43KB | 4.04Mbps | | 45 | 5.94KB | 1.46Mbps |

CRF 18→45 压带宽 7×；GOP：gop15 带宽 +69%、gop≥30 持平 → 默认 gop=60（2s）。

---

## 4. 三个致命坑（已锁死，防复发）

1. **`thread_count=1` 破 40ms 线**：其余参数相同的 4K 真实内容，tc=1 → **65.79ms**、
   tc=2 → 47.47ms、tc≥4 达标。`H264Encoder` 构造时钳制 `max(4, thread_count)`，
   有测试锁死（含 0=auto 也钳 4，不依赖跨机器一致性）。
2. **`mss.mss()` 废弃 API × pytest filterwarnings=error**：调用即测试炸（§2 硬约束①）。
3. **测量三陷阱**（bench 脚本已内置防护）：①内容合成混进计时循环 → 4K 假数据 71.67ms
   （真实 ~20ms）；②往返比较拿 BGRA 当 RGB → 假 PSNR 恒 ~15dB 且 CRF 无响应；
   ③真实屏幕抓帧必须 `mss.MSS()`。另：`ScreenShot` 无 `.mode` 属性、cv2 拒绝非连续
   dst（需 `reshape`/`ascontiguousarray`）。

---

## 5. 设计决定与偏离

| # | 内容 |
|---|------|
| D15 | **决策 A（用户 2026-09-25 确认）**：`JPEGEncoder` 改构造参数注入（`quality=60, min_quality=30, max_quality=95, bandwidth_limit=0`，默认值=v4 生产配置），移除 `rdc.config._v4_config.Config` 单例依赖；`service.py` 裸 `JPEGEncoder()` 照常工作；`set_quality` 不再写配置文件（会话期内存态，跨会话偏好归阶段 6 Settings） |
| D16 | **测试节奏（用户确认纪律 3）**：逐模块"写完→补测试→跑通"，不攒批 C |
| D17 | `JPEGEncoder.encode` 用 `@overload` 双签名：PIL→v4 tuple、ndarray→`EncodedFrame`，按入参类型分派（同一实例两调用面共存，v4 面阶段 9 删） |
| D18 | raters 采用**快降慢升 + 中位数窗口 + 死区 + 振荡冻结**的纯逻辑状态机；CRF→码率参考值用 §3.4 实测表插值；分辨率缩放仅在 CRF 打满仍 severe 时给 0.75 一档 |
| D19 | hw_probe 判定**只认 `ctx.open()` 成功**；本机结论（全硬件 open 失败）不写死进断言，测试用 mock 三层 + 真机只断言不变量 |
| D20 | `MssScreenSource` 默认采集**虚拟全屏**（v4 默认第一块物理屏）；`capture()` 内部双缓冲，`capture_into()` 交由调用方自备缓冲（两者都配合 `adopt=True`） |
| D21 | v4 外壳 `ScreenCapturer` 保留于同文件（service.py import 面）；`BandwidthProbe` 在 jpeg.py re-export（同上）。阶段 9 一并清除 |

### pyproject 维护（mypy 宽松清单，本阶段兑现 TODO(phase-5)）

- **移除**：`rdc.capture.diff`、`rdc.capture.mss_source`、`rdc.encoding.jpeg`
  （重写补注解后 strict 实测 0 error）；
- **保留**：`rdc.capture.framerate`（未重写，阶段 6 帧率自适应时处理）；
- 新增文件（source/monitor/base/h264/hw_probe/raters/bandwidth）一律全局 strict，
  未进清单。

### 遗留（低优先级，不影响验收）

- diff.py §5-3/4：`ignore_alpha=True` 路径与 `_dirty_rects` 各有一次可预分配的
  数组分配（默认关闭/8KB 级，实测对 3.5ms 无感知，留 TODO）；
- diff.py 首帧分支 `elapsed_ms` 取时风格与其他分支不一致（纯风格）；
- 阶段 4 遗留的 `rdc.constants` 双份与 `create_json/create_screen`（阶段 9）。

---

## 6. 阶段 6 起点

1. `capture/framerate.py` 重写（帧率自适应；消费 `DiffResult.change_ratio`/
   `dirty_rects` 与 `RaterSignals`），完成后移出 mypy 宽松清单最后一条 capture 条目；
2. 服务端广播管线：`MssScreenSource`（双缓冲）→ `ScreenDiffer.compare(adopt=True)`
   静止门控 → `H264Encoder` → `broadcaster.publish(ScreenFrame)`（阶段 4 背压已就位）；
   按 `hw_probe.best_encoder()` 选软/硬编；`AdaptiveRater` 接心跳 RTT/丢包与
   `DropOldestQueue.dropped`；
3. GUI（PySide6）与输入注入（阶段 7 前置）按需排期；
4. v4 运行时（`server/service.py` 及其 capture/encoding 兼容面）统一在阶段 9 拆除。

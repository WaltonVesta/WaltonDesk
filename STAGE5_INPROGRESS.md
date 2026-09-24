# STAGE5_INPROGRESS.md — 阶段 5 中途落盘（额度暂停点）

> 记录日期：2026-09-24（**第二次落盘**：批 A 三个文件已验证）
> 基线 HEAD：阶段 4 `b9a0cae` → WIP `ead8c60` → 本次验证提交
> 本文件性质：**临时进度快照**，阶段 5 完成后由 `STAGE5_REPORT.md` 取代
> 触发原因：7 天额度告急，用户指令"先落盘，等恢复后说继续"
>
> ✅ **当前工作区是绿色状态**：`capture/{source,monitor,diff}.py` 三个文件
> 已通过 **ruff 0 error + mypy 64 文件 0 error + 功能 smoke 全绿**（数据见 §1.5）。
> `diff.py` 的 4K 差分在生产路径实测 **3.18ms**，达成 <5ms 验收线。
> 剩余 8 项任务未开始，全部实现配方见 §3。

---

## 0. 一句话现状

阶段 5 的**全部技术选型已通过真机探针定案**（含 3 个会致命返工的坑），批 A 的
3 个文件（`source.py` / `monitor.py` / `diff.py`）已写完**并通过 ruff + mypy +
功能 smoke 验证**。剩余 8 项是"照 §3 既定配方写完 + 测试 + 基准脚本"的体力活。
**最贵的部分（测量与决策）已完成，数据全在本文件 §2，恢复后不必重测。**

---

## 1.5 批 A 验证结果（2026-09-24，真实输出）

```
$ uv run ruff check src tests scripts --output-format=concise
All checks passed!

$ uv run mypy src
Success: no issues found in 64 source files

$ uv run pytest -q --no-cov
439 passed in 23.78s        # 阶段 4 基线，**零回归**（v4 兼容面 get_changed_blocks/
                            # get_changed_region 仍被 service.py 正常调用）
```

⚠️ **下一批注意**：从 pyproject 移除 `rdc.capture.diff` 后，mypy 可能对
`_band_row_count` 方法与其 docstring 中提到的同名属性产生冗余告警，属预期，
按提示处理即可——**不要在未移除前重复跑 mypy 浪费额度**。

**首轮 mypy 报 3 错**（同一根因，已修）：`_band_rows` 既是方法名又被当属性赋值 →
`Cannot assign to a method`。这正是本报告初版 §5.2 预警的隐患，修复方式：
方法改名 `_band_row_count()`，并把 4 个分带属性（`_band_starts` / `_band_rows` /
`_needs_col_pad` / `_pad_buffers`）**在 `__init__` 中声明**（不再仅在
`_ensure_bands` 里创建）。

顺带修掉的另外 2 个 §5 遗留问题：
- **§5.1 已修**：`_column_padded` 原每次调用新分配 33MB → 改为在 `_ensure_bands`
  中一次性分配**成对**补齐缓冲，热路径只写有效像素区（列边带恒为 0 且两帧相同，
  不产生假变化，无需每帧清零）。
- **死分配已删**：`_mask_buffers`（每分辨率 `(带高 × 全宽)` 的 uint8 数组，
  4K 下约 8.3MB/带）**从未被读取** → 整块移除。

**功能 smoke（26 项断言全过）**

| 验证项 | 结果 |
|--------|------|
| 差分正确性 vs 朴素双重循环（6 种配置：4K/1080p/**1928×1080 非整除**/65×63/130×100@bs16/1080p@bs8） | **逐块全等 ✅ 全部 match** |
| 首帧语义 | `is_first=True`，v4 兼容面 `get_changed_blocks` 返回 `None` ✅ |
| 静止帧门控 | 全同帧 `any_change=False, changed=0` ✅ |
| 单像素变化 | `changed=1`，脏区 `[(576,480,32,32)]` ✅ 定位精确 |
| alpha 语义（默认 `ignore_alpha=False`） | 仅 alpha 变化 → 判为有变化 ✅ |
| alpha 语义（`ignore_alpha=True`） | 仅 alpha 变化 → `any_change=False` ✅ |
| 多矩形脏区（B5） | 桌面内容 24 块变化 → **2 个矩形**（无外接矩形膨胀） ✅ |
| `ArrayScreenSource` | 尺寸/序号/显示器索引/统计计数、close 后抛 `RuntimeError` ✅ |
| `bgra_view` 长度校验 | 不匹配抛 `ValueError` ✅ |
| `MonitorSelector` | 默认选虚拟全屏 `(0,0,4480,1440)`、`next/prev` 循环、`select(0)`、`to_local` 坐标换算、越界抛 `MonitorNotFoundError` ✅ |
| `ScreenDiffer.close()` + `stats()` 计数 | 42 帧统计正确、`workers=8` ✅ |

**4K 差分性能（`ScreenDiffer` 实测，含归约与脏区计算）**

| 场景 | 中位数 | 最小 | 判定 |
|------|--------|------|------|
| **生产路径：双缓冲交替 + `adopt=True`，高变化内容（230 块）** | **3.18 ms** | 2.91 | ✅ **<5ms PASS** |
| **生产路径：桌面式内容（24 块变化，2 脏区）** | **4.04 ms** | — | ✅ **<5ms PASS** |
| `adopt=False`（v4 兼容面，强制拷贝前帧） | 9.12–9.37 ms | 8.76 | ❌ 超线 |

→ ⚠️ **关键约束（必须传给 `mss_source.py`）**：`adopt=False` 比 `adopt=True`
慢 **6.19 ms**（每帧 33MB 拷贝）。**v5 主路径必须用双缓冲 + `adopt=True`**：
采集源每帧写入**另一个**持久缓冲，差分器只持引用。`adopt=False` 仅留给
v4 兼容面（PIL 入口），阶段 9 随之删除。

→ 与 §2.1 探针数据的差别说明：探针裸测 2.78ms 是"预分配 `np.not_equal` +
纯归约"，本次 3.18ms 是**经完整 `compare()` 公开 API**（含 `_as_u32` 视图、
脏区 `connectedComponents`、统计与 `DiffResult` 构造）的端到端耗时，
后者才是验收口径。**结论不变：达标。**

---

## 1. 任务逐项状态（对照任务书 §五 的 11 项）

| # | 任务 | 状态 | 说明 |
|---|------|------|------|
| 1 | `capture/source.py` ScreenSource Protocol | ✅ **已完成 + 已验证** | ruff/mypy/smoke 全绿；**无单测文件**（任务 11 待补） |
| 2 | `capture/mss_source.py` mss 实现（多显示器） | ❌ **未开始** | 仍是 v4 搬迁副本（PIL 路径 + 吞错 + 硬编码兜底）<br>⚠️ 必须 `mss.MSS()` + 双缓冲 `adopt=True`（见 §1.5 / §2.6 坑 3） |
| 3 | `capture/diff.py` 向量化 + 修 B4/B5 | ✅ **已完成 + 已验证** | 4K 生产路径 **3.18ms <5ms**；vs 朴素实现 6 配置逐块全等；**无单测文件** |
| 4 | `capture/monitor.py` 多显示器枚举/切换 | ✅ **已完成 + 已验证** | `MonitorInfo` + `MonitorSelector`，纯逻辑；**无单测文件** |
| 5 | `encoding/base.py` VideoEncoder Protocol | ❌ 未开始 | 接口设计已在 §3 定稿 |
| 6 | `encoding/h264.py` PyAV libx264 | ❌ 未开始 | **实现配方已在 §3 定稿（含关键代码）** |
| 7 | `encoding/hw_probe.py` 硬编探测 | ❌ 未开始 | 探测结论已在 §2.5 定稿 |
| 8 | `encoding/raters.py` 自适应码率 | ❌ 未开始 | CRF→码率实测数据已在 §2.4 |
| 9 | `encoding/bandwidth.py` BandwidthProbe | ❌ 未开始 | 现为 `encoding/jpeg.py` 内的 `BandwidthProbe`，待迁出 |
| 10 | `scripts/bench_encode.py` + `bench_diff.py` | ❌ 未开始 | 可直接复用 §2 的探针逻辑 |
| 11 | 测试（差分正确性/编码往返/码率收敛） | ❌ 未开始 | smoke 已覆盖正确性（§1.5），需固化为 pytest 文件 |

**已确认假设的落地情况**
- 假设 A（diff 改"静止门控 + 脏区列表"，B4 固定块、B5 多矩形，`get_changed_region` 保留不进主路径）→ 已按此写入 `diff.py`。
- 假设 B（capture/encoding 重写即补注解、移除 mypy 宽松清单条目；`rdc.constants` 与
  `create_json/create_screen` 留阶段 9）→ `diff.py`/`source.py`/`monitor.py` 均按
  **全注解 strict** 写，实测在移除清单前就已 0 error。
  ⚠️ **`pyproject.toml` 的 mypy 宽松清单尚未改动**（本批为省额度未单独验证）。
  `rdc.capture.diff` **现在即可移除**（下一批第一件事：删该行 + 跑一次 mypy 确认）；
  `rdc.capture.mss_source` / `rdc.capture.framerate` / `rdc.encoding.jpeg` 待各自重写后再移除。
  注：`source.py` / `monitor.py` 是新文件，**从未**进入宽松清单，天然 strict。

---

## 2. 实测数据（**恢复后不要重测，直接用**）

### 2.1 差分：最终选定路径与全部对比数据

**结论：`np.not_equal`（uint32 打包）+ 两级 `any` 归约 + 分带线程池。**

4K（3840×2160，块 32px，网格 120×68 = 8160 块）：

| 路径 | 耗时 | 备注 |
|------|------|------|
| v4 双重 Python 循环 + `np.array_equal` | 24.5 / 29.3 / 53.3 / 64.7 ms（4 次测量，内容相关） | 8,160 次循环 + 8,160 次数组比较/帧 |
| `cv2.absdiff` + numpy `max(axis=2)` | **149.7 ms** | ❌ 灾难 |
| `cv2.absdiff` + `np.any(d>0, axis=2)` | **94.5 ms** | ❌ |
| `cv2.absdiff` + `cv2.split` + `cv2.max` | 29.4 ms | ❌ 仍比 v4 慢 |
| **`cv2.absdiff` + 通道 max + `cv2.reduce`** | **27.5 ms** | ❌ **任务书建议路径实测比 v4 还慢** |
| `!=` u64 视图 + 归约 | 6.6 ms | |
| `!=` u32 视图 + 归约 | 5.6–6.3 ms | 单线程 |
| `np.not_equal(out=预分配)` u32 + 两级 any，单线程 | **4.7 ms（桌面内容）～6.3 ms（高变化内容）** | 波动来自内存带宽与内容 |
| 同上，2 带单线程分块 | 5.77 ms | 缓存局部性改善有限 |
| **同上，线程 x4（预分配分带缓冲）** | **3.41 ms** | ✅ |
| **同上，线程 x8（预分配分带缓冲）** | **2.78 ms（min 2.43）** | ✅ **最终选定** |
| 1080p 单线程 | 1.43–1.49 ms | ✅ 无需并行 |
| 2560×1440 单线程 | 2.69 ms | |
| 1280×720 单线程 | 0.61 ms | |

**内存带宽标定**（解释为何 5ms 是硬约束）：
- 4K BGRA 单帧 = 33.2 MB
- memcpy 33MB→33MB（66MB 流量）= 2.06 ms → **本机内存带宽 ≈ 32 GB/s**
- `a != b` 读 66MB 写 8.3MB = **3.15–3.44 ms ← 这是 4K 差分的物理下限**
- `cv2.absdiff`（读 66 写 33MB）= 4.03–4.07 ms → **写带宽是瓶颈，故 absdiff 天生更慢**
- `np.not_equal(a, b, out=buf)` 预分配 = **2.94 ms**（比会分配的写法快 15%）

→ 单线程 4K 无法稳定 <5ms（3.15ms 下限 + 归约 1.8ms ≈ 5ms，无余量）。
→ **分带多线程是唯一达标路径**，实测 x8 = 2.78ms，达成 <5ms 验收。

**归约策略对比**（4K，纯归约部分，输入 8.3MB bool）：
| 归约 | 耗时 |
|------|------|
| `reshape(r,BS,c,BS).any(axis=(1,3))` | 1.81 ms ← 采用 |
| `any(axis=2)` 再 `any(axis=1)` 两级 | 1.83 ms |
| `cv2.reduce(REDUCE_MAX)` 单级 | **0.51 ms**（快 3.5x，但需要额外拷贝，端到端未胜出：P2 实测 5.29ms > P1 4.70ms） |
| `transpose` 后单轴 any | 更慢 |

**正确性 oracle 已验证**：向量化结果与朴素双重循环 `np.array_equal` **逐块全等**，覆盖：
- 17 种分辨率：3840×2160、1920×1080、2560×1440、1280×720、**1928×1080 与 1080×1928（非整除块，需补齐）**、130×100、65×63、32×32、97×33、31×31、64×33、300×1000、16×16、**1×1**、**1×4000**、**4000×1** → 全部 match
- 4 种边界内容：全同帧、全异帧、单像素变化、**仅 alpha 变化**（须判为无变化或按 ignore_alpha 语义处理）→ 全部 match

**静止门控决策（重要）**
- `np.array_equal` 快路径实测 **4.34 ms**，全量差分实测 **4.76 ms** → **array_equal 几乎不比全量差分快**（都要扫 33MB），不值得单独做快路径。
- 门控直接用差分结果的 `mask.any()`，实测 **0.001 ms**。
→ `DiffResult.any_change` 即门控信号，已按此实现。

**零拷贝双缓冲 swap**：10000 次引用交换 = 0.63 ms（可忽略）。对比"每帧拷贝 33MB"= 3.19 ms → **swap 语义每帧省 3.19ms**，这是 `adopt=True` 参数存在的理由。

### 2.2 编码：决定性发现（省掉 24ms/帧）

| 1080p | 4K | 说明 |
|-------|-----|------|
| `av.VideoFrame.from_ndarray(bgra)` 单独 | 6.29 ms | **23.61 ms** ← PyAV 内部 swscale，极慢 |
| 仅 encode（帧已转好） | 5.89 ms | 17.87 ms |
| **合计 P1（from_ndarray + encode）** | 7.5–7.7 ms | **28.0–29.7 ms** |
| **P2：`cv2.cvtColor(BGRA2YUV_I420)` + 直接填 yuv420p 平面** | **3.32–3.58 ms** | **11.58–11.90 ms** ✅ |
| P3：cv2 转换 + `plane.update(bytes)` | 4.08–4.37 ms | 14.21–15.43 ms（比 P2 慢，因 tobytes 拷贝） |
| 其中 `cv2.cvtColor BGRA→I420` | 0.18 ms | 1.84 ms（可用 `dst=` 预分配） |
| 其中 `fill_planes_direct` | 0.11 ms | 0.79–1.00 ms |

→ **P2 路径 4K = 11.58ms，与文档既有基线"libx264 4K = 11.2ms"吻合**，证明我最初测到的
> 71.67ms 全是 `make_content` 混进计时循环的测量错误（见 §2.6 坑 3）。
→ **v5 必须用 P2**，4K 延迟从 28ms 降到 11.6ms，余量 3.4×。

`fill_planes_direct` 可行性的实测依据：
- `av.VideoFrame(w, h, "yuv420p")` 有 3 个 plane，各 plane 暴露
  `buffer_ptr / buffer_size / line_size / height / width / update`，**writable=True**
- 实测 line_size == width（无对齐填充），但代码**仍必须**按 `line_size` 分支处理
- 正确性：planes 填充 → `to_ndarray(format="bgra")` → 与原图 maxdiff=21 mean=1.17
  （色彩转换本身有损，属正常）

硬编/编码器能力实测：
- `av.CodecContext` **没有** `close()` / `release()` 方法 → 靠引用回收，**不要写 close 调用**
- `extradata` 长度 = **0**（Annex-B 风格，裸 `av.Packet(bytes)` 可直接喂 `h264` 解码器，实测 10/10 帧解出）→ **无需处理 AVCC/SDP**
- `delay=0`、`has_b_frames=False`（zerolatency 生效，无重排缓冲）
- 强制关键帧可行：`VideoFrame.pict_type` 存在，`av.video.frame.PictureType` 枚举存在，
  实测 `gop=300` 时把第 3 帧设 `pict_type=I` → 关键帧出现在 i=0 和 i=3 ✅

### 2.3 ⚠️ 致命坑 1：`thread_count` 不设会破 40ms 验收线

4K 真实屏幕内容，其余参数相同（ultrafast / zerolatency / crf28）：

| thread_count | 实测 ms/帧 | 判定 |
|--------------|-----------|------|
| **1** | **65.79** | ❌ **超 40ms 线** |
| 2 | 47.47 | ❌ 超线 |
| 4 | 37.11 | ✅ |
| 8 | 34.38 | ✅ 最优 |
| 16 | 34.73 | ✅ |
| 0（PyAV 默认 auto） | 11.58（P2 路径） | ✅ 自动选了合理值 |

→ **`hw_probe`/`h264.py` 里绝不能把 `thread_count` 设成 1 或让它"继承默认 1"。**
→ 注意"11.58ms(P2) vs 34.38ms(tc=8 且 from_ndarray)"的差异主要来自色彩转换路径，
   两者都要按最优配置：P2 + 显式 `thread_count>=4`（或依赖 auto）。

### 2.4 CRF → 码率 → PSNR 实测（`raters.py` 与带宽验收的数据基础）

**4K 真实屏幕内容（moving），1080p 同理：**

| CRF | 4K 平均包 | 4K 码率@30fps | 1080p 平均包 | 1080p 码率 |
|-----|----------|--------------|-------------|-----------|
| 18 | 41.93 KB | 10.30 Mbps | 18.87 KB | 4.64 Mbps |
| 23 | 23.41 KB | 5.75 Mbps | 10.21 KB | 2.51 Mbps |
| 28 | 16.43 KB | 4.04 Mbps | 7.85 KB | 1.93 Mbps |
| 32 | 13.75 KB | 3.38 Mbps | 6.42 KB | 1.58 Mbps |
| 38 | 9.48 KB | 2.33 Mbps | 4.60 KB | 1.13 Mbps |
| 45 | 5.94 KB | 1.46 Mbps | 2.77 KB | 0.68 Mbps |

→ CRF 与码率单调负相关，**CRF 18→45 可把 4K 带宽压 7×** → raters 的"quality→bitrate"
映射应走 **CRF 区间 [18, 45]**（而非 v4 的 JPEG quality [30,95]）。

**GOP 影响（1080p moving，crf28）**：gop15 = 13.29 KB/帧（3.27 Mbps），gop30/60/300 均
= 7.85 KB（1.93 Mbps）→ **gop 从 30 放宽到 300 不省带宽**（因为没触发到关键帧），
但 gop15 使带宽 **+69%** → 关键帧间隔默认 **2s @30fps = gop 60** 是合理折中（任务书要求默认 2s）。

**ABR 模式实测（1080p moving）**：设 `bit_rate` 且去掉 crf：
2Mbps→6.90KB，8Mbps→23.32KB，30Mbps→63.23KB → ABR 生效，码率随目标上升。
⚠️ 注意 ABR 2000k 目标但实测 1.70Mbps，说明 ultrafast+zerolatency 下 ABR 达不到目标也正常。

**PSNR 单调性验证（640×360 static，P2 路径）**：
crf18=35.23(min)/39.77(avg) dB → crf23=35.13/39.38 → crf28=34.91/38.78 →
crf35=34.00/36.53 → crf45=31.42/32.83 → **CRF 越低画质越高，符合预期，可作为往返测试断言**
1080p 真实内容往返：crf28 → PSNR min=38.44 avg=38.53 dB，25.27 KB/pkt；crf23 → 39.38/39.43 dB
`gop=1` 全关键帧：10/10 解出，keys=10，B/pkt 从 0.79 涨到 3.68 KB（4.7×）

### 2.5 硬编探测结论（`hw_probe.py` 的设计依据）

**探测必须实测 `ctx.open()`**，因为构造永远成功：

| 编码器 | `codecs_available` | 构造 | `ctx.open()` | 错误原文（关键） |
|--------|-------------------|------|-------------|----------------|
| `h264_nvenc` | True | ✅ 成功 | ❌ | `PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("h...` |
| `h264_qsv` | True | ✅ 成功 | ❌ | `ArgumentError: Invalid argument: 'avcodec_open2("h264_qsv", {})' re...` |
| `h264_amf` | True | ✅ 成功 | ❌ | `UnknownError: [Errno 1313558101] Unknown error occurred: 'avcodec_...` |
| `hevc_nvenc` | True | ✅ 成功 | ❌ | `PermissionError` 同上 |
| `h264_videotoolbox` | **False** | — | ❌ | `UnknownCodecError`（未编译进来） |
| `h264_vaapi` | **False** | — | ❌ | `UnknownCodecError` |
| `h264_v4l2m2m` | **False** | — | ❌ | `UnknownCodecError` |
| `libopenh264` | **False** | — | ❌ | `UnknownCodecError` |
| **`libx264`** | True | ✅ | ✅ **OPEN-OK** | 可用（唯一软编 H.264） |
| `libx265` | True | ✅ | ✅ **OPEN-OK** | 可用（v5 不用，但证明"必须 open 才算"） |

→ `hw_probe` 必须分三层报告：**未收录 / 收录但构造失败 / 构造成功但 open 失败**，
   且**只有 open 成功才算可用**。本机结论：nvenc/qsv/amf 全部 open 失败 → 跳过硬编。

### 2.6 测量陷阱（写基准脚本时必须避开）

1. **坑 1（差点误判验收失败）**：把内容合成函数放进计时循环 → 4K 编码测出
   **71.67 ms**（真实 11.58ms）。→ **必须先预生成帧池，计时只含被测操作**。
2. **坑 2（差点误判画质崩坏）**：往返 PSNR 恒 ~15 dB 且**改 CRF 完全无变化** →
   根因是拿 BGRA 源当 RGB 比（通道序错位）。修正后 34–39 dB 且 CRF 单调。
   → 比较前必须 `arr[:, :, [2,1,0]]` 或统一转回 bgra 比前三通道。
3. **坑 3（差点破 pytest）**：`mss.mss()` 在 mss 10.2.0 **已废弃**，调用即抛
   `DeprecationWarning: mss.mss is deprecated and will be removed in a future release; use mss.MSS instead`。
   而 `pyproject.toml` 的 `filterwarnings = ["error", ...]` 只豁免了
   `PySide6.*` 与 `google.protobuf.*` → **任何测试触发 mss.mss() 都会直接失败**。
   ✅ **v5 的 `mss_source.py` 必须用 `mss.MSS()`**（实测无警告，功能相同）。
   （顺带：`mss.factory` 也存在。）
4. **坑 4**：`cv2.max(g, tmp[:,:,1], dst=g)` 报
   `Layout of the output array dst is incompatible with cv::Mat`
   → numpy 切片的非连续布局不能当 cv2 的 dst，需先 `np.ascontiguousarray`。
5. **`ScreenShot` 没有 `.mode` 属性**（v4 若依赖会 AttributeError）。
   可用属性实测为：`bgra, from_size, height, left, pixel, pixels, pos, raw, rgb, size, top, width`。
6. 真实屏幕 `np.frombuffer(sct.grab(m).bgra, ...)` 零拷贝可用；alpha **实测恒 255**
   （`unique=[255]`，uniform=True）。

### 2.7 多场景带宽对比（验收"≥40%"，含高熵场景）

同内容同帧池（30 帧），JPEG q60（v4 配置：`optimize=True subsampling=0`）vs H.264 crf28：

| 场景 | JPEG q60 | H.264 crf28 | **降幅** |
|------|---------|-------------|---------|
| 合成 1080p static | 75.35 KB | 0.88 KB | **98.8%** |
| 合成 1080p moving | 75.88 KB | 1.36 KB | **98.2%** |
| 合成 1080p scroll（文档滚动） | 80.19 KB | 7.88 KB | **90.2%** |
| 合成 1080p noise（高熵） | 1817.46 KB | 672.59 KB | **63.0%** |
| **真实屏幕 1080p static** | 146.41 KB | 7.17 KB | **95.1%** |
| **真实屏幕 1080p moving** | 146.86 KB | 7.85 KB | **94.7%** |
| **真实屏幕 1080p scroll** | 148.87 KB | 10.13 KB | **93.2%** |
| 真实屏幕 1080p noise | 1817.43 KB | 672.59 KB | **63.0%** |
| **真实屏幕 4K static** | 407.37 KB | 16.33 KB | **96.0%** |
| **真实屏幕 4K moving** | 408.46 KB | 16.43 KB | **96.0%** |
| 真实屏幕 4K noise | 7268.88 KB | 2676.19 KB | **63.2%** |

→ **所有场景 ≥63%，验收线 ≥40% 通过**，含最坏的高熵随机噪声场景。
→ 文档"桌面场景降 96%"与真实屏幕实测 94.7–96.0% 吻合 ✅。
→ 码率视角（1080p 真实 moving）：JPEG 36.09 Mbps → H.264 1.93 Mbps。

### 2.8 B5（单外接矩形退化）实测复现与修复验证

场景：左上角 6×3 块 + 右下角 4×3 块同时变化（真实桌面 = 时钟 + 通知弹窗）。

| 方案 | 结果 |
|------|------|
| v4 单外接矩形 | `bbox=(0,0,120,68)` → 覆盖 **8,160 块 = 全屏**，实际仅 18 块变化，**膨胀 453×** |
| v5 多矩形（connectedComponents） | comp1 = px(0,0,96×64) 6块，comp2 = px(3712,2080,128×96) 12块 → **合计 18 块，零膨胀** |

`connectedComponentsWithStats` 在块网格（120×68）上耗时 **0.054 ms**（n=300 实测，24811 fps）
→ 脏区提取的开销可忽略，B5 修复**没有性能代价**。

---

## 3. 剩余任务的设计定稿（照此实现即可，无需再决策）

### `capture/mss_source.py`
```python
import mss                      # ⚠️ 用 mss.MSS()，绝不用 mss.mss()（见 §2.6 坑 3）
```
- `MssScreenSource` 实现 `ScreenSource` Protocol（`source.py` 已定义）
- `monitors()`：`sct.monitors[1:]` → `MonitorInfo(index=i+1, left, top, width, height,
  is_primary=m.get("is_primary", False), name=m.get("name",""), unique_id=m.get("unique_id",""))`
- `capture_into(dst)`：`grab()` → `bgra_view(screenshot.bgra, w, h)`（零拷贝）→
  `np.copyto(dst, view)`，**不经过 PIL**（v4 的 `Image.frombytes` 是纯浪费）
- `capture()`：允许返回 mss 新建缓冲的视图（零拷贝），供 `differ.compare(frame, adopt=True)`
  配合双缓冲 swap
- 失败一律 `raise ScreenCaptureError(...) from exc`；
  **禁止 v4 的 `except Exception: return 1920, 1080` 兜底**（v4 `mss_source.py:91`）
- 删除 v4 的 `HAS_MSS` try/except ImportError 静默降级与 `_capture_pil` 分支
  （mss 是硬依赖，pyproject 已声明）
- 必须幂等 `close()`；关闭后 `capture()` 抛错

### `encoding/base.py`（VideoEncoder Protocol）
```python
@runtime_checkable
class VideoEncoder(Protocol):
    @property
    def codec(self) -> Codec: ...                  # rdc.protocol.messages.Codec
    def encode(self, frame: npt.NDArray[np.uint8], *,
              force_keyframe: bool = False,
              pts: int | None = None) -> EncodedFrame: ...
    def request_keyframe(self) -> None: ...
    def set_quality(self, quality: int) -> None: ...   # 语义 = CRF（H264）/ JPEG q
    def set_resolution(self, width: int, height: int) -> None: ...
    def stats(self) -> EncoderStats: ...
    def close(self) -> None: ...
```
- `EncodedFrame`：frozen dataclass（`data: bytes, is_keyframe: bool, pts: int,
  width: int, height: int, quality: int, elapsed_ms: float`）
- 提供**契约测试基类**（叮嘱 e）：同一套测试跑 `H264Encoder` 与 `JPEGEncoder`

### `encoding/h264.py`（关键，配方已定）
- `av.CodecContext.create("libx264", "w")`，`pix_fmt="yuv420p"`，
  `options={"preset":"ultrafast","tune":"zerolatency","crf":str(crf)}`，
  `gop_size = int(keyframe_interval_sec * fps)`（默认 2s → 60 @30fps）
- **必须显式 `thread_count >= 4`**（或依赖 auto），**绝不能为 1**（§2.3）
- **必须走 P2 色彩路径**：`cv2.cvtColor(bgra, cv2.COLOR_BGRA2YUV_I420, dst=预分配)` →
  `av.VideoFrame(w,h,"yuv420p")` + 填平面（见 §2.2），**不要用 `from_ndarray(bgra)`**（慢 24ms）
- 填平面要按 `plane.line_size` 分支（本机实测无 padding，但代码须稳健）
- `force_keyframe`：`frame.pict_type = av.video.frame.PictureType.I`（实测有效）
- **不要调 `ctx.close()`**（该方法不存在，会 AttributeError）
- 输出即 `ScreenFrame(codec=Codec.H264, data=packet_bytes, keyframe=p.is_keyframe, ...)`
- 分辨率变化时**重建编码器**（libx264 不支持 in-place 改尺寸）

### `encoding/hw_probe.py`
- 三层结论：`UNLISTED` / `CONSTRUCT_FAILED` / `OPEN_FAILED` / `AVAILABLE`
- **判定只认 `ctx.open()` 成功**（§2.5）
- 候选表：`h264_nvenc, h264_qsv, h264_amf, h264_videotoolbox, h264_vaapi, h264_v4l2m2m`
- 接口保留（CI/其他机器可能有硬编），本机默认全失败 → 落 `libx264`
- 测试：mock 可用/不可用两种场景（叮嘱 c）；本机真实探测结果作参数化断言，
  但**断言写成"open 成功才算可用"的逻辑正确性**，不锁死具体失败原因（换机器会变）

### `encoding/raters.py`
- 输入：`rtt_ms`、`loss_rate`、`queue_depth`（+ `DropOldestQueue` 的 `dropped`/`drop_ratio`）
- 输出：`quality`(CRF ∈ [18,45])、`bitrate`(bps)、`resolution_scale`
- 用 §2.4 的 CRF↔码率实测表做标定
- 收敛测试（叮嘱 d）：模拟"高 RTT + 高丢包 + 队列堆积"→ CRF 必须上升、码率下降；
  反之下降；**并验证不振荡**（连续 N 次调整后输出趋于稳定）

### `encoding/bandwidth.py`
- 从 `encoding/jpeg.py:22` 迁出 `BandwidthProbe`，补类型注解 + docstring
- 加 `time.monotonic()`（v4 用 `time.time()`，系统时钟回拨会算错）

### `encoding/jpeg.py`（适配，叮嘱 e）
- `JPEGEncoder` 实现 `VideoEncoder` Protocol 作回退路径
- **保持 `service.py` 现有调用面不破**：`encode(img)->(bytes,int,str)`、`set_quality`、
  `adjust_quality`、`adjust_quality_by_bandwidth`、`get_stats` 都在（已核对 §调用点）
- ⚠️ 它现在 `from rdc.config._v4_config import Config`（未类型化 v4 层）→
  若要移出 mypy 宽松清单，需改用 `Settings` 或构造参数注入；**这是本阶段唯一需要
  谨慎权衡的地方**（建议：构造参数注入 + 默认值，避免依赖 v4 单例）

### `scripts/bench_diff.py` / `bench_encode.py`
- 直接复用 §2 的测量骨架（含朴素 oracle、多分辨率、多内容类型、JPEG 对比）
- **必须内置 §2.6 的三个防坑措施**（预生成帧池、通道序修正、不触发 mss 废弃 API）
- 输出对齐验收项：4K 差分 ms、编码 ms、多场景带宽降幅

### 测试文件（约 6 个）
`test_capture_monitor.py`、`test_capture_source.py`、`test_capture_diff.py`、
`test_encoding_h264.py`、`test_encoding_hw_probe.py`、`test_encoding_raters.py`
（+ `test_encoding_base.py` 契约测试基类）

---

## 4. 已写文件清单

### 项目内（**git 未跟踪/已修改，本次提交**）
| 文件 | 状态 | 行数级别 |
|------|------|---------|
| `src/rdc/capture/source.py` | 新建，代码完整 | ~450 行 |
| `src/rdc/capture/monitor.py` | 新建，代码完整 | ~300 行 |
| `src/rdc/capture/diff.py` | 重写（替换 v4 172 行版） | ~700 行 |

### 临时探针（系统 Temp 目录，**未纳入版本控制**）
`rdc_probe_stage5.py`、`rdc_probe2.py`～`rdc_probe8.py`（共 8 个）
> 处置决定：**不移入 `scripts/`**。理由：它们是散点测量脚手架（含为定位问题
> 故意保留的错误尝试），其**结论已全部固化在本文件 §2**；正式的基准脚本
> 是任务 10 的 `bench_diff.py`/`bench_encode.py`，届时按 §2 重写、更干净。
> 若你希望保留原始探针代码，说一声我再搬（会额外消耗额度）。

### 未改动
`encoding/*`（全部仍是 v4 搬迁版）、`capture/framerate.py`（v4 版）、
`server/service.py`（叮嘱 B：不碰）、`pyproject.toml`（mypy 宽松清单未动）、
`PROGRESS.md`（未更新）、无 `STAGE5_REPORT.md`。

---

## 5. `capture/diff.py` 遗留问题（**初版 5 项已处理 4 项**）

| # | 初版问题 | 现状 |
|---|---------|------|
| 1 | `_column_padded` 每次调用新分配 33MB | ✅ **已修**（`_ensure_bands` 一次性分配成对补齐缓冲） |
| 2 | 4 个分带属性未在 `__init__` 声明 → mypy 报错 | ✅ **已修**（全部声明；同时暴露并修掉了 `_band_rows` 方法/属性同名冲突） |
| 3 | `ignore_alpha=True` 路径 `np.bitwise_and` 新分配 33MB | ⏳ **未修**（默认 `False`，mss alpha 恒 255，主路径不受影响）。留 `TODO(stage5-followup)`；若开启需改为预分配 `np.bitwise_and(..., out=buf)` |
| 4 | `_dirty_rects` 里 `mask.astype(np.uint8)` 重复分配 + 上游已算过 `mask.any()` | ⏳ **未修**（8KB 分配 + 一次 8160 元素扫描，实测对 3.18ms 无感知影响）。属微优化 |
| 5 | 全文件未经任何验证 | ✅ **已修**：ruff / mypy / 26 项 smoke / 4K 性能 / 全量 pytest 全绿 |
| 6 | **新增遗留**：无 pytest 测试文件 | ⏳ 待补（任务 11）。smoke 断言清单可直接抄 §1.5 表格转成 `tests/unit/test_capture_diff.py` |
| 7 | **新增遗留**：首帧分支的 `elapsed_ms` 在 `_record` 之后重新取时间，与其他分支风格不一致（值正确） | ⏳ 纯风格，可在补测试时顺手统一 |

---

## 6. 恢复工作的建议顺序（额度最省路径）

### ✅ 已完成（本次，无需重做）
- ~~跑 ruff/mypy 验证 3 个文件~~ → **全绿**（§1.5）
- ~~修 §5 问题 1、2~~ → **已修**（问题 3、4 为低优先级，可留）
- ~~首次功能验证~~ → **26 项 smoke + 4K 性能 3.18ms + 全量 pytest 439 零回归**

### ⏳ 下一批从这里开始（**按序，勿并行**）

1. **`capture/mss_source.py`**（当前起点）——替换 v4 的 `ScreenCapturer`
   - ⚠️ 三条硬约束：① `mss.MSS()`（不是 `mss.mss()`，见 §2.6 坑 3）
     ② **双缓冲交替 + 差分 `adopt=True`**（否则每帧多付 6.19ms，见 §1.5）
     ③ 失败一律抛 `ScreenCaptureError`，**禁止** v4 的 `except → return 1920,1080`
   - 零拷贝用 `source.bgra_view(screenshot.bgra, w, h)`，不经 PIL
   - 顺手从 mypy 清单移除 `rdc.capture.mss_source` + `rdc.capture.diff`，跑一次 mypy
2. **批 B**：`encoding/{base,h264,hw_probe,raters,bandwidth}.py` + `jpeg.py` 适配
   —— 全部配方见 §3，关键约束：**`thread_count>=4`（1 会到 65.8ms）**、
   **P2 色彩路径（cv2 I420 + 填平面，4K=11.58ms）**、**不要写 `ctx.close()`（方法不存在）**
3. **批 C**：`scripts/bench_{diff,encode}.py`（内置 §2.6 三大防坑）+ 测试文件
   （smoke 断言清单直接抄 §1.5；差分 oracle 见 §2.1 的 17 种分辨率表）
4. 全量 `ruff` + `mypy` + `pytest` + `--cov` 出 `capture/`+`encoding/` 覆盖率（线 ≥85%）
5. 写 `STAGE5_REPORT.md`（含 §2 全部数据表 + 多场景带宽 + 覆盖率 + 阶段 6 起点）、
   更新 `PROGRESS.md`、**删除本文件**
6. commit：`feat(stage5): add vectorized diff and PyAV H.264 encoding`

### 验收目标对照
| 验收项 | 目标 | 实测 | 状态 |
|--------|------|---------|------|
| 4K 差分 | <5ms | **3.18ms**（生产路径，`ScreenDiffer` 端到端） | ✅ **已验证达标** |
| 编码延迟 | <40ms | **11.58ms** @4K（P2 路径，探针级） | ⏳ 待 `h264.py` 实现后正式验证 |
| 带宽降幅 | ≥40% | **63%～98.8%**（11 个场景） | ⏳ 待 `bench_encode.py` 正式取样 |
| capture/+encoding/ 覆盖率 | ≥85% | 未测 | ⏳ 待测试文件 |
| ruff/mypy 全绿 | 0 error | ✅ 0 / ✅ 0（64 文件） | ✅ |
| pytest 全绿 + 零回归 | — | ✅ 439 passed | ✅ |

---

## 7. 环境备忘（本次踩到/确认的）

- `uv` = `C:\Users\walton\.local\bin\uv.exe`，`git` = `C:\Program Files\Git\cmd\git.exe`（均不在 PATH）
- PowerShell 5.1：勿给原生命令加 `2>&1`（stderr 已单独捕获，加了会误报退出码）
- 本次探针**确实需要真实屏幕**（`mss.MSS()` 抓屏取样），已通过权限；
  正式测试里**禁止**依赖真实屏幕，一律用 `ArrayScreenSource`（`source.py` 已提供）
- 项目根：`C:\Users\walton\Desktop\remote_desktop\control_v4\control_v4.0\control_v4.0.3`
- 依赖全部已装（`av 18.1.0` / `cv2 5.0.0` / `numpy 2.5.3` / `mss 10.2.0`），无需再 `uv sync`

---

**本文件在阶段 5 完成后删除，其数据并入 `STAGE5_REPORT.md`。**

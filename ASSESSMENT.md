# 项目现状评估 — remote_desktop v4.0.3

> 评估日期：2026-09-19　评估方式：全量源码阅读 + 环境实测（非推断）
> 基线：git tag `v4.0.3-baseline` @ `dfe2281`，分支 `master`，48 个受版本控制文件

---

## 1. 规模总览

| 指标 | 实测值 |
|------|--------|
| Python 文件数 | 45 |
| Python 总行数 | **7,505** |
| 最大单文件 | `service/daemon.py` **1,057 行** |
| 次大 | `gui/main_window.py` 778、`config.py` 489、`gui/screen_widget.py` 378 |
| 单元测试 | **0 个**（无 `tests/` 目录） |
| `pyproject.toml` | **不存在** |
| Lint / 类型检查配置 | **不存在**（无 ruff/mypy/flake8 任何配置） |
| CI 配置 | **不存在**（无 `.github/`） |
| 构建脚本 | 仅 `build.bat`（PyInstaller `-F -w`） |

**结论：项目处于"零工程化"状态。** 阶段 1 的验收条件里写的"原 v4 测试能跑"无法成立 —— 根本不存在 v4 测试。这一条需改写为"建立测试骨架 + 为搬迁后的代码补首批回归测试"。

### 模块清单（按行数）

| 模块 | 行数 | 职责 | v5 处置 |
|------|------|------|---------|
| `service/daemon.py` | 1057 | 被控端服务：监听/认证/封禁/广播/输入执行/剪贴板 | **拆分**为 server/{service,auth,broadcaster,banlist}.py |
| `gui/main_window.py` | 778 | 主控端主窗口 + 连接状态机 | **重写** PySide6 |
| `config.py` | 489 | 配置单例 + 版本迁移 + DPAPI/Keychain/libsecret + bcrypt | **拆分**为 config/ + secrets/ |
| `gui/screen_widget.py` | 378 | Canvas 画面显示 + 鼠标键盘捕获 | **重写** QGraphicsView |
| `gui/settings_dialog.py` | 326 | 三页签设置对话框 | 重写 QTabWidget |
| `network/control_channel.py` | 314 | 控制通道 + TLS 包装 + TOFU 指纹 + 认证握手 | 重写 asyncio |
| `network/screen_channel.py` | 254 | 画面通道 + 发送队列 | 重写 asyncio |
| `gui/tray.py` | 248 | pystray 系统托盘 | 重写 QSystemTrayIcon |
| `screen/encoder.py` | 233 | JPEG 编码 + 带宽探测 + 质量自适应 | 改造，新增 H.264 路径 |
| `main.py` | 233 | 入口 + Tkinter 启动菜单 | 拆为 cli.py + ui/launcher.py |
| `core/protocol.py` | 215 | 15B 分帧 + CRC + zlib + PacketBuffer | 保留分帧，新增 PB/HMAC |
| `gui/toolbar.py` | 205 | 工具栏 | 重写 |
| `input/dpi.py` | 203 | 三平台 DPI 探测 | 搬迁 + 抽 PlatformAdapter |
| `service/autostart.py` | 194 | winreg/launchd/systemd 自启 | 搬迁 |
| `screen/diff.py` | 172 | 块级差分 + 自适应块大小 | **向量化重写** |
| `gui/clipboard.py` | 161 | 剪贴板双向同步 | 重写 QClipboard |
| `input/mouse.py` / `keyboard.py` | 139 / 134 | pynput 监听 + pyautogui 执行 | 降级为 legacy 后端 |
| `gui/performance_panel.py` | 129 | FPS/延迟/带宽面板 | 重写 |
| `core/reconnect.py` | 121 | 指数退避重连线程 | 改 asyncio |
| `service/pid_manager.py` | 115 | PID 文件 | 搬迁 |
| `logger.py` | 115 | 全局单例 + EmojiFilter + 轮转 | 改造 structlog |
| `core/heartbeat.py` | 109 | 心跳线程 | 改 asyncio |
| `screen/renderer.py` | 108 | PIL→ImageTk 渲染 | **删除**（QPixmap 取代） |
| `utils/system.py` | 106 | 锁屏/关机/管理员检测 | 搬迁 |
| `screen/capturer.py` | 99 | mss 截图 | 搬迁 + 注入点 |
| `screen/framerate.py` | 92 | 动态帧率 | 搬迁 |
| `utils/security.py` | 91 | TOFU 证书指纹信任列表 | 搬迁 |
| `constants.py` | 87 | 全局常量 | 搬迁 |
| `gui/quality_indicator.py` | 85 | Canvas 质量指示灯 | 重写 QPainter |
| `network/kcp_channel.py` | 85 | KCP 通道 | **☠️ 删除（死代码）** |
| `gui/status_bar.py` / `status_panel.py` | 80 / 72 | 状态栏 | 重写 |
| `utils/ip.py` | 64 | 本地 IP 探测 | 搬迁 |
| `core/rate_limiter.py` | 52 | 滑动窗口限流 | 搬迁 + 扩展 |
| `input/modifiers.py` | 43 | 修饰键状态集 | 搬迁 |
| `exceptions.py` | 30 | 7 个异常类 | 搬迁 + 扩展 |
| 各级 `__init__.py` ×8 | 89 | — | 重建 |

---

## 2. 环境实测结果（与任务书描述的偏差）

### 2.1 已装依赖 — 实测确认

```
PySide6 6.11.2 (Qt 6.11.2)   cryptography 50.0.1   bcrypt 5.0.0
Pillow 12.3.0                mss 10.2.0            numpy 2.5.3
av 18.1.0 (libavcodec 62.28) opencv-python-headless 5.0.0.93
pynput 1.8.2                 pyperclip 1.11.0      structlog 26.1.0
pydantic 2.13.5              pyautogui 0.9.54
Python 3.14.3 @ .venv/Scripts/python.exe
```

### 2.2 ⚠️ 偏差 1：任务书声称已装，实际**未装**

| 包 | 任务书 | 实测 | 影响 |
|----|--------|------|------|
| `pystray` | 已装 | **MISS** | v4 `gui/tray.py` 现在**已经跑不起来**（该文件用 try/except ImportError 兜住，静默降级为无托盘） |
| `psutil` | 已装 | **MISS** | v4 代码未使用，仅影响 v5 性能测试的 CPU/内存采集 |

### 2.3 ⚠️ 偏差 2：**全部**工具链与测试依赖未装

`ruff` `mypy` `pytest` `pytest-cov` `pytest-asyncio` `pytest-qt` `hypothesis` `protobuf` `grpcio-tools` `bandit` `pre-commit` `aiohttp` `sounddevice` `nuitka` `pytest-xvfb` `secretstorage` `locust` — **无一安装**。

任务书写"环境已就绪，不要重复安装"，但阶段 1 验收第一条就是 `ruff check 0 error`、`mypy 0 error`，阶段 3 要 Protobuf，阶段 6 要 pytest-qt。**这些必须先装才能开工。**

✅ 已用 `uv pip install --dry-run` 实测：以上全部包在 **Python 3.14.3 上均可解析安装**，版本为
`protobuf 7.36.2` / `grpcio-tools 1.84.0` / `pytest 9.1.1` / `mypy 2.3.1` / `ruff 0.16.8` /
`hypothesis 6.168.0` / `bandit 1.9.4` / `aiohttp 3.14.3` / `nuitka 4.2.1` / `pytest-qt 4.5.0` / `sounddevice 0.5.6`。
→ **不存在 Python 3.14 生态兼容阻塞**（但见 2.6 的 CI 版本冲突）。

### 2.4 ⚠️ 偏差 3：工具不在 PATH

| 工具 | 实际位置 | 状态 |
|------|----------|------|
| `uv` | `C:\Users\walton\.local\bin\uv.exe` | 不在 PATH，需全路径调用 |
| `git` | `C:\Program Files\Git\cmd\git.exe` | 不在 PATH，需全路径调用 |

### 2.5 🔴 偏差 4：**本机无任何 C 编译器**

实测 `cl.exe` / `gcc.exe` / `clang.exe` / `link.exe` **全部缺失**，无 Visual Studio（无 vswhere）、无 MinGW、无 MSYS2。

**直接后果：Nuitka 在本机无法运行**（Nuitka 必须调用 C 编译器生成二进制）。任务书阶段 10 推荐 Nuitka，这条路在当前机器上走不通。
✅ **PyInstaller 提供预编译 wheel，不需要 C 编译器**，本机可直接验证打包。→ 见决策问题 Q1。

### 2.6 ⚠️ 偏差 5：Python 版本与 CI 矩阵冲突

- 本机开发环境：**Python 3.14.3**
- 任务书阶段 10 要求 CI 矩阵：**Python 3.10 – 3.13**

3.14 不在要求的矩阵内，且 3.10 与 3.14 的依赖 ABI 差异大（numpy 2.5 / grpcio 1.84 / PySide6 6.11 对 3.10 的支持需单独验证）。→ 见决策问题 Q2。

### 2.7 ⚠️ 偏差 6：git 无 remote

`git remote -v` 输出为空。阶段 10 的 GitHub Actions CI/CD、GitHub Releases 自动更新检查、`pyupdater` 全部依赖远端仓库。**本机无法端到端验证任何一项**，只能交付配置文件 + 在本地模拟。→ 见决策问题 Q5。

---

## 3. 性能基线实测（阶段 5 验收依据）

任务书要求"4K 60fps 软编 < 40ms"、"带宽降低 ≥40%"。我在当前机器上用 PyAV 实测：

### 3.1 编码器可用性

| 编码器 | 状态 |
|--------|------|
| `libx264`（软编） | ✅ 可用 |
| `libvpx-vp9`（软编） | ✅ 可用 |
| `libopus` / `opus` / `aac` / `libmp3lame`（音频） | ✅ 全部可用 |
| `h264_nvenc` | ❌ `PermissionError: Operation not permitted` |
| `h264_qsv` | ❌ `ArgumentError: returned 22 (Invalid argument)` |
| `h264_amf` | ❌ `UnknownError: 1313558101` |

→ **确认任务书判断正确：硬编必须跳过**。但注意 `av.codec.Codec('h264_nvenc')` 构造**不报错**，只有 `ctx.open()` 才失败 —— 硬编探测接口必须实测 open，不能只查名字，否则会误判。

### 3.2 软编延迟（libx264, `preset=ultrafast`, `tune=zerolatency`, `crf=28`, yuv420p）

| 分辨率 | 实测延迟 | 实测吞吐 | 目标 | 判定 |
|--------|----------|----------|------|------|
| 1920×1080 | **3.3 ms/帧** | 300 fps | — | ✅ |
| 3840×2160 | **11.2 ms/帧** | 89 fps | < 40 ms | ✅ **余量 3.6×** |

→ 4K 60fps 软编目标**在当前机器上可达成**，且有充分余量。注意这是纯编码耗时，端到端还要加截图 + 差分 + 色彩空间转换。

### 3.3 带宽压缩比（模拟桌面场景：1080p，静态背景 + 每帧移动一个色块，30 帧均值）

| 编码 | 每帧大小 |
|------|----------|
| v4 现状：JPEG q60 `optimize=True subsampling=0` | 25 KB |
| v5 目标：H.264 crf28 zerolatency | **1.1 KB** |
| **降幅** | **96%** |

→ 远超"≥40%"目标。**但这是理想静态桌面场景**；高熵场景（视频播放、游戏）差距会大幅收窄。真实验收需在多种场景取样。

⚠️ 补充：H.264 是**有状态帧间编码**，不能像 JPEG 那样只发"变化的裁剪区域"。v4 的差分裁剪机制（`get_changed_region`）与 H.264 **在架构上不兼容** —— 要么放弃区域裁剪改用全帧 H.264（靠编码器自身的帧间压缩，实测效果更好），要么 H.264 走全帧 + JPEG 走差分双轨。这是阶段 5 的核心架构决策，我倾向前者。

---

## 4. 协议现状（v5 向后兼容的依据）

### 4.1 🔴 关键发现：v4 存在**两套**并行的 wire protocol

任务书阶段 3 只提到"保留 v4 分帧头"，但实际 v4 有：

**A. 主数据流：15 字节二进制分帧**（`core/protocol.py:19`）
```
">4sBBB I I"  →  Magic "RDPC"(4B) + Version(1B) + Type(1B) + Flags(1B) + Length(4B BE) + CRC32(4B BE)
```
实测 `struct.calcsize` = **15 字节**（大端，无对齐填充）。
⚠️ 源码注释 `core/protocol.py:18` 写的是"16 字节"，**注释是错的**，实际 15。
⚠️ 注意 `PROTOCOL_VERSION = 1`（`constants.py:7`），而 `__init__.py` 声明 `__protocol_version__ = 4` —— **两个版本号不一致且后者从未被使用**。

**B. 认证握手：裸 4 字节长度前缀，完全不走分帧**
- 客户端发：`[4B len BE][JSON {"password": "..."}]`（`control_channel.py:78-79`）
- 服务端回：`[4B len BE][JSON {"status":"AUTH_OK","token":"..."}]`（`daemon.py:791-800`）
- 画面通道发：`[4B len BE][JSON {"token":"..."}]`（`screen_channel.py:102-103`）

→ **这是 v4 兼容层的最大陷阱**：认证握手与业务数据流是两套独立的编解码，且握手发生在 TLS 之后、分帧协议之前。v5 若只实现"分帧 + Protobuf"而漏掉这套裸握手，v4 客户端将**卡在认证阶段无法连接**。任务书阶段 3 未提及此协议。

### 4.2 消息类型与 payload 字段清单

包头 Type 字段取值（`constants.py:10-17`）：

| Type | 值 | 方向 | Payload 结构 |
|------|-----|------|-------------|
| `PKT_TYPE_SCREEN` | 0x01 | S→C | `[4B meta_len][meta JSON][图像字节]`；meta 字段：`width, height, quality, is_diff, offset_x, offset_y, timestamp, checksum`（`checksum` = MD5 前 8 位十六进制） |
| `PKT_TYPE_CONTROL` | 0x02 | 双向 | JSON。鼠标：`{"type":"mouse","action":"move\|press\|release\|click\|scroll","x","y","button":"left\|right\|middle","delta"}`；键盘：`{"type":"key","action":"press\|release\|type","key","modifiers":[...],"text"}`；欢迎：`{"type":"welcome","version","timestamp"}` |
| `PKT_TYPE_HEARTBEAT` | 0x03 | 双向 | `{"type":"ping"\|"pong","timestamp":float}` |
| `PKT_TYPE_FILE` | 0x04 | — | **常量已定义但代码中无任何使用** |
| `PKT_TYPE_CLIPBOARD` | 0x05 | 双向 | `{"type":"clipboard","text","chunk","total"}` |
| `PKT_TYPE_AUTH` | 0x10 | C→S | **常量已定义但实际认证走的是 §4.1-B 裸握手，此类型从未使用** |
| `PKT_TYPE_AUTH_RESPONSE` | 0x11 | S→C | 同上，**从未使用** |
| `PKT_TYPE_STREAM_CONTROL` | 0x20 | C→S | `{"cmd":"start"\|"stop"\|"config","config":{"quality","max_fps","min_fps","enable_diff","enable_compression"}}` |

Flags 位（`constants.py:20-23`）：`COMPRESSED=0x01`（zlib level 3）、`ENCRYPTED=0x02`（**代码中仅有 TODO 占位，从未实现**）、`HAS_CRC=0x04`。
→ v5 新增 `PKT_FLAG_HMAC` 需避开 0x01/0x02/0x04，可用 **0x08**。

`MAX_PACKET_SIZE = 32MB`（`constants.py:52`），超长包会清空整个接收缓冲区。

### 4.3 🔴 兼容性行为地雷：v4 客户端的认证行为由**客户端本地配置**决定

`control_channel.py:56-76`：v4 客户端读**自己本地** `config.json` 的 `enable_auth` 和 `password_hash` 来决定是否发送认证消息：
- `enable_auth=False` → **完全不发任何握手字节**，直接进入分帧数据流
- `enable_auth=True` 且 `password_hash` 非空 → 弹窗要密码后发送
- `enable_auth=True` 且 `password_hash` 为空 → 发送空密码 `{"password":""}`

服务端侧（`daemon.py:444-479`）则由**服务端本地** `enable_auth` 决定是否读取握手。

→ **两端配置不一致就会字节流错位**：若 v5 服务端 `enable_auth=True` 而 v4 客户端 `enable_auth=False`，服务端会阻塞等待 4 字节长度前缀，客户端却直接发分帧数据 → 服务端把 `RDPC` 魔数当长度解析，得到 1.3GB 的荒谬长度，认证失败断连。
这正是 v4.0.3 的 commit 注释里反复修复的 bug 类型（`daemon.py:439-443` 有 5 行注释专门解释）。**v5 必须设计成协议自描述**（例如服务端先探测首 4 字节是否为 `RDPC` 魔数来判断对端是否走握手），而不能依赖两端配置对齐。

---

## 5. 明显坏味道（逐项带行号证据）

### 5.1 🔴 静默吞错 — 硬性约束 6 要求清零，实测 ~49 处

| 类型 | 数量 | 代表位置 |
|------|------|----------|
| `except Exception: pass` / `except Exception as e: pass` | ~42 | `daemon.py:191,195,207,213,220,390,982,986,997,1003,1009,1163`；`main_window.py:77,300,310,400,637,641`；`clipboard.py:68,80,179,184`；`tray.py:251,264,274`；`ip.py:22,50`；`dpi.py:70,164`；`config.py` ×7 |
| 裸 `except:` | 7 | `system.py:96,109`；`main.py:103`；`daemon.py:364,370`；`control_channel.py:321,333`；`screen_channel.py:237,258` |
| `except Exception: return False/None`（静默降级） | 多处 | `config.py:139,167,186,202`（三平台密钥环失败全部静默返回 False）；`encoder.py:21`（H.264 探测失败静默） |

特别严重的一处：`screen_channel.py:236-238` 与 `control_channel.py:320-322`，socket 关闭失败被裸 `except:` 吞掉，导致连接泄漏无法诊断。

### 5.2 🔴 功能性 Bug（非风格问题，是真的错）

| # | 位置 | 问题 |
|---|------|------|
| B1 | `screen/encoder.py:144-163` | **`encode_h264()` 是无效代码**。`cv2.imencode('.h264', ...)` 不是合法用法（imencode 只支持图像格式）；`VIDEOWRITER_PROP_QUALITY` 不是 imencode 参数；返回的 `buffer` 在此调用形态下不可用。且 `_check_h264_available()`（第 15-22 行）仅创建一个 fourcc 整数就返回 True，**毫无检测意义**。所幸该函数从未被调用（`daemon.py:1119,1128` 调 `encode(img)` 不传 `use_h264`，默认 False）→ **v4 实际一直是纯 JPEG**。 |
| B2 | `service/daemon.py:940-954` | **在服务端线程池的工作线程里 `tk.Tk()`** 处理剪贴板。Tk 非线程安全，且在 Windows 服务会话 / 无桌面环境下必崩。异常被 `except Exception` 吞掉 → 剪贴板功能静默失效，日志里只有一行 error。 |
| B3 | `service/daemon.py:1161` | **广播线程内同步 `conn.sendall(frame_bytes)`**，逐个客户端串行发送。任一慢客户端（弱网/后台）会**阻塞所有客户端**的画面推送 —— 典型 head-of-line blocking。这正是任务书阶段 4 要用 asyncio.Queue + 丢旧帧解决的问题。 |
| B4 | `screen/diff.py:110-126` + `:139-142` | **块大小可变导致坐标系错乱**。`_adjust_block_size` 会动态修改 `self.block_size`（±4，范围 16-128），但 `get_changed_blocks` 返回的块坐标 `(x,y)` 与 `get_changed_region` 里的 `min_x * self.block_size` 乘法，若两调用之间 block_size 发生变化，裁剪区域就会偏移。这是一个**未被发现的生产 bug**。 |
| B5 | `screen/diff.py:128-153` | 差分**只发送单个外接矩形**。若变化分散在屏幕两端（左上角时钟 + 右下角通知弹窗），外接矩形会覆盖大半个屏幕，差分收益归零，退化为全屏传输。 |
| B6 | `service/daemon.py:1162` 等 | `_stats['bytes_sent'] += n` 在广播线程 + N 个客户端处理线程中**并发自增且无锁**。CPython 下字典值的读-改-写非原子，统计数字会丢失。 |
| B7 | `service/daemon.py:551` | `_send_welcome` 硬编码 `'version': '4.0.0'`，与 `__init__.py` 的 `4.0.3` 不符。v5 需处理 v4 客户端对版本字符串的潜在依赖。 |
| B8 | `main.py:34` | `from __init__ import __version__` —— 把 `__init__` 当普通模块名导入，是反模式，**迁移到 `src/rdc/` layout 后必然失效**。 |
| B9 | `build.bat` | 脚本内写死 "远程桌面控制 v4.0.2 打包脚本"，而代码版本是 4.0.3 —— **构建脚本与代码版本已不同步**。 |
| B10 | `gui/quality_indicator.py:49` | `self.indicator.after(300, ...)` 在 `_draw_indicator(pulse=True)` 中被调用，而该方法可从 `main_window.py:647` 的重连路径（非主线程）触发 → 潜在跨线程 Tk 调用。 |

### 5.3 性能热点（阶段 5 目标的主要障碍）

| 位置 | 问题 | 量化 |
|------|------|------|
| `screen/diff.py:91-108` | **双重 Python for 循环**逐块 `np.array_equal` | 4K@32px 块 = 120×68 = **8,160 次循环 + 8,160 次数组比较/帧**。要达成"4K 差分 <5ms"必须向量化为单次 `cv2.absdiff` + reshape + `np.any(axis)` |
| `screen/diff.py:117` | `_adjust_block_size` 每帧对**整幅图**做 `np.abs(int16 - int16)` | 4K RGB → int16 需 **2 × 24MB 临时数组**，比主差分循环还贵，且每帧都跑 |
| `screen/diff.py:47,59` | `curr_arr.copy()` + `img.copy()` 每帧两次全图拷贝 | 4K 下 ~24MB×2 额外分配/帧 |
| `screen/diff.py:21` | 同时保留 `_last_frame`(ndarray) 和 `_last_image`(PIL) 两份 | 内存翻倍，且 `_last_image` **从未被读取** → 纯浪费 |
| `core/protocol.py:41` | 每个包都 `zlib.compress(level=3)` | 对已压缩的 JPEG/H.264 数据做 zlib 是**纯 CPU 浪费且可能增大体积**，v5 应对已压缩载荷跳过 |

### 5.4 结构性坏味道

| 类别 | 数量 | 证据 |
|------|------|------|
| **循环依赖症状**：函数体内延迟 `from logger import get_logger` | **18 处** | `config.py` ×9（315,365,407,436,451,461,471,490 + 更多）、`core/protocol.py` ×5（43,85,98,112,209）、`daemon.py` ×3、`main.py` ×1。根因：`logger` ↔ `config` 隐式循环 |
| `sys.platform == 'win32'` 硬分支 | 9 | `config.py:208,443` 等、`dpi.py`、`pid_manager.py` ×2、`tray.py` ×2、`system.py` |
| `ctypes.windll` 直接调用 | 17 | `config.py`(DPAPI ×4)、`dpi.py`(×10)、`system.py`(×2)、`pid_manager.py`(×1) |
| `winreg` 注册表操作 | 8 | `service/autostart.py` |
| **平台抽象层** | **不存在** | 所有平台特化代码内联在业务函数中，无 `PlatformAdapter` 接口 |
| 重复代码：DPAPI `DATA_BLOB` 结构体定义 | 2 份完全相同 | `config.py:80-81` 与 `config.py:103-104` |
| 重复代码：`ConnectionState` 枚举 | 2 份 | `gui/main_window.py:37` 与 `gui/toolbar.py:16`（名为 `ConnState`，字段完全相同） |
| 重复代码：修饰键映射表 | 2 份 | `gui/screen_widget.py:349-353` 与 `constants.py:97-101` |
| 重复代码：IP 获取 + tooltip 构建 | 3 份 | `gui/tray.py:63-64,149-150,164-165`；`_update_tooltip`/`_get_tooltip_text`/`_show_status` |
| 超长函数（>80 行） | 5 | `main_window.py:80-198`(~100)、`settings_dialog.py:31-175`(~130)、`dpi.py:86-167`(~80)、`daemon.py:419-545`(`_handle_client`, ~127)、`daemon.py:1102-1195`(`_broadcast_loop`, ~94) |
| 全局可变单例 | 3 | `logger.py:19` `_logger`、`utils/security.py:104` `_fingerprint_manager`、`config.py:226` `Config._instance` |
| 类型注解覆盖率 | ~60% | `gui/` 回调大量裸 `Callable` 无泛型参数；`screen_widget._mouse_callbacks: list` 无元素类型 |
| 公开方法 docstring 缺失 | ~30% | `toolbar.py` 的 `update_*` 系列、`status_bar.py` 的 setter |
| 魔法数字 | 多处 | `main_window.py:512` `if packet.type != 1`（应用 `PKT_TYPE_SCREEN`）、`main_window.py:115-116` 硬编码 1920×1080、`screen_widget.py:49-50` `_MOVE_THRESHOLD=3`/`_MOVE_INTERVAL_MS=30`、`diff.py:74` 变化阈值 `0.15`、`diff.py:118` `>30`、`daemon.py:1118` `len(changed_blocks) > 100`、`quality_indicator.py:64-83` 全部延迟/FPS 阈值 |
| MD5 使用 | 1 | `core/protocol.py:151` `hashlib.md5(img_data).hexdigest()[:8]` → **bandit B324 会报 high/medium**，阶段 9 需改为 `usedforsecurity=False` 或换 SHA-256 截断 |

### 5.5 密码存储三轨制（任务书要求删除，实际比描述的更复杂）

任务书说"删除 v4 的 `saved_password` / `password_hash` 双轨制"。实测实际是**三轨 + 一条隐式升级路径**：

1. **服务端**：`password_hash` — bcrypt rounds=12（`config.py:411-422`）
2. **客户端**：`saved_password` — 按平台分流：
   - Windows：`"DPAPI1:" + base64(DPAPI密文)`，附加熵 `b"RemoteDesktop-Control-v3"`（`config.py:70`，**熵字符串写的是 v3，代码是 v4.0.3**）
   - macOS：字面量 `"KEYCHAIN:"`，真实值在 Security.framework
   - Linux：字面量 `"LIBSECRET:"`，真实值在 secretstorage
3. **遗留**：无前缀的 base64 **明文**存储，读取时自动升级为 DPAPI（`config.py:517-526`）
4. **配置迁移**：v4→v5 迁移时，旧 SHA256 `password_hash` 被**直接清空**并强制用户重设（`config.py:310-319`）

→ v5 若删除双轨制，必须处理**存量用户**：已有 `DPAPI1:` 前缀密码的用户升级后会丢失记住的密码。需要一次性迁移读取 → 写入新 `SecretStore` → 清除旧字段。

### 5.6 线程模型

创建线程的位置共 **13 处**：`daemon.py:161,351,354,1080`、`control_channel.py:259`、`screen_channel.py:139,141`、`main_window.py:379`、`heartbeat.py:42`、`reconnect.py:50`、`clipboard.py:34`(线程池)、`tray.py:145,243`。
`threading.Lock` 8 处、`threading.Event` 4 处、`threading.Semaphore` 1 处。

✅ **好消息**：v4.0.3 已系统性修复跨线程 Tkinter 问题，主控端 UI 更新全部通过 `root.after_idle()` 调度（`main_window.py` 共 11 处）。这个模式可**直接映射**为 Qt 的 Signal/Slot（Qt 的跨线程 signal 投递语义等价于 `after_idle`）。→ 阶段 6 迁移风险因此显著降低。
⚠️ 唯一残留：§5.2 B10。

### 5.7 可测试性

| 难度 | 模块 | 障碍 | 需要注入点 |
|------|------|------|-----------|
| 🔴 高 | `screen/capturer.py` | 直接调 mss/ImageGrab，需真实屏幕 | `ScreenSource` 协议 |
| 🔴 高 | `input/mouse.py`, `keyboard.py` | pyautogui 直接操控真实系统，**测试时会真的动鼠标** | `InputBackend` 协议（阶段 7 正好要做） |
| 🔴 高 | `gui/*` 全部 | 绑定 Tkinter root | PySide6 + `pytest-qt` `qtbot` |
| 🟡 中 | `input/dpi.py` | `ctypes.windll` 在非 Windows 不可用 | `PlatformAdapter` |
| 🟡 中 | `service/autostart.py` | winreg/subprocess 直接改系统 | 注册表/shell 适配层 |
| 🟡 中 | `utils/ip.py` | `socket.connect` 到外部 DNS（8.8.8.8） | socket factory |
| 🟢 低 | `core/protocol.py`, `rate_limiter.py`, `heartbeat.py`, `reconnect.py`, `utils/security.py` | 纯算法/纯 IO | 无需注入，**可直接达 85%+ 覆盖** |

**核心结论**：v4 代码**零依赖注入**，所有外部依赖（屏幕、socket、注册表、系统输入）都是硬编码直接调用。要达成"核心模块覆盖率 ≥85%"，**必须先做依赖倒置改造，否则测试根本写不出来**。这部分工作量已计入阶段 1/5/7。

### 5.8 死代码与依赖使用

| 项 | 状态 |
|----|------|
| `network/kcp_channel.py` (85 行) | ☠️ **确认死代码** — 全项目无任何 import，`network/__init__.py` 未导出 → 直接删除 |
| `screen/renderer.py` (108 行) | 🔶 v5 中被 QPixmap/QGraphicsView 取代 → 删除 |
| `screen/diff.py:21` `_last_image` | 🔶 赋值但从未读取 → 删除 |
| `constants.py` `PKT_TYPE_FILE` / `PKT_TYPE_AUTH` / `PKT_TYPE_AUTH_RESPONSE` | 🔶 定义但从未使用 → v5 中 FILE 用于阶段 8，AUTH 两个应删除（实际走裸握手） |
| `PKT_FLAG_ENCRYPTED` | 🔶 仅 TODO 占位（`protocol.py:55-57,117-119`）→ v5 应删除（TLS 已提供加密）或复用为 HMAC 标志位 |
| `__init__.py` `__protocol_version__ = 4` | 🔶 从未被引用 → v5 应真正启用或删除 |
| 依赖使用 | `pyautogui`(mouse/keyboard/daemon)、`pynput`(mouse/keyboard)、`mss`(capturer)、`Pillow`(renderer/capturer/screen_widget/tray)、`numpy`(diff/encoder)、`pyperclip`(clipboard)、`pystray`(tray) — **无未使用依赖**（但 pystray 实际未安装，见 §2.2） |

---

## 6. UI 迁移工作量（Tkinter → PySide6）

| 文件 | 行数 | 难度 | 主要障碍 |
|------|------|------|----------|
| `main_window.py` | 778 | 🔴 高 | 连接状态机 + 11 处 `after_idle` + `threading.Event` 阻塞模式 → asyncio；`simpledialog`/`messagebox` → QDialog。**但 `after_idle`→Signal 是机械映射，风险可控** |
| `screen_widget.py` | 378 | 🔴 高 | PIL `ImageTk` → `QPixmap` 全面重写；Canvas 事件绑定 → `QGraphicsView`；持续移动定时器 → `QTimer` |
| `tray.py` | 248 | 🔴 高 | pystray（**且未安装**）→ `QSystemTrayIcon`；PIL `ImageDraw` → `QPainter`；菜单重建逻辑不同 |
| `settings_dialog.py` | 326 | 🟡 中 | `ttk.Notebook` → `QTabWidget`；`IntVar/StringVar trace` → Signal/Slot，结构可直接映射 |
| `toolbar.py` | 205 | 🟡 中 | ttk 控件 → QWidget/QAction；Combobox→QComboBox；Scale→QSlider |
| `clipboard.py` | 161 | 🟡 中 | pyperclip → `QClipboard`；线程池 → `asyncio.to_thread`；`after` 轮询 → `QTimer` |
| `quality_indicator.py` | 85 | 🟡 中 | Canvas 圆点 → QPainter；脉冲动画 → `QPropertyAnimation` |
| `performance_panel.py` | 129 | 🟢 低 | LabelFrame → QGroupBox；`after` → QTimer |
| `status_bar.py` / `status_panel.py` | 80 / 72 | 🟢 低 | 纯 Label 布局 |
| `main.py:show_gui()` | ~100 | 🟢 低 | Tkinter 启动菜单 → QDialog |

**建议迁移顺序**：先做 `status_bar` → `performance_panel` → `toolbar` 三个低风险件验证 PySide6 脚手架与主题系统，再攻 `screen_widget`（最难、最核心）和 `main_window`（状态机），最后 `tray` 和 `settings_dialog`。

---

## 7. 阻塞项汇总

| 级别 | 阻塞项 | 影响阶段 |
|------|--------|----------|
| 🔴 硬阻塞 | 无 C 编译器 → **Nuitka 无法在本机运行** | 阶段 10 |
| 🔴 硬阻塞 | 全部测试/lint/构建依赖未安装（ruff/mypy/pytest/protobuf/...） | 阶段 1 起全部 |
| 🔴 硬阻塞 | 不存在任何 v4 测试 → 阶段 1 验收条件"原 v4 测试能跑"无法满足 | 阶段 1 |
| 🟡 软阻塞 | git 无 remote → CI/CD 与自动更新无法端到端验证 | 阶段 10 |
| 🟡 软阻塞 | Python 3.14.3 开发环境 vs CI 矩阵要求 3.10-3.13 | 阶段 1、10 |
| 🟡 软阻塞 | `pystray`/`psutil` 未装（任务书声称已装） | 阶段 6（v4 托盘已不可用）、性能测试 |
| 🟡 软阻塞 | `uv`/`git` 不在 PATH，需全路径调用 | 全部 |
| 🟢 已排除 | ~~Python 3.14 生态不兼容~~ → dry-run 实测全部可装 | — |
| 🟢 已排除 | ~~4K 软编达不到 40ms~~ → 实测 11.2ms，余量 3.6× | — |
| 🟢 已排除 | ~~带宽降幅达不到 40%~~ → 实测桌面场景 96% | — |
| 🟢 已排除 | ~~硬编可尝试~~ → nvenc/qsv/amf 全部 open 失败，确认跳过 | — |

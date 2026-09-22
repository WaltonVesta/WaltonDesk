> ⚠️ **2026-09-22 范围重大调整**
> 
> 用户决定砍掉"v4 兼容"（用户只用 v5，v4 不稳定）：
> - §5 Q4（v4 兼容保留期）**作废**
> - §3 阶段 3 的 `legacy_v4.py` / 协议双版本 / golden-bytes 测试**作废**
> - 硬性约束 5（v5.0 保留 v4 兼容）**作废**
> 
> 详见 `PROGRESS.md` 的"⚡ 范围重大调整"和 `STAGE2_REPORT.md` §0。
> 
> 本文件保留为"原始计划"（决策演化轨迹），实际执行以 PROGRESS + STAGE*_REPORT 为准。




# REFACTOR_PLAN.md — remote_desktop v4.0.3 → v5.0.0

> 配套文档：`ASSESSMENT.md`（现状评估，含全部行号级证据与实测数据）
> 状态：**待确认** — 阶段 1 尚未开始，未写任何代码
> 基线：git `v4.0.3-baseline` @ `dfe2281`

---

## 0. 开工前必读：三个前置阻塞项

这三项不解决，阶段 1 的验收条件（`uv sync` 成功 + ruff 0 error + mypy 0 error）在物理上无法达成。

### 阻塞 A：测试/lint/构建依赖全部未安装

任务书写"环境已就绪，不要重复安装"，但实测 `ruff` `mypy` `pytest` `pytest-cov` `pytest-asyncio` `pytest-qt` `hypothesis` `protobuf` `grpcio-tools` `bandit` `pre-commit` `aiohttp` `sounddevice` `nuitka` `pytest-xvfb` `secretstorage` `pystray` `psutil` — **全部 MISS**。

✅ 已用 `uv pip install --dry-run` 实测：上述包在 Python 3.14.3 上**全部可解析**，版本为 `protobuf 7.36.2` / `grpcio-tools 1.84.0` / `pytest 9.1.1` / `mypy 2.3.1` / `ruff 0.16.8` / `hypothesis 6.168.0` / `bandit 1.9.4` / `aiohttp 3.14.3` / `nuitka 4.2.1`。

**请求：允许我在阶段 1 执行 `uv sync` 安装这些依赖。** 这不违反"不要重复安装"的本意 —— 它们确实不存在。

### 阻塞 B：不存在任何 v4 测试

项目里**没有 `tests/` 目录，0 个测试文件**。阶段 1 验收条件"原 v4 测试能跑"无法成立。

**建议改写为**：*"建立测试骨架，并为搬迁后的 `core/protocol`、`core/rate_limiter`、`config` 迁移逻辑补首批回归测试，全绿。"*

### 阻塞 C：本机无 C 编译器，Nuitka 不可用

实测 `cl.exe`/`gcc.exe`/`clang.exe`/`link.exe` 全部缺失，无 Visual Studio、无 MinGW、无 MSYS2。Nuitka 必须调用 C 编译器。

→ 见 **决策问题 Q1**。

---

## 1. 目标架构

```
remote_desktop/                      # 仓库根
├── pyproject.toml                   # PEP 621 + ruff + mypy + pytest 配置
├── uv.lock
├── .pre-commit-config.yaml
├── .gitignore                       # 已存在，需补充
├── ASSESSMENT.md / REFACTOR_PLAN.md / PROGRESS.md
├── README.md / CHANGELOG.md / MIGRATION.md
├── proto/
│   └── rdc.proto                    # Protobuf 定义
├── src/rdc/
│   ├── __init__.py                  # __version__ = "5.0.0"
│   ├── py.typed                     # PEP 561 标记
│   ├── __main__.py                  # python -m rdc
│   ├── cli.py                       # argparse → 子命令（serve/control/…）
│   ├── exceptions.py                # ← 搬迁 + 扩展
│   ├── constants.py                 # ← 搬迁（协议常量拆到 protocol/constants.py）
│   ├── logging/
│   │   ├── __init__.py
│   │   └── setup.py                 # ← logger.py 改造为 structlog
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py              # Pydantic v2 Settings 模型（分层：network/video/security/…）
│   │   ├── migration.py             # v1→v6 版本迁移链（含 v4 config.json → v5）
│   │   └── paths.py                 # 配置/日志/证书目录解析
│   ├── secrets/
│   │   ├── __init__.py
│   │   ├── base.py                  # SecretStore Protocol
│   │   ├── dpapi.py                 # Windows
│   │   ├── keychain.py              # macOS (pyobjc)
│   │   ├── libsecret.py             # Linux (secretstorage)
│   │   ├── fernet_fallback.py       # 机器ID + 口令派生
│   │   └── factory.py               # 平台探测 + 降级链
│   ├── protocol/
│   │   ├── __init__.py
│   │   ├── constants.py             # 包头字段、Type、Flags、PayloadType
│   │   ├── framing.py               # ← core/protocol.py：15B 分帧 + PacketBuffer
│   │   ├── codec.py                 # 编解码门面：v4(JSON) / v5(Protobuf) 双版本
│   │   ├── pb/                      # ← protoc 生成（gitignore，构建时生成）
│   │   ├── hmac_integrity.py        # PKT_FLAG_HMAC + TLS exporter 派生密钥
│   │   ├── legacy_v4.py             # ★ v4 裸握手 + JSON payload 兼容层
│   │   └── messages.py              # 领域消息 dataclass（wire-format 无关）
│   ├── transport/
│   │   ├── __init__.py
│   │   ├── base.py                  # Channel Protocol（asyncio 抽象）
│   │   ├── control_channel.py       # ← network/control_channel.py 重写
│   │   ├── screen_channel.py        # ← network/screen_channel.py 重写
│   │   ├── backpressure.py          # asyncio.Queue + 满队列丢旧帧
│   │   ├── ws_backend.py            # aiohttp WebSocket 备选后端
│   │   ├── tls.py                   # SSLContext 构建 + TOFU 指纹校验
│   │   ├── session_token.py         # secrets.token_urlsafe(32) + 绑定 + 60s TTL
│   │   └── reconnect.py             # ← core/reconnect.py 改 asyncio
│   ├── capture/
│   │   ├── __init__.py
│   │   ├── source.py                # ScreenSource Protocol（可注入 → 可测试）
│   │   ├── mss_source.py            # ← screen/capturer.py
│   │   ├── diff.py                  # ← screen/diff.py 向量化重写
│   │   └── monitor.py               # 多显示器枚举/切换
│   ├── encoding/
│   │   ├── __init__.py
│   │   ├── base.py                  # VideoEncoder Protocol
│   │   ├── h264.py                  # PyAV libx264
│   │   ├── jpeg.py                  # ← screen/encoder.py（回退路径）
│   │   ├── hw_probe.py              # NVENC/QSV/VideoToolbox/VAAPI 探测接口（本机跳过）
│   │   ├── raters.py                # 自适应码率：RTT + 丢包 + 队列深度
│   │   └── bandwidth.py             # ← BandwidthProbe
│   ├── server/
│   │   ├── __init__.py
│   │   ├── service.py               # ← daemon.py 主体（瘦身为生命周期编排）
│   │   ├── auth.py                  # ← daemon.py 认证段
│   │   ├── banlist.py               # ← daemon.py 封禁段（滑动窗口 + 持久化）
│   │   ├── broadcaster.py           # ← daemon._ScreenBroadcaster（asyncio + 每客户端队列）
│   │   ├── session.py               # 会话管理
│   │   ├── pid_manager.py           # ← service/pid_manager.py
│   │   └── audit.py                 # structlog 审计日志
│   ├── client/
│   │   ├── __init__.py
│   │   ├── controller.py            # 主控端会话状态机（无 UI 依赖）
│   │   ├── stats.py                 # FPS/延迟/带宽采集
│   │   └── ratelimit.py             # ← core/rate_limiter.py
│   ├── input/
│   │   ├── __init__.py
│   │   ├── base.py                  # InputBackend Protocol
│   │   ├── windows.py               # SendInput (ctypes)
│   │   ├── macos.py                 # CGEventPost
│   │   ├── linux_x11.py             # XTest
│   │   ├── linux_wayland.py         # libei / ydotool
│   │   ├── legacy_pyautogui.py      # ← input/mouse.py + keyboard.py（v4 回退）
│   │   ├── dpi.py                   # ← input/dpi.py
│   │   ├── modifiers.py             # ← input/modifiers.py
│   │   └── coalesce.py              # 鼠标事件流合并 + 帧对齐
│   ├── platform/
│   │   ├── __init__.py
│   │   ├── base.py                  # PlatformAdapter Protocol
│   │   ├── windows.py / macos.py / linux.py
│   │   ├── autostart.py             # ← service/autostart.py
│   │   ├── netinfo.py               # ← utils/ip.py
│   │   └── power.py                 # ← utils/system.py
│   ├── transfer/
│   │   ├── __init__.py
│   │   ├── file_transfer.py         # 1MB 分块 + SHA-256 + 断点续传
│   │   └── audio.py                 # sounddevice + Opus（可选）
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── app.py                   # QApplication + 高DPI策略 + QSS 加载
│   │   ├── theme/
│   │   │   ├── dark.qss
│   │   │   └── tokens.py
│   │   ├── launcher.py              # ← main.py show_gui()
│   │   ├── main_window.py           # ← gui/main_window.py 重写
│   │   ├── screen_view.py           # QGraphicsView + QGraphicsPixmapItem
│   │   ├── toolbar.py / status_bar.py / status_panel.py
│   │   ├── performance_panel.py / quality_indicator.py
│   │   ├── settings_dialog.py
│   │   ├── tray.py                  # QSystemTrayIcon
│   │   ├── clipboard_sync.py        # QClipboard
│   │   ├── password_dialog.py       # ← control_channel._prompt_password
│   │   ├── fingerprint_dialog.py    # ← control_channel.fingerprint_callback
│   │   └── transfer_dialog.py       # 文件传输拖拽 UI
│   ├── security/
│   │   ├── __init__.py
│   │   ├── password_policy.py       # ≥8位 + 字母+数字
│   │   ├── certs.py                 # 自签生成 / 用户证书加载 / 轮换
│   │   ├── fingerprint.py           # ← utils/security.py TOFU
│   │   └── audit_events.py          # 审计事件模型
│   └── updater/
│       ├── __init__.py
│       └── github_releases.py       # 版本检查
├── tests/
│   ├── conftest.py
│   ├── unit/          # 协议/差分/配置迁移/密钥/令牌
│   ├── integration/   # 双进程连接/TLS/认证/帧传输/重连
│   ├── e2e/           # pytest-qt 全链路
│   ├── load/          # 并发/压测
│   ├── compat/        # v4↔v5 双向
│   └── security/      # 重放/爆破/指纹/模糊
├── scripts/
│   ├── bench_encode.py
│   ├── bench_diff.py
│   ├── gen_proto.py
│   └── load_test.py
└── packaging/
    ├── windows/  (installer.iss, rdc.ico)
    ├── macos/    (Info.plist, create-dmg.sh)
    ├── linux/    (AppRun, rdc.desktop, rdc.appdata.xml)
    └── ci/       (GitHub Actions workflows)
```

### 核心设计原则

1. **依赖倒置**：所有外部世界（屏幕、socket、注册表、系统输入、时钟）通过 Protocol 注入。这是达成"核心模块覆盖率 ≥85%"的**前提**，v4 代码零注入，测试根本写不出来。
2. **UI 与业务彻底分离**：`client/controller.py` 不含任何 Qt 导入，可纯 asyncio 单测。`ui/` 只做信号槽绑定。
3. **协议双版本共存**：`codec.py` 按 `PayloadType` 分派到 `legacy_v4.py`(JSON) 或 `pb/`(Protobuf)，**领域消息统一为 `messages.py` 的 dataclass**，上层业务完全不感知 wire format。
4. **asyncio 单一事件循环**：Qt 主线程 + `qasync` 桥接 asyncio loop（避免 v4 的 13 线程模型）。

---

## 2. 完整文件清单

### 2.1 删除（3 个文件 + 若干代码段）

| 文件 | 行数 | 删除理由 |
|------|------|----------|
| `network/kcp_channel.py` | 85 | ☠️ 实测确认死代码：全项目无 import，`network/__init__.py` 未导出 |
| `screen/renderer.py` | 108 | PIL→ImageTk 渲染，被 QPixmap/QGraphicsView 完全取代 |
| `build.bat` | ~40 | PyInstaller 单文件脚本，版本已过期（写死 v4.0.2），被 `packaging/` 取代 |
| `screen/encoder.py:144-163` `encode_h264()` | — | **无效代码**：`cv2.imencode('.h264')` 不是合法用法，从未被调用 |
| `screen/encoder.py:15-25` `_check_h264_available()` | — | **无意义检测**：仅创建 fourcc 整数就返回 True |
| `screen/diff.py:21` `_last_image` | — | 每帧 `img.copy()` 但从未读取，4K 下浪费 ~24MB/帧 |
| `core/protocol.py:55-57,117-119` `PKT_FLAG_ENCRYPTED` TODO | — | 空占位，TLS 已提供加密 |
| `constants.py` `PKT_TYPE_AUTH`/`PKT_TYPE_AUTH_RESPONSE` | — | 定义但从未使用（实际认证走裸握手） |
| `__init__.py` `__protocol_version__ = 4` | — | 从未被引用；v5 改为在协议头 Version 字段真正使用 |

### 2.2 搬迁（机械移动，不改逻辑）— 阶段 1

| v4 路径 | → v5 路径 | 备注 |
|---------|-----------|------|
| `exceptions.py` | `src/rdc/exceptions.py` | 直接搬 |
| `constants.py` | `src/rdc/constants.py` + `src/rdc/protocol/constants.py` | 协议常量拆出 |
| `logger.py` | `src/rdc/logging/setup.py` | 先搬，阶段 2 改 structlog |
| `core/protocol.py` | `src/rdc/protocol/framing.py` | 先搬 |
| `core/rate_limiter.py` | `src/rdc/client/ratelimit.py` | 先搬 |
| `core/heartbeat.py` | `src/rdc/transport/heartbeat.py` | 先搬，阶段 4 改 asyncio |
| `core/reconnect.py` | `src/rdc/transport/reconnect.py` | 先搬，阶段 4 改 asyncio |
| `config.py` | `src/rdc/config/settings.py`（临时保留原样） | 阶段 2 拆 |
| `screen/capturer.py` | `src/rdc/capture/mss_source.py` | 先搬 |
| `screen/diff.py` | `src/rdc/capture/diff.py` | 先搬，阶段 5 重写 |
| `screen/framerate.py` | `src/rdc/capture/framerate.py` | 先搬 |
| `screen/encoder.py` | `src/rdc/encoding/jpeg.py` | 先搬（删死代码段） |
| `network/control_channel.py` | `src/rdc/transport/control_channel.py` | 先搬，阶段 4 重写 |
| `network/screen_channel.py` | `src/rdc/transport/screen_channel.py` | 先搬，阶段 4 重写 |
| `service/daemon.py` | `src/rdc/server/service.py` | 先搬，阶段 3-4 拆分 |
| `service/pid_manager.py` | `src/rdc/server/pid_manager.py` | 先搬 |
| `service/autostart.py` | `src/rdc/platform/autostart.py` | 先搬 |
| `input/mouse.py`+`keyboard.py` | `src/rdc/input/legacy_pyautogui.py` | 合并搬迁 |
| `input/dpi.py` | `src/rdc/input/dpi.py` | 先搬 |
| `input/modifiers.py` | `src/rdc/input/modifiers.py` | 先搬 |
| `utils/ip.py` | `src/rdc/platform/netinfo.py` | 先搬 |
| `utils/system.py` | `src/rdc/platform/power.py` | 先搬 |
| `utils/security.py` | `src/rdc/security/fingerprint.py` | 先搬 |
| `gui/*.py` ×10 | **不搬迁** | Tkinter 代码无复用价值，阶段 6 直接重写 |
| `main.py` | `src/rdc/cli.py`（重写）+ `src/rdc/ui/launcher.py`（重写） | Tkinter 菜单不复用 |
| `icon.ico` | `packaging/windows/rdc.ico` | 搬迁 |

**注意**：`gui/` 全部 2,588 行 Tkinter 代码在阶段 1 **不搬迁**（避免 mypy strict 下为死代码写类型注解的浪费），保留在 `legacy_v4/` 目录作为重写参考，阶段 6 完成后删除。

### 2.3 新增文件清单（按阶段）

| 阶段 | 新增文件 |
|------|----------|
| 1 | `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `src/rdc/__init__.py`, `src/rdc/py.typed`, `src/rdc/__main__.py`, `PROGRESS.md`, `tests/conftest.py`, `tests/unit/test_smoke.py`, `.gitignore`(更新) |
| 2 | `config/settings.py`, `config/migration.py`, `config/paths.py`, `secrets/{base,dpapi,keychain,libsecret,fernet_fallback,factory}.py`, `logging/setup.py`(structlog 改造), + 6 个测试文件 |
| 3 | `proto/rdc.proto`, `protocol/{codec,hmac_integrity,legacy_v4,messages}.py`, `protocol/pb/*_pb2.py`(生成), `scripts/gen_proto.py`, + 5 个测试文件 |
| 4 | `transport/{base,backpressure,ws_backend,tls,session_token}.py`, + 6 个测试文件 |
| 5 | `capture/{source,monitor}.py`, `encoding/{base,h264,hw_probe,raters,bandwidth}.py`, `capture/diff.py`(重写), `scripts/bench_{encode,diff}.py`, + 6 个测试文件 |
| 6 | `ui/*` 全部（18 个文件）, `ui/theme/*`, + 8 个测试文件 |
| 7 | `input/{base,windows,macos,linux_x11,linux_wayland,coalesce}.py`, + 5 个测试文件 |
| 8 | `transfer/{file_transfer,audio}.py`, `ui/transfer_dialog.py`, + 4 个测试文件 |
| 9 | `security/{password_policy,certs,audit_events}.py`, `server/{auth,banlist,audit}.py`(拆分), + 6 个测试文件 |
| 10 | `packaging/**` 全部, `.github/workflows/*`, `updater/github_releases.py`, `README.md`, `CHANGELOG.md`, `MIGRATION.md`, `docs/**` |

### 2.4 修改（重写既有搬迁文件）

`service/daemon.py`(1057行 → 拆为 6 个文件)、`config.py`(489行 → 拆为 3+6 个文件)、`screen/diff.py`(向量化)、`screen/encoder.py`(拆分)、`core/protocol.py`(扩展)、`network/{control,screen}_channel.py`(asyncio)、`logger.py`(structlog)、`core/{heartbeat,reconnect}.py`(asyncio)。

---

## 3. 阶段任务分解与工时估算

> 工时按"专注开发小时"计，非日历时间。总计 **约 330-430 小时**（中位 380h）。

### 阶段 1：项目脚手架（18-24h）

| # | 任务 | h |
|---|------|---|
| 1.1 | 写 `pyproject.toml`：PEP 621 元数据 + `[project.dependencies]`（13 个运行时）+ `[dependency-groups]` dev/test/package + `[project.scripts]` rdc | 3 |
| 1.2 | ruff 配置：`line-length=100`，`select=["E","F","I","N","UP","B","SIM"]`，逐目录 per-file-ignores | 1.5 |
| 1.3 | mypy 配置：`strict=true`，`mypy_path=src`，`plugins=[pydantic.mypy]`，第三方库 `ignore_missing_imports` | 1.5 |
| 1.4 | `.pre-commit-config.yaml`：ruff + ruff-format + mypy + 通用钩子 | 1 |
| 1.5 | **安装依赖**：`uv sync`（含前置阻塞 A） | 1 |
| 1.6 | 建 `src/rdc/` layout + 各子包 `__init__.py` + `py.typed` | 2 |
| 1.7 | 机械搬迁 25 个文件（见 §2.2），**只改 import 路径，不改逻辑** | 6 |
| 1.8 | `gui/*.py` 移到 `legacy_v4/`（不参与 ruff/mypy） | 0.5 |
| 1.9 | 删除死代码：`kcp_channel.py`、`renderer.py`、`encode_h264`、`_check_h264_available`、`_last_image` | 1 |
| 1.10 | 清零 49 处静默吞错（改 `logger.exception` 或显式处理）— **硬性约束 6** | 4 |
| 1.11 | 建 `tests/` 骨架 + `conftest.py`（fixtures：临时配置目录、假屏幕源、事件循环） | 2 |
| 1.12 | 首批回归测试：`test_framing.py`(分帧往返)、`test_rate_limiter.py`、`test_config_migration.py` | 3 |
| 1.13 | 更新 `.gitignore`（加 `src/rdc/protocol/pb/`、`.mypy_cache/`、`.ruff_cache/`、`htmlcov/`、`legacy_v4/`） | 0.5 |

**验收**：`uv sync` 成功 · `uv run ruff check src tests` 0 error · `uv run mypy src` 0 error · `uv run pytest tests/unit -q` 全绿 · `uv run python -m rdc --version` 输出 5.0.0

**风险点**：mypy strict 会对 7,505 行 v4 代码报**数百个错**（~40% 公开函数缺注解）。1.7 的"不改逻辑"与 mypy strict 0 error 冲突 → **策略：搬迁时补齐类型注解但不改运行时行为**，这是 1.7 估 6h 而非 2h 的原因。若仍超限，用 `# type: ignore[code]` 带错误码的显式豁免 + `TODO(v5.x)` 标记，逐个消解（**禁止裸 `# type: ignore`**）。

### 阶段 2：配置与密钥（28-36h）

| # | 任务 | h |
|---|------|---|
| 2.1 | `Settings` Pydantic v2 模型：分层子模型（Network/Video/Security/Audio/Transfer/UI）+ 字段校验器（端口范围、质量区间、FPS 区间、连接数上限 —— 迁移 `config.py:329-355` 的手写 `_validate`） | 6 |
| 2.2 | `migration.py`：v1→v6 完整迁移链，**保留 v4 的 v1-v5 逻辑**（`config.py:270-327`）并新增 v5→v6（拆双轨密码、删 `saved_password`） | 6 |
| 2.3 | `SecretStore` Protocol + `factory.py` 平台探测降级链 | 2 |
| 2.4 | `dpapi.py`：提取重复的 `DATA_BLOB`（`config.py:80-81` 与 `103-104` 完全相同）为单一实现 | 3 |
| 2.5 | `keychain.py`(pyobjc) + `libsecret.py`(secretstorage) | 4 |
| 2.6 | `fernet_fallback.py`：机器 ID + 用户口令 → PBKDF2/HKDF 派生 → Fernet | 3 |
| 2.7 | **删除三轨制**：`password_hash`(服务端 bcrypt) 保留但移入 `secrets/`；`saved_password` 三平台分流统一为 `SecretStore`；遗留 base64 明文路径一次性迁移 | 4 |
| 2.8 | `logging/setup.py` 改造为 structlog（JSON + console 双 renderer，保留轮转） | 3 |
| 2.9 | 测试：三平台 SecretStore mock、v1→v6 全路径迁移、Fernet 派生确定性、bcrypt 往返 | 5 |

**验收**：三平台 SecretStore mock 测试全过 · v4 `config.json` 实测无损迁移到 v5（用真实 v4 配置文件做 fixture） · 覆盖率 `config/` + `secrets/` ≥85%

**⚠️ 风险**：删除双轨制会让**存量用户丢失已记住的密码**。缓解：迁移时先读旧 `DPAPI1:`/`KEYCHAIN:`/`LIBSECRET:` 值 → 写入新 SecretStore → 校验成功 → 才删旧字段（失败则保留旧字段并告警，不静默丢数据）。

### 阶段 3：协议层（32-40h）

| # | 任务 | h |
|---|------|---|
| 3.1 | `proto/rdc.proto`：8 个 message（AuthRequest/AuthResponse/ScreenFrame/MouseEvent/KeyEvent/ClipboardEvent/StreamControl/Heartbeat）+ PayloadType 枚举 + StreamCmd 枚举 | 4 |
| 3.2 | `scripts/gen_proto.py`：调 grpcio-tools 生成到 `protocol/pb/`（生成物 gitignore，CI 中生成） | 2 |
| 3.3 | `messages.py`：wire-format 无关的领域 dataclass（`Frame`, `MouseEvt`, `KeyEvt`, ...） | 4 |
| 3.4 | `codec.py`：按 PayloadType 分派 JSON↔PB，双向转换领域消息 | 5 |
| 3.5 | `framing.py` 扩展：Version 字段真正启用（v4=1, v5=2）；新增 `PKT_FLAG_HMAC=0x08`（避开 0x01/0x02/0x04） | 3 |
| 3.6 | `hmac_integrity.py`：HMAC-SHA256 + TLS exporter (`ssl.SSLSocket.export_keying_material`) 派生密钥 | 4 |
| 3.7 | **`legacy_v4.py`：★ 最关键的兼容层** | 8 |
| | — 复现裸 4B 长度前缀握手（`control_channel.py:78-79`、`daemon.py:791-800`、`screen_channel.py:102-103`） | |
| | — 复现 SCREEN 包 `[4B meta_len][meta JSON][图像字节]` + MD5 checksum 前 8 位（`protocol.py:143-157`） | |
| | — 复现 CONTROL/HEARTBEAT/CLIPBOARD/STREAM_CONTROL 的全部 JSON 字段名与取值 | |
| | — **协议自描述探测**：读首 4 字节，若 == `RDPC` 魔数则对端未走握手（v4 `enable_auth=False` 客户端），否则按握手处理 → 解决 §4.3 的配置错位地雷 | |
| 3.8 | 测试：hypothesis property-based 往返（任意消息 → 编码 → 解码 == 原值） | 5 |
| 3.9 | 测试：分帧模糊（随机字节流喂 PacketBuffer 不崩溃、不 OOM、不死循环） | 3 |
| 3.10 | 测试：v4 字节级快照（用真实 v4 代码生成 golden bytes，v5 必须逐字节复现） | 4 |

**验收**：hypothesis 往返 100% 通过 · 模糊测试 10,000 例不崩溃 · v4 golden bytes 逐字节一致 · `protocol/` 覆盖率 ≥85%

**⚠️ 最高风险阶段**。`legacy_v4.py` 的 golden-bytes 测试是安全网 —— 建议在阶段 3 开工**第一步**就先写这个测试（用 v4 原代码生成期望字节），再写实现。

### 阶段 4：传输层（30-38h）

| # | 任务 | h |
|---|------|---|
| 4.1 | `base.py`：Channel Protocol（`connect/send/recv_task/close`）+ asyncio 抽象 | 3 |
| 4.2 | `control_channel.py` asyncio 重写（TLS + 握手 + 收发协程） | 6 |
| 4.3 | `screen_channel.py` asyncio 重写 | 5 |
| 4.4 | `backpressure.py`：`asyncio.Queue(maxsize=N)`，满时丢最旧帧 + 丢帧计数上报 | 3 |
| 4.5 | `tls.py`：SSLContext 构建（强制 TLS 1.2+）、TOFU 指纹校验（迁移 `control_channel.py:120-207` 的降级逻辑） | 4 |
| 4.6 | `session_token.py`：`secrets.token_urlsafe(32)` + 绑定 `(client_ip, client_fingerprint)` + 60s TTL + 单次使用 | 3 |
| 4.7 | `ws_backend.py`：aiohttp WebSocket 备选后端 | 4 |
| 4.8 | `reconnect.py` + `heartbeat.py` 改 asyncio | 3 |
| 4.9 | **修复 B3**：broadcaster 改为每客户端独立发送协程 + 独立队列（消除 head-of-line blocking） | 4 |
| 4.10 | 测试：双进程集成、100 并发压测（含 fd/内存泄漏检测）、令牌重放 | 6 |

**验收**：集成测试全绿 · 100 并发无泄漏（fd 数与 RSS 回落到基线） · 令牌第二次使用必失败 · `transport/` 覆盖率 ≥85%

### 阶段 5：采集与编码（26-34h）

| # | 任务 | h |
|---|------|---|
| 5.1 | `diff.py` 向量化重写：`cv2.absdiff` → reshape 分块 → `np.any(axis=(2,3))` 单次得出变化掩码；**消除 `_adjust_block_size` 每帧全图 int16 运算**（改为按掩码比例采样，或降频到每 N 帧） | 6 |
| 5.2 | **修复 B4**（block_size 可变导致坐标系错乱）：改为固定块大小 + 多级金字塔，或每次调用内锁定 block_size | 2 |
| 5.3 | **修复 B5**（单外接矩形退化为全屏）：改为多矩形脏区列表（≤N 个），或全帧 H.264 路径直接放弃区域裁剪 | 4 |
| 5.4 | `source.py` ScreenSource Protocol + `mss_source.py`（注入点，可测试） | 3 |
| 5.5 | `encoding/base.py` VideoEncoder Protocol + 帧引用零拷贝管线 | 2 |
| 5.6 | `h264.py`：PyAV libx264，`tune=zerolatency`/`preset=ultrafast`/CRF 动态；**关键决策：H.264 是有状态帧间编码，与 v4 区域裁剪不兼容**（见 Q3） | 5 |
| 5.7 | `hw_probe.py`：NVENC/QSV/VideoToolbox/VAAPI 探测接口（**必须实测 `ctx.open()`，仅查 Codec 名会误判** —— 实测 nvenc 构造成功但 open 抛 PermissionError） | 2 |
| 5.8 | `raters.py`：自适应码率，输入 RTT + 丢包率 + 发送队列深度，输出 quality/bitrate/resolution | 4 |
| 5.9 | `scripts/bench_diff.py` + `bench_encode.py`（含 v4 JPEG 基线对比） | 3 |
| 5.10 | 测试：差分正确性（随机图像对，与朴素实现比对）、编码器往返、自适应码率收敛 | 5 |

**验收**：4K 差分 <5ms · 编码延迟软编 <40ms（**实测已达 11.2ms**） · 带宽降幅 ≥40%（**实测桌面场景 96%**，需补高熵场景取样）

**已知实测基线**（无需重测）：libx264 4K = 11.2ms/帧 (89fps)，1080p = 3.3ms/帧 (300fps)；模拟桌面 1080p JPEG q60 = 25KB/帧 vs H.264 crf28 = 1.1KB/帧。

### 阶段 6：UI 迁移 PySide6（44-58h）★ 最大工作量

| # | 任务 | h |
|---|------|---|
| 6.1 | `app.py`：QApplication + `setHighDpiScaleFactorRoundingPolicy(PassThrough)` + QSS 加载 | 2 |
| 6.2 | `theme/dark.qss` + `tokens.py`：深色主题设计令牌（颜色/间距/字号） | 4 |
| 6.3 | **asyncio↔Qt 桥接**：qasync 或自建 loop-in-thread + `Signal` 投递（替代 v4 的 11 处 `after_idle`） | 5 |
| 6.4 | 低风险件先行验证脚手架：`status_bar.py` → `performance_panel.py` → `status_panel.py` | 4 |
| 6.5 | `toolbar.py` + `quality_indicator.py`（QPainter + QPropertyAnimation 脉冲） | 5 |
| 6.6 | **`screen_view.py`**：QGraphicsView + QGraphicsPixmapItem，差分区域合成，坐标变换（客户端坐标→远端坐标，含多显示器） | 10 |
| 6.7 | **`main_window.py`**：连接状态机 + 全部对话框编排（v4 是 778 行，状态机逻辑需完整迁移） | 10 |
| 6.8 | `settings_dialog.py`：QTabWidget 三页签 | 4 |
| 6.9 | `password_dialog.py` + `fingerprint_dialog.py`（**修复 B2**：不再在工作线程创建 Tk） | 3 |
| 6.10 | `tray.py`：QSystemTrayIcon + QPainter 图标（替换未安装的 pystray） | 4 |
| 6.11 | `clipboard_sync.py`：QClipboard + QTimer 轮询 + 变更去抖 | 3 |
| 6.12 | `launcher.py`：启动菜单 QDialog（替换 `main.py:show_gui()`） | 2 |
| 6.13 | 测试：pytest-qt 覆盖所有对话框 + 主窗口状态流转 + 冷启动计时 | 6 |
| 6.14 | 删除 `legacy_v4/` 目录（2,588 行 Tkinter 代码） | 1 |

**验收**：pytest-qt 覆盖所有对话框 · 冷启动 <1.5s · **禁止任何跨线程控件操作**（用 lint 规则或代码审查确认）

### 阶段 7：输入层（26-34h）

| # | 任务 | h |
|---|------|---|
| 7.1 | `base.py` InputBackend Protocol（mouse/key/text/多显示器） | 2 |
| 7.2 | `windows.py`：SendInput (ctypes)，含 MOUSEINPUT/KEYBDINPUT 结构、Unicode 输入、绝对坐标归一化 | 7 |
| 7.3 | `macos.py`：CGEventPost（Quartz） | 5 |
| 7.4 | `linux_x11.py`：XTest | 4 |
| 7.5 | `linux_wayland.py`：libei / ydotool 探测 + 实现 | 4 |
| 7.6 | `legacy_pyautogui.py`：保留 v4 路径作为回退 | 1 |
| 7.7 | `coalesce.py`：鼠标事件流累积位移 + 按帧率合并 + 帧对齐 | 3 |
| 7.8 | 剪贴板图片/文件、IME 输入支持 | 3 |
| 7.9 | 测试：Xvfb 自动化（Linux CI）+ 各平台手动测试清单 | 4 |

**验收**：各平台手动测试通过 · Xvfb 自动化测试通过 · `input/` 覆盖率 ≥70%

**⚠️ 本机限制**：Windows 上只能验证 SendInput 路径；macOS/Linux 实现**无法在本机测试**，只能在 CI 中验证（Linux）或交付后由用户在 macOS 验证。

### 阶段 8：文件传输 + 音频（20-26h，可选）

| # | 任务 | h |
|---|------|---|
| 8.1 | `file_transfer.py`：1MB 分块 + SHA-256 + 断点续传（偏移协商 + 部分校验） | 7 |
| 8.2 | `transfer_dialog.py`：拖拽 UI + 进度 + 暂停/续传 | 4 |
| 8.3 | **路径穿越防护**：目标路径规范化 + 沙箱根目录校验（阶段 9 渗透清单要求） | 2 |
| 8.4 | `audio.py`：sounddevice 采集 + Opus 编码（**实测 `libopus` 可用**）+ 播放 + 开关 | 6 |
| 8.5 | 测试：大文件校验一致性、断点续传、音频回环延迟 | 5 |

**验收**：大文件传输校验一致 · 音频延迟 <100ms

**⚠️ 本机限制**：音频需真实音频设备，本机 CI 环境可能无声卡 → 交付回环 mock 测试 + 用户手动验证。

### 阶段 9：安全加固（20-26h）

| # | 任务 | h |
|---|------|---|
| 9.1 | `password_policy.py`：≥8 位 + 字母 + 数字（validator + UI 提示） | 2 |
| 9.2 | `banlist.py`：滑动窗口限流 + 封禁 + `banlist.json` 持久化（迁移 `daemon.py:733-787`，保留 1000 条上限裁剪） | 4 |
| 9.3 | `certs.py`：用户证书加载（非自签名）+ 证书轮换 + 过期检测（迁移 `daemon.py:269-330`） | 5 |
| 9.4 | `audit.py` + `audit_events.py`：structlog 结构化审计（认证/连接/控制操作全覆盖） | 4 |
| 9.5 | **MD5 处理**：`protocol.py:151` 的 `hashlib.md5` 会被 bandit B324 标记 → v4 兼容路径加 `usedforsecurity=False`，v5 路径改 SHA-256 截断 | 1 |
| 9.6 | 渗透清单实测：重放 / MITM / 弱口令爆破 / 路径穿越 | 5 |
| 9.7 | 工具跑通：`bandit -r src/` 0 high + `pip-audit` 0 vuln + 协议模糊 | 4 |

**验收**：bandit 0 high · pip-audit 0 vuln · 渗透清单全过 · `security/` + `server/auth` 覆盖率 ≥85%

### 阶段 10：打包发布（30-40h）

| # | 任务 | h |
|---|------|---|
| 10.1 | PyInstaller `-D` 目录模式（**本机可用**，见 Q1）+ spec 文件 | 5 |
| 10.2 | Nuitka 配置（**仅 CI 中执行**，本机无编译器） | 3 |
| 10.3 | Windows：Inno Setup `.iss` → `.exe` 安装器 | 4 |
| 10.4 | macOS：`Info.plist` + create-dmg → `.dmg`（CI 中） | 3 |
| 10.5 | Linux：AppImage（AppRun + desktop + appdata）+ fpm → `.deb`（CI 中） | 5 |
| 10.6 | `updater/github_releases.py`：版本检查 + 下载 + 校验（见 Q5） | 4 |
| 10.7 | CI/CD：GitHub Actions 三平台矩阵（见 Q2） | 5 |
| 10.8 | 文档：`README.md` + 用户手册 + API 文档（pdoc/mkdocs）+ `MIGRATION.md` + `CHANGELOG.md` | 6 |
| 10.9 | 本机可验证部分：Windows 打包 + 冷启动计时 + 安装/卸载 | 3 |

**验收**：三平台安装包可安装/卸载 · 冷启动 <2s · 模拟自动更新 5.0.0→5.0.1

**⚠️ 本机限制**：macOS/Linux 打包**物理上无法在本机执行**；git 无 remote，CI 无法真实触发。只能交付配置 + 本地模拟验证。

---

## 4. 风险清单与缓解措施

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|------|------|------|----------|
| R1 | **v4 兼容层出错** — 认证握手是裸 4B 长度前缀，不走分帧，任务书阶段 3 未提及；漏掉则 v4 客户端卡在认证 | 高 | 致命 | 阶段 3 第一件事就写 **golden-bytes 快照测试**（用 v4 原代码生成期望字节），再写实现；`tests/compat/` 双向跑真实 v4 代码 |
| R2 | **协议配置错位地雷** — v4 客户端是否发握手取决于**客户端本地** `enable_auth`，两端配置不一致则字节流错位（v4.0.3 已修过此类 bug，见 `daemon.py:439-443`） | 高 | 致命 | v5 服务端做**协议自描述探测**：读首 4 字节判断是否为 `RDPC` 魔数，而非依赖配置对齐 |
| R3 | **mypy strict 对 7,505 行 v4 代码报数百错** — 阶段 1 "不改逻辑" 与 "0 error" 冲突 | 极高 | 高 | 搬迁时补注解但不改运行时行为（工时已计入）；确需豁免用带错误码的 `# type: ignore[code]` + `TODO` 标记，**禁止裸 ignore**；分批消解 |
| R4 | **H.264 与 v4 区域差分架构不兼容** — H.264 是有状态帧间编码，无法只发裁剪区域 | 高 | 高 | 见 **Q3**：需你决策走全帧 H.264 还是双轨 |
| R5 | **本机无 C 编译器** — Nuitka 无法运行 | 确定 | 中 | 见 **Q1**：本机用 PyInstaller 验证，Nuitka 只在 CI 跑 |
| R6 | **macOS/Linux 平台代码无法本机验证** — 阶段 7 输入层、阶段 2 密钥环、阶段 10 打包 | 确定 | 中 | Linux 部分靠 CI（ubuntu runner）；macOS 靠 CI（macos runner）+ 交付后用户验证；本机用 mock 保证单测覆盖率 |
| R7 | **git 无 remote** — CI/CD、Releases 自动更新无法端到端验证 | 确定 | 中 | 见 **Q5** |
| R8 | **Qt 与 asyncio 事件循环冲突** — 两个 loop 争抢主线程 | 中 | 高 | 阶段 6.3 优先验证桥接方案（qasync vs loop-in-thread），做成最小可运行 demo 再铺开 UI |
| R9 | **删除密码双轨制导致存量用户丢失已记住密码** | 中 | 中 | 迁移时先读旧值→写新 SecretStore→校验成功→才删旧字段；失败保留旧值并告警，**不静默丢数据** |
| R10 | **Python 3.14 vs CI 矩阵 3.10-3.13** | 确定 | 中 | 见 **Q2** |
| R11 | **覆盖率 ≥85% 对不可测模块难达成** — capturer/input 直接操作真实系统 | 高 | 高 | 阶段 1 就引入 Protocol 注入点（ScreenSource/InputBackend），这是达成覆盖率的前提而非可选项 |
| R12 | **带宽降幅 ≥40% 在高熵场景可能不达标** — 实测 96% 是静态桌面理想场景 | 中 | 中 | 验收补多场景取样（静态桌面/文档滚动/视频播放/游戏），分别报告；高熵场景 H.264 优势会收窄但仍应优于 JPEG |
| R13 | **8 小时稳定性测试** 耗时长 | 确定 | 低 | 做成后台长跑脚本，分阶段并行推进，不阻塞主线 |
| R14 | **PySide6 6.11 + Python 3.14 组合的 Qt 绑定边缘 bug** | 低 | 中 | 阶段 6.1 先做最小可运行验证（一个窗口 + 一个定时器 + asyncio 桥接）再铺开 |
| R15 | **`legacy_v4/` 保留 2,588 行 Tkinter 代码影响 lint** | 低 | 低 | 加入 ruff/mypy `exclude`，阶段 6 完成后整体删除 |

---

## 5. 决策记录（Q1-Q3 + 两项前置阻塞已确认；Q4-Q5 默认采纳待复核）

| # | 问题 | 决策 | 对计划的约束 |
|---|------|------|--------------|
| Q1 | 本机无 C 编译器，打包工具 | **本机 PyInstaller -D，Nuitka 只在 CI** | 阶段 10.1 本机执行并验证冷启动；10.2 Nuitka 配置只交付 + CI 执行，本机不跑 |
| Q2 | Python 版本策略 | **CI 矩阵改为 3.11-3.14** | `pyproject.toml` 声明 `requires-python = ">=3.11"`；CI 矩阵 `["3.11","3.12","3.13","3.14"]` |
| Q3 | H.264 与区域差分不兼容 | **v5 走全帧 H.264，v4 走 JPEG 差分** | 阶段 5.6 按对端能力协商编码路径；`diff.py` 的 B4/B5 两个 bug 仅需在 v4 兼容路径修复；H.264 路径不做区域裁剪 |
| Q4 | v4 兼容保留期 | **v5.0 保留 → v5.1 标记 deprecated → v6.0 删除**（默认采纳） | `legacy_v4.py` 加 deprecation 注释与版本计划；`MIGRATION.md` 需写明时间表 |
| Q5 | git 无 remote，CI 验收 | **交付完整配置 + 本地模拟验证**（默认采纳） | 阶段 10.6/10.7 用本地 HTTP server 模拟 Releases API 跑通 5.0.0→5.0.1；**不推送任何代码到外部**，如需真实 CI 由用户单独授权 |
| — | 前置阻塞 A（依赖未装） | **批准 `uv sync` 安装全部缺失工具链** | 阶段 1.5 执行 |
| — | 前置阻塞 B（无 v4 测试） | **批准改写阶段 1 验收条件** | 新验收：建立测试骨架 + 为搬迁后的 `protocol/framing`、`client/ratelimit`、`config/migration` 补首批回归测试并全绿 |

### 5.1 原始决策问题（保留供追溯）

#### Q1 🔴 本机无 C 编译器，Nuitka 无法运行 — 打包工具怎么选？

实测：`cl.exe`/`gcc.exe`/`clang.exe`/`link.exe` 全部缺失，无 Visual Studio、无 MinGW、无 MSYS2。任务书阶段 10 推荐 Nuitka（"推荐"）或 PyInstaller -D。

| 方案 | 优点 | 缺点 |
|------|------|------|
| **A. 本机用 PyInstaller -D，Nuitka 只在 CI 跑（推荐）** | 本机立即可验证打包产物与冷启动；CI 中 Nuitka 有编译器可用；两条路径都覆盖 | 本机产物与发布产物不完全一致，需 CI 补验 |
| B. 只交付 Nuitka 配置，本机不打包 | 与任务书"推荐"一致 | 本机**完全无法验证**打包结果，阶段 10 验收"冷启动<2s"只能盲猜 |
| C. 安装 Visual Studio Build Tools（~7GB） | 本机可跑 Nuitka | 体积大、耗时长、改变系统环境；需你授权 |

✅ **已确认选 A**：阶段 10.1 在本机用 PyInstaller -D 打包并实测冷启动；10.2 的 Nuitka 配置只交付 + 在 CI runner 执行，本机不跑。

#### Q2 🟡 Python 版本策略 — 本机 3.14.3 vs CI 矩阵要求 3.10-3.13

本机开发环境是 Python 3.14.3，但任务书阶段 10 要求 CI 矩阵 `Python 3.10-3.13`。3.14 不在矩阵内。

| 方案 | 说明 |
|------|------|
| **A. CI 矩阵改为 3.11-3.14（推荐）** | 与本机一致，开发/CI 同一版本，减少"本地过 CI 挂"；3.10 已 EOL（2026-10），3.11 是当前最低稳妥线 |
| B. 严格保持 3.10-3.13，本机装 3.12 | 需在本机再建一个 venv；uv 可以做到，但开发/验证环境与实际不符 |
| C. 双矩阵：本机 3.14 开发，CI 跑 3.10-3.14 | 覆盖最全，但 CI 时间 ×5，且 3.10 上 numpy 2.5/PySide6 6.11/grpcio 1.84 的支持需逐个验证（可能装不上） |

✅ **已确认选 A**：CI 矩阵固定为 `["3.11","3.12","3.13","3.14"]`，`pyproject.toml` 声明 `requires-python = ">=3.11"`。本机 3.14.3 与 CI 最高版本一致。

#### Q3 🔴 H.264 与 v4 区域差分不兼容 — 画面管线怎么设计？

这是阶段 5 的核心架构决策，任务书未涉及。H.264 是**有状态帧间编码**（P 帧依赖前帧的完整解码状态），无法像 JPEG 那样"只发屏幕变化的裁剪矩形"。而 v4 的整个差分机制（`diff.py:get_changed_region` + `SCREEN` 包的 `is_diff/offset_x/offset_y` 字段）建立在区域裁剪之上。

| 方案 | 说明 | 优点 | 缺点 |
|------|------|------|------|
| **A. v5 客户端走全帧 H.264，v4 客户端走 JPEG 差分（推荐）** | 编码器按对端能力协商：v5↔v5 用 H.264 全帧；v4↔v5 保留 v4 的 JPEG+差分路径 | 架构最干净；H.264 自身帧间压缩已远优于手工区域裁剪（实测桌面场景 96% 降幅）；兼容层不变 | 需维护两条编码路径 |
| B. H.264 也做区域裁剪 | 每帧对变化区域单独编码 | 理论上带宽更低 | **技术上不成立**：H.264 编码器需固定分辨率的连续帧序列，频繁改变编码区域会破坏帧间预测，实测反而更差、更复杂 |
| C. 全项目放弃差分，一律 H.264 全帧 | 最简单 | 只需一条路径 | **破坏 v4 兼容**（v4 客户端只认 JPEG+差分）；且静态屏幕时 JPEG 差分本来就很省 |

✅ **已确认选 A**：阶段 5.6 按对端能力协商编码路径 —— v5↔v5 走全帧 H.264，v4↔v5 保留 JPEG + 区域差分。

**连带收益**：`diff.py` 的 B4（块大小可变导致坐标系错乱）与 B5（单外接矩形退化为全屏）两个 bug **仅需在 v4 兼容路径修复**，H.264 路径不涉及区域裁剪故自动消失。阶段 5.2/5.3 工时相应可下调约 4h。

#### Q4 🟡 v4 兼容代码保留到什么时候？

任务书硬性约束 5 写"v5.0 内必须保留"v4 兼容。但保留期越长，维护成本越高（`legacy_v4.py` + JPEG 差分路径 + 三轨密码迁移）。

| 方案 | 说明 |
|------|------|
| **A. v5.0 保留，v5.1 标记 deprecated，v6.0 删除（推荐）** | 符合约束；给用户一个完整次版本的升级窗口 |
| B. v5.0 保留，不设删除时间表 | 永久维护负担，`codec.py` 双路径长期存在 |
| C. v5.0 保留但默认关闭，需配置显式开启 | 更安全的默认值，但 v4 客户端连 v5 服务端会直接失败，**违反硬性约束 5** |

⏳ **默认采纳 A，用户尚未明确确认**（本项未列入确认卡片）。若无异议即按 A 执行；如需改为 B 或 C，请在阶段 3 开工前提出 —— `legacy_v4.py` 一旦按 A 落地，改 C 会牵连兼容层设计。

#### Q5 🟡 git 无 remote — CI/CD 与自动更新怎么验收？

`git remote -v` 输出为空。阶段 10 的 GitHub Actions、GitHub Releases 自动更新、`pyupdater` 全部依赖远端。

| 方案 | 说明 |
|------|------|
| **A. 交付配置 + 本地模拟验证（推荐）** | 写完整 workflow YAML + 用 `act` 或本地脚本模拟矩阵；自动更新用本地 HTTP server 模拟 Releases API，跑通 5.0.0→5.0.1 全流程 |
| B. 你提供 GitHub 仓库地址，我推送并真实跑 CI | 验证最彻底，但**推送代码到外部服务需你明确授权**，且涉及项目是否公开 |
| C. 阶段 10 只交付配置，不做验证 | 最快，但验收标准"CI 全绿"无法满足 |

⏳ **默认采纳 A，用户尚未明确确认**（本项未列入确认卡片）。若走 B 需用户提供仓库地址并**单独明确授权推送**——在此之前我不会把任何代码推送到外部服务。

---

## 6. 执行纪律（我会严格遵守）

1. 每阶段结束：跑全部验收命令并**贴出真实输出**（不伪造、不省略）
2. 更新 `PROGRESS.md`（完成项 / 遗留项 / 下一步）
3. `git add` 指定文件（不用 `git add .`）+ 约定式提交（feat/fix/refactor/test/docs/chore）
4. **停下来等你确认**，再进下一阶段
5. 每写完一个模块立即写测试，跑通再继续
6. 所有命令用 `uv run` 前缀（uv 全路径 `C:\Users\walton\.local\bin\uv.exe`，git 全路径 `C:\Program Files\Git\cmd\git.exe` —— 两者均不在 PATH）
7. 遇不确定：列 2-3 方案 + 权衡，交你决定，**不自行决定重大架构**
8. 性能敏感代码：提供基准脚本 + 前后对比数据（v4 基线已实测，见 ASSESSMENT.md §3）
9. 不引入 GPL 依赖 —— 已核查计划中全部依赖：PySide6(LGPL) / cryptography(Apache-2.0,BSD) / bcrypt(Apache-2.0) / Pillow(HPND) / mss(BSD) / numpy(BSD) / av(BSD) / opencv-headless(Apache-2.0) / pynput(LGPL) / pyperclip(BSD) / structlog(Apache/MIT) / pydantic(MIT) / protobuf(BSD) / aiohttp(Apache-2.0) / grpcio(Apache-2.0) / sounddevice(MIT) / psutil(BSD) —— **无 GPL**（LGPL 在约束允许范围内）
   - ⚠️ **唯一例外待核实**：`qasync`（阶段 6.3 的 Qt↔asyncio 桥接候选）。已 dry-run 实测 `qasync==0.28.0` 在 Python 3.14.3 上可解析安装，但**许可证未核实**（dry-run 不返回该字段，我不凭记忆断言）。→ 列为**阶段 6 开工前置检查项**；若为 GPL 则改用自建的 loop-in-thread + Signal 投递方案（计划中已作为备选，不构成阻塞）。

---

## 7. 下一步

### 7.1 已确认（2026-09-19，用户在确认卡片中明确选择）

- ✅ **前置阻塞 A**：批准 `uv sync` 安装全部缺失工具链（17 个包）
- ✅ **前置阻塞 B**：批准改写阶段 1 验收条件为"建立测试骨架 + 为搬迁后的 `protocol/framing`、`client/ratelimit`、`config/migration` 补首批回归测试并全绿"
- ✅ **Q1 打包**：本机 PyInstaller -D，Nuitka 只在 CI
- ✅ **Q2 Python**：CI 矩阵 3.11-3.14，`requires-python >= 3.11`
- ✅ **Q3 画面管线**：v5 全帧 H.264，v4 JPEG 差分

### 7.2 默认采纳、待用户复核（未列入确认卡片）

- ⏳ **Q4 v4 兼容保留期**：默认 A（v5.0 保留 → v5.1 deprecated → v6.0 删除）
- ⏳ **Q5 CI 验收方式**：默认 A（交付配置 + 本地模拟，不推送外部）

> 这两项若不提异议即按默认执行。**Q4 若改为 C 需尽早提出**（会牵连阶段 3 兼容层设计）；Q5 走 B（真实推送 CI）需用户单独授权仓库地址。

### 7.3 阶段 1 开工前置检查项

1. **qasync 许可证核实**（见 §6 第 9 条）—— 已实测 0.28.0 在 Python 3.14.3 可解析安装；许可证待查，GPL 则切自建 loop-in-thread 方案，不阻塞阶段 1（阶段 6 才用到）
2. `uv sync` 后**实测**：`uv run ruff --version`、`uv run mypy --version`、`uv run pytest --version` 确认可调用，再开始搬迁
3. 首次 `git commit` 前先 `git config user.name/email` 检查（本机 git 未在 PATH，用全路径）

### 7.4 待用户回复

**我现在停下，等你确认 Q4/Q5（或明确"按默认走"）后，开始阶段 1。** 阶段 1 第一步是写 `pyproject.toml` + `uv sync`，之后机械搬迁 + 清零 49 处静默吞错 + 建测试骨架。


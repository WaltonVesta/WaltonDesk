# STAGE7_REPORT.md — 阶段 7：输入层（Windows 优先）✅ 完成

> 完成日期：2026-09-30
> 基线 HEAD：`bd7c7d1`（阶段 6 完成）→ 本阶段 6 个提交（批 A~F）
> 验收：**ruff All checks passed · mypy Success（91 files，宽松清单 input 条目清零）· pytest 1137 passed（阶段 6 基线 821 → +316，零回归）· input/ 覆盖率 95.5%（验收线 ≥70%）**

---

## 0. 范围（用户 2026-09-30 决策）

**Windows 优先 + macOS/Linux 空实现**。理由：主用 Windows、macOS/Linux 本机
不可测、发布目标未定、保留 `InputBackend` Protocol 扩展性。补 macOS/Linux
只改"实现"（替换方法体），Protocol 与上层零改动。

三个开工决策点（均已按用户确认落地）：
1. **测试不打真机**：单测在 `SendInput` 边界打桩，只断言组装的 INPUT 结构体
   （flags/dx/dy/mouseData/wVk/wScan），无自动真实注入；真机验证由用户手动执行；
2. **剪贴板扩展 `ui/clipboard_sync.py`**（复用 Qt 路径，本机可测，不新建 Win32 模块）；
3. **IME 口径**：发送侧不破坏 IME、接收侧不模拟 IME——已上屏文本
   `KEYEVENTF_UNICODE` 逐码元直投 + 键名→VK 映射 + 修饰键，不模拟组合态。

## 1. 交付物（按批）

| 批 | 模块 | 内容 | 测试 |
|----|------|------|------|
| A | `input/base.py` | `InputBackend` 协议（mouse/key/text/多显示器 + reset/supported）、`MouseButton`/`ScreenRect`（负原点虚拟桌面）、`normalize_key_name`（X11/Qt/规范名三风格归一，值域=v4 `KEY_MAP`）、`default_backend` 工厂（平台探测/空实现回退/依赖缺失回退） | 契约 36 + 单元 68（工厂回退/探测全分支） |
| A | `input/macos.py` / `linux_x11.py` / `linux_wayland.py` | **空实现**：全方法 `NotImplementedError` + `supported()=False`；Wayland 提供 `wayland_session_detected()` 探测；模块 docstring 固化未来实现要点（CGEventPost/XTest/libei + 许可证注意事项） | 契约 24（逐方法×3 平台断言抛错）——覆盖率 100% |
| B | `input/windows.py` | ctypes `INPUT/MOUSEINPUT/KEYBDINPUT` 复刻（x64 sizeof=40 测试锁定）；绝对坐标 `ABSOLUTE\|VIRTUALDESK` 归一化 0–65535（含负原点）；`KEYEVENTF_UNICODE` 逐 UTF-16 码元（emoji 代理对、`\n`→VK_RETURN）；VK 表 + 扩展键标志；`VkKeyScanW` 标点兜底（需 Shift 拒绝→指向 type_text）；reset 记账逆序释放；`send/bounds/scan_char` 全注入化 | 59（结构布局/坐标/键/文本/reset/背压/契约复用） |
| C | `input/coalesce.py` | `InputCoalescer`：移动只留末值（v4 每像素一包病灶消除）；节拍 `max(画面fps, min_input_fps)` 消费 `FrameDecision.fps`（鸭子协议，不依赖 capture 层）——静止 idle 档不饿死输入；ScreenView 载荷解析（`move/press:btn/release:btn/wheel:dir`）；非移动事件排队不丢 + `max_pending` 背压提前出队；连续 `feed_text` 拼接；单调 `seq` | 35+1（假时钟全确定，零 sleep） |
| D | `input/legacy_pyautogui.py` | v4 `mouse.py`+`keyboard.py` 注入路径合并为协议实现；pyautogui/bounds 注入化；剔除监听侧与 50ms 防抖（归 UI/批 C）；修复 v4 吞错→`InputControlError`；补 reset 记账 | 36（替身断言调用序列 + 契约复用，CJK 限制显式断言） |
| E | `ui/clipboard_sync.py`（扩展） | 载荷 text/image/files；两级检测（100ms 廉价轮询 + `dataChanged` 全量确认，图片 PNG 摘要仅全量路径）；优先级 `urls > image > text`（Windows 复制文件带路径文本）；`apply_remote_*` 读回基线防回环；阶段 6 `localChanged` 基线原样 | 11（offscreen 真实往返，含防回环/优先级/清空边界） |
| E | `ui/screen_view.py` + `main_window.py` + `input/keys.py`（IME） | 物理键一律 `keyEvent`（阶段 6 基线不变）；`textCommitted` 新通道收 IME 提交；viewport `eventFilter` 截获（InputMethod 事件**无冒泡**）；preedit 永不外发；`qt_key_to_name`；`ClientController.send_text`/`ClientEvents.clipboard_{image,files}_from_remote` 接线 | 6+主窗口接线回归 |
| F | 收口 | 删除 v4 `input/{mouse,keyboard,dpi,modifiers}.py`（零引用核验：v5 树内仅 migrate 脚本字符串映射与注释）；mypy 宽松清单 input 条目清零；`input/__init__` 文档图 | 全量回归 |

## 2. 验收对照（真实输出）

```
ruff:   All checks passed!（src tests）
mypy:   Success: no issues found in 91 source files（input 四件删除；
        新 input/* 与批 E UI 件全 strict，宽松清单 rdc.input.* 清零）
pytest: 1136 passed（批 F 补 poll 测试后 1137；基线 821 → +315，零回归）

input/ 覆盖率（验收线 ≥70%；UI 与非 UI 分进程口径）：
  base.py 100.0% · coalesce.py 100.0% · keys.py 100.0%
  macos/linux_x11/linux_wayland 100.0%（空实现契约全验）
  legacy_pyautogui 94.3% · windows 88.3% → TOTAL 95.5%
  缺口说明（决策点 1 的必然代价）：windows 的 _real_send/_system_bounds/
  _win_scan_char 真实 Win32 边界（165-190）+ legacy 的默认装载路径
  （56-58,67-82）——单测一律注入替身，不打真机；此段由用户手动验证 + CI 覆盖。
```

## 3. 设计决定与偏离（D30 起）

| # | 内容 |
|---|------|
| D30 | **InputBackend 方法集定稿**（协议含 `move_mouse/click/scroll/key_press/key_release/type_text/bounds/reset/supported`）；`supported()` 为范围调整新增项——空实现自报 False，工厂据此回退，未来 macOS/Linux 换实现不改上层 |
| D31 | 键名**规范值域 = v4 `KEY_MAP` 值域**（`enter/esc/ctrl/win/pageup/kp0…`）：X11/pynput 风格（`Control_L`）与 Qt 风格（`Escape`）经 `normalize_key_name` 归一；pyautogui 回退因此零映射成本（v4 本就按它设计） |
| D32 | **Windows 坐标归一化按虚拟桌面并集**（GetSystemMetrics 76–79 实时读，分辨率变化自动跟随）；`click/scroll` 一律"先 move 后动作"（契约锁定），远端只需末值坐标 |
| D33 | 需 Shift 组合的字符 `key_press` **拒绝**并指向 `type_text`（不自动按 Shift——避免与被控端键盘布局耦合）；大小写文本同理 |
| D34 | coalesce 节拍 `max(fps, min_input_fps=30)`：静止门控把画面降到 idle 档时，指针移动（不改变远端差分）仍按 33ms 节奏出批；`poll`（到点出批，装配层定时器用）与 `drain`（强制刷新）分离；`set_fps` 节拍变化**只提前不推迟**（每帧调用时顺延会饿死出批） |
| D35 | IME 双通道最终口径：**物理按键一律 `keyEvent`**（阶段 6 基线原样保留——远端键盘布局/远端 IME 由被控端自然决定），仅候选窗提交串走 `textCommitted`。批 E 初稿曾把可打印键改走文本通道，被契约基线测试抓住回滚（约束"无回归"直接生效） |
| D36 | 剪贴板两级检测：轮询只做廉价键（文本/路径哈希、图片尺寸键），`dataChanged` 全量确认（图片 PNG 摘要）——避免逐轮询编码大图，也避免文件/图片场景文本误发 |
| D37 | v4 四件删除：`DPIAdapter.to_remote` 已被 ScreenView 场景矩阵替代（D25）、`ModifierState` 记账被后端 `_pressed_*` 替代、监听器归 ScreenView——v5 树内零消费方，git 历史可查 |

## 4. PySide6 6.11 绑定陷阱（探针实测，防复发）

1. **`QInputMethodEvent(commit_or_preedit, attrs)` 两参构造把第一个串填入
   preedit，`commitString()` 恒空**（探针 `probe_ime_ctor.py`）——测试用子类覆写
   `commitString()` 模拟提交事件，不依赖构造器语义；
2. **`InputMethod` 事件无冒泡**：生产 IME 事件落在 viewport（焦点代理），
   QGraphicsView 的 `inputMethodEvent` 覆写收不到，必须 `installEventFilter`
   （`screen_view.eventFilter`）；
3. 存根/运行时错位两处：`QImage.save(device, "PNG")` 存根要 `bytes` 运行时只收
   `str`；`bytes(QByteArray)` 存根不收运行时支持 buffer 协议（`clipboard_sync`
   内 ignore 注释固定）；`QMimeData` 无 `image()` 便捷法（用 `imageData()`）；
   `QClipboard` 无 `urls()`（用 `mimeData().urls()`）；
4. offscreen 空剪贴板 `mimeData()` 返回 **None**（阶段 6 的 `text()` 返回 `""`
   掩盖了这点）——快照面全部 None 防护；
5. `frozen+slots` dataclass 在 3.14 对未知属性赋值抛 `TypeError` 而非
   `FrozenInstanceError`（CPython 已知粗糙点）；`ScreenRect` 实例极少，去掉 slots。
6. Qt 进程级全局**剪贴板跨测试泄漏**：测试 fixture 必须 `clear()` 后再构造
   同步器（批 E 教训——实现正确、基线被污染）。

## 5. 与 v4 行为对照（输入层）

| v4 病灶 | v5 处置 |
|---------|---------|
| 每像素一个鼠标包 | coalesce 移动末值合并（30fps 每批 ≤1 条 move） |
| 键盘 50ms 防抖丢键 | 删除——合并归帧节拍，注入层不丢键 |
| `execute_event(dict)` 裸字典 | 协议化方法 + 类型注解（mypy strict） |
| `except Exception` 后静默 | 一律 `InputControlError`，失败可观测 |
| 断连残留卡住的修饰键 | `reset()` 记账释放（键+鼠标键，逆序、幂等） |
| 水平滚动 TODO | `HWHEEL` 落地（批 B/契约锁定） |
| DPIAdapter 手算缩放 | 删除——ScreenView 场景矩阵 + 虚拟桌面归一化（D25/D32） |

## 6. 验收清单复跑（用户真机手动验证项）

以下**只能真机验**（单测按决策点 1 不打真机），阶段 9 装配完成后一次性走查：
- 鼠标移动/三键点击/垂直+水平滚轮在远端生效且坐标跨屏正确（副屏负原点）；
- 文本：英文直投、中文经**控制端 IME** 提交后远端上屏一致、emoji；
- 键盘：F 键/方向键/编辑键/小键盘、Ctrl+组合热键、断连后无卡键；
- 剪贴板：文本双向、截图粘贴（图片）、文件清单复制（内容传输待阶段 8）；
- 性能观感：移动不抢画面带宽（对照 v4 每像素一包）。

## 7. 下一步：阶段 8 — 文件传输 + 音频（可选，20-26h）

起点备忘（与阶段 7 的接缝）：
1. **8.1 文件传输**：1MB 分块 + SHA-256 + 断点续传（偏移协商）——协议
   `ClipboardEvent(chunk_total/chunk_index)` 分帧已就绪（批 E 把剪贴板图片/文件
   字节交给它即可，`ClipboardSync` 载荷只到内存对象、未触网）；
2. **8.2 `transfer_dialog.py`**：拖拽 UI + 进度 + 暂停/续传（PySide6，阶段 6 件
   复用对话框非模态+Future 模式 D26）；
3. **8.3 路径穿越防护**：目标路径规范化 + 沙箱根校验（阶段 9 渗透清单前置）；
4. **8.4 音频**：sounddevice + Opus（实测 libopus 可用）——`pyproject` 的
   mypy/依赖面本阶段已预留 `sounddevice.*`；
5. 阶段 7 遗留接线全部归**阶段 9**：controller 实现 `send_mouse/send_key/
   send_text`（ScreenView 信号已备）、`clipboard_{text,image,files}` 双向、
   coalesce 挂 pipeline 节拍（`sync_to_frame`）、`default_backend` 装配、
   `input/windows.py` 真机走查（本报告 §6）。

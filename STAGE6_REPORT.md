# STAGE6_REPORT.md — 阶段 6：UI 迁移 PySide6 + 广播管线 ✅ 完成

> 完成日期：2026-09-25
> 基线 HEAD：`561053f`（阶段 5 完成）→ 本阶段 6 个提交（PoC / 批A / 批B / 批C / 批D / 删除+收口）
> 验收：**ruff All checks passed · mypy Success（87 files）· pytest 821 passed · UI 覆盖率 90.3% · 冷启动 441.5ms <1500ms**

---

## 1. 任务逐项状态（对照任务书 16 项）

| # | 任务 | 状态 | 交付物与验证 |
|---|------|------|--------------|
| 1 | framerate.py 重写 | ✅ | 纯决策状态机（零睡眠、单调时钟可注入、假时钟全确定测试）；v4 兼容面实测保留；mypy 宽松清单 capture 条目**清零** |
| 2 | 广播管线串联 | ✅ | `server/pipeline.py` ScreenSource→Differ(adopt=True)→门控→编码(hw 选型)→Broadcaster.publish；测试 15 项（含真实 broadcaster 全链路 + **publish 主线程断言 + encode worker 线程断言**） |
| 3 | ui/app.py | ✅ | PassThrough DPI（仅新构造时设置）+ QSS Template 渲染 + qasync 桥接入口 + `ensure_gui_thread` 守卫 |
| 4 | theme/dark.qss + tokens.py | ✅ | 单一令牌源（string.Template `$token` 渲染——CSS 花括号禁 str.format，踩坑记录 §5-T1） |
| 5 | asyncio↔Qt 桥接 | ✅ | **qasync 入选（PoC 双轨实测，判据全过）**，脚本固化 scripts/poc/ |
| 6 | 低风险件 | ✅ | status_bar（FPS 内聚）/ performance_panel（QProgressBar）/ status_panel（HostStatsProvider 协议） |
| 7 | toolbar + quality_indicator | ✅ | 纯 Signal 件；QPainter 圆点 + QPropertyAnimation 脉冲；等级阈值与 v4 逐档一致（测试锁） |
| 8 | screen_view.py | ✅ | QGraphicsView/PixmapItem；场景坐标=远端虚拟桌面绝对坐标（负原点多显示器）；越界黑边不发送输入；26 测试 |
| 9 | main_window.py | ✅ | 六态状态机单入口；ClientController/ClientEvents 协议注入（fake 全驱动）；F11/Ctrl+Alt+P/Esc；closeEvent 级联 |
| 10 | settings_dialog.py | ✅ | QTabWidget 三页签 ↔ 扁平 v5 Settings（D8 兑现）；apply() 返回新值对象不写文件 |
| 11 | password/fingerprint_dialog.py | ✅ | **B2 根治**：非模态 + asyncio.Future（v4 的 after+Event 阻塞缝合拆除）；超时→取消/拒绝（TOFU 安全默认） |
| 12 | tray.py | ✅ | QSystemTrayIcon + QPainter 三态图标（v4 pystray 未安装问题一并消除）；菜单 v4 全保留 |
| 13 | clipboard_sync.py | ✅ | QClipboard+QTimer 100ms 轮询 + hash 去抖 + apply_remote 防回环 |
| 14 | launcher.py | ✅ | LaunchOption StrEnum + awaitable launch_choice（默认键=被控端，v4 序） |
| 15 | 测试 | ✅ | pytest-qt 22 文件全绿（offscreen）；**覆盖所有对话框 + 主窗口状态流转 + 冷启动计时** |
| 16 | 删除 legacy_v4/ | ✅ | 独立提交 `chore(stage6)`：45 文件 7,505 行，零引用核查（git grep+工作区扫描，仅注释/配置提及）后删除；git 历史可恢复 |

## 2. 验收对照（真实输出）

```
ruff:   All checks passed!（src tests scripts）
mypy:   Success: no issues found in 87 source files
pytest: 821 passed in 32.72s（阶段5基线 695 → +126，零回归）

UI 覆盖率（4 个 UI 测试文件 --cov=src/rdc/ui）：
  TOTAL 1221 stmts → 90.3%（验收线 ≥70%；main_window 77.0% 最低，全组件 ≥77%）
framerate.py 100.0% · pipeline.py 92.5%（capture/+encoding/ 合并 62.1% 系旧
  兼容外壳 ScreenCapturer 拉低，阶段 9 拆除后回升）

冷启动（scripts/bench_coldstart.py，独立子进程×5，offscreen）：
  samples = 458/441/427/441/438ms → median 441.5ms  PASS（<1500ms）
  （余量 3.4×，预授权的 lazy-import 对策未触发）
```

## 3. 桥接选型结果（叮嘱 a：PoC 判据与数据）

**qasync 0.28.0**：许可证 PyPI 实测 **BSD-2-Clause**（约束 9 合规）。官方 README 声明支持
Python <3.14，本机 3.14.3 四判据实测：

| 判据 | 甲 qasync | 乙 loop-in-thread+Signal |
|------|-----------|--------------------------|
| QTimer 1ms 裸精度 | p50 1.0/p95 1.2ms ✅ | p50 1.0/p95 1.2ms ✅ |
| 4×纯忙等竞争下 | p50 1.0/**p95 1.15ms** ✅ | p95 11–16ms、2s 仅 700–800 tick（丢 1.5s）⚠️ |
| 异常传播 | 直接 await ✓；未检索任务经 handler+stderr 可见 ✓；future 传递 ✓ | 跨线程异常回主线程 re-raise ✓；loop 内任务异常上抛 ✓ |
| Ctrl+C/信号退出 | ≤3s 干净退出 ✓（run_forever 用 stop 非 quit，坑①） | ✓ |

**预授权规则命中"双通过选甲"→ qasync**。核心发现：**loop-in-thread 不防 GIL 争用**——
纯 Python 忙等照样拖垮 Qt 主线程定时器（方案乙反而劣化）。PoC 代码固化
`scripts/poc/`（run_poc.py + poc_results.json 选型证据）。
**3.16 风险登记**：qasync 内部用 `asyncio.iscoroutinefunction`（3.14 已弃用/3.16 移除），
测试 filterwarnings 已按消息豁免并注释迁移触发条件 → 届时切方案乙（PoC 代码现成）。

## 4. 广播管线与 GIL 实测（叮嘱 b）

**线程模型**：生产单元（capture+diff+decide+encode）整体投**单 worker** executor
（串行=编码器/差分器状态安全 + 双缓冲帧引用在编码前不被覆写）；
**publish 固定事件循环线程**（DropOldestQueue.put_nowait 唤醒的是循环内 getter）；
测试断言：`bc.publish_threads` 全 == 主线程 ident、`enc.threads` 全 != 主线程且单一。

**GIL 释放口径**（scripts/bench_looplag.py，worker 连续重负载下主线程 1ms 定时器滞后）：
```
encode 1080p ×120：p50=1.0ms p95=2.0ms
encode 4K ×40    ：p50=1.0ms p95=3.8ms
diff 1080p ×120  ：p50=1.0ms p95=2.0ms   （均 ≪ 15ms 判据）
```
→ **PyAV/cv2/numpy 重段基本释放 GIL，executor 模型成立，无需进程池**。
⚠️ 测量陷阱（坑②）：Windows 时钟默认粒度 15.6ms 把一切钳在 15ms 地板，须
`timeBeginPeriod(1)` 且**每用例独立进程**（同进程顺序跑时前序 PyAV open 后原生库
把粒度复位）。注：纯忙等计数口径的"GIL 保持率"44–57% 是调度公平性指标，对 UI
无意义——以循环滞后为准。管线另测：qasync 事件循环挂 4 个忙等任务时定时器 p95 仍
1.15ms（辅证）。

## 5. 设计决定与偏离（D22 起）

| # | 内容 |
|---|------|
| D22 | 桥接选型 **qasync**（PoC 实测四判据全过；许可证 BSD-2 合规；3.16 迁移路径预置） |
| D23 | pipeline **单 worker executor** 承载全部重段；broadcaster 所有权在会话层（阶段 9 装配），pipeline.close 不关 broadcaster |
| D24 | **h264.py 新增 `codec_name` 注入**（hw_probe.best_encoder 落地路径）；硬编选项 `_hardware_options` 返回空映射——nvenc/qsv/amf 选项键与 x264 不互通且本机全不可用，**禁止发明未验证参数**，真启用时标定（阶段 10 跟进项） |
| D25 | screen_view 场景坐标 = 远端虚拟桌面绝对坐标（item offset 承载负原点），坐标变换交给 Qt 场景矩阵（v4 手算缩放+忽略负原点两类病灶结构性消除）；输入默认只读，控制开关后外发 |
| D26 | B2 根治为**非模态对话框 + asyncio.Future**（qasync 同线程结构下 v4 的 after+Event 缝合整类拆除）；密码超时→取消、指纹超时→**拒绝**（S1-S4 口径） |
| D27 | 托盘 pystray → **QSystemTrayIcon + QPainter 图标**（v4 未安装依赖消除）；菜单/tooltip 语义与 v4 全保留 |
| D28 | SettingsDialog 纯编辑视图：`apply()` 返回新 Settings，持久化/save 归 main_window（校验+落盘+re-sync）；remember_password 升为可用字段（v5 扁平 schema，D8 兑现） |
| D29 | framerate v5 重写为**纯决策器**（decide→FrameDecision(action/fps/delay_s/reason)）；v4 兼容面（wait_frame/adjust_fps/set_fps/…）保留但 wait_frame 不再睡眠（v4 运行时实测无调用点，零风险收紧） |

## 6. 踩坑清单（测试/闸门抓出，防复发）

1. **t1** `shiboken: '__init__' not called`——widget 构造中 `QGraphicsScene(self)` 先于
   `super().__init__` 引用自身；必须先 super 再建场景。
2. **t2** `string.Template` 渲染 QSS：CSS 块自带 `{}`，**禁用 str.format**；同时注释里
   出现裸 `$token` 会触发 KeyError——模板占位统一 `$name`，注释避裸 `$`。
3. **t3** `QPushButton/default` 是错选择器 → Qt 静默 **整表解析失败**（"Could not parse
   application stylesheet"）；正确 `:default`。
4. **t4** Qt 6 枚举名实核：`QDialogButtonBox.StandardButton` 无 Quit（用 Close）、
   `ScrollPhase.NoScrollPhase`（非 NoPhase）。
5. **t5** pytest-qt 的 `qtbot.addWidget` 只收 QWidget，QSystemTrayIcon 会 TypeError。
6. **t6** QGraphicsView 默认**居中场景**——小帧坐标系统性偏移；远桌面语义须滚动条置
   min（左上对齐，黑边在右下）。
7. **t7** 信号槽不能直连协程（ensure_future 不执行）→ main_window `_spawn` 投递。
8. **t8** 父窗未 show 时子控件 `isVisible()` 恒 False → 显隐断言用 `isHidden()` 或先 show。
9. **t9** `QApplication.instance()` 存根返回 QCoreApplication——触达 clipboard/
   styleSheet 前须 isinstance 收窄。
10. **t10** Windows 下 SIGINT 送达 Qt/C++ 泵不可靠：live 模式实测须 `signal.signal`
    装处理器（甲方案 `loop.call_soon_threadsafe(loop.stop)`，非 quit）——已固化 PoC。
11. **t11** coverage.py 对 Qt 定时器×事件循环 tracing 在 Windows 挂死（>13min）→
    UI 与非 UI 覆盖率**分进程测**（本报告数据即此口径合成）。

## 7. 遗留（不影响验收）

- `client/` 无代码（阶段 9 装配层拆 `rdc/client/controller.py`，UI 红线：controller 零
  Widgets 导入——本阶段 Protocol 已为此预留）；
- pipeline `run()` 长循环与真实 MSS 源的集成冒烟未跑（需真屏+真 socket，归阶段 9）；
- toolbar `set_connection_state` 在 disconnected 分支未显式 set host editable
  （仅依赖 set_connection_state("disconnected") 路径自带，行为与 v4 一致）；
- v4 运行时 `service.py` 的 `from gui.tray import` 降级注释仍标 TODO(phase-6)——
  按 D14 该文件整体归**阶段 9 拆除**，本阶段不碰（越权编辑已回滚，见批 E 记录）；
- 阶段 4/5 遗留不变（constants 双份、create_json/create_screen、_v4_config）。

## 8. 阶段 7 起点

1. **输入层 InputBackend Protocol**（REFACTOR_PLAN §7.1-7.6）：`base.py` 协议 +
   `windows.py` SendInput(ctypes)（本机可验）+ macos/linux（CI/用户验）；
   v4 `input/{mouse,keyboard,dpi,modifiers}.py` 重写补注解后移出 mypy 宽松清单；
2. `coalesce.py` 鼠标事件流合并 + 帧对齐——**消费本阶段 pipeline 的帧率决策
   （FrameDecision.delay_s/fps）与 ScreenView 的 mouseMoved 载荷格式**
   （`move / press:btn / release:btn / wheel:dir`，远端绝对坐标）；
3. UI → controller 的 `send_mouse/send_key` 契约已定（MainWindow 经 Protocol 调用，
   阶段 7 产出的 backend 由阶段 9 装配层实现该 Protocol——UI 零改动）；
4. IME/文本输入（7.8）注意与 qasync 主线程 pynput 回调线程的边界：回调**只进
   队列**，触控件一律 Signal。

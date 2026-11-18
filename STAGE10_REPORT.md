# STAGE10_REPORT.md — 阶段 10：品牌更名 + 图标接入 + 打包发布 ✅ 完成（用户指定清单范围）

> 日期：2026-11-14 初版收口 · **2026-11-15~18 收口批**（用户真机走查推翻三项：
> 输入模块漏包致控制断联 🔴 / 安装包中英混杂 / 双击闪黑框；全部修复并重新打包，
> **终值以 §8.4 为准**，§0~§7 为初版过程记录含被推翻的架构决策）
> 范围：v4.0.3 → v5.0.0 重构阶段 10，**按用户下发的四组清单执行**
> （一 全局品牌更名 / 二 图标接入与 PyInstaller 配置 / 三 Windows 安装包 / 四 本地验证与文档收口）
> 配套：`REFACTOR_PLAN.md` §阶段10（原 9 项任务表）、`STAGE9_REPORT.md`（上一阶段）、
> `PROGRESS.md`（进度总览）、`README.md` / `CHANGELOG.md`（面向用户的交付文档）
> 提交：`0904fb9`（更名+图标）→ `c4e3c06`（打包链路+冒烟修复）→ 收口批（§8 三项修复）

## 0. 结论摘要（真实输出）

> ⚠️ 本节是**初版**数据，其中"console 形态构建"已被收口批反转为 windowed。
> 交付前请以 **§8.4 收口批验收** 为准。

```
pytest:  1700 passed / 2 skipped，退出码 0（进场基线 1650 passed / 2 skipped，+50 哨兵）
         分组（pytest --collect-only 实数）：
           品牌组 29 = test_stage10_branding.py
             TestBrandSingleSource 6 + TestLegacyRootPathSafety 3 +
             TestMigrateLegacyRoot 10 + TestServiceIdentifier 2 +
             TestConfigBomTolerance 2 + TestAutostartRegistryRename 6
           打包入口组 21 = test_desktop_entry.py
             TestKeepsConsole 12（裁决表参数化 11 + argv 不改写 1）+
             TestFrozenEntry 5 + TestDetachedConsoleStreamHygiene 4
ruff:    All checks passed!（src + tests + scripts）
mypy:    Success: no issues found in 106 source files（进场 103，+brand/+icons/+desktop_entry）
bandit:  High 0 / Medium 0 / Low 0
产物:    dist/WaltonDesk/WaltonDesk.exe —— --version 输出 "WaltonDesk v5.0.0"（exit 0）
冷启动:  5 次独立计时 median 0.419s（验收线 <2s）
安装包:  dist/WaltonDesk_setup.exe（Inno Setup 7.1.1 本机 iscc 编译，见 §5/§6）
```

双红哨兵纪律延续：本阶段新增 50 项哨兵，逐项注入病灶复现转红（品牌组 6/6、
打包入口组 7/7），恢复后全绿。

## 1. 品牌更名（清单 · 一）

### 1.1 更名边界：什么改、什么刻意不改

**改**（用户可见 + 发行元数据）：

| 面 | 落点 |
|---|---|
| 发行名 | `pyproject.toml` `name = "waltondesk"`、描述/作者/keywords/urls |
| 命令入口 | `[project.scripts] waltondesk = "rdc.cli:main"`；`argparse prog="waltondesk"` |
| CLI 横幅 | `description` / `--version` 均输出 `WaltonDesk v5.0.0` |
| 窗口标题 | 启动器 `WaltonDesk v5.0.0`、主窗 `… — 主控端`、初始密码窗 `… — 初始密码` |
| 托盘 | tooltip 首段 `WaltonDesk`、初始密码气泡标题 `WaltonDesk · 初始密码` |
| 应用名 | `QApplication.setApplicationName("WaltonDesk")` |
| 数据目录 | `~/.remote_desktop` → **`~/.waltondesk`** |
| 自启注册表 | HKCU Run 值名 `RemoteControlService` → **`WaltonDesk`** |
| PID 文件 | `~/.waltondesk/WaltonDesk.pid` |

**刻意不改**（改了会静默破坏存量状态，已在 `rdc/brand.py` 顶部登记为契约）：

1. `secrets/dpapi.py` 的 `DPAPI_ENTROPY = b"RemoteDesktop-Control-v3"`——熵串参与
   DPAPI 密文派生，改动即令**已保存口令永久解不开**（`test_secrets_dpapi_roundtrip`
   锁死其字面值）；
2. `secrets/keychain.py` 的 `SERVICE` / `secrets/libsecret.py` 的 `APP_NAME`——
   macOS/Linux 密钥环既有条目的归属键；
3. `transport/tls.py` 自签证书 `CN=RemoteDesktop`——存量 TOFU 指纹按证书主体记录，
   改代码也改不了已落盘的历史证书，改名会让全部存量信任失效。

> v5.1.0 若要做"密钥库命名空间迁移"，须与凭证重签一并设计，不在 v5.0.0 范围。

**导入包名保持 `rdc`**：只改发行名与入口名。若连 import 路径一起改，将牵连
上千处 import、`mypy packages`、`pythonpath`、`coverage source` 以及全部
`python -m rdc` 形态（**开机自启命令正依赖它**），收益为零、风险极高。

### 1.2 品牌单点模块 `rdc/brand.py`（新增）

`BRAND_NAME` / `SERVICE_ID` / `brand_title()` 三个常量成为唯一来源。动机是历史
教训：更名不彻底最典型的形态就是"改了窗口标题漏了托盘 tooltip"。
`autostart.SERVICE_NAME` 与 `server/service._SERVER_NAME` 现同时引用
`SERVICE_ID`，注册表值名与 PID 文件名同名、品牌不再散落。

### 1.3 数据目录一次性迁移 `paths.migrate_legacy_root()`

**为什么必须迁**：DPAPI 密文索引（`secrets.dpapi.json`）与 `config.json` 里的
bcrypt 哈希都躺在旧目录，目录名一改就读不到——用户表现为"密码框明明存过却说
没设密码"，属静默数据丢失。

**时序**：`cli.main()` 第一行调用，排在**一切配置/密钥/日志读取之前**。晚一步
就堵死了——`load_settings` 在文件缺失时会直接新建空配置并落盘，把"迁移"永久
挡在门外。

**只搬不删**：跨目录搬迁非原子，"复制成功→删除"中间任一步出错都会留下两边都
不完整的状态；留个空壳旧目录是更好的结局。删除留给用户。

**三道安全闸**（均有哨兵）：
1. 新目录已有 `config.json`（或完成标记）→ 整体放弃，绝不覆盖新数据（回滚/
   重装场景下新目录可能才是权威数据源）；
2. copy2 兜底路径**逐项跳过已存在目标** + 写完成标记防重复扫描；
3. **override 根（便携/测试模式）下自动迁移直接跳过**——pytest 的 `tmp_path`
   位于真实 `~` 之下，若照跑会把用户**真实的** `~/.remote_desktop` 整目录
   rename 走。这条是写哨兵时想到的真实破坏性场景，守卫必须先于任何 IO。
   另 conftest 会话级开关默认关闭真实迁移。

**失败不抛**：迁移异常只留痕（走标准库 logging，因该函数被日志系统装配前调用），
新目录照常可用。

### 1.4 自启注册表更名与旧值清理

旧值 `RemoteControlService` 指向**过时**的解释器/脚本路径：用户若曾勾过自启，
开机既拉不起新程序又与新值并存（表现为"勾了自启却没生效"）。
`install()` 与 `uninstall()` 都顺带清理旧值；`query` 在只有旧值时仍如实报
"已开启"（否则托盘显示未勾选、用户点下去却什么都没发生）。
**显式自定义服务名（多实例场景）不越界清理**——哨兵专门锁这条。

### 1.5 残留哨兵（AST 级）

`test_no_old_brand_in_user_visible_literals`：用 AST 遍历 `src/rdc/**`，只扫
**字符串字面量**（排除 docstring 与注释——注释解释"阶段 10 前叫 X"是正当历史
记录，用白名单硬豁免反而脆弱）。`RemoteControlService` 仅允许作为 autostart 的
清理锚点存在，"远程桌面控制"零容忍。

> 该哨兵第一次运行即被仓库 6 个 v4 搬迁文件的 UTF-8 BOM 绊倒（`ast.parse` 报
> SyntaxError：裸 utf-8 读把 BOM 变成源码首字符 U+FEFF），改 `utf-8-sig` 后通过
> ——与 §3.1 修的是同一课，这已是第二遍付学费。

## 2. 图标接入（清单 · 二）

### 2.1 生成 `scripts/gen_icon.py`

输入 `icon.png` 实测 **1183×1220 RGBA（非正方形、四角透明）**，非任务描述的
1024×1024。先补透明正方形画布居中（否则直接缩到 256×256 会把 W 压扁），再出
**9 档尺寸**（16/20/24/32/48/64/96/128/256）——少一档 Windows 会挑最近尺寸放大
拉伸，16px 托盘上糊得最明显。

**双写两处**：
- 根目录 `icon.ico`——PyInstaller `icon=` 与 Inno `SetupIconFile` 需要**构建期
  磁盘路径**；
- `src/rdc/ui/theme/icon.ico`——**运行期唯一加载点**。打包产物里没有项目根，
  读根路径在 exe 下必然失败；`theme/` 已作为 datas 进包，`importlib.resources`
  在源码/frozen 两种形态同路径，零额外配置。

### 2.2 接入点

- `rdc/ui/icons.py`（新）：`build_app_icon()` 进程级缓存，失败返回 `None` 且
  **只告警一次**。图标缺失是外观问题，绝不能让主控端起不了窗；
- `create_app()` 设 `applicationName` + 应用级默认图标 → **全部**窗口自动继承
  （漏设单个窗体的可能性归零）；主窗另显式 `setWindowIcon` 防 pytest-qt 复用
  app 实例时漏设；
- 托盘 `build_tray_icon()`：品牌底图 + **右下角状态角标**（绿/橙/灰 + 白环描边，
  深浅两种任务栏底色都清晰）；资源缺失回落阶段 6.10 的纯色圆点画法——托盘
  **始终**可用。既有断言 `not build_tray_icon(st).isNull()` 三态全保持。

## 3. 打包（清单 · 二之 PyInstaller）

### 3.1 打包真机冒烟发现并修复三项

冒烟方式：隔离 `USERPROFILE` + `--config` 指向临时文件，真跑 frozen exe 起被控端。

| # | 问题 | 根因与修复 |
|---|---|---|
| 1 | **配置含 UTF-8 BOM 被误判损坏并静默重置**（实测复现：日志 `配置文件损坏已备份到 …corrupt`，随后配置全部回落默认值） | 记事本（Win10 1903+ 默认存带 BOM 的 UTF-8）或 Excel 另存过 `config.json` 后，裸 utf-8 解码令 `json.loads` 抛 "Unexpected UTF-8 BOM" → 走"损坏"分支：**整份配置被改名 `.corrupt` 并用默认值重建**，用户表现为"设置和口令突然全没了"。改 `utf-8-sig`（对无 BOM 文件行为逐字节一致）。哨兵锁定"带 BOM 仍读回原端口值 + 不产生 .corrupt"与"应用自写文件不带 BOM"（防止修了解读留下写侧病灶） |
| 2 | **frozen 下回退输入后端静默失效** | `input/legacy_pyautogui._load_pyautogui()` 是函数内延迟 import，PyInstaller 分析器收不到 → 打包后回退路径抛 `PlatformUnsupportedError`（源码能跑、exe 不行，最难查的形态）。spec 补 `pyautogui` 及 `pygetwindow/mouseinfo/pymsgbox/pyscreeze` 到 hiddenimports |
| 3 | 服务标识残留旧品牌（`~/.waltondesk/RemoteControlService.pid`）+ 包 docstring 旧中文名 | 见 §1.1/§1.2；PID 属运行时临时文件，正常退出即删，无需迁移 |

### 3.2 console 形态 + 启动时释放控制台（架构决策）

`desktop_entry.py`（新）+ spec `console=True`。**为什么不 `console=False`**：
`--version`/`--help` 由 argparse 内部写 `sys.stdout`，windowed 下该流启动即为
`None` → 直接 `AttributeError` 崩（正是第十二批打包守卫修的那类崩溃）；`serve`
横幅与 `set-password` 的 `getpass` 也依赖真实控制台。

GUI/托盘形态改由入口在装配前 `FreeConsole()` 释放黑框。**关键收尾**：释放后必须
把 `sys.stdout/stderr/stdin` 置 `None`——否则 `console_stream()` 仍认为"stdout
存在"并返回指向已失效句柄的对象，Feature 18 首启密码横幅写它即抛 OSError 炸掉
被控端启动。置 None 后 `stdin_is_absent()` 返回 True，正好落回守卫既有的
"**无控制台 → 必须起 GUI**"分支，第十二批的三态判定语义天然继续成立（哨兵
显式锁这条：`cli._gui_launch_expected() is True`）。

裁决表（纯函数 `keeps_console`，参数化 11 例）：

| 调用 | 释放控制台 |
|---|---|
| 无参数 / `--gui` | ✅（双击图标 → GUI） |
| `serve --tray`（含 `--silent` 自启） | ✅（托盘常驻 = 桌面形态） |
| `serve`（headless） | ❌ 保留（横幅 + Ctrl+C） |
| `control` / `set-password` / `--version` / `--help` | ❌ 保留 |

非 frozen（源码/venv）不裁决：开发期在终端跑 `serve --tray` 本就该看到日志。

### 3.3 `waltondesk.spec`（新增；`.gitignore` 加 `!waltondesk.spec` 例外）

`.gitignore` 原有 `*.spec` 是 v4 时代忽略 PyInstaller 临时产物的规则，本文件是
**交付物**，不加例外则克隆仓库后无法构建。

**体积实测**：首轮 `collect_all("PySide6")` 产物 **899 MB**——光
`Qt6WebEngineCore.dll` 194 MB + 其 devtools `.pak` 83 MB，Designer/Pdf/Qml/Quick
全用不上（Qt6 DLL 共 144 个）。全树 grep 实测源码只 import `QtCore/QtGui/QtWidgets`
三模块，改为按模块 `collect_submodules` + excludes 挡大件：

- **899 MB → 358 MB**，Qt6 DLL 144 → **13 个**；
- 剩余大头是依赖固有体积（`cv2.pyd` 82 MB + videoio ffmpeg 29 MB、
  numpy 的 openblas 20 MB、`av.libs/avcodec-62` 18 MB）——换库收益有限，留 v5.1 评估；
- spec 内写明红线：将来引入新 Qt 模块必须同步 `PYSIDE_MODULES`，否则 frozen 下
  `ImportError`（源码能跑、exe 打不开）。

**datas 显式两项**（漏一个即闪退）：`rdc/protocol/pb`、`rdc/ui/theme`（含
`dark.qss` 与运行期 `icon.ico`）。`collect_all` 仅 `av` / `shiboken6`（包内二进制
非 import 可达：FFmpeg DLL 全套、`shiboken6.abi3.dll` 供 `main_window` 的对象
存活判定）。excludes 挡 pytest/pytestqt/hypothesis（体积 + 拖入 Qt Test）、
tkinter（v5 源码零引用）、pynput（阶段 7 已剔除监听侧，grep 实测零引用）。

## 4. 本机验证（清单 · 四之一）

| 项 | 结果 |
|---|---|
| `dist\WaltonDesk\WaltonDesk.exe --version` | `WaltonDesk v5.0.0`，exit 0 |
| `--help` / `serve --help` | 正常输出（exit 0） |
| 冷启动（`--version` ×5 独立进程） | 0.436/0.419/0.415/0.424/0.408s，**median 0.419s**（验收线 <2s） |
| 必需资源入包核验 | `rdc/protocol/pb` ✅、`rdc/ui/theme/dark.qss` ✅、`theme/icon.ico` ✅、`PySide6/plugins/platforms/qwindows.dll` ✅、`av/`(74 文件) ✅、`shiboken6/`(20 文件) ✅ |
| **frozen 被控端冒烟**（隔离数据根真跑） | 初始密码生成并显眼打印 → TLS 自签证书生成 → 双栈监听 → libx264 探测 AVAILABLE → pipeline 编码器就绪；**stderr 零输出** |
| `QT_QPA_PLATFORM=offscreen` 跑 `--gui` | 存活 6s 不闪退（证明 frozen 下 qss/icon/pb 真实可用，非"import 得到但资源读不到"） |
| `uv run pytest tests -q` | **1700 passed / 2 skipped**，退出码 0（更名零回归） |
| 发行元数据 | `importlib.metadata` → `Name: waltondesk, Version: 5.0.0`；`.venv/Scripts/waltondesk.exe` 已生成 |

## 5. Windows 安装包（清单 · 三）

`waltondesk_installer.iss` + 本机 `iscc` 编译（Inno Setup 7.1.1，`iscc` 在 PATH）。
产物与逐条要求核验见 §6。

要点：默认 `{autopf}\WaltonDesk`、`PrivilegesRequired=lowest` + 允许切 per-user、
桌面/开始菜单快捷方式、卸载注册（`AppId` 固定 GUID 保证覆盖升级而非并存）、
可选勾选"开机自动启动被控端"（**默认不勾选**——远程控制入口未经明确同意不得
默认开机常驻）写 HKCU Run（值名/命令与应用内 `AutostartManager` 逐字一致，
两边状态互认同步）、`SetupIconFile=icon.ico`、卸载前确认 + `taskkill` 结束被控端
（它常驻托盘会锁住 exe）、`[UninstallDelete]` 整目录删除但**不含**用户数据根。

### Inno Setup 7 实测踩到的四个坑（都真实报过错，改前请先读）

| # | 报错 | 教训 |
|---|---|---|
| 1 | `Unknown preprocessor directive`（`[Code]` 段 Pascal 的 `#13#10`） | ISPP 把以 `#` 开头的 token 当预处理指令。换行改用 `Chr(13)+Chr(10)` 拼（`const` 段不能用 `Chr()`，故放局部变量） |
| 2 | 同上，`[Tasks]`/`[Registry]` 等段用反斜杠续行 | **段内条目不支持续行**（只有 ISPP 指令支持），必须单行写完 |
| 3 | `Value of … "PrivilegesRequiredOverridesAllowed" is invalid` | 合法值只有 `none/commandline/dialog/all`，**没有** `output` |
| 4 | `Invalid number of parameters`（`MsgBox(文本, 标题, 类型, 默认)`） | Inno 的 `MsgBox` **只接受 3 个参数**，没有标题位 |
| 附 | `Unknown identifier 'NewLine'` | 向导常量 `NewLine` 在 `[Code]` 段不存在 |
| 附 | `skipifdoesntexist` 用于 `[UninstallRun]` | 该旗标属 `[Run]` 段，卸载段不支持 |

**为把上述迭代成本从"每次几十分钟"降到"几秒"**，新增
`scripts/check_iss_syntax.py`：抽出 `[Code]` 段拼成极简 iss 单独编译（Pascal
语法错误发生在压缩之前，1.6s 返回），段参数合法性仍需正式编译。

## 6. 验收数据收口

（编译结果见本节末"安装包编译"段）

```
ruff:    All checks passed!（src + tests + scripts）
mypy:    Success: no issues found in 106 source files
         （进场 103 → +rdc.brand、+rdc.ui.icons、+rdc.desktop_entry，全部走全局 strict 零豁免）
bandit:  Total issues by severity —— Undefined 0 / Low 0 / Medium 0 / High 0
pytest:  1700 passed / 2 skipped，退出码 0
         进场基线 1650 passed / 2 skipped → +50 哨兵（分组实数见 §0）：
           品牌组 29（品牌单点 6 + 路径安全 3 + 数据根迁移 10 + 服务标识 2 +
                      BOM 容错 2 + 自启注册表 6）
           打包入口 21（裁决表 12 + frozen 入口 5 + 标准流卫生 4）
双红哨兵：品牌组 6/6 注入病灶转红（override 守卫失效=真实旧目录被搬走 / CLI 漏挂
         迁移 / 自启旧值未清理〔2 项〕/ 托盘 tooltip 旧品牌 / 主窗标题旧品牌 /
         包内图标缺失）；打包入口组 7/7 转红（always-detach / never-detach /
         保留失效流 / stderr 不清 / serve --tray 判成 CLI / 异常不降级 /
         FreeConsole 返回值不检查）。全部恢复后转绿。
```

### 哨兵覆盖的关键风险（不是"覆盖率数字"，是"漏了就静默坏掉"的那几条）

1. override 根下自动迁移**必须**跳过——否则把用户真实 `~/.remote_desktop` 整目录搬走；
2. `cli.main()` 必须在任何配置读写**之前**迁移——晚一步被新建空配置堵死；
3. 新根已有 `config.json` → 整体放弃（新目录可能才是权威数据源）；
4. copy 兜底绝不覆盖已存在目标 + 完成标记短路；
5. 迁移/图标/控制台释放失败一律不上抛（外观与迁移不该炸启动）；
6. 释放控制台后标准流置 None → `_gui_launch_expected()` 必须仍为 True（第十二批病灶）；
7. `--version`/`serve` headless 绝不释放控制台；`serve --tray` 必须释放（开机黑框）；
8. 自启自定义服务名不越界清理别人的条目；
9. 包内图标资源真实存在且可解码（漏生成 = 发布产物没图标且无人发现）；
10. 用户可见字面量零旧品牌残留（AST 级扫描）。

### 安装包编译

初版编译成功（131.4s，99.0 MB，版本信息读回全部正确），但**向导语言为中英混杂**
——该项连同"打包形态"在用户真机走查后被推翻重做，全部修复与复验见 **§8 收口批**。

## 7. 遗留清单（明确记入，不谎称完工）

### 7.1 本次用户清单之外、原 REFACTOR_PLAN 阶段 10 仍未做的项

用户下发的清单是四组（更名/图标+spec/安装包/验证+文档），以下原计划项**不在
本次范围**，如实列出：

| # | 原任务 | 状态 | 说明 |
|---|---|---|---|
| 10.2 | Nuitka 配置（仅 CI 执行） | ❌ 未做 | 本机无 C 编译器（Q1 决策：本机 PyInstaller、Nuitka 只在 CI） |
| 10.4 | macOS `Info.plist` + `.dmg` | ❌ 未做 | 本机物理上无法产出与验证（R6） |
| 10.5 | Linux AppImage + `.deb` | ❌ 未做 | 同上 |
| 10.6 | `updater/github_releases.py` 更新通道 | ❌ 未做 | git 无 remote（Q5），需本地 HTTP 模拟才能验收 |
| 10.7 | CI/CD 三平台矩阵（GitHub Actions） | ❌ 未做 | 无 remote，无法真实触发；且按 Q5"不推送任何代码到外部"须单独授权 |
| 10.8 | 用户手册 + API 文档（pdoc/mkdocs）+ `MIGRATION.md` | 部分 | README 已扩写为面向用户的安装/使用/构建手册、CHANGELOG 已建；pdoc/mkdocs API 文档与独立 MIGRATION.md 未做（迁移逻辑本身已实现并在 README/CHANGELOG 写明） |

### 7.2 阶段 9 携带进来的遗留（本阶段未触碰）

- **(a) 帧 HMAC 无序号**：同会话原样重投已签名帧不可检测。**阶段 8 引入文件
  传输/指令序列等有状态操作前必须先补帧序号/滑动窗口**。本阶段未引入任何有状态
  操作，遗留面未扩大。
- **(b) 文件传输面渗透待阶段 8 补测**（该 skip 用例仍 skip，未伪称通过）。
- 阶段 8（文件传输 + 音频）整体延后未开，用户手动验证清单里"性能物理机复测"
  仍未做（本次冷启动已远超线，但画面流畅度需真机）。
- 硬编选项映射为空（`_hardware_options` 不发明参数），逐卡标定未做——冒烟日志
  显示 h264_nvenc/qsv/amf 在本机 OPEN_FAILED、libx264 AVAILABLE，符合预期。
- qasync 3.16 切换预案（PoC 方案乙代码现成）。
- `host/netinfo.py` 宽异常未收窄（阶段 9 遗留 5）。

### 7.3 本阶段新增的小额遗留

| # | 项 | 建议 |
|---|---|---|
| 1 | venv 里旧入口脚本 `.venv/Scripts/rdc.exe` 仍在（uv 重装不清理旧脚本文件），`importlib.metadata` 仍能枚举到 `rdc` 入口 | 纯开发环境残留，不影响产物；需要干净环境时重建 venv 即可 |
| 2 | 产物 358 MB 中 `cv2.pyd`+videoio ffmpeg 占 111 MB | 源码只用 absdiff/cvtColor/imencode/resize/connectedComponentsWithStats，v5.1 评估换 lighter 方案或剔除 videoio |
| 3 | 安装包未做代码签名（无 EV 证书），SmartScreen 会告警 | 发布前补 Authenticode 签名；EV 证书同时是 v5.1.0 UIAccess 的前置 |
| 4 | ~~Inno `[Languages]` 用内置 Default.isl（向导按钮为英文），仅自定义文案是中文~~ | ✅ **已消除**（收口批 §8：换官方 `ChineseSimplified.isl` + Tasks 全中文字面量，实测向导全中文） |
| 5 | `.venv` 环境里 `pyproject` 旧名 `rdc` 的 dist-info 残留 | 同 1，重建 venv 消除 |

---

## 8. 收口批：用户真机走查推翻三项（2026-11-15 ~ 2026-11-18）

用户安装 `WaltonDesk_setup.exe` 实测后回报：**一个致命断联 + 两处 UI 瑕疵**。
本批据此反转上一批的一个核心架构决策（console=True），并补一类此前只补一半的
漏包缺陷。三条全部修复、复验、重打包，交付物见 §8.4。

### 8.1 🔴 致命：输入模块漏包致控制断联（含全局排查）

**现象与取证**：主控端能看到画面，启用控制并发输入后被控端断联，日志
`No module named 'rdc.input.windows'` / `'rdc.input.legacy_pyautogui'` →
`PlatformUnsupportedError`。取证（`build/waltondesk/PYZ-00.toc`）：`src/rdc/input/`
8 个子模块只入包 3 个（`base`/`coalesce`/`linux_wayland`——恰好是被静态 import 的那几个），
`windows`/`macos`/`linux_x11`/`legacy_pyautogui`/`keys` 全部缺席。根因
`input/base.py:325,351` 与 `input/__init__.py:59` 的 `importlib.import_module`。

**上一批为什么没修干净（如实记录）**：c4e3c06 已意识到"延迟导入分析器收不到"，
但只补了第三方包 `pyautogui` 链，**漏了 rdc 自己的平台后端**——同一条教训只用了
一半，用户因此在真机撞断联。

**修复**：spec 显式登记用户点名的 5 个后端 + `collect_submodules("rdc")` 全量兜底
（代价 <1 MB）。

**全局排查要求 → 用机器核验替代人脑排查**：新增 `scripts/check_frozen_modules.py`，
以"源码 `rdc.*` 全部模块"对"PYZ 入包清单"做**差集比对**（`rdc.desktop_entry`/
`rdc.__main__` 两个入口豁免并写明理由）。首轮差集即抓出 **11 个漏网**——不止报障
的 5 个，另含 `encoding.jpeg`（JPEG 编码回退）、`encoding.bandwidth`、`input.keys`、
`host.power`、`ui.status_panel`。**若只按报障清单补 5 个，`encoding.jpeg` 仍会在
H.264 回退路径上炸**，这正是"想到哪个补哪个"的失效模式。
`secrets/factory.py`、`encoding/`、`host/` 经 grep 复核为模块顶层静态 import
（平台判断发生在函数体内、import 是硬编码），本就在包内——差集核验同时给出了
"已覆盖"的正面证据。

**依赖闭合**：`windows.py` import 面实测纯 stdlib（ctypes/sys + rdc 内部）；
pyautogui 及 pygetwindow/mouseinfo/pymsgbox/pyscreeze 五个依赖 `in_toc=True`。
入包 + 依赖闭合两面都验证，才算排除运行期 ImportError。

**核验结果**：`check_frozen_modules.py` → 源码 106 个模块**全部入包**，输出
`[OK] … 无动态导入漏网`（退出码 0，可作为构建后门禁）。

### 8.2 双击闪黑框 → console=True 架构反转（用户指令，含一处连带缺陷自纠）

**根因**：console=True 时 bootloader 先建控制台、我们代码再销毁，"先有再关"
物理上关不掉那一瞬闪烁。**修复**：`console=False`（PE 实测 `subsystem=2` GUI
子系统，双击进程**没有**控制台，零闪烁是物理保证而非时序技巧）。

**必须自纠的连带缺陷**（本轮核验当场发现并修掉）：windowed 下 `sys.stdout` 恒
为 None，若按直觉直接 `AttachConsole(父)`，会把 `WaltonDesk.exe > log.txt` 的
输出**劫持进控制台**——实测重定向文件 **0 字节**，首启密码/横幅静默丢失。
修复为四级决策（`desktop_entry.ensure_console`）：
① `sys.stdout` 非 None（源码/venv）→ 不碰任何 Win32 面；
② **`GetStdHandle` 句柄可用即复用**（文件/管道/继承控制台全保真，零弹框；
   `GetStdHandle`/`GetFileType` 显式声明 `restype=HANDLE`，防 64 位句柄被 ctypes
   缺省 c_int 截断——与 `input/windows.py` argtypes 审计同一条纪律）；
③ 挂不上才 `AttachConsole(父)`；④ 纯双击带参数才 `AllocConsole`。

**已知取舍（如实）**：windowed 是 GUI 子系统进程，从 cmd 跑 headless `serve`
时父 shell 不等待、Ctrl+C 发不到它；日常 `--tray`/自启形态不受影响，headless
调试推荐源码形态。

**启动期输出链路核查**：全树 grep `print(` 复核——CLI/UI 侧全部经
`console_stream()` 或属 CLI 子命令专用路径（不在 GUI 启动链），`console_safety`
本就是为"三流 None"设计（`_NullStream` + CPython print 对 stdout=None 天然静默），
故 windowed 下启动路径不存在裸 print 崩溃面。

**哨兵反转**（`tests/unit/test_desktop_entry.py` 重写，21 → 24 项）：新增
`test_inherited_handles_preferred_over_console`（句柄可用绝不弹控制台 = 重定向劫持
病灶）、`test_source_form_stdout_present_short_circuits`、
`test_all_paths_fail_degrade_to_false`；原"释放流"组替换为"补建序列"组。

### 8.3 安装包中英混杂 → 官方简体中文语言包

`[Languages]` 换 `compiler:Languages\ChineseSimplified.isl`（实测安装目录存在
该文件）；`[Tasks]` 描述改**中文字面量**（不再用 `{cm:CreateDesktopIcon}`，
防语言包缺键回退英文）。**编译日志实测**两次读取
`C:\Program Files\Inno Setup 7\Languages\ChineseSimplified.isl`，exit=0。
"全界面 100% 纯中文"以官方消息包为凭据成立——消息表编译进产物且压缩存储
（二进制内搜不到中文字符串，属预期），未实装安装向导截图核验。

### 8.4 收口批验收（真实输出）

```
pytest:  1703 passed / 2 skipped，退出码 0（+3 项 = 新架构哨兵净增）
ruff:    All checks passed!（src + tests + scripts）
mypy:    Success: no issues found in 106 source files
bandit:  Undefined 0 / Low 0 / Medium 0 / High 0（exit 0）
双红哨兵：打包入口组 7/7 注入病灶转红（GUI 形态补控制台=黑框复发 / CLI 不补=
         --version 不可见 / 源码形态不早退 / **句柄复用被忽略=重定向劫持** /
         reconnect 失败仍 True=半接半漏 / Alloc 不兜底 / 异常上抛不降级），
         恢复后 24 项全绿
产物:    dist/WaltonDesk/ 355.1 MB；PE subsystem=2（windowed 物理证据）；
         check_frozen_modules.py → [OK] 无动态导入漏网；
         --version 重定向 stdout 19 B / stderr 140 B 保真（劫持病灶 0 B 已消）；
         冷启动 0.429/0.418/0.421/0.413/0.411s，median 0.418s
安装包:  dist/WaltonDesk_setup.exe 98.4 MB，MZ 有效 + 版本信息读回全对
         （ProductName=WaltonDesk / ProductVersion=5.0.0 /
           FileDescription=WaltonDesk Setup / CompanyName=WaltonDesk Team），
         编译日志实测读取官方 ChineseSimplified.isl
```

**顺带修掉的第 7 项**（收口批核验发现，非用户报障）：`pydantic` 自带
`pydantic.mypy` 插件（仅 mypy 检查期加载，运行时不 import）把整个 `mypy` 包
（72 文件）拖进产物。excludes 断链后产物 358.3 → 355.1 MB，开发期校验不受影响。

**收口批新增遗留**：`dist\WaltonDesk_setup.exe` 编译期间若并发再起一次 `iscc`，
会因输出文件占用报 Error 32（实测踩过两次，第二次让产物一度残缺）。构建纪律：
**同一时刻只跑一次 iscc**，完成后再核验。

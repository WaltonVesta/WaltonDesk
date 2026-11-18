# WaltonDesk v5.0.0

跨平台远程桌面软件，主控端 / 被控端双模式。（原名 `rdc` / 远程桌面控制，
v5.0.0 起更名 WaltonDesk；Python 导入包名仍为 `rdc`。）

## 安装

### 普通用户

下载 `WaltonDesk_setup.exe` 双击运行：

- 默认安装到 `C:\Program Files\WaltonDesk`（无管理员权限时可改选"仅为当前用户"，
  装到 `%LocalAppData%\Programs\WaltonDesk`）；
- 自动创建桌面与开始菜单快捷方式，并在"应用和功能"里注册卸载入口；
- 安装最后一步提供两个勾选项：
  - **创建桌面快捷方式**；
  - **开机自动启动 WaltonDesk 被控端（静默，托盘常驻）** —— 默认**不勾选**。
    被控端是远程控制入口，未经你明确同意不会默认开机常驻。装完后也可以随时
    在托盘菜单里改（见下文）。

卸载：从"应用和功能"或开始菜单的"WaltonDesk 卸载"进入。卸载会结束被控端进程
并删除程序目录，**不会删除**你的配置、证书与口令目录（`%USERPROFILE%\.waltondesk`），
需要彻底清除请手动删除该目录。

### 数据目录与迁移

配置、日志、证书、密钥库索引统一存放在 `%USERPROFILE%\.waltondesk`
（macOS/Linux 为 `~/.waltondesk`）。从旧版（`~/.remote_desktop`）升级时，首次
启动会自动完成一次性迁移：同盘走原子改名，跨盘退回逐项复制且**不覆盖**新目录
已有文件、**不删除**旧目录（宁留空壳也不冒"删一半"的风险）。迁移失败只记录日志、
不阻断启动。

## 使用

### 被控端首次运行：初始密码从哪看

首次启动被控端会自动生成随机 8 位强口令，并**同时通过三条独立通道**展示，
任一通道失效（无控制台、无系统托盘）时其余通道仍然可用：

1. **控制台**：命令行启动（`waltondesk serve`）时，在分隔线块里显眼打印
   `首次启动，已生成随机初始密码：XXXXXXXX`。开机自启/静默形态也会先打印再静默。
2. **托盘气泡**：任务栏托盘图标弹出通知（约 1 帧延迟，规避 Windows 通知区
   注册时序），**点击气泡**即表示"不再提示"。
3. **非模态弹窗**：大字号明文可直接选中复制，带"不再提示"复选框。

拿到密码后建议立刻改成自己的：托盘菜单 → **修改密码…**，或命令行
`waltondesk set-password`（强度要求 ≥8 位且含字母与数字）。改密后不再弹初始提示。

> 口令明文只出现在上述展示通道里，**绝不写入日志、配置文件或异常文案**；
> 落盘的是 bcrypt 哈希，明文另存于系统密钥库（Windows DPAPI）仅供回显。

### 托盘菜单

被控端运行期间常驻托盘，图标右下角状态点表示状态（绿=运行 / 橙=暂停 / 灰=停止）。
菜单项：

| 项 | 说明 |
|---|---|
| 📡 状态 | 查看被控端状态 |
| 📋 复制 IPv4 / IPv6 | **每次展开菜单实时查询**（无缓存），点复制取当前值。Windows 会轮换 IPv6 隐私临时地址，展开即见新值；多网卡优先私网地址 |
| ⏸ 暂停服务 / ▶ 恢复服务 | 软暂停：冻结画面与输入、拒新连接，但已建立会话保持存活 |
| 🔄 重启服务 | 原地重启被控端服务 |
| 🚀 开机自启（静默被控端） | 勾选状态即注册表现状，与安装时的勾选项是同一个值，两边互认同步 |
| 🔑 修改密码… | 实时强度校验、两次一致后生效（需重启被控端） |
| 🚪 退出 | 结束被控端 |

### 开机自启说明

两种等价方式，操作的是同一个注册表值
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `WaltonDesk`：

- 安装时勾选"开机自动启动被控端"；
- 或在托盘菜单勾选"🚀 开机自启（静默被控端）"（取消勾选即注销）。

自启命令为 `WaltonDesk.exe serve --tray --silent`：**静默进被控端**，不弹启动菜单、
不弹控制台黑窗，托盘图标照常出现，因此开机后你仍能在托盘看到状态、复制 IP、
以及（若是首次运行）看到初始密码气泡。注册失败会回滚勾选状态并提示，不会静默
显示"已开启"。

改过安装位置后请重新勾选一次自启（旧路径的值会在下次注册/注销时被自动清理）。

### 命令行

```bash
waltondesk --version            # WaltonDesk v5.0.0
waltondesk                      # 图形界面（选被控端 / 主控端）
waltondesk serve                # 被控端（终端里跑，Ctrl+C 退出）
waltondesk serve --tray         # 被控端 + 系统托盘
waltondesk serve --tray --silent  # 静默形态（开机自启用的就是这条）
waltondesk control 192.168.1.20 # 主控端命令行冒烟（认证 + 收帧统计）
waltondesk set-password         # 设置被控端口令
```

源码形态等价入口：`python -m rdc ...`（导入包名未改）。

### 网络地址

被控端默认全接口监听为 **IPv4/IPv6 双栈**，主控端可直接填被控端的 IPv4 或
IPv6 地址（IPv6 建议带方括号，如 `[2409:8a55:xxxx::1]`）。`bind_host` 若收紧为
`127.0.0.1` 等单地址，则仅监听对应协议族。

## 从源码构建（Windows）

```bash
uv sync --all-groups                        # 装依赖（含 package 组）

# 1) 生成品牌图标（改过 icon.png 后需要重跑；双写根目录与包内 theme/）
uv run python scripts/gen_icon.py

# 2) 生成 Protobuf 代码（改过 proto/rdc.proto 后需要重跑）
uv run python scripts/gen_proto.py

# 3) PyInstaller 目录模式 → dist/WaltonDesk/WaltonDesk.exe
uv run pyinstaller waltondesk.spec --noconfirm

# 4) Inno Setup 安装包 → dist/WaltonDesk_setup.exe
iscc waltondesk_installer.iss
```

改过 `.iss` 想快速验证 Pascal 语法（不必等全量压缩）：
`uv run python scripts/check_iss_syntax.py`。

### 开发环境

```bash
uv sync                       # dev + test 组默认启用
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests -q        # 全量测试
uv run --group security bandit -r src/
```

## 协议兼容性

> ⚠️ **v5.0 不与 v4 协议兼容**（2026-09-22 决定）：v1–v4 从未稳定可用、无存量
> 用户，"兼容从未存在过的用户"判定为伪需求。v5 协议为单版本：Protobuf 载荷 +
> HKDF-SHA256 从 TLS 通道内交换的 nonce 派生会话密钥的 HMAC-SHA256 帧完整性，
> 握手（认证 + nonce 交换）到数据流统一走 15 字节分帧、协议自描述。
> v4 遗留的裸 4B 长度前缀握手与 JSON 载荷已移除；传输层为 asyncio + TLS 1.2+，
> v4 客户端无法连接 v5 服务端，反之亦然。

## 已知限制（v5.0.0）

- **Win 键（开始菜单键）无法完整远程使用**：Windows 在系统层接管 Win 键，
  主控端应用层收不到它的**释放**事件，被控端会保持"Win 按下"状态、后续按键
  全部变成 Win 组合键。v5.0.0 的应急兜底：按住 Win 超过 **2 秒**未收到释放，
  客户端自动向被控端补发一次释放（下一次按键时、或静置约 2 秒后生效），
  因此不会出现持续卡键，但**单键短按 Win 的语义（打开开始菜单）在 v5.0.0
  无法实现**——被控端只会收到按下，随后被自动释放。
  `Win+X` / `Win+Q` / `Win+W` / `Win+V` 这类完整组合由 Windows 系统直接消费，
  不会转发到被控端。
  完整支持（低级键盘钩子 `WH_KEYBOARD_LL` 拦截按下+释放，或 Windows 服务 +
  UIAccess）属 **v5.1.0 特权操作范围**。
- **安全桌面（UAC 确认弹窗）内容不可见、不可操作**：受 Windows 安全边界限制，
  需要服务化 + 代码签名 + UIAccess 声明（v5.1.0 范围）。v5.0.0 的表现是
  状态栏提示"远端处于安全桌面，画面暂停"，UAC 结束后自动恢复。
- **文件传输与系统音频尚未提供**：协议字段已就绪，实现与验收归阶段 8。
- **发布产物仅 Windows 本机验证**：macOS/Linux 打包在本机物理上无法执行，
  相关配置与 CI 定义属交付项，未在本版本产出安装包。

## 技术栈

- **UI**：PySide6（Qt6，深色主题，原生高 DPI）
- **网络**：asyncio + TLS 1.2+ 强制 + TOFU 证书指纹信任
- **协议**：15 字节分帧头（Version 字段启用）+ Protobuf（v5）载荷 + HMAC-SHA256 完整性
- **编码**：PyAV H.264（软编 libx264），JPEG 回退
- **输入**：平台原生后端（Windows SendInput；macOS/Linux 留扩展位）
- **安全**：bcrypt 口令 + 认证限流封禁 + 结构化审计 + 平台密钥库（DPAPI 等）

## 项目文档

| 文件 | 内容 |
|---|---|
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更与破坏性变更 |
| [`PROGRESS.md`](PROGRESS.md) | 重构进度总览 |
| [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md) | 重构计划与决策记录 |
| [`ASSESSMENT.md`](ASSESSMENT.md) | v4 现状评估 |
| `STAGE*_REPORT.md` | 各阶段交付与验收数据 |
| [`STAGE10_REPORT.md`](STAGE10_REPORT.md) | 阶段 10：更名、图标、打包、安装包 |

## 许可证

MIT（第三方依赖许可证见 `pyproject.toml` 注释）。

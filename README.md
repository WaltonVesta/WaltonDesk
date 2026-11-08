# 远程桌面控制 (rdc) v5.0.0

跨平台远程桌面控制软件，主控端 / 被控端双模式。

> ⚠️ **本文档为重构期占位版**。完整的用户手册、API 文档在阶段 10 交付（见 `REFACTOR_PLAN.md`）。

## 技术栈

- **UI**：PySide6（Qt6，深色主题，原生高 DPI）
- **网络**：asyncio + TLS 1.2+ 强制
- **协议**：15 字节分帧头（Version 字段启用）+ Protobuf（v5）载荷 + HMAC-SHA256 完整性
- **编码**：PyAV H.264（软编 libx264），JPEG 回退
- **输入**：平台原生后端（Windows SendInput / macOS CGEventPost / Linux XTest·libei）

## 版本状态

当前处于 **v4.0.3 → v5.0.0 重构阶段 9 ✅ 完成（安全加固 + 装配）**，真机走查已修复第十四批问题（托盘 IPv4/IPv6 实时展示、被控端暂停时主控端红字提示且连接保持、首启随机密码控制台/托盘气泡/弹窗三通道可见）并交付开机自启（Feature 17）与初始密码策略（Feature 18），下一步 **阶段 10（打包发布）**。重构进度见 [`PROGRESS.md`](PROGRESS.md)，计划见 [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md)，现状评估见 [`ASSESSMENT.md`](ASSESSMENT.md)，阶段 9 交付见 [`STAGE9_REPORT.md`](STAGE9_REPORT.md)。

**被控端首次运行**会自动生成随机 8 位强口令，通过**三条独立通道**展示：控制台醒目打印、托盘气泡、非模态弹窗（可一键复制，支持"不再提示"）。建议登录后立即在设置或托盘"修改密码"改为自己的口令。托盘与启动菜单提供"开机自启（静默被控端）"开关；自启形态下不弹启动菜单。`rdc serve --tray --silent` 为开机自启使用的静默命令。

**被控端暂停**为软暂停（保活冻结）：画面管线与输入被冻结、新连接被拒，但**已建立的会话保持存活**，主控端状态栏显示红字"被控端已暂停"而**不会误显断开**；恢复后画面自动续流。

**网络地址**：被控端默认全接口监听为 **IPv4/IPv6 双栈**（v5.0.0 走查第十三批起），主控端可直接填被控端的 IPv4 或 IPv6 地址（IPv6 建议带方括号，如 `[2409:8a55:xxxx::1]`）。`bind_host` 若收紧为 `127.0.0.1` 等单地址，则仅监听对应协议族。托盘菜单中的 IP 地址为**每次展开时实时查询**（无缓存），系统轮换 IPv6 临时地址后再展开即显示新值；多网卡时优先可用私网地址，tooltip 按 `IPv4:` / `IPv6:` 协议标注。

## 协议兼容性

> ⚠️ **v5.0 不与 v4 协议兼容**（2026-09-22 决定）：v1-v4 从未稳定可用、无存量用户，
> "兼容从未存在过的用户"判定为伪需求。v5 协议为单版本：Protobuf 载荷 +
> HKDF-SHA256 从 TLS 通道内交换的 nonce 派生会话密钥的 HMAC-SHA256 帧完整性，
> 握手（认证 + nonce 交换）到数据流统一走 15 字节分帧、协议自描述。
> v4 遗留的裸 4B 长度前缀握手与 JSON 载荷已移除；阶段 4 传输层（asyncio + TLS 1.2+）
> 已落地，v4 客户端无法连接 v5 服务端，反之亦然。

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
  UIAccess）属 **v5.1.0 特权操作范围**，与"进入安全桌面（UAC 确认界面）
  看/操作"同批规划，见 `STAGE9_REPORT.md` §6 v5.1.0 特权操作范围。
- **安全桌面（UAC 确认弹窗）内容不可见、不可操作**：受 Windows 安全边界限制，
  需要服务化 + 代码签名 + UIAccess 声明（v5.1.0 范围）。v5.0.0 的表现是
  状态栏提示"远端处于安全桌面，画面暂停"，UAC 结束后自动恢复。

## 开发环境

```bash
# 安装依赖（dev + test 组默认启用）
uv sync

# 全部依赖组（含 security/package/proto）
uv sync --all-groups

# 重新生成 Protobuf 代码（proto/rdc.proto 变更后执行）
uv run python scripts/gen_proto.py

# 代码检查
uv run ruff check src tests
uv run mypy src

# 测试
uv run pytest tests/unit -v --cov=src/rdc --cov-report=html
```

## 许可证

MIT（第三方依赖许可证见 `pyproject.toml` 注释）。

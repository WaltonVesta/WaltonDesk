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

当前处于 **v4.0.3 → v5.0.0 重构阶段 3**（协议层）。重构进度见 [`PROGRESS.md`](PROGRESS.md)，计划见 [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md)，现状评估见 [`ASSESSMENT.md`](ASSESSMENT.md)。

## 协议兼容性

> ⚠️ **v5.0 不与 v4 协议兼容**（2026-09-22 决定）：v1-v4 从未稳定可用、无存量用户，
> "兼容从未存在过的用户"判定为伪需求。v5 协议为单版本：Protobuf 载荷 +
> TLS exporter / HKDF 派生的 HMAC-SHA256 帧完整性，握手到数据流统一走 15 字节分帧。
> v4 遗留的裸 4B 长度前缀握手与 JSON 载荷已随协议层重写移除，
> 阶段 4 传输层（asyncio + TLS）落地后旧客户端无法连接 v5 服务端，反之亦然。

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

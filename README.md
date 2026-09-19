# 远程桌面控制 (rdc) v5.0.0

跨平台远程桌面控制软件，主控端 / 被控端双模式。

> ⚠️ **本文档为阶段 1 占位版**。完整的用户手册、API 文档、迁移指南在阶段 10 交付（见 `REFACTOR_PLAN.md`）。

## 技术栈

- **UI**：PySide6（Qt6，深色主题，原生高 DPI）
- **网络**：asyncio + TLS 1.2+ 强制
- **协议**：15 字节分帧头 + Protobuf（v5）/ JSON（v4 兼容）双载荷
- **编码**：PyAV H.264（软编 libx264），JPEG 回退
- **输入**：平台原生后端（Windows SendInput / macOS CGEventPost / Linux XTest·libei）

## 版本状态

当前处于 **v4.0.3 → v5.0.0 重构阶段 1**（项目脚手架）。重构进度见 [`PROGRESS.md`](PROGRESS.md)，计划见 [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md)，现状评估见 [`ASSESSMENT.md`](ASSESSMENT.md)。

## 开发环境

```bash
# 安装依赖（dev + test 组默认启用）
uv sync

# 全部依赖组（含 security/package/proto）
uv sync --all-groups

# 代码检查
uv run ruff check src tests
uv run mypy src

# 测试
uv run pytest tests/unit -v --cov=src/rdc --cov-report=html
```

## v4 向后兼容

v5.0 保留对 v4 客户端的向后兼容（协议层双版本共存）。兼容期计划：v5.0 保留 → v5.1 标记 deprecated → v6.0 删除。详见 `MIGRATION.md`（阶段 10 交付）。

## 许可证

MIT（第三方依赖许可证见 `pyproject.toml` 注释）。

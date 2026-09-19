"""阶段 1 机械搬迁脚本：v4 根目录布局 → src/rdc/ layout。

用途：把 v4.0.3 的 45 个 .py 文件按计划映射表复制到 ``src/rdc/``，
并**只改写 import 路径**，不改动任何运行时逻辑。

安全约束（必须遵守）：
1. **字节级读取 + 显式 UTF-8 写回**。禁止用 PowerShell ``Get-Content |
   Set-Content`` 管道 —— PS 5.1 默认按 GBK/UTF-16 处理，会毁掉全部中文注释。
   （已实测：项目 48 个文件全部是合法 UTF-8，PS 显示乱码仅为终端解码问题。）
2. import 改写用**显式有序映射表**（长前缀优先），不用通用正则，避免误伤
   字符串字面量或第三方库导入。
3. 本脚本**只新增**文件到 src/rdc/，不删除、不移动任何 v4 原文件。
   v4 原文件的处置（移入 legacy_v4/）由后续 git mv 单独执行。

用法::

    uv run python scripts/migrate_v4_layout.py            # 执行搬迁
    uv run python scripts/migrate_v4_layout.py --check     # 只报告将做什么
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# 项目根 = 本脚本上级目录
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "rdc"

# ----------------------------------------------------------------------------
# 文件映射表：v4 相对路径 → v5 相对 src/rdc/ 路径
# None 表示该文件在阶段 1 不搬迁（Tkinter GUI / 入口重写 / 死代码）
# ----------------------------------------------------------------------------
FILE_MAP: dict[str, str] = {
    # --- 根级模块 ---
    "exceptions.py": "exceptions.py",
    "constants.py": "constants.py",
    # 计划原定 logging/setup.py，但 "logging" 会遮蔽标准库 → 保持单模块名，
    # 阶段 2 改 structlog 时再重组为包。
    "logger.py": "logger.py",
    # 计划原定 config/settings.py；改为 _v4_config.py 以明确标记"待阶段 2 重写"
    "config.py": "config/_v4_config.py",
    # --- core/ ---
    "core/protocol.py": "protocol/framing.py",
    "core/heartbeat.py": "transport/heartbeat.py",
    "core/reconnect.py": "transport/reconnect.py",
    "core/rate_limiter.py": "client/ratelimit.py",
    # --- screen/ ---
    "screen/capturer.py": "capture/mss_source.py",
    "screen/diff.py": "capture/diff.py",
    "screen/framerate.py": "capture/framerate.py",
    "screen/encoder.py": "encoding/jpeg.py",
    # screen/renderer.py：Tkinter ImageTk 渲染，阶段 6 由 QGraphicsView 取代 → 不搬迁
    # --- network/ ---
    "network/control_channel.py": "transport/control_channel.py",
    "network/screen_channel.py": "transport/screen_channel.py",
    # network/kcp_channel.py：实测确认死代码（无任何 import）→ 不搬迁
    # --- service/ ---
    "service/daemon.py": "server/service.py",
    "service/pid_manager.py": "server/pid_manager.py",
    "service/autostart.py": "host/autostart.py",
    # --- input/ ---
    # 计划原定 mouse+keyboard 合并为 legacy_pyautogui.py；合并两文件不属于
    # "机械搬迁"，改为各自独立搬迁，阶段 7 引入 InputBackend 时再重组。
    "input/mouse.py": "input/mouse.py",
    "input/keyboard.py": "input/keyboard.py",
    "input/dpi.py": "input/dpi.py",
    "input/modifiers.py": "input/modifiers.py",
    # --- utils/ ---
    "utils/ip.py": "host/netinfo.py",
    "utils/system.py": "host/power.py",
    "utils/security.py": "security/fingerprint.py",
    # --- 不搬迁 ---
    # main.py：入口已在阶段 1 重写为 cli.py + __main__.py
    # gui/*.py ×10：Tkinter 代码无复用价值，移入 legacy_v4/ 作阶段 6 重写参考
}

# ----------------------------------------------------------------------------
# import 改写映射（有序，长前缀在前，避免 "core.protocol" 被 "core" 规则先匹配）
# 键为 v4 的模块路径前缀，值为 v5 的 rdc.* 路径
# ----------------------------------------------------------------------------
IMPORT_MAP: list[tuple[str, str]] = [
    ("core.protocol", "rdc.protocol.framing"),
    ("core.rate_limiter", "rdc.client.ratelimit"),
    ("core.heartbeat", "rdc.transport.heartbeat"),
    ("core.reconnect", "rdc.transport.reconnect"),
    ("screen.capturer", "rdc.capture.mss_source"),
    ("screen.framerate", "rdc.capture.framerate"),
    ("screen.encoder", "rdc.encoding.jpeg"),
    ("screen.diff", "rdc.capture.diff"),
    ("network.control_channel", "rdc.transport.control_channel"),
    ("network.screen_channel", "rdc.transport.screen_channel"),
    ("service.pid_manager", "rdc.server.pid_manager"),
    ("service.autostart", "rdc.host.autostart"),
    ("service.daemon", "rdc.server.service"),
    ("utils.security", "rdc.security.fingerprint"),
    ("utils.system", "rdc.host.power"),
    ("utils.ip", "rdc.host.netinfo"),
    ("input.modifiers", "rdc.input.modifiers"),
    ("input.keyboard", "rdc.input.keyboard"),
    ("input.mouse", "rdc.input.mouse"),
    ("input.dpi", "rdc.input.dpi"),
    # 根级模块：必须放在最后，且用词边界防止 "config" 匹配到 "configparser"
    ("constants", "rdc.constants"),
    ("exceptions", "rdc.exceptions"),
    ("logger", "rdc.logger"),
    ("config", "rdc.config._v4_config"),
]

# 需要新建的包目录（含 __init__.py）
PACKAGES: list[str] = [
    "config",
    "protocol",
    "transport",
    "capture",
    "encoding",
    "client",
    "server",
    "host",
    "input",
    "security",
]

# 各包 __init__.py 的 docstring（不做 eager re-export —— 见下方说明）
PACKAGE_DOCS: dict[str, str] = {
    "config": "配置管理。阶段 1 保留 v4 实现于 ``_v4_config``，阶段 2 用 Pydantic Settings 重写。",
    "protocol": "协议层：15 字节分帧 + v4(JSON)/v5(Protobuf) 双载荷编解码。",
    "transport": "传输层：控制通道 / 画面通道 / 心跳 / 重连。阶段 4 重写为 asyncio。",
    "capture": "屏幕采集：截图源 / 差分 / 帧率控制。阶段 5 向量化重写。",
    "encoding": "编码层：H.264（v5 全帧）与 JPEG（v4 兼容差分）双路径。",
    "client": "主控端会话逻辑与限流。",
    "server": "被控端服务。阶段 1 保留 v4 的 ``service``，阶段 3-4 拆分为 auth/broadcaster/banlist。",
    "host": "宿主系统集成：开机自启 / 网络信息 / 电源操作。（原名 platform，避免遮蔽标准库）",
    "input": "输入注入：鼠标 / 键盘 / DPI / 修饰键。阶段 7 引入 InputBackend 抽象。",
    "security": "安全组件：TOFU 证书指纹信任列表。阶段 9 扩展密码策略与审计。",
}


def build_import_rewriter() -> list[tuple[re.Pattern[str], str]]:
    """构建 import 改写用的正则规则列表。

    仅匹配行首（可含缩进）的 ``from <mod> import`` 与 ``import <mod>``，
    用词边界 ``\\b`` 防止前缀误伤（如 ``config`` 匹配 ``configparser``）。

    Returns:
        ``(compiled_pattern, replacement)`` 元组列表，顺序与 ``IMPORT_MAP`` 一致。
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    for old, new in IMPORT_MAP:
        esc = re.escape(old)
        # from <old>[.sub] import ...  →  from <new>[.sub] import ...
        pat_from = re.compile(rf"^(\s*)from\s+{esc}(\.|\s+import)", re.MULTILINE)
        rules.append((pat_from, rf"\1from {new}\2"))
        # import <old>  →  import <new>（v4 中未出现，保留以防万一）
        pat_imp = re.compile(rf"^(\s*)import\s+{esc}\s*$", re.MULTILINE)
        rules.append((pat_imp, rf"\1import {new}"))
    return rules


def rewrite_imports(text: str, rules: list[tuple[re.Pattern[str], str]]) -> tuple[str, int]:
    """按规则改写源码中的项目内 import。

    Args:
        text: UTF-8 解码后的源码文本。
        rules: :func:`build_import_rewriter` 产出的规则列表。

    Returns:
        ``(改写后的文本, 改写次数)``。
    """
    total = 0
    for pat, repl in rules:
        text, n = pat.subn(repl, text)
        total += n
    return text, total


def migrate(check_only: bool = False) -> int:
    """执行搬迁。

    Args:
        check_only: 为 True 时只报告将执行的操作，不写任何文件。

    Returns:
        进程退出码，0 表示成功。
    """
    rules = build_import_rewriter()
    missing: list[str] = []
    plan: list[tuple[Path, Path, int]] = []

    for v4_rel, v5_rel in FILE_MAP.items():
        src_path = ROOT / v4_rel
        if not src_path.is_file():
            missing.append(v4_rel)
            continue
        # 字节级读取 → 显式 UTF-8 解码，确保中文注释不被破坏
        raw = src_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"❌ {v4_rel} 不是合法 UTF-8，中止：{exc}", file=sys.stderr)
            return 1
        new_text, n_rewrites = rewrite_imports(text, rules)
        plan.append((src_path, SRC / v5_rel, n_rewrites))

    if missing:
        print("❌ 以下 v4 源文件缺失，映射表有误：", file=sys.stderr)
        for m in missing:
            print(f"   - {m}", file=sys.stderr)
        return 1

    # --- 报告 ---
    print(f"将搬迁 {len(plan)} 个文件到 {SRC.relative_to(ROOT)}/")
    for src_path, dst_path, n in plan:
        print(
            f"  {src_path.relative_to(ROOT)!s:<32} → "
            f"{dst_path.relative_to(ROOT)!s:<42} (import 改写 {n} 处)"
        )
    print(f"将新建 {len(PACKAGES)} 个包目录（__init__.py 不做 eager re-export）")

    if check_only:
        print("\n[--check] 未写入任何文件。")
        return 0

    # --- 建包目录 ---
    for pkg in PACKAGES:
        d = SRC / pkg
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            doc = PACKAGE_DOCS.get(pkg, f"{pkg} 子包。")
            init.write_text(
                f'"""{doc}\n\n'
                f"注意：本 __init__ 刻意**不做** eager re-export。\n"
                f"v4 的包 __init__ 全部 eager import 子模块，形成了\n"
                f"service/__init__ → daemon → gui.tray → tkinter 的加载链，\n"
                f"并迫使 18 处 ``from logger import ...`` 写进函数体规避循环导入。\n"
                f"v5 起一律用完整模块路径导入（如 ``from rdc.protocol.framing import Packet``）。\n"
                f'"""\n',
                encoding="utf-8",
            )

    # --- 复制 + 改写 ---
    copied = 0
    for src_path, dst_path, _n in plan:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        raw = src_path.read_bytes()
        text = raw.decode("utf-8")
        new_text, _ = rewrite_imports(text, rules)
        dst_path.write_text(new_text, encoding="utf-8", newline="\n")
        copied += 1

    print(f"\n✅ 已写入 {copied} 个文件，{len(PACKAGES)} 个 __init__.py")
    return 0


def main() -> int:
    """脚本入口。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只报告将做什么，不写文件")
    args = parser.parse_args()
    return migrate(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())

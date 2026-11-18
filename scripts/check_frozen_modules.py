"""动态导入漏包排查：源码全部 rdc.* 子模块 vs PyInstaller 实际入包清单。

为什么需要它：``importlib.import_module`` 与函数内延迟 import 对 PyInstaller
静态分析不可见，漏掉的模块**源码里跑得好好的，exe 里运行期才炸**（真机实例：
被控端收到输入指令 → ``No module named 'rdc.input.windows'`` → 控制断联）。
靠人脑"想到哪个补哪个"必然再漏，所以用差集比对做机器核验。

用法::

    uv run python scripts/check_frozen_modules.py [--toc build/waltondesk/PYZ-00.toc]

退出码 0 = 无缺失；非 0 = 存在未入包模块（列出清单）。

平台不适用模块（macOS/Linux 后端在 Windows 包里）不算缺失——它们必须**显式**
出现在 ``expected_modules`` 里由 spec 强制收集（否则用户改天在 macOS 上跑同一个
spec 又会漏），因此本脚本把它们单列一节报告，并检查 spec 是否已登记。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = ROOT / "src" / "rdc"
SPEC = ROOT / "waltondesk.spec"


def source_modules() -> set[str]:
    """src/rdc 下全部模块的点分名（跳过 protoc 生成物 pb/）。"""
    names: set[str] = set()
    for f in PKG_ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT / "src")
        parts = list(rel.with_suffix("").parts)
        if "pb" in parts:  # protoc 生成物，由 datas 带
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
            if not parts:
                continue
        names.add(".".join(parts))
    return names


def modules_in_toc(toc_path: Path) -> set[str]:
    """从 PYZ-00.toc 抽取模块名。

    TOC 是 Python 字面量元组列表（``('name', 'path', 'TYPE')``），用 ast 安全
    解析而非 eval——构建产物不该被 exec。
    """
    text = toc_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        # 部分版本 TOC 带前置注释/包裹，退化为正则式抽取首元素
        import re

        return set(re.findall(r"^\s*\('([\w.]+)',", text, re.MULTILINE))
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Tuple) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def spec_registered() -> set[str]:
    """spec 文本里出现的 rdc.* 模块名（用于核验平台模块已登记）。"""
    if not SPEC.is_file():
        return set()
    import re

    return set(re.findall(r"rdc(?:\.[A-Za-z_][\w]*)+", SPEC.read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toc", default="build/waltondesk/PYZ-00.toc")
    args = parser.parse_args()

    toc = ROOT / args.toc
    if not toc.is_file():
        print(f"[FAIL] 找不到入包清单 {toc}（请先跑一次 pyinstaller）", file=sys.stderr)
        return 2

    src = source_modules()
    in_pkg = modules_in_toc(toc)
    registered = spec_registered()

    # 已知"不在 PYZ-00.toc 里但属正常"的两个，附理由，不算缺失：
    #   - rdc.desktop_entry：Analysis 的入口脚本，编译进 EXE 的 script 段而非 PYZ；
    #   - rdc.__main__：仅 ``python -m rdc`` 用，exe 走 desktop_entry 不经过它。
    known_ok = {"rdc.desktop_entry", "rdc.__main__"}
    missing = sorted(src - in_pkg - known_ok)

    print(f"源码 rdc.* 模块总数：{len(src)}")
    print(f"入包模块数（PYZ 清单）：{len(in_pkg)}")
    print()
    if not missing:
        print("[OK] 源码模块全部入包（入口脚本与 __main__ 属正常豁免），无动态导入漏网")
        return 0

    print(f"[MISS] 未入包模块 {len(missing)} 个：")
    for m in missing:
        tag = "已在 spec 登记（平台后端，构建时被排除属正常）" if m in registered else "⚠ 未在 spec 登记 → 运行期可能 ImportError"
        print(f"  - {m}  —— {tag}")
    unregistered = [m for m in missing if m not in registered]
    print()
    if unregistered:
        print(f"[FAIL] 其中 {len(unregistered)} 个既未入包也未在 spec 登记：")
        for m in unregistered:
            print(f"  - {m}")
        return 1
    print("[OK] 未入包模块均已在 spec 显式登记（平台后端由 spec 强制收集）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

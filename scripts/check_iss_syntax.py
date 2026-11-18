"""安装包脚本秒级语法校验：抽出 [Code] 段拼成极简 iss 编译，避免每次等全量压缩。

用法::

    uv run python scripts/check_iss_syntax.py

主脚本 [Files] 打包 358 MB（lzma2/ultra64 需数十分钟），而 Pascal 语法错误在
压缩**之前**就会报。本脚本把 [Code] 段单独喂给 iscc，几秒返回，迭代成本从
"几十分钟"降到"几秒"。它只校验 Pascal 段语法，段（[Files]/[Registry] 等）的
参数合法性仍需正式编译。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "waltondesk_installer.iss"

# [Code] 段里引用了这两个 ISPP 宏，探针脚本必须同样 #define，否则预处理器报
# "Unknown identifier"，把语法校验变成误报。
MINIMAL_SETUP = """#define MyAppName "WaltonDesk"
#define MyRegRunKey "Software\\Microsoft\\Windows\\CurrentVersion\\Run"

[Setup]
AppName={#MyAppName}
AppVersion=0
DefaultDirName={autopf}\\WaltonDesk
DisableWelcomePage=no
"""


def extract_code(text: str) -> str:
    """取 [Code] 段内容（到文件末尾）。"""
    idx = text.find("\n[Code]")
    if idx < 0:
        raise SystemExit("[FAIL] 脚本里没有 [Code] 段")
    return text[idx + len("\n[Code]") :]


def find_iscc() -> str:
    """定位 ISCC 可执行文件（PATH 优先，回退默认安装位置）。"""
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    for cand in (
        Path(r"C:\Program Files\Inno Setup 7\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    ):
        if cand.is_file():
            return str(cand)
    raise SystemExit("[FAIL] 找不到 ISCC.exe，请确认 Inno Setup 已安装且在 PATH")


def main() -> int:
    if not MAIN.is_file():
        print(f"[FAIL] 找不到 {MAIN}", file=sys.stderr)
        return 1
    code = extract_code(MAIN.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="isscheck-") as td:
        probe = Path(td) / "syntax_check.iss"
        probe.write_text(MINIMAL_SETUP + "\n[Code]\n" + code, encoding="utf-8")
        proc = subprocess.run(  # nosec B603 B607 -- 固定 argv，不经 shell
            [find_iscc(), str(probe)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = [ln for ln in out.splitlines() if ln.strip()][-8:]
        for line in tail:
            print(line)
        if proc.returncode == 0:
            print("[OK] [Code] 段 Pascal 语法通过")
        else:
            print("[FAIL] [Code] 段语法未通过")
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

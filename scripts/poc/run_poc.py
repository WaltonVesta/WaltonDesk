"""阶段 6 桥接方案 PoC 跑分器：对比 甲(qasync) vs 乙(loop-in-thread+Signal)。

用法：uv run python scripts/poc/run_poc.py
输出：每方案每模式的 TAG 度量行 + 末尾汇总判定表。

live 模式（Ctrl+C 干净退出）：Windows 下无共享控制台可发 CTRL_C_EVENT，
用 CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT 等价触发 SIGBREAK；
CPython 默认把 SIGBREAK 转成 KeyboardInterrupt，与被测异常路径一致。
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
POCS = {"A-qasync": "poc_qasync.py", "B-threadloop": "poc_threadloop.py"}
MODES_FAST = ("timer", "mixed", "exc")
TIMEOUT = 90


def run_mode(script: str, mode: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HERE / script), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
    )
    tags = {}
    for line in proc.stdout.splitlines():
        if " " in line:
            head, _, rest = line.partition(" ")
            if head.isupper():
                with contextlib.suppress(json.JSONDecodeError):
                    tags[head] = json.loads(rest)
    if proc.returncode != 0:
        tags["_rc"] = proc.returncode
        tags["_stderr_tail"] = proc.stderr[-800:]
    return tags


def run_live(script: str) -> dict:
    proc = subprocess.Popen(
        [sys.executable, str(HERE / script), "live"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if "READY" not in line:
        err = proc.stderr.read()[-600:] if proc.stderr else ""
        proc.kill()
        return {"ok": False, "why": "no READY", "stderr_tail": err}
    time.sleep(1.5)
    try:
        proc.send_signal(0x01)  # CTRL_BREAK_EVENT
    except Exception as e:  # noqa: BLE001
        proc.kill()
        return {"ok": False, "why": f"signal fail: {e}"}
    start = time.monotonic()
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": False, "why": "hang >5s after signal"}
    dur = time.monotonic() - start
    clean = False
    threads_left = None
    for out_line in proc.stdout.read().splitlines():
        if out_line.startswith("CLEAN_EXIT"):
            clean = True
            with contextlib.suppress(Exception):  # 解析失败不致命：clean 已判定
                threads_left = json.loads(out_line.partition(" ")[2]).get("threads_left")
    return {
        "ok": clean and rc in (0, 1) and dur <= 3.0,
        "exit_secs": round(dur, 2),
        "rc": rc,
        "threads_left": threads_left,
        "clean_exit_marker": clean,
    }


def main() -> int:
    report: dict[str, dict] = {}
    for name, script in POCS.items():
        one: dict[str, dict] = {}
        for mode in MODES_FAST:
            try:
                one[mode] = run_mode(script, mode)
            except subprocess.TimeoutExpired:
                one[mode] = {"_error": "TIMEOUT"}
        try:
            one["live"] = run_live(script)
        except Exception as e:  # noqa: BLE001
            one["live"] = {"_error": repr(e)}
        report[name] = one
        print(f"\n================ {name} ================")
        for mode, data in one.items():
            print(f"[{mode}] {json.dumps(data, ensure_ascii=False)}")
    out = HERE / "poc_results.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""批 5 loopback 端到端的被控端 runner（独立子进程主脚本，非 pytest 采集对象）。

装配与 ``rdc serve`` headless 路径**同一 HostService 核心**（service.py start →
TransportServer 双端口 TLS + pipeline 推流 + 控制会话分派），仅把两个真机副作用
换成替身——本脚本运行在用户工作机上，绝不允许移动真光标或截真屏：

- 采集源：``ArrayScreenSource``（两帧交替 → 差分恒"有变化" → pipeline 持续编码发布）；
- 输入后端：``RecordingBackend``（只记录调用序列，经 stdout 统计行暴露给父进程）。

与父进程的通信协议（stdout，一行一 JSON；``PYTHONIOENCODING=utf-8`` + 默认
ensure_ascii，纯 ASCII 行，无编码歧义）::

    {"event": "ready", "ok": true}
    {"event": "stats", "published": N, "encoded": N, "inputs": N,
     "encoder": "H264", "running": true, "calls": [最近若干条注入记录]}

关停：父进程关闭本进程 stdin → ``readline`` 返回 → 干净 shutdown → 退出码 0。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="rdc e2e host runner")
    parser.add_argument("--config", required=True, help="config.json 路径（测试构造）")
    parser.add_argument(
        "--root", required=True, help="应用数据根重定向目录（证书/日志/封禁落盘处）"
    )
    parser.add_argument("--stats-interval", type=float, default=0.4)
    args = parser.parse_args()

    # ⚠️ 顺序红线：先重定向数据根，再导入任何在 import 期固化路径的模块
    # （security.fingerprint 的 KNOWN_FINGERPRINTS_PATH 是模块级常量）。
    from rdc.config.paths import ensure_dirs, set_override_root

    set_override_root(Path(args.root))
    ensure_dirs()

    # 控制台 handler 走 stdout——先把 structlog 降到 WARNING 并把文件写进
    # 重定向根，避免 INFO 日志与 stdout 上的 JSON 行协议混流。
    from rdc.logging import configure_logging

    configure_logging("WARNING", log_file=Path(args.root) / "logs" / "rdc.log")

    import numpy as np

    from rdc.capture.source import ArrayScreenSource
    from rdc.config.settings import load_settings
    from rdc.server.service import HostService

    class RecordingBackend:
        """InputBackend 契约替身：记录调用序列，不做任何真实注入。"""

        def __init__(self) -> None:
            self.calls: list[list[Any]] = []

        def move_mouse(self, x: int, y: int) -> None:
            self.calls.append(["move", x, y])

        def click(self, x: int, y: int, button: Any, press: bool) -> None:
            self.calls.append(["click", x, y, str(button), press])

        def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
            self.calls.append(["scroll", x, y, dx, dy])

        def key_press(self, key: str) -> None:
            self.calls.append(["press", key])

        def key_release(self, key: str) -> None:
            self.calls.append(["release", key])

        def type_text(self, text: str) -> None:
            self.calls.append(["text", text])

        def bounds(self) -> Any:
            raise AssertionError("runner 不触 bounds")

        def reset(self) -> None:
            self.calls.append(["reset"])

        def supported(self) -> bool:
            return True

    frame_a = np.zeros((64, 64, 4), dtype=np.uint8)
    frame_b = np.full((64, 64, 4), 90, dtype=np.uint8)

    settings = load_settings(Path(args.config))
    backend = RecordingBackend()
    service = HostService(
        settings=settings,
        source=ArrayScreenSource([frame_a, frame_b], loop=True),
        input_backend=backend,
        show_tray=False,
    )

    async def _stats_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.stats_interval)
                return
            except TimeoutError:
                pass
            stats = service.get_stats()
            pipeline = getattr(service, "_pipeline", None)
            encoder = pipeline.stats().encoder if pipeline is not None else ""
            _emit(
                {
                    "event": "stats",
                    "published": stats["published_frames"],
                    "encoded": stats["frames_encoded"],
                    "inputs": len(backend.calls),
                    "encoder": encoder,
                    "control_sessions": stats["active_connections"],
                    "subscribers": stats["subscriber_count"],
                    "running": service.is_running,
                    "calls": backend.calls[-16:],
                }
            )

    async def _amain() -> int:
        ok = await service.start()
        _emit({"event": "ready", "ok": ok})
        if not ok:
            await service.shutdown()
            return 1
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _wake(_task: object) -> None:
            stop.set()

        stdin_task = loop.create_task(asyncio.to_thread(sys.stdin.readline), name="stdin")
        stdin_task.add_done_callback(_wake)
        stats_task = loop.create_task(_stats_loop(stop), name="stats")
        try:
            await stop.wait()
        finally:
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task
            await service.shutdown()
        return 0

    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

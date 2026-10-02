"""批 3 真机验证脚本（临时用，不入交付）：rdc serve 全链路冒烟。

流程：临时配置（随机端口 + 真实口令哈希）→ 子进程 ``rdc serve`` → 客户端
真实 TLS 连接（正确口令）→ 控制通道认证 + 画面通道加入 → 收取真实采集的
H.264 首帧 → 关停。不发送输入事件（真机 SendInput 会动用户鼠标，注入路径
已由集成测试覆盖）；不使用错误口令（避免在真实 HOME 写 banlist 副作用）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

from rdc.protocol.messages import ScreenFrame
from rdc.secrets.password import hash_password
from rdc.transport.control_channel import ControlChannel
from rdc.transport.screen_channel import ScreenChannel

PW = "stage9-verify-pw"
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp") / "stage9_serve"


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _confirm(_fp: str, _host: str) -> bool:
    return True


async def _try_connect(cfg: dict, deadline: float) -> ControlChannel:
    last: Exception | None = None
    while time.monotonic() < deadline:
        ch = ControlChannel(
            cfg["bind_host"], cfg["control_port"],
            password_provider=lambda: PW, fingerprint_confirm=_confirm,
        )
        try:
            await asyncio.wait_for(ch.connect(), timeout=5)
            return ch
        except (TimeoutError, OSError) as exc:  # 服务未就绪：退避重试
            last = exc
            with contextlib.suppress(Exception):
                await ch.close()
            await asyncio.sleep(0.5)
    raise RuntimeError(f"等待服务就绪超时: {last!r}")


async def _run_client(cfg: dict, deadline: float) -> dict:
    ch = await _try_connect(cfg, deadline)
    print(f"[client] 控制通道认证 OK: token={len(ch.session_token or '')}B cid={len(ch.connection_id or b'')}B hmac={ch.stream.hmac_enabled}")
    sc = ScreenChannel(
        cfg["bind_host"], cfg["port"],
        session_token=ch.session_token or "",
        connection_id=ch.connection_id or b"",
        fingerprint_confirm=_confirm,
    )
    await asyncio.wait_for(sc.connect(), timeout=10)
    print("[client] 画面通道加入 OK，等待真实首帧（mss 采集 + x264 编码）...")
    msg = await asyncio.wait_for(sc.next_message(), timeout=30)
    assert isinstance(msg, ScreenFrame), f"首帧类型异常: {type(msg).__name__}"
    print(
        f"[client] ScreenFrame OK: seq={msg.seq} {msg.width}x{msg.height} "
        f"codec={msg.codec.name} keyframe={msg.keyframe} data={len(msg.data)}B"
    )
    await sc.close()
    await ch.close()
    return {"width": msg.width, "height": msg.height, "codec": msg.codec.name, "bytes": len(msg.data)}


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg = {
        "version": 6,
        "bind_host": "127.0.0.1",
        "port": _free_port(),
        "control_port": _free_port(),
        "enable_auth": True,
        "password_hash": hash_password(PW),
        "max_connections": 10,
    }
    config_file = ROOT / "config.json"
    config_file.write_text(json.dumps(cfg), encoding="utf-8")
    print(f"[setup] 临时配置: {config_file} ports={cfg['control_port']}/{cfg['port']}")

    proc = subprocess.Popen(
        [sys.executable, "-m", "rdc", "serve", "--config", str(config_file)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        ready = time.monotonic() + 40
        result = asyncio.run(_run_client(cfg, ready))
        print(f"[verify] 真机起服务验证 PASS: {result}")
        return 0
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0] or ""
            for line in out.splitlines()[:12]:
                print(f"[serve] {line}")
        except subprocess.TimeoutExpired:
            proc.kill()
            print("[serve] 终止超时已 kill")


if __name__ == "__main__":
    raise SystemExit(main())

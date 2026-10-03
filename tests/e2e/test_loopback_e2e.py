"""批 5 判据①：同机双进程 loopback 端到端（认证 + H.264 帧 + 输入受理）。

架构（STAGE9_PLAN 用户确认口径）：

- **被控端 = 真实子进程**：``tests/e2e/_host_runner.py`` 走与 ``rdc serve``
  相同的 HostService 核心装配（TLS 双端口 + pipeline 推流 + 控制会话分派），
  数据根重定向到 pytest tmp 目录，绝不触碰真实 ``~/.remote_desktop``；
- **主控端 = 本进程 ClientController**（与 CLI ``rdc control`` / GUI 同一实现），
  ``image_factory`` 用冒烟替身（零 Qt，红线一致）。

三项自动判据：

1. **认证通过**：bcrypt 口令校验 + nonce/HMAC 握手成功 → ``state=connected``，
   且被控端统计里出现控制会话（``on_auth_success`` 已触发）；
2. **收到 H.264 帧**：画面通道订阅建立 → pipeline 经真实编码器（hw_probe
   选型，本机 libx264）发布 → 主控端 ``frame_arrived`` ≥2 帧、解码像素
   64x64、被控端 ``published>0``；
3. **输入被受理**：controller ``send_mouse/send_key/send_text/flush_now``
   → 事件经控制通道上行 → 被控端输入替身记录到对应注入序列（不动真机光标）。

端口约定：画面端口 = 控制端口 + ``screen_port_offset``（默认 1）；测试成对
取相邻空闲端口，与被控端 ``port``/``control_port`` 配置及 controller 推导一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rdc.client.controller import ClientController
from rdc.config.settings import Settings, save_settings
from rdc.secrets.password import hash_password
from rdc.security import fingerprint as fp_mod

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

PW = "e2e-strong-1"
RUNNER = Path(__file__).with_name("_host_runner.py")

READY_TIMEOUT_S = 60.0  # 含 RSA-2048 自签证书生成 + 首帧编码
FRAME_TIMEOUT_S = 30.0
INPUT_TIMEOUT_S = 20.0


def _free_port_pair() -> tuple[int, int]:
    """一对相邻空闲端口 (control, control+1)：画面端口 = 控制端口 + offset(1)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        first = int(probe.getsockname()[1])
    if first >= 65535:  # pragma: no cover - OS 从低位分配，理论不可达
        return _free_port_pair()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe2:
        try:
            probe2.bind(("127.0.0.1", first + 1))
        except OSError:
            return _free_port_pair()
    return first, first + 1


class _Host:
    """被控端子进程 + stdout JSON 行协议读取器。"""

    def __init__(self, proc: subprocess.Popen[str], *, control_port: int) -> None:
        self.proc = proc
        self.control_port = control_port
        self.last: dict[str, Any] = {}
        self.noise: list[str] = []  # 非 JSON 行（启动期 console 日志等），超时诊断用

    async def _next_event(self, timeout: float) -> dict[str, Any]:
        """读下一个 JSON 事件行；非 JSON 噪声行跳过并记录。"""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        assert self.proc.stdout is not None
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise AssertionError(f"读取超时（{timeout}s）；噪声：{self.noise[-5:]}")
            line = await asyncio.wait_for(
                loop.run_in_executor(None, self.proc.stdout.readline),
                timeout=remaining,
            )
            if not line:
                raise AssertionError(
                    f"被控端子进程意外退出（returncode={self.proc.returncode}）；"
                    f"噪声：{self.noise[-5:]}"
                )
            try:
                data: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                self.noise.append(line.rstrip())
                continue
            return data

    async def wait_ready(self) -> None:
        data = await self._next_event(READY_TIMEOUT_S)
        assert data.get("event") == "ready", f"首行不是 ready: {data}"
        assert data.get("ok") is True, f"被控端 start() 失败: {data}"

    async def wait_for(self, predicate: Any, timeout: float) -> dict[str, Any]:
        """持续读 stats 行直到 predicate(row) 为真，返回该行。"""
        end = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"等待被控端条件超时（最后状态：{self.last}）")
            data = await self._next_event(remaining)
            if data.get("event") != "stats":
                continue
            self.last = data
            if predicate(data):
                return data


@pytest.fixture
def host(tmp_app_dir: Path) -> Iterator[_Host]:
    """启动被控端子进程（数据根隔离在 tmp），结束回收并断言干净退出。"""
    control, screen = _free_port_pair()
    settings = Settings(
        bind_host="127.0.0.1",
        control_port=control,
        port=screen,
        enable_auth=True,
        password_hash=hash_password(PW),
    )
    config_path = tmp_app_dir / "config.json"
    save_settings(settings, config_path)

    src_root = Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(src_root), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(RUNNER),
            "--config",
            str(config_path),
            "--root",
            str(tmp_app_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(src_root.parent),
    )
    h = _Host(proc, control_port=control)
    try:
        yield h
    finally:
        if proc.poll() is None:
            assert proc.stdin is not None
            proc.stdin.close()  # runner 的 stdin 哨兵 → 干净 shutdown
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                proc.kill()
                proc.wait(timeout=5)
        err = proc.stderr.read() if proc.stderr is not None else ""
        rc = proc.returncode
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        assert rc == 0, f"被控端子进程退出码 {rc}；stderr 尾部：\n{err[-3000:]}"


@pytest.fixture(autouse=True)
def _isolate_fingerprint(tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_app_dir / "known_fp.json")
    monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)


class _FrameSink:
    """主控端事件收集器（ClientEvents 结构体冒烟实现）。"""

    def __init__(self) -> None:
        self.frames: list[tuple[Any, int, int]] = []
        self.states: list[str] = []

    def state_changed(self, state: str) -> None:
        self.states.append(state)

    def frame_arrived(self, image: Any, *, left: int = 0, top: int = 0) -> None:
        self.frames.append((image, left, top))

    def metrics_updated(self, **kw: object) -> None:
        pass

    def clipboard_from_remote(self, text: str) -> None:
        pass


def _smoke_image_factory(width: int, height: int, arr: Any) -> tuple[int, int, Any]:
    return width, height, arr


def _confirm(_fp: str, _host: str) -> bool:
    return True  # TOFU 冒烟口径（指纹确认真实链路已有单测/TLS 测试锁定）


async def test_loopback_e2e_auth_h264_input(host: _Host) -> None:
    """三项判据：认证通过 → 收到 H.264 帧 ≥2 → 输入事件被被控端受理。"""
    await host.wait_ready()
    settings = Settings(bind_host="127.0.0.1", control_port=host.control_port)

    sink = _FrameSink()
    ctl = ClientController(settings=settings, image_factory=_smoke_image_factory)
    ctl.set_events(sink)
    try:
        # —— 判据 1：认证通过（bcrypt + nonce/HMAC 握手）——
        await ctl.connect(
            "127.0.0.1",
            host.control_port,
            get_password=lambda _h: PW,
            confirm_fingerprint=_confirm,
        )
        assert ctl.state == "connected"
        assert "connected" in sink.states
        await host.wait_for(lambda s: s["control_sessions"] >= 1, FRAME_TIMEOUT_S)

        # —— 判据 2：收到 H.264 帧（真实编码 + 真实解码链路）——
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FRAME_TIMEOUT_S
        while len(sink.frames) < 2:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"{FRAME_TIMEOUT_S}s 内未收到 2 帧（已收 {len(sink.frames)}）")
            await asyncio.sleep(0.05)
        stats = await host.wait_for(lambda s: s["published"] > 0, 10.0)
        assert stats["encoder"], "pipeline 未报告编码器名"
        width, height, arr = sink.frames[0][0]
        assert (width, height) == (64, 64)
        assert tuple(arr.shape) == (64, 64, 3)  # rgb24 ndarray：真实解码产物
        assert stats["subscribers"] >= 1

        # —— 判据 3：输入事件被受理（被控端替身后端记录，不动真机）——
        ctl.set_control_enabled(True)
        ctl.send_mouse("move", 33, 44)
        ctl.send_mouse("press:left", 33, 44)
        ctl.send_mouse("release:left", 33, 44)
        ctl.send_key(0x01000004, True)  # Qt Key_Return → 规范名 "enter"
        ctl.send_text("hi")
        ctl.flush_now()  # 立即出批（不等画面节拍）
        stats = await host.wait_for(lambda s: s["inputs"] >= 5, INPUT_TIMEOUT_S)
        calls = [tuple(c) for c in stats["calls"]]
        assert ("move", 33, 44) in calls
        clicks = [c for c in calls if c[0] == "click"]
        assert len(clicks) >= 2  # press + release 都到
        assert all(c[1] == 33 and c[2] == 44 for c in clicks)
        assert ("press", "enter") in calls
        assert ("text", "hi") in calls
    finally:
        await ctl.disconnect()

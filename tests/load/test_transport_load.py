"""transport 并发压测（阶段 4 验收）：100 并发无 fd/内存泄漏。

流程：warm-up 一轮 → 采基线（句柄数 + RSS）→ 100 个客户端各建控制 + 画面
双通道（真实 TLS 握手 + 认证 + nonce + HMAC 强制）→ 画面广播若干帧 →
全部关闭 + broadcaster.shutdown 兜底 → gc → 复测。断言句柄数与 RSS 回落
到基线附近，且无 ``screen-send-*`` 任务残留。

Windows 下 socket 无 POSIX fd 语义，用 psutil ``num_handles()`` 作泄漏代理
（每连接泄漏表现为句柄不回落）。RSS 因分配器缓存不主动归还 OS，设宽松
阈值只防失控增长；**主判据是句柄回落 + 任务零残留**。

标记 ``load`` + ``integration``。
"""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path

import psutil
import pytest

from rdc.protocol.messages import Codec, ScreenFrame
from rdc.security import fingerprint as fp_mod
from rdc.transport.broadcaster import ScreenBroadcaster, SubscriberInfo
from rdc.transport.control_channel import ControlChannel
from rdc.transport.screen_channel import ScreenChannel
from rdc.transport.server import TransportServer
from tests._transport_support import gen_cert

pytestmark = [pytest.mark.load, pytest.mark.integration]

CLIENTS = 100
FRAMES = 10
#: 句柄回落容差：Windows 每连接占多个内核对象；完全不关会累积数百。
HANDLE_TOLERANCE = 200
#: RSS 容差（MB）：分配器缓存不归还 OS，阈值宽松、只防失控。
RSS_TOLERANCE_MB = 220


@pytest.fixture(autouse=True)
def _isolate_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp_mod, "KNOWN_FINGERPRINTS_PATH", tmp_path / "known_fp.json")
    monkeypatch.setattr(fp_mod, "_fingerprint_manager", None)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _confirm(_fp: str, _host: str) -> bool:
    return True


def _sample() -> tuple[int, int]:
    gc.collect()
    p = psutil.Process()
    return p.num_handles(), p.memory_info().rss


class TestLoad100:
    async def test_100_concurrent_no_leak(self, tmp_path: Path) -> None:
        cert, key = gen_cert(tmp_path)

        async def on_control(session) -> None:  # noqa: ANN001
            await session.next_message()  # 客户端关闭 → None → 会话回收

        async def wave(tag: str) -> None:
            bc = ScreenBroadcaster()

            async def on_screen(session) -> None:  # noqa: ANN001
                sid = f"{session.client_ip}-{id(session)}"
                bc.add(SubscriberInfo(sid, session.stream, session.hmac_key))
                await session.next_message()

            cport, sport = _free_port(), _free_port()
            server = TransportServer(
                host="127.0.0.1",
                control_port=cport,
                screen_port=sport,
                cert_path=cert,
                key_path=key,
                max_connections=CLIENTS * 2 + 10,
                on_control_session=on_control,
                on_screen_session=on_screen,
            )
            await server.start()
            try:
                done_count = 0
                join_all = asyncio.Event()

                async def single() -> None:
                    nonlocal done_count
                    ch = ControlChannel(
                        "127.0.0.1", cport, password_provider=lambda: "", fingerprint_confirm=_confirm
                    )
                    await ch.connect()
                    sc = ScreenChannel(
                        "127.0.0.1",
                        sport,
                        session_token=ch.session_token or "",
                        connection_id=ch.connection_id or b"",
                        fingerprint_confirm=_confirm,
                    )
                    await sc.connect()
                    msg = await asyncio.wait_for(sc.next_message(), timeout=60)
                    assert isinstance(msg, ScreenFrame)
                    await sc.close()
                    await ch.close()
                    done_count += 1
                    if done_count == CLIENTS:
                        join_all.set()

                clients = [asyncio.create_task(single()) for _ in range(CLIENTS)]
                # 等全部画面会话注册（100 个订阅者）
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 120
                while len(bc.subscriber_ids()) < CLIENTS and loop.time() < deadline:
                    await asyncio.sleep(0.05)
                assert len(bc.subscriber_ids()) == CLIENTS, f"{tag}: 未达 100 订阅者"
                # 广播帧；每订阅者密钥独立签名（broadcaster 内部完成）
                for seq in range(1, FRAMES + 1):
                    bc.publish(
                        ScreenFrame(
                            seq=seq,
                            timestamp=0.0,
                            width=16,
                            height=16,
                            codec=Codec.JPEG,
                            data=b"frame" * 20,
                            keyframe=True,
                        )
                    )
                await asyncio.wait_for(join_all.wait(), timeout=120)
                errs = [t.exception() for t in clients if t.exception()]
                assert not errs, f"{tag}: 客户端异常 {errs[:3]}"
                # 兜底清理未 auto-remove 的订阅者，并等待会话回收
                await bc.shutdown()
                assert bc.subscriber_ids() == [], f"{tag}: broadcaster 残留订阅者"
                stale = [
                    t
                    for t in asyncio.all_tasks()
                    if t.get_name().startswith("screen-send-") and not t.done()
                ]
                assert stale == [], f"{tag}: 发送 task 残留 {len(stale)}"
            finally:
                await server.stop()

        await wave("warmup")
        handles0, rss0 = _sample()
        await wave("measure")
        handles1, rss1 = _sample()
        dh = handles1 - handles0
        dmb = (rss1 - rss0) / 2**20
        print(f"\n[load100] handles {handles0} → {handles1} (Δ{dh}) | RSS {round(rss0 / 2**20)} → {round(rss1 / 2**20)}MB (Δ{round(dmb, 1)})")
        assert dh <= HANDLE_TOLERANCE, f"句柄未回落：基线 {handles0} → 压测后 {handles1}（Δ{dh}）"
        assert dmb < RSS_TOLERANCE_MB, f"RSS 增长 {round(dmb, 1)}MB ≥ {RSS_TOLERANCE_MB}MB"

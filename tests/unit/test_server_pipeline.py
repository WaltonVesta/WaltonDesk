"""server.pipeline 单元测试（阶段 6 A3）。

用户叮嘱 b 的验收口径：假屏幕源 → diff → 编码 → broadcaster.publish →
假订阅者接收（真实 ScreenBroadcaster + make_stream_pair 全链路一条），
其余分支用录制替身保持快与确定。

线程红线断言：publish 必须发生在事件循环线程（主线程）；
生产单元（capture/encode）必须发生在 worker 线程且**串行**。
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np

from rdc.capture.framerate import FramerateController
from rdc.encoding.base import EncodedFrame, EncoderStats
from rdc.encoding.raters import AdaptiveRater, RaterSignals
from rdc.protocol.messages import Codec, ScreenFrame
from rdc.server.pipeline import (
    PipelineStats,
    ScreenPipeline,
)
from rdc.transport.broadcaster import ScreenBroadcaster, SubscriberInfo
from rdc.transport.hmac_nonce import new_nonce
from tests._transport_support import make_stream_pair
from tests.unit.test_capture_framerate import FakeClock

# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class RecordingEncoder:
    """VideoEncoder 协议替身：录制调用线程与参数。"""

    def __init__(self) -> None:
        self.encode_calls: list[tuple[tuple[int, int], bool]] = []
        self.threads: list[int] = []
        self.qualities: list[int] = []
        self.keyframe_requests = 0
        self.resolutions: list[tuple[int, int]] = []
        self.closed = False
        self._pts = 0

    @property
    def codec(self) -> Codec:
        return Codec.H264

    def encode(
        self,
        frame: np.ndarray,
        *,
        force_keyframe: bool = False,
        pts: int | None = None,
    ) -> EncodedFrame:
        self.threads.append(threading.get_ident())
        h, w = int(frame.shape[0]), int(frame.shape[1])
        self.encode_calls.append(((w, h), force_keyframe))
        self._pts += 1
        return EncodedFrame(
            data=b"\x00\x00\x00\x01fake" + bytes(str(self._pts), "ascii"),
            is_keyframe=True,
            pts=self._pts,
            width=w,
            height=h,
            quality=28,
            elapsed_ms=0.1,
        )

    def request_keyframe(self) -> None:
        self.keyframe_requests += 1

    def set_quality(self, quality: int) -> None:
        self.qualities.append(quality)

    def set_resolution(self, width: int, height: int) -> None:
        self.resolutions.append((width, height))

    def stats(self) -> EncoderStats:
        return EncoderStats(
            frames=len(self.encode_calls),
            keyframes=len(self.encode_calls),
            total_bytes=0,
            last_ms=0.1,
            avg_ms=0.1,
            max_ms=0.1,
            quality=28,
        )

    def close(self) -> None:
        self.closed = True


class RecordingBroadcaster:
    """broadcaster 替身：录制 publish 线程与帧。"""

    def __init__(self) -> None:
        self.frames: list[ScreenFrame] = []
        self.publish_threads: list[int] = []

    def publish(self, frame: ScreenFrame) -> int:
        self.publish_threads.append(threading.get_ident())
        self.frames.append(frame)
        return 1

    def stats(self):  # noqa: ANN201 - 与 ScreenBroadcaster.stats 鸭子类型对齐
        from rdc.transport.broadcaster import BroadcasterStats

        return BroadcasterStats(subscribers=0, published=len(self.frames), total_dropped=0)


def make_source_frames(n: int = 4, h: int = 32, w: int = 48) -> list[np.ndarray]:
    """互不相同的确定性帧序列。"""
    rng = np.random.default_rng(7)
    return [rng.integers(0, 256, (h, w, 4), dtype=np.uint8) for _ in range(n)]


def build_pipeline(
    frames: list[np.ndarray],
    *,
    encoder: RecordingEncoder | None = None,
    broadcaster: object | None = None,
    rater: AdaptiveRater | None = None,
    signals: RaterSignals | None = None,
    clock: FakeClock | None = None,
):  # noqa: ANN201 -> ScreenPipeline
    from rdc.capture.source import ArrayScreenSource

    enc = encoder or RecordingEncoder()
    bc = broadcaster or RecordingBroadcaster()
    src = ArrayScreenSource(frames)
    pipe = ScreenPipeline(
        src,
        encoder_factory=lambda w_, h_: enc,
        broadcaster=bc,  # type: ignore[arg-type]
        framerate=FramerateController(clock=clock or FakeClock()),
        rater=rater,
        signals_provider=(lambda: signals) if signals is not None else None,
    )
    return pipe, enc, bc, src


# ---------------------------------------------------------------------------
# 端到端：采集→差分→决策→编码→发布
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_first_frame_publishes_full_message(self) -> None:
        frames = make_source_frames(2)
        pipe, enc, bc, _ = build_pipeline(frames)
        decision = await pipe.run_once()
        assert decision is not None and decision.should_capture
        assert len(bc.frames) == 1
        msg = bc.frames[0]
        assert isinstance(msg, ScreenFrame)
        assert (msg.width, msg.height) == (48, 32)
        assert msg.seq == 0
        assert msg.codec is Codec.H264
        assert msg.data and msg.keyframe
        pipe.close()

    async def test_seq_increments_and_timestamp_from_source(self) -> None:
        frames = make_source_frames(3)
        clock = FakeClock()
        pipe, _, bc, _ = build_pipeline(frames, clock=clock)
        for _ in range(3):
            clock.advance(0.05)
            await pipe.run_once()
        assert [m.seq for m in bc.frames] == [0, 1, 2]
        assert bc.frames[1].timestamp > bc.frames[0].timestamp
        pipe.close()

    async def test_real_broadcaster_subscriber_receives_decoded_frame(self) -> None:
        """全链路：真实 ScreenBroadcaster + 流对 + 消费端 HMAC 解码。"""

        frames = make_source_frames(2)
        stream, peer, own_writer, _ = make_stream_pair()
        key = new_nonce()
        peer.enable_hmac(key)
        bc = ScreenBroadcaster()
        bc.add(SubscriberInfo("c1", stream, key))
        pipe, _, _, _ = build_pipeline(frames, broadcaster=bc)

        await pipe.run_once()
        pump_task = asyncio.create_task(_pump(own_writer, peer))
        msg = await asyncio.wait_for(peer.recv_message(), timeout=2.0)
        pump_task.cancel()
        assert isinstance(msg, ScreenFrame)
        assert msg.seq == 0 and (msg.width, msg.height) == (48, 32)
        await bc.shutdown()
        pipe.close()

    async def test_close_stops_and_releases(self) -> None:
        pipe, enc, _, src = build_pipeline(make_source_frames(1))
        await pipe.run_once()
        pipe.close()
        pipe.close()  # 幂等
        assert await pipe.run_once() is None
        assert enc.closed and src.is_closed


async def _pump(writer, target) -> None:  # noqa: ANN001 - 测试泵（见 broadcaster 测试同款）
    while True:
        await asyncio.sleep(0)
        data = bytes(writer.sent)
        writer.sent.clear()
        if data:
            target._reader.feed_data(data)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 静止门控
# ---------------------------------------------------------------------------


class TestStaticGate:
    async def test_static_frame_skips_encode(self) -> None:
        same = make_source_frames(1)
        clock = FakeClock()
        pipe, enc, bc, _ = build_pipeline(same * 4, clock=clock)
        d1 = await pipe.run_once()  # 首帧必采
        assert d1 is not None and d1.reason == "first"
        clock.advance(1.0)
        d2 = await pipe.run_once()  # 同帧重放 → static → IDLE
        assert d2 is not None and d2.reason == "static"
        assert len(bc.frames) == 1  # 无新增发布
        assert len(enc.encode_calls) == 1
        st = pipe.stats()
        assert st.idle_skips == 1 and st.captured == 2 and st.encoded == 1
        assert st.published == 1 and st.last_reason == "static"
        pipe.close()

    async def test_force_refresh_after_idle_interval(self) -> None:
        frames = make_source_frames(1)
        clock = FakeClock()
        pipe, enc, bc, _ = build_pipeline(frames * 6, clock=clock)
        pipe._framerate._idle_force_interval_s = 5.0  # noqa: SLF001 - 测试内构造场景
        await pipe.run_once()  # first
        clock.advance(1.0)
        assert (await pipe.run_once()).reason == "static"  # type: ignore[union-attr]
        clock.advance(4.0)  # 距上次采集 5s → 强制刷新
        d = await pipe.run_once()
        assert d is not None and d.reason == "forced"
        assert len(enc.encode_calls) == 2
        pipe.close()


# ---------------------------------------------------------------------------
# 线程红线（跨线程控件操作的管线侧对应物）
# ---------------------------------------------------------------------------


class TestThreadingDiscipline:
    async def test_publish_on_loop_thread_encode_on_worker(self) -> None:
        main = threading.get_ident()
        pipe, enc, bc, _ = build_pipeline(make_source_frames(2))
        await pipe.run_once()
        await pipe.run_once()
        assert bc.publish_threads and all(t == main for t in bc.publish_threads)
        assert enc.threads and all(t != main for t in enc.threads)
        # 单 worker → 全部编码在同一线程且串行（无并发交叠由 worker 数=1 保证）
        assert len(set(enc.threads)) == 1
        pipe.close()

    async def test_run_loop_beats_and_stops(self) -> None:
        frames = make_source_frames(1)
        clock = FakeClock()
        pipe, enc, bc, _ = build_pipeline(frames * 10, clock=clock)
        stop = asyncio.Event()

        async def _ticker() -> None:
            for _ in range(4):
                await asyncio.sleep(0)
                clock.advance(0.04)  # 模拟真实时间流逝（决策 delay 扣除后继续）

        tick = asyncio.create_task(_ticker())
        run = asyncio.create_task(pipe.run(stop=stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(run, timeout=2.0)
        await tick
        assert enc.encode_calls  # 至少跑过生产单元
        pipe.close()

    async def test_run_once_after_close_returns_none(self) -> None:
        pipe, _, _, _ = build_pipeline(make_source_frames(1))
        pipe.close()
        assert await pipe.run_once() is None


# ---------------------------------------------------------------------------
# rater 接线
# ---------------------------------------------------------------------------


class TestRaterWiring:
    async def test_congestion_lowers_crf_via_set_quality(self) -> None:
        bad = RaterSignals(rtt_ms=400.0, loss_rate=0.1)  # severe
        frames = make_source_frames(4)
        rater = AdaptiveRater(window_frames=2, initial_crf=28)
        clock = FakeClock()
        pipe, enc, bc, _ = build_pipeline(frames, rater=rater, signals=bad, clock=clock)
        for _ in range(4):
            clock.advance(0.05)
            await pipe.run_once()
        assert enc.qualities, "rater 决策应已写入编码器"
        assert enc.qualities[-1] > 28, f"拥塞应升 CRF（降质量），实得 {enc.qualities}"
        pipe.close()

    async def test_scale_change_requests_keyframe(self) -> None:
        class ScaleRater:
            def __init__(self) -> None:
                self.n = 0

            def observe(self, signals: RaterSignals):  # noqa: ANN201
                from rdc.encoding.raters import RaterDecision

                self.n += 1
                scale = 0.75 if self.n >= 2 else 1.0
                return RaterDecision(
                    crf=28, bitrate_bps=4_000_000, resolution_scale=scale,
                    score=0.0, adjusted=scale != 1.0, oscillating=False,
                )

        frames = make_source_frames(4, h=64, w=64)
        clock = FakeClock()
        rater = ScaleRater()  # type: ignore[assignment]
        pipe, enc, bc, _ = build_pipeline(frames, rater=rater, clock=clock)  # type: ignore[arg-type]
        await pipe.run_once()  # observe#1 scale=1.0
        clock.advance(0.05)
        await pipe.run_once()  # observe#2 scale=0.75 → 下一单元降采样+关键帧
        clock.advance(0.05)
        await pipe.run_once()
        sizes = [s for s, _ in enc.encode_calls]
        assert (64, 64) in sizes
        assert any(w_ < 64 for w_, _ in sizes), f"应出现缩放后的帧尺寸，实得 {sizes}"
        assert enc.keyframe_requests >= 1
        pipe.close()

    async def test_signals_provider_none_defaults_zero(self) -> None:
        rater = AdaptiveRater(window_frames=2)
        pipe, enc, _, _ = build_pipeline(make_source_frames(3), rater=rater)
        for _ in range(3):
            await pipe.run_once()
        assert enc.qualities  # observe 走通（全 0 信号 → 可能未调整，但 set_quality 必被调）
        pipe.close()


# ---------------------------------------------------------------------------
# 编码器默认工厂与 hw 选型接线
# ---------------------------------------------------------------------------


class TestEncoderFactory:
    async def test_default_factory_uses_probed_codec_name(self, monkeypatch) -> None:
        import rdc.server.pipeline as pl

        calls: dict[str, object] = {}

        class SpyEnc(RecordingEncoder):
            def __init__(self, w: int, h: int, **kw: object) -> None:
                calls.update({"w": w, "h": h, **kw})
                super().__init__()

        monkeypatch.setattr(pl, "default_codec_name", lambda: "libx264")
        import rdc.encoding.h264 as h264mod

        monkeypatch.setattr(h264mod, "H264Encoder", SpyEnc)  # type: ignore[attr-defined]
        from rdc.capture.source import ArrayScreenSource

        pipe = ScreenPipeline(
            ArrayScreenSource(make_source_frames(1)),
            broadcaster=RecordingBroadcaster(),  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
        )
        await pipe.run_once()
        assert calls["w"] == 48 and calls["h"] == 32
        assert calls["codec_name"] == "libx264"
        pipe.close()

    async def test_default_codec_name_is_process_cached(self, monkeypatch) -> None:
        import rdc.server.pipeline as pl

        calls: list[int] = []

        def fake_best(*args: object, **kw: object) -> str:
            calls.append(1)
            return "libx264"

        monkeypatch.setattr(pl, "_PROBED_CODEC_NAME", None)
        monkeypatch.setattr(pl, "best_encoder", fake_best)
        assert pl.default_codec_name() == "libx264"
        assert pl.default_codec_name() == "libx264"
        assert len(calls) == 1, "进程级只应探测一次"

    def test_stats_snapshot_shape(self) -> None:
        s = PipelineStats()
        assert s.captured == 0 and s.encoder == ""


# ---------------------------------------------------------------------------
# 走查 bug 3：奇数分辨率（mss 窗口模式）经真实默认工厂不再崩，发布对齐尺寸
# ---------------------------------------------------------------------------


class TestOddResolutionEndToEnd:
    async def test_odd_source_frames_publish_aligned_size(self) -> None:
        from rdc.capture.source import ArrayScreenSource

        bc = RecordingBroadcaster()
        frames = make_source_frames(2, h=911, w=955)
        pipe = ScreenPipeline(
            ArrayScreenSource(frames),
            broadcaster=bc,  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
        )
        decision = await pipe.run_once()
        assert decision is not None and decision.should_capture
        msg = bc.frames[0]
        # 病灶：奇数直接进 H264Encoder → avcodec_open2 ExternalError
        assert (msg.width, msg.height) == (954, 910)
        assert msg.keyframe and msg.data
        pipe.close()


class _SizingSource:
    """采集源替身：第 switch_after 帧起**换尺寸**（模拟被控端运行中改分辨率）。

    ArrayScreenSource 强制全序列同尺寸（测试替身的构造期校验），动态分辨率
    需要的是"序列中途变尺寸"，故独立小替身：capture() 返回新形状的
    CaptureFrame，其余协议成员透传内层（同 :class:`_FlakySource` 模式）。
    """

    def __init__(self, big: list[np.ndarray], small: list[np.ndarray], *, switch_after: int) -> None:
        from rdc.capture.source import ArrayScreenSource

        self._big = ArrayScreenSource(big)
        self._small = ArrayScreenSource(small)
        self._switch_after = switch_after
        self._n = 0

    def capture(self):  # noqa: ANN201 - 返回内层 CaptureFrame
        self._n += 1
        src = self._small if self._n > self._switch_after else self._big
        return src.capture()

    def __getattr__(self, name: str):  # noqa: ANN201 - 协议其余成员透传
        return getattr(self._big, name)


class TestDynamicResolutionEndToEnd:
    """走查第十一批端到端：源尺寸突变 → 编码器跟随 + 新尺寸关键帧。

    采集层病灶（region 过期、帧尺寸卡旧值）由 test_capture_mss 的
    ``TestDynamicGeometry`` 锁定；本类锁定**修复生效后的下游链**——一旦
    采集层交出尺寸不同的新帧，管线必须自动发新尺寸 IDR（用户期望"全链
    无感跟随"的两段证明各测一段，装配语义完整覆盖）。
    """

    async def test_size_change_publishes_new_size_keyframe(self) -> None:
        from rdc.encoding.h264 import H264Encoder

        bc = RecordingBroadcaster()
        big = make_source_frames(2, h=64, w=96)
        small = make_source_frames(2, h=32, w=48)
        src = _SizingSource(big, small, switch_after=1)
        # 真实编码器：走"encode 内自动跟随重建"分支（替身测不到重建语义）
        enc = H264Encoder(96, 64)
        pipe = ScreenPipeline(
            src,  # type: ignore[arg-type]
            encoder_factory=lambda w_, h_: enc,
            broadcaster=bc,  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
        )
        await pipe.run_once()  # 帧1：96x64 IDR
        await pipe.run_once()  # 帧2：尺寸突变
        m1, m2 = bc.frames[0], bc.frames[1]
        assert (m1.width, m1.height) == (96, 64)
        assert (m2.width, m2.height) == (48, 32), "尺寸未跟随（编码器未重建）"
        assert m2.keyframe, "重建后首帧必须是 IDR，否则主控端花屏"
        assert m2.data, "IDR 载荷非空"
        # 跟随分支：同一实例内部重建，非 pipeline 新建替换（close 后 encoder 归 None，须在 close 前断言）
        assert enc is pipe.encoder
        pipe.close()
        enc.close()


# ---------------------------------------------------------------------------
# 走查第六批 P0：采集失败退避不崩 + 停滞边沿回调（安全桌面自愈）
# ---------------------------------------------------------------------------


class _FlakySource:
    """ArrayScreenSource 装饰替身：可控失败 N 次后自愈。"""

    def __init__(self, frames: list[np.ndarray], fail_times: int) -> None:
        from rdc.capture.source import ArrayScreenSource

        self._inner = ArrayScreenSource(frames)
        self._fail = fail_times
        self.capture_calls = 0

    def capture(self):  # noqa: ANN201 - 转发内层帧类型
        self.capture_calls += 1
        if self.capture_calls <= self._fail:
            from rdc.exceptions import ScreenCaptureError

            raise ScreenCaptureError("屏幕采集失败: BitBlt failed")
        return self._inner.capture()

    def __getattr__(self, name: str):  # noqa: ANN201 - 协议其余成员透传
        return getattr(self._inner, name)


class TestCaptureBackoffAndStall:
    async def test_capture_failure_returns_backoff_idle_not_crash(self) -> None:
        """病灶：ScreenCaptureError 直接炸 run() task（pipeline 永久停）。"""
        from rdc.capture.framerate import FrameAction

        frames = make_source_frames(2)
        src = _FlakySource(frames, fail_times=1)
        pipe = ScreenPipeline(
            src,  # type: ignore[arg-type]
            encoder_factory=lambda w_, h_: RecordingEncoder(),
            broadcaster=RecordingBroadcaster(),  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
        )
        d1 = await pipe.run_once()
        assert d1 is not None
        assert d1.action == FrameAction.IDLE
        assert d1.reason == "capture_failed"
        assert d1.delay_s == 0.25  # 首次退避
        d2 = await pipe.run_once()  # 自愈出帧
        assert d2 is not None and d2.should_capture
        pipe.close()

    async def test_backoff_doubles_and_caps(self) -> None:
        frames = make_source_frames(2)
        src = _FlakySource(frames, fail_times=4)
        pipe = ScreenPipeline(
            src,  # type: ignore[arg-type]
            encoder_factory=lambda w_, h_: RecordingEncoder(),
            broadcaster=RecordingBroadcaster(),  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
        )
        delays = [(await pipe.run_once()).delay_s for _ in range(4)]
        assert delays == [0.25, 0.5, 1.0, 2.0]  # 指数翻倍，封顶 2s
        pipe.close()

    async def test_stall_hook_fires_once_on_threshold_and_on_recovery(self) -> None:
        # 10 帧互异序列：失败期间内层源被调用的那几次（成功路径）不会耗尽序列，
        # 避免差分对同帧判 IDLE 误触发"恢复"边沿。
        frames = make_source_frames(10)
        src = _FlakySource(frames, fail_times=5)
        events: list[tuple[bool, str]] = []
        pipe = ScreenPipeline(
            src,  # type: ignore[arg-type]
            encoder_factory=lambda w_, h_: RecordingEncoder(),
            broadcaster=RecordingBroadcaster(),  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
            capture_stall_hook=lambda stalled, detail: events.append((stalled, detail)),
        )
        for _ in range(3):
            await pipe.run_once()
        # 第 3 次连续失败恰好触发一次停滞
        assert events == [(True, "屏幕采集失败: BitBlt failed")]
        for _ in range(2):
            await pipe.run_once()  # 第 4/5 次失败：不重复触发停滞边沿
        assert len(events) == 1
        src._fail = 0  # 自愈
        await pipe.run_once()
        assert events[-1] == (False, "")  # 恢复通知一次
        pipe.close()

    async def test_stall_hook_runs_on_event_loop_thread(self) -> None:
        """红线：回调必须来自事件循环线程，不是 executor worker 线程。"""
        import threading

        loop_tid = threading.get_ident()
        seen: list[bool] = []
        frames = make_source_frames(2)
        src = _FlakySource(frames, fail_times=3)

        def _hook(stalled: bool, detail: str) -> None:
            seen.append(threading.get_ident() == loop_tid)

        pipe = ScreenPipeline(
            src,  # type: ignore[arg-type]
            encoder_factory=lambda w_, h_: RecordingEncoder(),
            broadcaster=RecordingBroadcaster(),  # type: ignore[arg-type]
            framerate=FramerateController(clock=FakeClock()),
            capture_stall_hook=_hook,
        )
        for _ in range(3):
            await pipe.run_once()
        assert seen == [True]
        pipe.close()

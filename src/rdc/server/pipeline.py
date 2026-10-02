"""服务端画面管线（阶段 6 A3：采集→差分→门控→编码→广播 串联）。

数据流（REFACTOR_PLAN 阶段 6 / STAGE5_REPORT §6 起点 2）::

    ScreenSource(双缓冲) ──executor──▶ ScreenDiffer.compare(adopt=True)
        └─▶ FramerateController.decide(diff, signals)   # 静止门控 + 节奏
        └─▶ VideoEncoder.encode(BGRA)                    # hw_probe 选型
        └─▶ ScreenBroadcaster.publish(ScreenFrame)       # 阶段 4 背压已就位
    AdaptiveRater.observe(RaterSignals) ──▶ encoder.set_quality(crf) / 降分辨率

线程模型（⚠️ 跨线程红线）：

- **生产者单元**（capture+diff+decide+encode，全部 CPU 密集）整体投递到
  **单 worker** 的 executor 串行执行——单 worker 同时保证三点：
  ①编码器/差分器的有状态调用不并发；②采集双缓冲的帧引用在编码完成前
  不会被下一帧覆写（事件循环 ``await`` 上一单元结束后才提交下一单元）；
  ③事件循环线程（qasync 下即 Qt 主线程）全程零忙等，UI 不掉帧。
- **broadcaster.publish 只在事件循环线程调用**：``DropOldestQueue.push`` 走
  ``put_nowait``，唤醒的是循环内的 getter，跨线程调用非线程安全。
- GIL：PyAV 在 ``avcodec_send/receive`` 期间释放 GIL（``scripts/bench_looplag.py``
  实测数据入 STAGE6_REPORT）；numpy 归约同理。纯 Python 段（构造/决策）
  占比 <2%，不构成事件循环阻塞源。

生命周期：首帧采集得到尺寸后才创建编码器（``encoder_factory`` 调用点）；
:meth:`close` 释放 source/differ/encoder 与自建 executor（幂等）。broadcaster
所有权在调用方（会话层，阶段 9 装配），本模块不关闭它。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import numpy as np

from rdc.capture.diff import ScreenDiffer
from rdc.capture.framerate import FrameDecision, FramerateController
from rdc.capture.source import ScreenSource
from rdc.encoding.hw_probe import best_encoder
from rdc.encoding.raters import RaterSignals
from rdc.logging import get_logger
from rdc.protocol.messages import ScreenFrame

if TYPE_CHECKING:
    from concurrent.futures import Executor

    import numpy.typing as npt

    from rdc.encoding.base import VideoEncoder
    from rdc.encoding.raters import AdaptiveRater
    from rdc.transport.broadcaster import ScreenBroadcaster

__all__ = ["PipelineStats", "ScreenPipeline"]

_LOG = get_logger("server.pipeline")

#: 默认编码器工厂之外的硬件选型缓存（进程级：探测有 open 代价，只探一次）。
_PROBED_CODEC_NAME: str | None = None


def default_codec_name() -> str:
    """进程级缓存的 :func:`~rdc.encoding.hw_probe.best_encoder` 结果。"""
    global _PROBED_CODEC_NAME
    if _PROBED_CODEC_NAME is None:
        _PROBED_CODEC_NAME = best_encoder()
    return _PROBED_CODEC_NAME


EncoderFactory = Callable[[int, int], "VideoEncoder"]
SignalsProvider = Callable[[], "RaterSignals | None"]


@dataclass(frozen=True)
class PipelineStats:
    """管线累计统计快照。

    Attributes:
        captured: 采集帧数（含被静止门控丢弃的）。
        encoded: 实际编码帧数。
        published: 交给 broadcaster 的帧数（恒等于 encoded）。
        idle_skips: 因静止门控跳过的周期数。
        total_encode_ms: 编码累计耗时（毫秒）。
        total_capture_ms: 采集累计耗时（毫秒）。
        last_reason: 最近一次决策的 ``reason`` 标签。
        encoder: 编码器名（首帧后才有值）。
    """

    captured: int = 0
    encoded: int = 0
    published: int = 0
    idle_skips: int = 0
    total_encode_ms: float = 0.0
    total_capture_ms: float = 0.0
    last_reason: str = ""
    encoder: str = ""


class ScreenPipeline:
    """采集 → 编码 → 广播的单生产者管线（asyncio）。

    Args:
        source: 画面采集源（生产用 ``MssScreenSource``；测试用
            :class:`~rdc.capture.source.ArrayScreenSource`）。
        encoder_factory: ``(width, height) -> VideoEncoder`` 工厂；默认以
            :func:`default_codec_name`（hw_probe 实测选型）构造
            :class:`~rdc.encoding.h264.H264Encoder`。
        broadcaster: 阶段 4 的 :class:`~rdc.transport.broadcaster.ScreenBroadcaster`。
        differ: 差分器；默认 :class:`~rdc.capture.diff.ScreenDiffer`()。
        framerate: 帧率决策器；默认 :class:`~rdc.capture.framerate.FramerateController`()。
        rater: 自适应码率器；``None`` 表示不做码率自适应。
        signals_provider: 每周期取网络信号（RTT/丢包等由心跳/会话层提供，
            队列与丢弃率由本管线从 broadcaster 统计补齐后一并喂给 ``rater``，
            见 :meth:`_observe_rater`）。
        executor: 生产者线程池；``None`` 自建单 worker（关闭时随之关闭）。

    Raises:
        构造期无重操作；编码器创建延后到首帧采集后，其失败从
        :meth:`run_once`/:meth:`run` 抛出（``RuntimeError``，含编码器名）。
    """

    def __init__(
        self,
        source: ScreenSource,
        *,
        encoder_factory: EncoderFactory | None = None,
        broadcaster: ScreenBroadcaster,
        differ: ScreenDiffer | None = None,
        framerate: FramerateController | None = None,
        rater: AdaptiveRater | None = None,
        signals_provider: SignalsProvider | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._source = source
        self._broadcaster = broadcaster
        self._differ = differ if differ is not None else ScreenDiffer()
        self._framerate = framerate if framerate is not None else FramerateController()
        self._rater = rater
        self._signals_provider = signals_provider
        self._encoder_factory = encoder_factory
        self._encoder: VideoEncoder | None = None
        self._seq = 0
        self._scale = 1.0
        self._force_keyframe = False

        if executor is not None:
            self._executor: Executor | None = executor
            self._owns_executor = False
        else:
            # 单 worker：串行化生产单元（见模块 docstring 线程模型）
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rdc-pipe")
            self._owns_executor = True

        self._captured = 0
        self._encoded = 0
        self._idle_skips = 0
        self._encode_ms = 0.0
        self._capture_ms = 0.0
        self._last_reason = ""
        self._closed = False

    # -- 公共 API ---------------------------------------------------------------

    @property
    def framerate(self) -> FramerateController:
        """帧率决策器（装配层接线用：StreamControl CONFIG 帧率/输入节拍同步）。"""
        return self._framerate

    @property
    def encoder(self) -> VideoEncoder | None:
        """首帧后的编码器实例（诊断/测试）。"""
        return self._encoder

    def stats(self) -> PipelineStats:
        """统计快照。"""
        return PipelineStats(
            captured=self._captured,
            encoded=self._encoded,
            published=self._encoded,  # encode 成功必然 publish（同一单元内）
            idle_skips=self._idle_skips,
            total_encode_ms=self._encode_ms,
            total_capture_ms=self._capture_ms,
            last_reason=self._last_reason,
            encoder=self._encoder.codec.name if self._encoder else "",
        )

    def request_keyframe(self) -> None:
        """请求下一帧强制关键帧（新客户端加入时由会话层调用；线程安全：
        标志位写入受 GIL 保护，最迟下一生产单元生效）。"""
        self._force_keyframe = True

    async def run_once(self) -> FrameDecision | None:
        """执行一个"采集→差分→决策→（编码→发布）"周期。

        Returns:
            本周期决策（含下一周期等待时长）；管线已关闭时 ``None``。
        """
        if self._closed:
            return None
        loop = asyncio.get_running_loop()
        unit = await loop.run_in_executor(self._executor, self._produce)
        if unit is None:
            return None
        msg, decision = unit
        if msg is not None:
            self._broadcaster.publish(msg)  # 事件循环线程（红线，见模块 docstring）
        self._observe_rater()
        return decision

    async def run(self, *, stop: asyncio.Event) -> None:
        """主循环：持续到 ``stop`` 置位。

        等待用 ``asyncio.sleep``（帧率控制器只给时长、绝不睡眠——v5 与 v4 的
        根本区别）。生产单元自身耗时从等待中扣除，维持目标节拍。
        """
        while not stop.is_set():
            t0 = time.monotonic()
            decision = await self.run_once()
            if decision is None:
                return
            remain = decision.delay_s - (time.monotonic() - t0)
            if remain > 0:
                await _wait_or_stop(stop, remain)

    def close(self) -> None:
        """释放全部自有资源（幂等；不关闭 broadcaster）。"""
        if self._closed:
            return
        self._closed = True
        self._source.close()
        self._differ.close()
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False)
        self._executor = None

    # -- 生产者单元（executor 线程内） -------------------------------------------

    def _produce(self) -> tuple[ScreenFrame | None, FrameDecision] | None:
        """capture → diff → decide →（必要时）encode。返回值交事件循环发布。"""
        if self._closed:
            return None
        t_cap = time.perf_counter()
        frame = self._source.capture()
        self._capture_ms += (time.perf_counter() - t_cap) * 1000.0
        self._captured += 1

        diff = self._differ.compare(frame.data, adopt=True)
        decision = self._framerate.decide(diff, self._signals_snapshot())
        self._last_reason = decision.reason

        if not decision.should_capture:
            self._idle_skips += 1
            return None, decision

        encoder = self._ensure_encoder(frame.width, frame.height)
        payload = frame.data
        scale = self._scale
        if scale < 1.0:
            payload = _downscale(payload, scale)
        t_enc = time.perf_counter()
        encoded = encoder.encode(payload, force_keyframe=self._force_keyframe)
        self._force_keyframe = False
        self._encode_ms += (time.perf_counter() - t_enc) * 1000.0
        self._encoded += 1

        msg = ScreenFrame(
            seq=self._seq,
            timestamp=frame.timestamp,
            width=encoded.width,
            height=encoded.height,
            codec=encoder.codec,
            data=encoded.data,
            keyframe=encoded.is_keyframe,
        )
        self._seq += 1
        return msg, decision

    def _ensure_encoder(self, width: int, height: int) -> VideoEncoder:
        """首帧建编码器（延后构造是管线与采集尺寸解耦的关键）。"""
        if self._encoder is None:
            if self._encoder_factory is not None:
                self._encoder = self._encoder_factory(width, height)
            else:
                from rdc.encoding.h264 import H264Encoder

                self._encoder = H264Encoder(
                    width, height, codec_name=default_codec_name(), fps=self._framerate.max_fps
                )
            _LOG.info(
                "pipeline_encoder_ready",
                codec=self._encoder.codec.name,
                size=f"{width}x{height}",
            )
        return self._encoder

    # -- rater 接线（事件循环线程） ----------------------------------------------

    def _signals_snapshot(self) -> RaterSignals | None:
        """调用方的网络信号（生产单元内调用；provider 约定为轻量纯读取）。"""
        if self._signals_provider is None:
            return None
        return self._signals_provider()

    def _observe_rater(self) -> None:
        """把 broadcaster 背压统计并入信号，喂 rater，应用其决策到编码器。"""
        if self._rater is None:
            return
        base = self._signals_snapshot()
        if base is None:
            base = RaterSignals(rtt_ms=0.0, loss_rate=0.0)
        st = self._broadcaster.stats()
        enqueued = sum(q.enqueued for q in st.per_subscriber.values())
        dropped = sum(q.dropped for q in st.per_subscriber.values())
        queue_depth = max(
            (q.enqueued - q.dequeued) for q in st.per_subscriber.values()
        ) if st.per_subscriber else 0
        signals = RaterSignals(
            rtt_ms=base.rtt_ms,
            loss_rate=base.loss_rate,
            queue_depth=queue_depth,
            dropped_ratio=(dropped / enqueued) if enqueued else 0.0,
        )
        decision = self._rater.observe(signals)
        enc = self._encoder
        if enc is None:
            return
        if decision.crf:
            enc.set_quality(decision.crf)
        if decision.resolution_scale != self._scale:
            self._scale = decision.resolution_scale
            enc.request_keyframe()
            self._force_keyframe = True
            _LOG.info("pipeline_resolution_scale", scale=self._scale)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _downscale(bgra: npt.NDArray[np.uint8], scale: float) -> npt.NDArray[np.uint8]:
    """BGRA 帧等比缩放（raters 降分辨率档）；cv2 INTER_AREA（缩小抗锯齿最优）。"""
    import cv2

    h, w = bgra.shape[:2]
    nw, nh = max(16, int(w * scale) & ~1), max(16, int(h * scale) & ~1)  # 偶数边（yuv420）
    # cv2.resize 存根返回 dtype 联合过宽；输入 uint8 时输出恒为 uint8
    return cast("npt.NDArray[np.uint8]", cv2.resize(bgra, (nw, nh), interpolation=cv2.INTER_AREA))


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """可被 stop 事件提前唤醒的 sleep；被取消时自然向上传播（正常关停路径）。"""
    with contextlib.suppress(TimeoutError):  # py3.11+: wait_for 超时即"无 stop"正常继续
        await asyncio.wait_for(stop.wait(), timeout=seconds)

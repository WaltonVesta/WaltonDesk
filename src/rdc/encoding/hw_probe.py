"""硬件 H.264 编码器探测（阶段 5 任务 7）。

**为什么必须实测 ``ctx.open()``**（STAGE5_INPROGRESS §2.5 本机实测）：
PyAV 的编解码器**构造永远成功**——只要名字编译进了 FFmpeg，
``av.CodecContext.create(name, "w")`` 不报错；真正的可用性（驱动、显卡、
权限）要到 ``ctx.open()`` 才暴露。本机实测：

- ``h264_nvenc``：构造 ✅ → open ❌ ``PermissionError``（无 NVENC 会话授权）
- ``h264_qsv``：构造 ✅ → open ❌ ``ArgumentError``
- ``h264_amf``：构造 ✅ → open ❌ ``UnknownError``
- ``h264_vaapi`` / ``h264_videotoolbox`` / ``h264_v4l2m2m``：**未编译进来**，
  连构造都 ``UnknownCodecError``
- ``libx264`` / ``libx265``：构造 ✅ open ✅

只查 ``av.codecs_available`` 或构造成功就判定"可用"，会把上述全部坑当成绿灯，
选中的编码器在首帧才炸——所以本模块把探测提前，且分三层报告：

- :attr:`ProbeOutcome.UNLISTED`：FFmpeg 未编译该编码器（查名单即可）；
- :attr:`ProbeOutcome.CONSTRUCT_FAILED`：名单里有但构造失败；
- :attr:`ProbeOutcome.OPEN_FAILED`：构造成功但 ``open()`` 失败（**最常见**）；
- :attr:`ProbeOutcome.AVAILABLE`：open 成功 = 真可用。

探测本身开的是"最小帧配置"（1280x720 yuv420p，preset 不设），耗时 <100ms/个，
只在启动时跑一次；结果可注入 :class:`~rdc.encoding.h264.H264Encoder` 的选型
（阶段 6 集成时按 ``best()`` 决定软编还是硬编）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

import av

from rdc.logging import get_logger

__all__ = [
    "HARDWARE_CANDIDATES",
    "SOFTWARE_FALLBACK",
    "ProbeOutcome",
    "ProbeResult",
    "probe_encoder",
    "probe_all",
]

_LOG = get_logger("encoding.hw_probe")

#: 候选硬件编码器（按优先级：NVENC → QSV → AMF → 平台特定）。
HARDWARE_CANDIDATES: Final = (
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
    "h264_videotoolbox",
    "h264_vaapi",
    "h264_v4l2m2m",
)

#: 软编兜底（本机实测唯一稳定可用的 H.264 编码器）。
SOFTWARE_FALLBACK: Final = "libx264"


class ProbeOutcome(IntEnum):
    """探测结论（值越大越"可用"，方便 max()/排序取最优）。"""

    UNLISTED = 0
    CONSTRUCT_FAILED = 1
    OPEN_FAILED = 2
    AVAILABLE = 3


@dataclass(frozen=True)
class ProbeResult:
    """一个编码器的探测记录。

    Attributes:
        name: 编码器名。
        outcome: 三层结论之一。
        error: 失败原因摘要（成功为空串）。
        elapsed_ms: 本次探测耗时。
    """

    name: str
    outcome: ProbeOutcome
    error: str = ""
    elapsed_ms: float = 0.0

    @property
    def available(self) -> bool:
        """open 成功才算可用（§2.5 的决定性教训）。"""
        return self.outcome is ProbeOutcome.AVAILABLE


def probe_encoder(name: str) -> ProbeResult:
    """探测单个编码器：名单 → 构造 → **open** 三连，任何一步不过即定档。

    Args:
        name: FFmpeg 编码器名（如 ``h264_nvenc``）。

    Returns:
        :class:`ProbeResult`——**不抛错**，失败以 outcome 表达（探测是
        启动期旁路，不该有异常路径）。
    """
    t0 = time.perf_counter()

    def _done(outcome: ProbeOutcome, error: str = "") -> ProbeResult:
        return ProbeResult(
            name=name,
            outcome=outcome,
            error=error,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    if name not in av.codecs_available:
        return _done(ProbeOutcome.UNLISTED, "编码器未编译进 FFmpeg")

    try:
        # 用 Any 承接：基类 CodecContext 存根不含 width/height/pix_fmt 等视频字段，
        # 运行时实际返回的是 VideoCodecContext 子类（属性齐备）。
        ctx: Any = av.CodecContext.create(name, "w")
    except Exception as exc:
        return _done(ProbeOutcome.CONSTRUCT_FAILED, f"{type(exc).__name__}: {exc}")

    try:
        # 最小可行配置：720p yuv420p。不设 preset/options——硬编默认值
        # 即能 open；open 失败反映的是设备/驱动层问题，与参数无关。
        ctx.width = 1280
        ctx.height = 720
        ctx.pix_fmt = "yuv420p"
        ctx.open()
    except Exception as exc:
        # ⚠️ 只丢引用释放，不写 ctx.close()（PyAV CodecContext 无此方法）
        ctx = None
        return _done(ProbeOutcome.OPEN_FAILED, f"{type(exc).__name__}: {exc}")

    ctx = None  # 释放（同上，不 close）
    return _done(ProbeOutcome.AVAILABLE)


def probe_all(
    candidates: tuple[str, ...] = HARDWARE_CANDIDATES,
    *,
    software: str = SOFTWARE_FALLBACK,
) -> list[ProbeResult]:
    """批量探测候选 + 软编兜底，按原始顺序返回。

    Args:
        candidates: 硬件候选序列。
        software: 兜底软编名（一并探测，正常必 AVAILABLE）。

    Returns:
        结果列表（含 software 一项）。上层用
        ``next((r.name for r in results if r.available), None)`` 取首选。
    """
    results = [probe_encoder(name) for name in (*candidates, software)]
    for r in results:
        _LOG.info(
            "编码器探测",
            encoder=r.name,
            outcome=r.outcome.name,
            error=r.error or None,
            elapsed_ms=round(r.elapsed_ms, 1),
        )
    return results


def best_encoder(
    candidates: tuple[str, ...] = HARDWARE_CANDIDATES,
    *,
    software: str = SOFTWARE_FALLBACK,
    results: list[ProbeResult] | None = None,
) -> str:
    """返回首个可用编码器名（候选序优先，全灭则软件兜底）。

    Args:
        candidates: 硬件候选。
        software: 兜底软编。
        results: 复用已有探测结果（避免重复 open）；``None`` 现探。

    Returns:
        编码器名。注意：若连 ``software`` 都不可用，仍返回它——
        :class:`~rdc.encoding.h264.H264Encoder` 构造时会抛出真实错误，
        选型函数不做二次报错。
    """
    pool = results if results is not None else probe_all(candidates, software=software)
    for r in pool:
        if r.available and r.name != software:
            return r.name
    return software

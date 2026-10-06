"""H.264 编码器（PyAV libx264，v5 主路径）。

阶段 5 任务 6。实现 :class:`~rdc.encoding.base.VideoEncoder` 协议。

三条实测硬约束（STAGE5_INPROGRESS §2.2/§2.3，违反任一条本阶段验收直接失败）
--------------------------------------------------------------------------
1. **P2 色彩路径**：``cv2.cvtColor(BGRA→YUV_I420)``（4K 1.84ms）+ 直接填充
   ``av.VideoFrame`` 的 yuv420p 平面（0.79ms）。**禁止**
   ``av.VideoFrame.from_ndarray(bgra)``——PyAV 内部 swscale 4K 要 23.6ms，
   整帧编码从 11.6ms 恶化到 29.7ms。
2. **``thread_count`` 显式 ≥4**：libx264 在 ``thread_count=1`` 时 4K 实测
   **65.8ms/帧**（破 40ms 线），``=8`` 时 34.4ms、auto(0) 亦可达标但不可依赖
   跨机器一致性，故显式下限 4（37.1ms）并允许注入调优。
3. **不要调用 ``ctx.close()``**：PyAV 18.x 的 ``CodecContext`` 没有 close/
   release 方法（实测 AttributeError），靠引用回收；重建编码器=丢弃旧引用。

其他实测事实（代码按此写，换机器需重探的只有硬件部分——见 hw_probe）
--------------------------------------------------------------------
- ``extradata`` 长度 0 → 输出是 Annex-B 裸流，裸 ``av.Packet(bytes)`` 可直接喂
  解码器（探针实测 10/10 帧解出），**无需 AVCC/SDP 封装**；
- ``tune=zerolatency`` 下 ``delay=0``、``has_b_frames=False``，一帧进一帧出；
- 强制关键帧：``frame.pict_type = av.video.frame.PictureType.I``（gop=300 时
  把第 3 帧设 I → 关键帧出现在 0 和 3，实测有效）；
- 填平面按 ``plane.line_size`` 分支：本机实测 ``line_size == width`` 无对齐
  填充，但代码必须兼容有 padding 的情况（跨平台稳健性）。

与 rater 的联动
---------------
``set_quality(crf)`` 通过**重建**编码器生效（libx264 的 crf 走 options，不支持
in-place 改），重建后强制下一帧关键帧——与 :meth:`set_resolution` 同一策略。
重建在下一帧 :meth:`encode` 时惰性执行，避免在 rater 回调线程里做重活。
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Final

import av
import av.video.frame
import cv2
import numpy as np
import numpy.typing as npt

from rdc.encoding.base import EncodedFrame, EncoderStats, VideoEncoder
from rdc.logging import get_logger
from rdc.protocol.messages import Codec

__all__ = [
    "H264Encoder",
    "DEFAULT_CRF",
    "CRF_RANGE",
]

_LOG = get_logger("encoding.h264")

#: 默认 CRF：§2.4 实测 4K 4.04Mbps/30fps 的均衡点，ultrafast 下文字仍清晰。
DEFAULT_CRF: Final = 28

#: CRF 合法区间（与 raters 一致；§2.4 标定范围外无意义）。
CRF_RANGE: Final = (18, 45)

#: thread_count 下限（硬约束 2：实测 1→65.8ms、2→47.5ms 都破 40ms 线）。
_MIN_THREADS: Final = 4

#: 默认关键帧间隔（秒）——任务书要求默认 2s。
DEFAULT_KEYFRAME_INTERVAL_SEC: Final = 2.0

#: 默认目标帧率（仅用于 gop 换算与 pts 步进，不控制实际节拍）。
DEFAULT_FPS: Final = 30.0


def _align_even(width: int, height: int) -> tuple[int, int]:
    """尺寸向下取偶（yuv420p 要求宽高均为偶数，硬件编码器同样受此约束）。

    mss 窗口模式抓到的分辨率常见奇数（如 955x910），直接送
    ``avcodec_open2`` 会炸 ExternalError（走查 bug 3），编码层自兜底：
    向下取偶，源帧多余的 1 行/1 列由 :meth:`H264Encoder._bgra_to_frame`
    裁掉。对齐后不足 2x2 无意义，抛 ValueError。
    """
    aligned = (width & ~1, height & ~1)
    if aligned[0] < 2 or aligned[1] < 2:
        raise ValueError(f"编码尺寸过小: {width}x{height}（偶数对齐后须 ≥2x2）")
    return aligned


def _hardware_options(codec_name: str, crf: int, preset: str) -> dict[str, str]:
    """硬件编码器的 AVDictionary 选项映射（保守最小集）。

    nvenc/qsv/amf 的选项命名与 x264 **不互通**（nvenc 的 preset 是 p1-p7，
    无 ``crf`` 键，对应概念是 ``rc``/``max_bitrate``/``quality``）。本机三类
    硬件 ``open()`` 全部失败（阶段 5 实测），任何映射都无法真机验证——
    故此处刻意**不发明参数**：gop/尺寸/线程走 AVCodecContext 字段（与实现
    无关），质量档留给 FFmpeg 默认。⚠️ 硬编真正启用（CI 或有卡机器）时，
    本函数连同 ``H264Encoder.set_quality`` 需按编码器逐个标定（阶段 10 跟进）。

    Args:
        codec_name: 硬件编码器名（信息用途，日志/未来分支）。
        crf: 期望质量（当前未映射，保留签名以便扩展）。
        preset: 期望 preset（当前未映射）。

    Returns:
        选项字典（当前恒为空）。
    """
    _ = codec_name, crf, preset
    return {}


class H264Encoder(VideoEncoder):
    """libx264 编码器（ultrafast + zerolatency，全帧 H.264）。

    Args:
        width: 初始帧宽（像素，须 >0；奇数向下取偶对齐，见 :func:`_align_even`）。
        height: 初始帧高（同上；编码输出与 EncodedFrame 报告的是对齐后尺寸）。
        crf: 初始质量（钳制到 :data:`CRF_RANGE`）。
        fps: 名义帧率，用于 gop 换算（``gop = keyframe_interval_sec * fps``）。
        keyframe_interval_sec: 关键帧最大间隔（秒）。实测 gop≥30 后带宽不再
            变化（§2.4：gop30/60/300 同为 7.85KB/帧），gop15 带宽 +69%，
            故默认 2s×30fps=60 是稳健折中。
        thread_count: x264 线程数，钳制到 ``≥4``（硬约束 2）。
        preset: x264 preset，默认 ultrafast（延迟优先）。

    Raises:
        ValueError: 尺寸非法（≤0，或偶数对齐后不足 2x2，如 1x1）。
        RuntimeError: libx264 不可用（正常发行版不会出现；探测请用
            :mod:`rdc.encoding.hw_probe`，构造失败会在首帧编码时抛错）。

    示例:
        >>> enc = H264Encoder(64, 64)          # doctest: +SKIP
        >>> out = enc.encode(np.zeros((64, 64, 4), np.uint8))  # doctest: +SKIP
        >>> out.is_keyframe                     # 首帧必为关键帧  # doctest: +SKIP
        True
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        crf: int = DEFAULT_CRF,
        fps: float = DEFAULT_FPS,
        keyframe_interval_sec: float = DEFAULT_KEYFRAME_INTERVAL_SEC,
        thread_count: int = 0,
        preset: str = "ultrafast",
        codec_name: str = "libx264",
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"编码尺寸非法: {width}x{height}")
        if fps <= 0:
            raise ValueError(f"fps 必须 >0，实得 {fps}")
        if not codec_name:
            raise ValueError("codec_name 不能为空")
        self._width, self._height = _align_even(width, height)
        self._crf = max(CRF_RANGE[0], min(CRF_RANGE[1], crf))
        self._fps = fps
        self._preset = preset
        #: 编码实现名（libx264 或 hw_probe.best_encoder() 的硬件名）。
        #: 硬编选项集与 x264 不同（无 crf/preset 同名键），_open_codec 按名分支。
        self._codec_name = codec_name
        self._gop = max(1, int(round(keyframe_interval_sec * fps)))
        # 硬约束 2：显式下限 4；0（auto）也钳到 4，保证跨机器延迟可预期
        self._threads = max(_MIN_THREADS, thread_count)

        self._ctx: Any | None = None
        self._pending_rebuild = False
        self._force_next_keyframe = False
        self._pts = 0
        self._closed = False

        # 色彩转换预分配缓冲（4K I420 = 24MB；避免每帧分配）
        self._i420: npt.NDArray[np.uint8] | None = None

        # 统计
        self._frames = 0
        self._keyframes = 0
        self._total_bytes = 0
        self._last_ms = 0.0
        self._sum_ms = 0.0
        self._max_ms = 0.0

        self._open_codec()

    # -- VideoEncoder 协议实现 --------------------------------------------------

    @property
    def codec(self) -> Codec:
        """:attr:`Codec.H264`。"""
        return Codec.H264

    def encode(
        self,
        frame: npt.NDArray[np.uint8],
        *,
        force_keyframe: bool = False,
        pts: int | None = None,
    ) -> EncodedFrame:
        """BGRA 帧 → H.264 Annex-B 分片。

        Args:
            frame: ``(H, W, 4)`` uint8 BGRA（采集层原生格式）。
            force_keyframe: 本帧强制 I 帧（或此前 :meth:`request_keyframe`
                / 重建过编码器）。
            pts: 显式时间戳；``None`` 时内部自增（每帧 +1）。

        Returns:
            :class:`EncodedFrame`。zerolatency 下保证每帧恰好产出一个包。

        Raises:
            RuntimeError: 编码器已关闭，或 x264 拒绝该尺寸（<16x16 等）。
            ValueError: 帧形状/dtype 非法。
        """
        if self._closed:
            raise RuntimeError("H264Encoder 已关闭")
        if frame.ndim != 3 or frame.shape[2] != 4 or frame.dtype != np.uint8:
            raise ValueError(f"需要 (H, W, 4) uint8 BGRA，实得 {frame.shape} {frame.dtype}")
        if self._pending_rebuild:
            self._rebuild(self._width, self._height)
        height, width = int(frame.shape[0]), int(frame.shape[1])
        aligned = _align_even(width, height)
        if aligned != (self._width, self._height):
            # 采集分辨率变了但没走 set_resolution：自动跟随，首帧必关键帧
            # （先对齐再比较：源持续奇数尺寸时不会每帧误判重建）
            self._rebuild(*aligned)

        t0 = time.perf_counter()
        ctx = self._ctx
        assert ctx is not None  # nosec B101 -- _open_codec 成功后恒成立（内部不变量，非外部输入校验）
        video_frame = self._bgra_to_frame(frame)
        if force_keyframe or self._force_next_keyframe:
            video_frame.pict_type = av.video.frame.PictureType.I
            self._force_next_keyframe = False
        if pts is not None:
            if pts < 0:
                raise ValueError(f"pts 不能为负: {pts}")
            self._pts = pts
        video_frame.pts = self._pts
        self._pts += 1

        packets = ctx.encode(video_frame)
        data = b"".join(bytes(p) for p in packets)
        is_key = any(getattr(p, "is_keyframe", False) for p in packets)
        # 防御：非空包但无关键帧标志（x264 偶发）时按帧号兜底
        if not is_key and self._frames == 0 and data:
            is_key = True
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._record(elapsed_ms, len(data), is_key)
        return EncodedFrame(
            data=data,
            is_keyframe=is_key,
            pts=self._pts - 1,
            width=self._width,
            height=self._height,
            quality=self._crf,
            elapsed_ms=elapsed_ms,
        )

    def request_keyframe(self) -> None:
        """下一帧转关键帧（客户端新加入/解码错误恢复时由会话层调用）。"""
        self._force_next_keyframe = True

    def set_quality(self, quality: int) -> None:
        """设置 CRF（钳到 :data:`CRF_RANGE`）。

        值变化时标记**惰性重建**：下一帧 :meth:`encode` 里执行，重建后首帧
        自动关键帧。不在本方法内做重建，避免 rater 高频调用路径上的抖动。
        """
        clamped = max(CRF_RANGE[0], min(CRF_RANGE[1], int(quality)))
        if clamped == self._crf:
            return
        self._crf = clamped
        self._pending_rebuild = True
        self._force_next_keyframe = True

    def set_resolution(self, width: int, height: int) -> None:
        """切换分辨率：立即重建编码器（libx264 不支持 in-place 改尺寸）。

        奇数尺寸向下取偶后比较/重建（同 :meth:`__init__` 口径）。
        """
        if width <= 0 or height <= 0:
            raise ValueError(f"编码尺寸非法: {width}x{height}")
        aligned = _align_even(width, height)
        if aligned == (self._width, self._height) and self._ctx is not None:
            return
        self._rebuild(*aligned)

    def stats(self) -> EncoderStats:
        """统计快照（``quality`` 为当前 CRF）。"""
        return EncoderStats(
            frames=self._frames,
            keyframes=self._keyframes,
            total_bytes=self._total_bytes,
            last_ms=self._last_ms,
            avg_ms=self._sum_ms / self._frames if self._frames else 0.0,
            max_ms=self._max_ms,
            quality=self._crf,
        )

    def close(self) -> None:
        """释放编码器（幂等；仅丢引用——PyAV 无 close API，见硬约束 3）。"""
        if self._closed:
            return
        self._closed = True
        self._ctx = None  # 引用回收即释放（硬约束 3：不写 ctx.close()）
        self._i420 = None

    # -- 内部 -------------------------------------------------------------------

    def _open_codec(self) -> None:
        """创建并打开编码上下文（构造即 open，失败抛 RuntimeError）。

        ``codec_name == "libx264"`` 走阶段 5 已实测的 x264 参数组；其余
        （hw_probe 选出的硬件名）走 :func:`_hardware_options` 的通用映射。
        注意 hw_probe 教训：选项错误只会在 ``open()`` 时暴露，因此
        失败信息必须带编码器名，供调用方（pipeline）回退 libx264。
        """
        # ctx 标注 Any：av 存根对 create() 的 overload 只在字面量编码名时返回
        # VideoCodecContext；本类支持动态 codec_name（hw_probe 选型），落到
        # str 分支的基类声明缺 width/pix_fmt 等属性（阶段 5 D13 同类口径）。
        ctx: Any = av.CodecContext.create(self._codec_name, "w")
        ctx.width = self._width
        ctx.height = self._height
        ctx.pix_fmt = "yuv420p"
        ctx.gop_size = self._gop
        ctx.thread_count = self._threads  # 硬约束 2
        if self._codec_name == "libx264":
            ctx.options = {
                "preset": self._preset,
                "tune": "zerolatency",
                "crf": str(self._crf),
            }
        else:
            ctx.options = _hardware_options(self._codec_name, self._crf, self._preset)
        try:
            ctx.open()
        except Exception as exc:
            raise RuntimeError(
                f"{self._codec_name} 打开失败 ({self._width}x{self._height}): {exc}"
            ) from exc
        self._ctx = ctx
        self._pending_rebuild = False
        _LOG.debug(
            "H.264 编码器就绪",
            codec=self._codec_name,
            width=self._width,
            height=self._height,
            crf=self._crf,
            gop=self._gop,
            threads=self._threads,
        )

    def _rebuild(self, width: int, height: int) -> None:
        """丢旧上下文 → 建新（顺序不可反：新 ctx 建失败时旧的仍可用）。"""
        self._width, self._height = width, height
        self._i420 = None
        self._open_codec()  # 内含 _pending_rebuild=False + open 失败抛错
        self._force_next_keyframe = True

    def _bgra_to_frame(self, frame: npt.NDArray[np.uint8]) -> Any:
        """P2 色彩路径：cv2 BGRA→I420（预分配 dst）→ 直写 yuv420p 平面。

        实测 1080p 合计 0.29ms、4K 2.6ms，比 from_ndarray 的 6.3/23.6ms 快一个
        数量级（§2.2）。平面按 ``line_size`` 分支：无 padding 走整行直写，
        有 padding 逐行写入目标缓冲的带距视图。
        """
        width, height = self._width, self._height
        if frame.shape[0] != height or frame.shape[1] != width:
            # 奇数源帧向下取偶后多出的 1 行/1 列在此裁掉（见 _align_even）
            frame = frame[:height, :width]
        src = frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)

        # cv2.COLOR_BGRA2YUV_I420 输出扁平 (h*3//2 * w) uint8：Y 平面 + U + V
        flat_size = width * height * 3 // 2
        if self._i420 is None or self._i420.size != flat_size:
            self._i420 = np.empty(flat_size, dtype=np.uint8)
        # dst 必须是可整形为 (h*3//2, w) 的视图（cv2 拒绝非连续布局，§2.6 坑 4）
        dst = self._i420.reshape(height * 3 // 2, width)
        cv2.cvtColor(src, cv2.COLOR_BGRA2YUV_I420, dst=dst)

        video_frame = av.VideoFrame(width, height, "yuv420p")
        planes = video_frame.planes
        y_size = width * height
        # I420 扁平布局：[Y(h*w) | U(h*w/4) | V(h*w/4)]
        self._fill_plane(planes[0], self._i420[:y_size].reshape(height, width), width, height)
        chroma_w, chroma_h = width // 2, height // 2
        u = self._i420[y_size : y_size + chroma_w * chroma_h].reshape(chroma_h, chroma_w)
        v = self._i420[y_size + chroma_w * chroma_h :].reshape(chroma_h, chroma_w)
        self._fill_plane(planes[1], u, chroma_w, chroma_h)
        self._fill_plane(planes[2], v, chroma_w, chroma_h)
        return video_frame

    @staticmethod
    def _fill_plane(plane: Any, rows: npt.NDArray[np.uint8], row_bytes: int, height: int) -> None:
        """把 ``(h, row_bytes)`` 源写进平面；``line_size > row_bytes`` 时逐行带距写。"""
        line_size = int(plane.line_size)
        if line_size == row_bytes:
            # 本机实测走此分支：无对齐填充，整块更新（最快路径）
            plane.update(rows.tobytes())
            return
        array_type = ctypes.c_uint8 * (line_size * height)
        target = array_type.from_address(plane.buffer_ptr)
        view = np.frombuffer(target, dtype=np.uint8).reshape(height, line_size)
        view[:, :row_bytes] = rows

    def _record(self, elapsed_ms: float, nbytes: int, is_key: bool) -> None:
        """更新统计。"""
        self._frames += 1
        self._last_ms = elapsed_ms
        self._sum_ms += elapsed_ms
        if elapsed_ms > self._max_ms:
            self._max_ms = elapsed_ms
        self._total_bytes += nbytes
        if is_key:
            self._keyframes += 1

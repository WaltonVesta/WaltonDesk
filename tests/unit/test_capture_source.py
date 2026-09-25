"""采集源抽象层（rdc.capture.source）单元测试——批 A smoke 的 pytest 固化。"""

from __future__ import annotations

import numpy as np
import pytest

from rdc.capture.monitor import VIRTUAL_SCREEN_INDEX, MonitorInfo
from rdc.capture.source import ArrayScreenSource, CaptureFrame, bgra_view


class TestBgraView:
    """零拷贝 BGRA 视图工厂。"""

    def test_shape_and_dtype(self) -> None:
        buf = bytes(np.arange(8 * 4 * 4, dtype=np.uint8).tobytes())
        view = bgra_view(buf, 8, 4)
        assert view.shape == (4, 8, 4)
        assert view.dtype == np.uint8

    def test_zero_copy(self) -> None:
        buf = bytes(np.arange(64 * 32 * 4, dtype=np.uint8).tobytes())
        view = bgra_view(buf, 64, 32)
        assert not view.flags["WRITEABLE"]  # bytes → 只读共享内存
        assert view.base is not None  # 未复制（共享底层缓冲）
        mutable = bytearray(64 * 32 * 4)
        view2 = bgra_view(mutable, 64, 32)
        view2[0, 0, 0] = 7  # bytearray → 可写共享
        assert mutable[0] == 7

    @pytest.mark.parametrize(
        ("width", "height", "size"),
        [(10, 5, 10 * 5 * 4 - 1), (10, 5, 10 * 5 * 4 + 4), (16, 9, 500)],
    )
    def test_length_mismatch_raises(self, width: int, height: int, size: int) -> None:
        with pytest.raises(ValueError, match="BGRA 缓冲长度不符"):
            bgra_view(bytes(size), width, height)

    def test_accepts_memoryview(self) -> None:
        arr = np.zeros((3, 4, 4), np.uint8)
        view = bgra_view(memoryview(arr.tobytes()), 4, 3)
        assert view.shape == (3, 4, 4)


class TestCaptureFrame:
    """帧值对象的派生属性。"""

    def make(self, w: int = 32, h: int = 16) -> CaptureFrame:
        return CaptureFrame(
            data=np.zeros((h, w, 4), np.uint8), timestamp=1.0, monitor_index=1, seq=0
        )

    def test_derived_props(self) -> None:
        frame = self.make(32, 16)
        assert frame.width == 32 and frame.height == 16
        assert frame.size == (32, 16)
        assert frame.nbytes == 32 * 16 * 4


class TestArrayScreenSource:
    """测试/基准专用的内存采集源（smoke §1.5 的 9 项断言固化）。"""

    def test_defaults_synthesize_frame(self) -> None:
        src = ArrayScreenSource()
        frame = src.capture()
        assert frame.size == (1920, 1080)
        assert src.frame_size == (1920, 1080)
        assert src.current_monitor.width == 1920

    def test_size_seq_monitor_index(self) -> None:
        frames = [np.full((8, 16, 4), i, np.uint8) for i in range(3)]
        src = ArrayScreenSource(frames)
        got = [src.capture() for _ in range(3)]
        assert [f.seq for f in got] == [0, 1, 2]
        assert all(f.monitor_index == 0 for f in got)  # 默认虚拟全屏（selector 语义）
        assert got[2].data[0, 0, 0] == 2
        assert src.current_monitor.index == VIRTUAL_SCREEN_INDEX

    def test_stats_counts(self) -> None:
        src = ArrayScreenSource([np.zeros((4, 4, 4), np.uint8)])
        for _ in range(7):
            src.capture()
        st = src.stats()
        assert st.captured == 7
        assert st.failed == 0
        assert st.max_capture_ms >= st.avg_capture_ms >= 0

    def test_close_then_raise(self) -> None:
        src = ArrayScreenSource([np.zeros((4, 4, 4), np.uint8)])
        src.close()
        src.close()  # 幂等
        assert src.is_closed
        with pytest.raises(RuntimeError, match="已关闭"):
            src.capture()
        with pytest.raises(RuntimeError, match="已关闭"):
            src.capture_into(np.zeros((4, 4, 4), np.uint8))

    def test_mixed_sizes_rejected(self) -> None:
        with pytest.raises(ValueError, match="尺寸一致"):
            ArrayScreenSource([np.zeros((4, 4, 4), np.uint8), np.zeros((5, 5, 4), np.uint8)])

    def test_loop_wrap_and_stop(self) -> None:
        frames = [np.full((4, 4, 4), i, np.uint8) for i in range(2)]
        looping = ArrayScreenSource(frames, loop=True)
        assert [looping.capture().data[0, 0, 0] for _ in range(4)] == [0, 1, 0, 1]
        stopped = ArrayScreenSource(frames, loop=False)
        assert [stopped.capture().data[0, 0, 0] for _ in range(4)] == [0, 1, 1, 1]

    def test_capture_into_validation(self) -> None:
        src = ArrayScreenSource([np.full((4, 4, 4), 9, np.uint8)])
        dst = np.zeros((4, 4, 4), np.uint8)
        frame = src.capture_into(dst)
        assert frame.data is dst and dst[3, 3, 0] == 9
        with pytest.raises(ValueError, match="尺寸"):
            src.capture_into(np.zeros((8, 8, 4), np.uint8))
        with pytest.raises(ValueError, match="dtype"):
            src.capture_into(np.zeros((4, 4, 4), np.uint16))

    def test_seek_and_push(self) -> None:
        src = ArrayScreenSource([np.zeros((4, 4, 4), np.uint8)])
        with pytest.raises(IndexError):
            src.seek(5)
        src.push_frame(np.ones((4, 4, 4), np.uint8))
        with pytest.raises(ValueError, match="尺寸"):
            src.push_frame(np.zeros((9, 9, 4), np.uint8))
        src.seek(1)
        assert src.position == 1
        assert src.capture().data[0, 0, 0] == 1

    def test_monitors_override(self) -> None:
        from rdc.capture.monitor import MonitorNotFoundError

        mons = [
            MonitorInfo(index=1, left=0, top=0, width=10, height=10, is_primary=True),
            MonitorInfo(index=2, left=10, top=0, width=10, height=10),
        ]
        src = ArrayScreenSource([np.zeros((10, 10, 4), np.uint8)], monitors=mons)
        assert [m.index for m in src.monitors()] == [1, 2]
        src.set_monitor(2)
        assert src.capture().monitor_index == 2
        with pytest.raises(MonitorNotFoundError):
            src.set_monitor(9)

    def test_timestamp_progression(self) -> None:
        src = ArrayScreenSource([np.zeros((4, 4, 4), np.uint8)] * 3, timestamp_step=0.5)
        stamps = [src.capture().timestamp for _ in range(3)]
        assert stamps[1] - stamps[0] == pytest.approx(0.5)

    def test_protocol_satisfaction(self) -> None:
        from rdc.capture.source import ScreenSource

        assert isinstance(ArrayScreenSource(), ScreenSource)

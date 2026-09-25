"""mss 采集源（rdc.capture.mss_source）单元测试——**不依赖真实屏幕**。

策略：monkeypatch 模块级 ``mss`` 名为假对象。三条硬约束中可在单测锁死的：
① 只许 ``mss.MSS()``——假模块的 ``mss`` 属性一旦被调用直接 AssertionError；
③ 失败必抛 ScreenCaptureError——构造/grab/枚举三类故障各测一遍。
②双缓冲交替——用"第 3 帧不得覆盖第 2 帧缓冲"的对象身份断言锁死。
"""

from __future__ import annotations

import types
from typing import Any

import numpy as np
import pytest

import rdc.capture.mss_source as mss_module
from rdc.capture.monitor import MonitorNotFoundError
from rdc.capture.mss_source import MssScreenSource, ScreenCapturer
from rdc.exceptions import ScreenCaptureError

# 假显示器布局：两块 320x240 横排 → 虚拟全屏 640x240
_MON1 = {"left": 0, "top": 0, "width": 320, "height": 240, "is_primary": True, "name": "FAKE-1", "unique_id": "id-1"}
_MON2 = {"left": 320, "top": 0, "width": 320, "height": 240, "is_primary": False, "name": "FAKE-2", "unique_id": "id-2"}
_VIRTUAL = {"left": 0, "top": 0, "width": 640, "height": 240}


class FakeShot:
    """仿 mss.ScreenShot：只提供 bgra/width/height（v4 曾用的 .rgb/.pixels 不提供）。"""

    def __init__(self, width: int, height: int, value: int) -> None:
        arr = np.full((height, width, 4), value, dtype=np.uint8)
        arr[:, :, 3] = 255
        self.width = width
        self.height = height
        self.bgra: bytes = arr.tobytes()


class FakeMSS:
    """仿 mss.MSS()：记录调用，grab 内容逐次变化。"""

    def __init__(self) -> None:
        self.monitors = [_VIRTUAL, dict(_MON1), dict(_MON2)]
        self.closed = False
        self.grab_regions: list[dict[str, int]] = []
        self._value = 7
        self.force_size: tuple[int, int] | None = None  # 模拟"回传尺寸≠请求尺寸"

    def grab(self, region: dict[str, int]) -> FakeShot:
        if self.closed:
            raise RuntimeError("grab after close")
        self.grab_regions.append(dict(region))
        w, h = self.force_size or (region["width"], region["height"])
        self._value = self._value * 31 % 250 + 1
        return FakeShot(w, h, self._value)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把模块的 mss 换成假模块；返回 dict 供断言拿到实例。"""

    created: dict[str, Any] = {}

    def _mss_ctor() -> FakeMSS:
        backend = FakeMSS()
        created["backend"] = backend
        return backend

    def _forbidden_mss_func(*a: Any, **k: Any) -> Any:  # 硬约束 ①
        raise AssertionError("禁止调用已废弃的 mss.mss()（硬约束①：必须 mss.MSS()）")

    monkeypatch.setattr(
        mss_module, "mss", types.SimpleNamespace(MSS=_mss_ctor, mss=_forbidden_mss_func)
    )
    return created


class TestConstruction:
    """构造期：后端初始化与显示器枚举。"""

    def test_uses_mss_MSS_not_deprecated(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()  # 构造走假 MSS()；若走 mss.mss() 直接 AssertionError
        assert isinstance(fake_backend["backend"], FakeMSS)
        src.close()

    def test_default_selects_virtual_screen(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        assert src.current_monitor.index == 0
        assert src.frame_size == (640, 240)
        src.close()

    def test_monitors_excludes_virtual(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        mons = src.monitors()
        assert [m.index for m in mons] == [1, 2]
        assert mons[0].is_primary and mons[0].unique_id == "id-1"
        src.close()

    def test_backend_init_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise OSError("no display server")

        monkeypatch.setattr(mss_module, "mss", types.SimpleNamespace(MSS=_boom))
        with pytest.raises(ScreenCaptureError, match="初始化"):
            MssScreenSource()

    def test_no_monitors_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Headless:
            monitors = [_VIRTUAL]  # 只有虚拟屏、无物理屏 → 枚举结果为空

            def grab(self, region: dict[str, int]) -> FakeShot:
                raise AssertionError("不该被调用")

            def close(self) -> None:
                pass

        monkeypatch.setattr(mss_module, "mss", types.SimpleNamespace(MSS=lambda: Headless()))
        with pytest.raises(ScreenCaptureError, match="没有可用显示器"):
            MssScreenSource()

    def test_enum_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class BadEnum:
            @property
            def monitors(self) -> Any:
                raise RuntimeError("enum failed")

            def close(self) -> None:
                pass

        monkeypatch.setattr(mss_module, "mss", types.SimpleNamespace(MSS=lambda: BadEnum()))
        with pytest.raises(ScreenCaptureError, match="枚举显示器失败"):
            MssScreenSource()

    def test_bad_buffer_count(self, fake_backend: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="buffer_count"):
            MssScreenSource(buffer_count=0)


class TestCapture:
    """capture/capture_into：双缓冲、零拷贝路径、故障传播。"""

    def test_double_buffer_alternation(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        f1 = src.capture()
        f2 = src.capture()
        # 硬约束②核心不变量：第 3 帧**绝不**写第 2 帧所在缓冲
        snapshot = f2.data.copy()
        f3 = src.capture()
        assert np.array_equal(f2.data, snapshot), "双缓冲被破坏：新帧覆盖了差分器持有的前帧"
        assert f3.data is f1.data  # 乒乓回到第一块
        assert f1.data is not f2.data
        src.close()

    def test_capture_region_follows_monitor(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        src.capture()
        assert fake_backend["backend"].grab_regions[-1] == {"left": 0, "top": 0, "width": 640, "height": 240}
        src.set_monitor(2)
        src.capture()
        assert fake_backend["backend"].grab_regions[-1] == {"left": 320, "top": 0, "width": 320, "height": 240}
        src.close()

    def test_capture_is_copy_not_view(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        frame = src.capture()
        # 返回的是源自己的持久缓冲，而非 shot 字节视图（写权限 + 生命周期安全）
        assert frame.data.flags["WRITEABLE"]
        assert frame.nbytes == 640 * 240 * 4
        src.close()

    def test_capture_into(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        dst = np.zeros((240, 640, 4), dtype=np.uint8)
        frame = src.capture_into(dst)
        assert frame.data is dst
        assert dst[:, :, :3].min() > 0  # 真的写入了内容
        with pytest.raises(ValueError, match="尺寸"):
            src.capture_into(np.zeros((100, 100, 4), dtype=np.uint8))
        with pytest.raises(ValueError, match="dtype"):
            src.capture_into(np.zeros((240, 640, 4), dtype=np.uint16))
        src.close()

    def test_resolution_change_reallocates(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        src.capture()
        fake_backend["backend"].force_size = (100, 80)  # 模拟改缩放：回传尺寸≠记录尺寸
        frame = src.capture()
        assert frame.size == (100, 80)
        # capture_into 则明确报错（目标缓冲尺寸不再匹配）
        with pytest.raises(ScreenCaptureError, match="分辨率"):
            src.capture_into(np.zeros((240, 640, 4), dtype=np.uint8))
        src.close()

    def test_grab_failure_raises_and_counts(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()

        def _boom(region: dict[str, int]) -> Any:
            raise OSError("display off")

        fake_backend["backend"].grab = _boom  # type: ignore[method-assign]
        with pytest.raises(ScreenCaptureError, match="屏幕采集失败"):
            src.capture()
        assert src.stats().failed == 1
        src.close()

    def test_capture_after_close(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        src.close()
        src.close()  # 幂等
        with pytest.raises(ScreenCaptureError, match="已关闭"):
            src.capture()
        with pytest.raises(ScreenCaptureError, match="已关闭"):
            _ = src.frame_size
        with pytest.raises(ScreenCaptureError, match="已关闭"):
            src.monitors()


class TestStatsAndSwitch:
    """统计与切屏。"""

    def test_stats(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        for _ in range(5):
            src.capture()
        st = src.stats()
        assert st.captured == 5
        assert st.failed == 0
        assert st.last_capture_ms >= 0
        assert 0 < st.avg_capture_ms <= st.max_capture_ms
        src.close()

    def test_set_monitor_unknown(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        with pytest.raises(MonitorNotFoundError):
            src.set_monitor(99)
        src.close()

    def test_seq_advances(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        assert [src.capture().seq for _ in range(3)] == [0, 1, 2]
        src.close()

    def test_backend_close_called(self, fake_backend: dict[str, Any]) -> None:
        src = MssScreenSource()
        src.close()
        assert fake_backend["backend"].closed


class TestV4Wrapper:
    """v4 兼容外壳 ScreenCapturer（service.py 的 import 面）。"""

    def test_capture_returns_pil_rgb(self, fake_backend: dict[str, Any]) -> None:
        cap = ScreenCapturer()
        img = cap.capture()
        assert img.mode == "RGB" and img.size == (320, 240)  # monitor=1 默认物理屏
        cap.close()

    def test_get_screen_size(self, fake_backend: dict[str, Any]) -> None:
        cap = ScreenCapturer()
        assert cap.get_screen_size() == (320, 240)
        # 关闭后触发异常路径 → v4 历史兜底 (1920, 1080)（仅此外壳保留的行为）
        cap.close()
        assert cap.get_screen_size() == (1920, 1080)

    def test_get_monitors_v4_dict(self, fake_backend: dict[str, Any]) -> None:
        cap = ScreenCapturer()
        mons = cap.get_monitors()
        assert len(mons) == 2
        assert mons[0] == {"left": 0, "top": 0, "width": 320, "height": 240}
        cap.close()

    def test_set_monitor_semantics(self, fake_backend: dict[str, Any]) -> None:
        cap = ScreenCapturer()
        cap.set_monitor(2)
        assert cap.monitor == 2
        assert cap.get_screen_size() == (320, 240)
        cap.set_monitor(99)  # 越界：v4 语义静默、保持原屏（绝不抛错）
        assert cap.monitor == 2
        cap.set_monitor(0)  # 非法：v4 语义忽略
        assert cap.monitor == 2
        cap.close()

    def test_monitor_floor_at_one(self, fake_backend: dict[str, Any]) -> None:
        cap = ScreenCapturer(monitor=-5)
        assert cap.monitor == 1
        cap.close()

"""硬件编码器探测（rdc.encoding.hw_probe）单元测试。

断言写"逻辑正确性"不锁死具体失败原因（§2.5：换机器结果会变）：
- mock 三层各一（未收录/构造失败/open 失败/成功）
- 本机真实探测：只断言"libx264 必可用、best 必返回可用名"这类不变量
"""

from __future__ import annotations

from typing import Any

import pytest

import rdc.encoding.hw_probe as probe_module
from rdc.encoding.hw_probe import (
    HARDWARE_CANDIDATES,
    SOFTWARE_FALLBACK,
    ProbeOutcome,
    ProbeResult,
    best_encoder,
    probe_all,
    probe_encoder,
)


class _FakeCtx:
    """构造成功；open 行为由 open_ok 决定。"""

    def __init__(self, *, open_ok: bool, exc: Exception | None = None) -> None:
        self._open_ok = open_ok
        self._exc = exc or RuntimeError("open boom")
        self.open_calls = 0
        self.width = self.height = 0
        self.pix_fmt = ""

    def open(self) -> None:
        self.open_calls += 1
        if not self._open_ok:
            raise self._exc


class _CodecContextFactory:
    """仿 av.CodecContext（只提供 create 类方法语义）。"""

    def __init__(self, owner: _FakeCodecModule) -> None:
        self._owner = owner

    def create(self, name: str, mode: str) -> Any:  # noqa: ARG002 - mode 恒 "w"
        return self._owner._make(name)


class _FakeCodecModule:
    """替换 av：可编排名单、构造失败集合、open 失败集合。"""

    def __init__(
        self,
        *,
        available: set[str],
        construct_fail: set[str] = frozenset(),
        open_fail: set[str] = frozenset(),
    ) -> None:
        self.codecs_available = available
        self._construct_fail = set(construct_fail)
        self._open_fail = set(open_fail)
        self.constructed: list[str] = []
        self.CodecContext = _CodecContextFactory(self)

    def _make(self, name: str) -> Any:
        self.constructed.append(name)
        if name in self._construct_fail:
            raise ValueError(f"construct boom {name}")
        return _FakeCtx(open_ok=name not in self._open_fail)


@pytest.fixture
def fake_av(monkeypatch: pytest.MonkeyPatch) -> Any:
    holder: dict[str, Any] = {}

    def install(**kw: Any) -> Any:
        fake = _FakeCodecModule(**kw)
        monkeypatch.setattr(probe_module, "av", fake)
        holder["fake"] = fake
        return fake

    return install


class TestProbeOutcomeLayers:
    """三层结论的判定逻辑。"""

    def test_unlisted_when_name_absent(self, fake_av: Any) -> None:
        fake = fake_av(available=set())
        r = probe_encoder("h264_nvenc")
        assert r.outcome is ProbeOutcome.UNLISTED
        assert not r.available
        assert fake.constructed == []  # 名单检查不过，绝不尝试构造

    def test_construct_failed(self, fake_av: Any) -> None:
        fake_av(available={"enc"}, construct_fail={"enc"})
        r = probe_encoder("enc")
        assert r.outcome is ProbeOutcome.CONSTRUCT_FAILED
        assert "construct boom" in r.error

    def test_open_failed_is_the_subtle_one(self, fake_av: Any) -> None:
        """构造成功但 open 失败——必须判不可用（§2.5 的核心教训）。"""
        fake_av(available={"enc"}, open_fail={"enc"})
        r = probe_encoder("enc")
        assert r.outcome is ProbeOutcome.OPEN_FAILED
        assert not r.available
        assert "open boom" in r.error

    def test_available_requires_open_success(self, fake_av: Any) -> None:
        fake_av(available={"enc"})
        r = probe_encoder("enc")
        assert r.outcome is ProbeOutcome.AVAILABLE
        assert r.available
        assert r.error == ""


class TestProbeAllAndBest:
    """批量探测与选型。"""

    def test_probe_all_covers_candidates_plus_fallback(self, fake_av: Any) -> None:
        fake_av(available=set(HARDWARE_CANDIDATES) | {SOFTWARE_FALLBACK})
        results = probe_all()
        names = [r.name for r in results]
        assert names == [*HARDWARE_CANDIDATES, SOFTWARE_FALLBACK]

    def test_probe_all_all_open_fail(self, fake_av: Any) -> None:
        fake_av(
            available=set(HARDWARE_CANDIDATES) | {SOFTWARE_FALLBACK},
            open_fail=set(HARDWARE_CANDIDATES) | {SOFTWARE_FALLBACK},
        )
        assert all(not r.available for r in probe_all())

    def test_best_prefers_first_available_hardware(self, fake_av: Any) -> None:
        fake = fake_av(
            available=set(HARDWARE_CANDIDATES) | {SOFTWARE_FALLBACK},
            open_fail=set(HARDWARE_CANDIDATES) - {"h264_qsv"},  # 只有 QSV 可用
        )
        assert best_encoder() == "h264_qsv"
        assert "h264_qsv" in fake.constructed

    def test_best_falls_back_to_software(self, fake_av: Any) -> None:
        fake_av(available={SOFTWARE_FALLBACK}, construct_fail={SOFTWARE_FALLBACK})
        # 连软编都构造失败：仍返回兜底名（构造它的责任在 H264Encoder）
        assert best_encoder() == SOFTWARE_FALLBACK

    def test_best_skips_hardware_when_none_open(self, fake_av: Any) -> None:
        fake_av(
            available=set(HARDWARE_CANDIDATES) | {SOFTWARE_FALLBACK},
            open_fail=set(HARDWARE_CANDIDATES),
        )
        assert best_encoder() == SOFTWARE_FALLBACK

    def test_reuses_provided_results(self, fake_av: Any) -> None:
        fake = fake_av(available={SOFTWARE_FALLBACK}, construct_fail={SOFTWARE_FALLBACK})
        pre = [ProbeResult("x", ProbeOutcome.AVAILABLE)]
        assert best_encoder(results=pre) == "x"
        assert fake.constructed == []  # 复用结果时不再探测


class TestLocalRealProbe:
    """本机真实探测（不 mock）——只断言不变量，不锁死失败原因。"""

    def test_libx264_truly_available(self) -> None:
        r = probe_encoder(SOFTWARE_FALLBACK)
        assert r.available, f"本机 libx264 应可用：{r.outcome.name} {r.error}"

    def test_garbage_name_unlisted(self) -> None:
        assert probe_encoder("no_such_codec_xyz").outcome is ProbeOutcome.UNLISTED

    def test_probe_all_shape_and_determinism(self) -> None:
        results = probe_all()
        assert len(results) == len(HARDWARE_CANDIDATES) + 1
        again = {r.name: r.outcome for r in probe_all()}
        assert {r.name: r.outcome for r in results} == again  # 探测可重复

    def test_best_returns_something_usable(self) -> None:
        name = best_encoder()
        assert probe_encoder(name).available or name == SOFTWARE_FALLBACK

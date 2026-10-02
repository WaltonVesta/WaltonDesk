"""server/banlist 单元测试（阶段 9.2）：假时钟全确定性，零 sleep。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdc.server.banlist import Banlist


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def file_path(tmp_path: Path) -> Path:
    return tmp_path / "ban" / "banlist.json"


def make_banlist(
    clock: FakeClock, file_path: Path, **kw: object
) -> Banlist:
    opts: dict = {
        "path": file_path,
        "max_attempts": 3,
        "ban_duration": 300.0,
        "failure_window_s": 60.0,
        "clock": clock,
    }
    opts.update(kw)  # type: ignore[arg-type]
    return Banlist(**opts)  # type: ignore[arg-type]


class TestValidation:
    def test_max_attempts_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            Banlist(max_attempts=0)

    def test_non_positive_ban_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="ban_duration"):
            Banlist(ban_duration=0)

    def test_non_positive_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_window_s"):
            Banlist(failure_window_s=-1)

    def test_max_records_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_records"):
            Banlist(max_records=0)


class TestSlidingWindow:
    def test_below_threshold_not_banned(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        assert bl.record_failure("1.2.3.4") is False
        assert bl.record_failure("1.2.3.4") is False
        assert bl.is_banned("1.2.3.4") is False
        assert bl.failure_count("1.2.3.4") == 2

    def test_third_failure_within_window_bans(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        clock.advance(10)
        bl.record_failure("1.2.3.4")
        clock.advance(10)
        assert bl.record_failure("1.2.3.4") is True
        assert bl.is_banned("1.2.3.4") is True

    def test_old_failures_slide_out_of_window(
        self, clock: FakeClock, file_path: Path
    ) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        clock.advance(59)
        bl.record_failure("1.2.3.4")
        clock.advance(59)  # 第一条已滑出 60s 窗口
        assert bl.failure_count("1.2.3.4") == 1
        assert bl.record_failure("1.2.3.4") is False  # 窗口内仅 2 次
        assert bl.is_banned("1.2.3.4") is False

    def test_boundary_exactly_window_width_prunes(
        self, clock: FakeClock, file_path: Path
    ) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        clock.advance(60.0)  # now - t0 == window → cutoff = now-60 = t0，stamp[0] < cutoff 为 False
        assert bl.failure_count("1.2.3.4") == 1  # 等号边界保留（严格 <）
        clock.advance(0.001)
        assert bl.failure_count("1.2.3.4") == 0

    def test_reset_clears_window_counts(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        bl.record_failure("1.2.3.4")
        bl.reset("1.2.3.4")
        assert bl.failure_count("1.2.3.4") == 0
        # 计数清零后重新累计：两次失败不触发
        bl.record_failure("1.2.3.4")
        bl.record_failure("1.2.3.4")
        assert bl.is_banned("1.2.3.4") is False

    def test_isolated_per_ip(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(2):
            bl.record_failure("1.1.1.1")
        assert bl.record_failure("2.2.2.2") is False
        assert bl.failure_count("2.2.2.2") == 1


class TestBanLifecycle:
    def test_ban_expires_and_autounbans(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(3):
            bl.record_failure("1.2.3.4")
        assert bl.is_banned("1.2.3.4") is True
        clock.advance(299.9)
        assert bl.is_banned("1.2.3.4") is True
        clock.advance(0.2)
        assert bl.is_banned("1.2.3.4") is False
        # 解封后计数也清空，重新开始
        assert bl.failure_count("1.2.3.4") == 0

    def test_remaining_seconds_counts_down(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        assert bl.remaining_seconds("9.9.9.9") == 0.0
        for _ in range(3):
            bl.record_failure("9.9.9.9")
        assert bl.remaining_seconds("9.9.9.9") == pytest.approx(300.0)
        clock.advance(100)
        assert bl.remaining_seconds("9.9.9.9") == pytest.approx(200.0)

    def test_cleanup_expired_batch(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            for _ in range(3):
                bl.record_failure(ip)
                clock.advance(1)  # 封禁起点：ip1=1002、ip2=1005、ip3=1008；循环后 now=1009
        clock.advance(293)  # now=1302：仅 ip1 满 300s（ip2=297s、ip3=294s）
        assert bl.cleanup_expired() == 1
        assert bl.is_banned("1.1.1.1") is False
        assert bl.is_banned("3.3.3.3") is True

    def test_unban_manual(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(3):
            bl.record_failure("1.2.3.4")
        bl.unban("1.2.3.4")
        assert bl.is_banned("1.2.3.4") is False

    def test_ban_idempotent_no_double_extend(
        self, clock: FakeClock, file_path: Path
    ) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(3):
            bl.record_failure("1.2.3.4")
        started = clock.now
        clock.advance(100)
        # 封禁期内再次失败不刷新封禁起点
        bl.record_failure("1.2.3.4")
        assert bl.snapshot()["ban_timestamps"]["1.2.3.4"] == started

    def test_failure_after_ban_stays_single(
        self, clock: FakeClock, file_path: Path
    ) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(5):
            bl.record_failure("1.2.3.4")
        assert bl.is_banned("1.2.3.4") is True
        assert len(bl.snapshot()["ban_timestamps"]) == 1


class TestPersistence:
    def test_roundtrip_reload(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        bl.record_failure("1.2.3.4")
        for _ in range(1):
            bl.record_failure("5.6.7.8")
        clock.advance(10)
        bl.record_failure("5.6.7.8")
        bl.record_failure("5.6.7.8")  # 触发封禁

        bl2 = make_banlist(clock, file_path)
        bl2.load()
        assert bl2.is_banned("5.6.7.8") is True
        assert bl2.failure_count("1.2.3.4") == 2

    def test_expired_bans_dropped_on_load(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        for _ in range(3):
            bl.record_failure("1.2.3.4")
        clock.advance(301)
        bl2 = make_banlist(clock, file_path)
        bl2.load()
        assert bl2.is_banned("1.2.3.4") is False
        assert bl2.snapshot()["ban_timestamps"] == {}

    def test_missing_file_loads_empty(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.load()
        assert bl.snapshot()["ban_timestamps"] == {}

    def test_corrupt_file_warns_and_resets(
        self, clock: FakeClock, file_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("{not json", encoding="utf-8")
        bl = make_banlist(clock, file_path)
        bl.load()
        assert bl.snapshot()["ban_timestamps"] == {}

    def test_old_v4_shape_discarded(self, clock: FakeClock, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps({"ban_timestamps": {"1.2.3.4": "NaN"}, "failure_counts": {"x": 3}}),
            encoding="utf-8",
        )
        bl = make_banlist(clock, file_path)
        bl.load()
        assert bl.snapshot()["ban_timestamps"] == {}
        assert bl.failure_count("x") == 0

    def test_non_dict_root_discarded(self, clock: FakeClock, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("[1,2,3]", encoding="utf-8")
        bl = make_banlist(clock, file_path)
        bl.load()
        assert bl.snapshot()["ban_timestamps"] == {}

    def test_save_is_atomic_no_tmp_leftover(
        self, clock: FakeClock, file_path: Path
    ) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        assert file_path.exists()
        assert not file_path.with_name(file_path.name + ".tmp").exists()

    def test_save_failure_keeps_memory_authoritative(
        self, clock: FakeClock, file_path: Path, tmp_path: Path
    ) -> None:
        # path 指向"文件"下的子路径 → mkdir 必失败（NotADirectoryError 属 OSError）
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        bl = Banlist(path=blocker / "ban" / "banlist.json", clock=clock)
        for _ in range(3):
            bl.record_failure("1.2.3.4")  # save 内部吞 OSError 并 warning
        assert bl.is_banned("1.2.3.4") is True  # 内存态不受落盘失败影响

    def test_persist_disabled_no_file(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path, persist=False)
        bl.record_failure("1.2.3.4")
        assert not file_path.exists()
        bl.load()  # no-op

    def test_trim_keeps_newest_bans(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path, max_records=2)
        for i in range(3):
            bl.record_failure(f"10.0.0.{i}")
            bl.record_failure(f"10.0.0.{i}")
            bl.record_failure(f"10.0.0.{i}")  # 封禁 ip_i
            clock.advance(1)
        bl.save()
        data = json.loads(file_path.read_text(encoding="utf-8"))
        assert len(data["ban_timestamps"]) == 2
        assert "10.0.0.2" in data["ban_timestamps"]  # 最新保留
        assert "10.0.0.0" not in data["ban_timestamps"]  # 最早裁剪

    def test_saved_payload_keys(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        assert set(data) == {"ban_timestamps", "failure_timestamps", "saved_at"}


class TestSnapshot:
    def test_snapshot_shape(self, clock: FakeClock, file_path: Path) -> None:
        bl = make_banlist(clock, file_path)
        bl.record_failure("1.2.3.4")
        snap = bl.snapshot()
        assert snap["max_attempts"] == 3
        assert snap["ban_duration"] == 300.0
        assert list(snap["failure_timestamps"]) == ["1.2.3.4"]

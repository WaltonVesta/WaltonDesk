"""认证封禁管理（阶段 9.2）：滑动窗口限流 + 封禁 + ``banlist.json`` 持久化。

迁移自 v4 ``server/service.py`` 的内联封禁段（``_record_auth_failure`` /
``_cleanup_expired_bans`` / ``_load_banlist`` / ``_save_banlist``，原
``daemon.py:733-787``），语义差异与增强：

====================  =============================  =========================
维度                  v4（service.py 内联）           v5（本模块）
====================  =============================  =========================
失败计数口径          累计计数（不清除历史窗口）        **滑动时间窗**（仅计 ``failure_window_s`` 内）
封禁判定              ``count >= max_auth_attempts``   窗口内失败次数 ≥ 阈值
到期处理              仅在新连接到来时惰性清理          查询即清理 + 显式 ``cleanup()``
落盘                  直接 ``open(w)``（可撕裂）        **原子写**（tmp + ``os.replace``）
条数上限              1000（裁剪最早封禁）             保留（:attr:`max_records`）
损坏文件              warning + 内存清零               同（warning 可观测，不静默）
线程模型              ``threading.Lock``               单事件循环持有，无锁（asyncio 线程内独占）
====================  =============================  =========================

时钟可注入（``clock``），测试用假时钟全确定性运行，零 sleep。
持久化路径默认 :func:`rdc.config.paths.banlist_file`，测试用 ``tmp_path`` 注入。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from rdc.config.paths import banlist_file
from rdc.logging import get_logger

__all__ = ["Banlist", "DEFAULT_BAN_DURATION", "DEFAULT_FAILURE_WINDOW", "DEFAULT_MAX_RECORDS"]

#: 默认封禁时长（秒）——与 Settings.auth_ban_duration 默认值一致。
DEFAULT_BAN_DURATION = 300.0
#: 默认滑动窗口宽度（秒）：仅窗口内的失败计入触发次数。
DEFAULT_FAILURE_WINDOW = 60.0
#: 持久化封禁记录条数上限（v4 语义保留，超出裁剪最早封禁）。
DEFAULT_MAX_RECORDS = 1000

_logger = get_logger("server.banlist")


class Banlist:
    """按 IP 的认证失败滑动窗口 + 封禁表 + 文件持久化。

    用法（服务端认证路径）::

        bans = Banlist(max_attempts=settings.max_auth_attempts,
                       ban_duration=settings.auth_ban_duration)
        if bans.is_banned(ip):        # 到期自动解封
            reject
        ...认证失败...
        bans.record_failure(ip)       # 返回是否触发封禁
        ...认证成功...
        bans.reset(ip)                # 清该 IP 的窗口计数
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        max_attempts: int = 3,
        ban_duration: float = DEFAULT_BAN_DURATION,
        failure_window_s: float = DEFAULT_FAILURE_WINDOW,
        max_records: int = DEFAULT_MAX_RECORDS,
        clock: Callable[[], float] = time.time,
        persist: bool = True,
    ) -> None:
        """
        Args:
            path: 持久化文件路径；``None`` 取 :func:`banlist_file`。
            max_attempts: 滑动窗口内失败达到该次数即封禁（≥1）。
            ban_duration: 封禁时长（秒）。
            failure_window_s: 滑动窗口宽度（秒）。
            max_records: 持久化封禁条数上限（超出裁剪最早封禁，v4 语义）。
            clock: 时钟注入（测试用假时钟）。
            persist: 关闭后不读写文件（纯内存模式，测试/无盘环境）。
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts 必须 ≥1，收到 {max_attempts}")
        if ban_duration <= 0:
            raise ValueError(f"ban_duration 必须 >0，收到 {ban_duration}")
        if failure_window_s <= 0:
            raise ValueError(f"failure_window_s 必须 >0，收到 {failure_window_s}")
        if max_records < 1:
            raise ValueError(f"max_records 必须 ≥1，收到 {max_records}")
        self._path = Path(path) if path is not None else banlist_file()
        self._max_attempts = max_attempts
        self._ban_duration = ban_duration
        self._failure_window = failure_window_s
        self._max_records = max_records
        self._clock = clock
        self._persist = persist
        #: ip -> 封禁起始时刻
        self._bans: dict[str, float] = {}
        #: ip -> 窗口内失败时刻队列（单调递增）
        self._failures: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ 判定

    @property
    def path(self) -> Path:
        """持久化文件路径。"""
        return self._path

    def is_banned(self, ip: str) -> bool:
        """``ip`` 当前是否处于封禁期（到期自动解封并安排落盘）。"""
        started = self._bans.get(ip)
        if started is None:
            return False
        if self._clock() - started >= self._ban_duration:
            self.unban(ip)
            return False
        return True

    def remaining_seconds(self, ip: str) -> float:
        """封禁剩余秒数；未封禁返回 0.0（供 UI/日志展示）。"""
        started = self._bans.get(ip)
        if started is None:
            return 0.0
        left = self._ban_duration - (self._clock() - started)
        return max(0.0, left)

    def failure_count(self, ip: str) -> int:
        """滑动窗口内 ``ip`` 的失败次数（只读，不触发封禁）。"""
        return len(self._prune(ip, self._clock()))

    # ------------------------------------------------------------------ 变更

    def record_failure(self, ip: str) -> bool:
        """记录一次认证失败。

        Returns:
            本次失败是否**触发**了封禁（窗口内达到阈值）。
        """
        now = self._clock()
        stamp = self._prune(ip, now)
        stamp.append(now)
        self._failures[ip] = stamp
        if len(stamp) >= self._max_attempts and ip not in self._bans:
            self._bans[ip] = now
            self._failures.pop(ip, None)
            _logger.warning(
                "auth.ban", ip=ip, attempts=len(stamp), duration_s=self._ban_duration
            )
            self.save()
            return True
        self.save()
        return False

    def reset(self, ip: str) -> None:
        """认证成功路径调用：清除该 IP 的窗口计数（不影响已有封禁）。"""
        if self._failures.pop(ip, None) is not None:
            self.save()

    def unban(self, ip: str) -> None:
        """显式解封（到期自动调用，或管理接口手动调用）。"""
        if self._bans.pop(ip, None) is not None:
            self._failures.pop(ip, None)
            _logger.info("auth.unban", ip=ip)
            self.save()

    def cleanup_expired(self) -> int:
        """清理全部到期封禁，返回解封数量（服务空闲定时器的兜底路径）。"""
        now = self._clock()
        expired = [ip for ip, started in self._bans.items() if now - started >= self._ban_duration]
        for ip in expired:
            del self._bans[ip]
            self._failures.pop(ip, None)
        if expired:
            _logger.info("auth.unban_batch", count=len(expired))
            self.save()
        return len(expired)

    # ------------------------------------------------------------------ 观测

    @property
    def banned_count(self) -> int:
        """当前封禁 IP 数（诊断/统计口径，不含到期未清理的）。"""
        return len(self._bans)

    def snapshot(self) -> dict[str, object]:
        """当前状态快照（诊断/测试用；不含文件 IO）。"""
        return {
            "ban_timestamps": dict(self._bans),
            "failure_timestamps": {ip: list(q) for ip, q in self._failures.items()},
            "max_attempts": self._max_attempts,
            "ban_duration": self._ban_duration,
            "failure_window_s": self._failure_window,
        }

    # ------------------------------------------------------------------ 持久化

    def load(self) -> None:
        """从文件载入封禁与窗口计数；文件缺失=空表，损坏=warning+空表。

        v4 兼容说明：v4 的 ``failure_counts`` 为累计 int，v5 为时间戳列表，
        两版本不共存（v4 兼容已作废），读到旧格式一律丢弃并重建。
        """
        self._bans = {}
        self._failures = {}
        if not self._persist:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _logger.warning("banlist.load_failed", path=str(self._path), error=str(exc))
            return
        if not isinstance(raw, dict):
            _logger.warning("banlist.load_shape_invalid", path=str(self._path))
            return
        bans = raw.get("ban_timestamps")
        if isinstance(bans, dict):
            now = self._clock()
            for ip, started in bans.items():
                if not isinstance(ip, str) or not isinstance(started, (int, float)):
                    continue
                if now - float(started) < self._ban_duration:  # 加载即清理过期项
                    self._bans[ip] = float(started)
        stamps = raw.get("failure_timestamps")
        if isinstance(stamps, dict):
            for ip, points in stamps.items():
                if not isinstance(ip, str) or not isinstance(points, list):
                    continue
                clean = sorted(float(p) for p in points if isinstance(p, (int, float)))
                if clean:
                    self._failures[ip] = deque(clean)
        if self._bans or self._failures:
            _logger.info(
                "banlist.loaded", bans=len(self._bans), tracked_ips=len(self._failures)
            )

    def save(self) -> None:
        """原子写持久化：超过 ``max_records`` 先裁剪最早封禁（v4 语义保留）。"""
        if not self._persist:
            return
        self._trim()
        payload = {
            "ban_timestamps": self._bans,
            "failure_timestamps": {ip: list(q) for ip, q in self._failures.items()},
            "saved_at": self._clock(),
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            # 内存态仍是权威（服务继续正常封禁），落盘失败可观测、下次写重试。
            _logger.warning("banlist.save_failed", path=str(self._path), error=str(exc))
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup_exc:  # pragma: no cover - 清理失败仅记录
                _logger.warning(
                    "banlist.tmp_cleanup_failed", path=str(tmp), error=str(cleanup_exc)
                )

    # ------------------------------------------------------------------ 内部

    def _prune(self, ip: str, now: float) -> deque[float]:
        """返回 ``ip`` 的失败队列（已丢弃窗口外时间戳）。"""
        stamp = self._failures.get(ip)
        if stamp is None:
            return deque()
        cutoff = now - self._failure_window
        while stamp and stamp[0] < cutoff:
            stamp.popleft()
        if not stamp:
            self._failures.pop(ip, None)
            return deque()
        return stamp

    def _trim(self) -> None:
        """裁剪到 ``max_records`` 条：保留封禁时刻最新的（v4 同语义），并清掉被裁 IP 的计数。"""
        if len(self._bans) <= self._max_records:
            return
        keep = sorted(self._bans.items(), key=lambda item: item[1], reverse=True)
        dropped = {ip for ip, _ in keep[self._max_records:]}
        for ip in dropped:
            self._bans.pop(ip, None)
            self._failures.pop(ip, None)
        _logger.info("banlist.trimmed", max_records=self._max_records)

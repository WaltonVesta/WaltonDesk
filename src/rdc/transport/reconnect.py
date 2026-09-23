"""自动重连（阶段 4，4.8）：asyncio 版重连控制器。

替代 v4 的线程轮询模型。核心参数语义与 v4 一致（回归测试锁定）：

- 指数退避：``delay * 2^(attempt-1)``，封顶 ``max_delay``（v4 为 60s）；
- ``max_attempts`` 次失败后放弃返回 ``False``。

v4 的 ``on_success``/``on_failure`` 回调参数由 :meth:`run` 的**返回值**取代
（调用方 ``await rc.run()`` 或 ``await rc.start()`` 后分支处理），
避免回调异常在后台线程里丢失的 v4 问题；``stop()`` 随时可取消。

v4 遗留竞态（线程 join 自身、``_stop`` 与循环间无同步）在本实现中由
asyncio 任务取消语义天然消除。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final

from rdc.logging import get_logger

__all__ = ["ReconnectController", "backoff_delay"]

#: 单次退避等待上限（秒，v4 硬编码 60 提取为常量）。
DEFAULT_MAX_DELAY: Final[float] = 60.0


def backoff_delay(attempt: int, base: float, max_delay: float = DEFAULT_MAX_DELAY) -> float:
    """第 ``attempt`` 次（1 起）重试前应等待的秒数。

    指数退避封顶：``min(base * 2^(attempt-1), max_delay)``。
    """
    return float(min(base * (2 ** (attempt - 1)), max_delay))


class ReconnectController:
    """异步重连控制器。

    Args:
        connect: 一次连接尝试；返回 ``True`` 成功。异常视同失败（内部捕获
            ``OSError``/``TimeoutError``，其余上抛——不静默吞配置/编程错误）。
        max_attempts: 最大尝试次数。
        delay: 退避基数秒。
        use_backoff: ``False`` 时固定间隔 ``delay``。
        max_delay: 退避上限秒。
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[bool]],
        *,
        max_attempts: int = 10,
        delay: float = 2.0,
        use_backoff: bool = True,
        max_delay: float = DEFAULT_MAX_DELAY,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if delay <= 0:
            raise ValueError("delay 必须为正")
        self._connect = connect
        self._max_attempts = max_attempts
        self._delay = delay
        self._use_backoff = use_backoff
        self._max_delay = max_delay
        self._attempts = 0
        self._last_result = False
        self._task: asyncio.Task[bool] | None = None
        self._log = get_logger("transport.reconnect")

    async def run(self) -> bool:
        """执行重连循环直到成功或耗尽次数。

        Returns:
            最终是否连接成功。
        """
        self._attempts = 0
        while self._attempts < self._max_attempts:
            self._attempts += 1
            wait = (
                backoff_delay(self._attempts, self._delay, self._max_delay)
                if self._use_backoff
                else self._delay
            )
            self._log.info(
                "reconnect_attempt",
                attempt=self._attempts,
                max=self._max_attempts,
                wait=round(wait, 1),
            )
            try:
                ok = await self._connect()
            except (OSError, TimeoutError) as exc:  # 网络类失败：记一次重试
                self._log.warning("reconnect_attempt_failed", error=repr(exc))
                ok = False
            self._last_result = ok
            if ok:
                self._log.info("reconnect_success", attempt=self._attempts)
                return True
            if self._attempts >= self._max_attempts:
                break
            await asyncio.sleep(wait)
        self._log.warning("reconnect_exhausted", attempts=self._max_attempts)
        return False

    def start(self) -> asyncio.Task[bool]:
        """以后台任务方式启动重连（同一实例重复调用返回既有任务）。"""
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done():
            return self._task
        self._task = loop.create_task(self.run(), name="reconnect")
        return self._task

    def stop(self) -> None:
        """取消重连任务（幂等）。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    # -- 观测（与 v4 面一致，便于上层/测试） -----------------------------------

    def reset(self) -> None:
        """重置尝试计数与结果。"""
        self._attempts = 0
        self._last_result = False

    def is_running(self) -> bool:
        """是否正在重连。"""
        return self._task is not None and not self._task.done()

    def get_attempts(self) -> int:
        """已进行的尝试次数。"""
        return self._attempts

    def get_last_result(self) -> bool:
        """最后一次连接结果。"""
        return self._last_result

    def get_progress(self) -> float:
        """重连进度 0.0~1.0。"""
        return min(1.0, self._attempts / self._max_attempts)

"""心跳保活（阶段 4，4.8）：asyncio 版心跳控制器。

替代 v4 的线程 + ``time.sleep`` 轮询模型：单个 asyncio 任务按固定节拍
发 ping、判超时，**无跨线程回调竞态**（v4 的 ``stop()`` 自 join 防御、
``on_timeout`` 在心跳线程内执行的坑全部消失）。

语义保留（v4 实测行为，回归测试锁定）：

- 发送连续失败 ``max_failures`` 次 → 判定链路坏死，触发 ``on_timeout`` 一次并退出；
- 距最后一次收到任何对端数据超过 ``timeout`` 秒 → 触发 ``on_timeout`` 一次并退出；
- :meth:`on_pong`（收到任何对端消息即应调用）刷新活跃时间并清零失败计数。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Final

from rdc.logging import get_logger

__all__ = ["HeartbeatController"]

#: 心跳发送间隔（秒，与 v4 ``HEARTBEAT_INTERVAL`` 一致）。
DEFAULT_INTERVAL: Final[float] = 3.0
#: 对端静默超时（秒，与 v4 ``HEARTBEAT_TIMEOUT`` 一致）。
DEFAULT_TIMEOUT: Final[float] = 10.0
#: 连续发送失败容忍次数（v4 为 3）。
DEFAULT_MAX_FAILURES: Final[int] = 3


class HeartbeatController:
    """asyncio 心跳控制器。

    Args:
        send_ping: 发送 ping 的异步回调；抛异常计一次发送失败。
        on_timeout: 判定超时时的同步回调（**至多触发一次**；运行于事件循环内，
            不得阻塞）。通常绑定"断开重连"逻辑。
        interval: 发送节拍（秒）。
        timeout: 对端静默判死阈值（秒）。
        max_failures: 连续发送失败判死阈值。
        clock: 单调时钟（测试注入假时钟；生产 :func:`time.monotonic`）。
    """

    def __init__(
        self,
        send_ping: Callable[[], Awaitable[None]],
        on_timeout: Callable[[], None],
        *,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_failures: int = DEFAULT_MAX_FAILURES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0 or timeout <= 0:
            raise ValueError("interval 与 timeout 必须为正")
        if max_failures < 1:
            raise ValueError("max_failures 至少为 1")
        self._send_ping = send_ping
        self._on_timeout = on_timeout
        self._interval = interval
        self._timeout = timeout
        self._max_failures = max_failures
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._alive = False
        self._last_recv = 0.0
        self._failures = 0
        self._log = get_logger("transport.heartbeat")

    # -- 生命周期 -------------------------------------------------------------

    def start(self) -> None:
        """启动心跳循环（重复调用无效果）。"""
        if self._task is not None and not self._task.done():
            return
        self._alive = True
        self._last_recv = self._clock()
        self._failures = 0
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="heartbeat")

    async def stop(self) -> None:
        """停止心跳（幂等；等待循环任务收尾）。"""
        self._alive = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def is_running(self) -> bool:
        """心跳任务是否存活。"""
        return self._task is not None and not self._task.done()

    # -- 外部事件 -------------------------------------------------------------

    def on_pong(self) -> None:
        """通知"对端仍活跃"（收到任何对端消息都可调用）。"""
        self._last_recv = self._clock()
        self._failures = 0
        self._alive = True

    def is_alive(self) -> bool:
        """链路是否判定存活。"""
        return self._alive

    def reset(self) -> None:
        """重置活跃时间与失败计数（重连成功后调用）。"""
        self._last_recv = self._clock()
        self._failures = 0
        self._alive = True

    # -- 内部循环 -------------------------------------------------------------

    async def _fail(self, reason: str) -> None:
        """判死：置非存活、触发一次回调。"""
        self._alive = False
        self._log.warning("heartbeat_timeout", reason=reason)
        try:
            self._on_timeout()
        except Exception as exc:  # 回调异常不得静默杀死心跳任务（约束 6：记录）
            self._log.error("heartbeat_timeout_callback_failed", error=repr(exc))

    async def _loop(self) -> None:
        """心跳主循环：每 ``interval`` 发一次 ping 并判超时。"""
        try:
            while self._alive:
                await asyncio.sleep(self._interval)
                if not self._alive:
                    return
                if self._clock() - self._last_recv >= self._timeout:
                    await self._fail("peer_silent")
                    return
                try:
                    await self._send_ping()
                    self._failures = 0
                except OSError as exc:  # ConnectionError 亦派生自 OSError
                    self._failures += 1
                    self._log.warning(
                        "heartbeat_send_failed",
                        failures=self._failures,
                        max=self._max_failures,
                        error=repr(exc),
                    )
                    if self._failures >= self._max_failures:
                        await self._fail("send_failures")
                        return
        except asyncio.CancelledError:  # stop() 取消：正常退出
            raise

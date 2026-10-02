"""画面订阅登记（阶段 9 批 3 拆分，D14）。

把 ``ScreenSession``（传输层会话）接入 ``transport.broadcaster.ScreenBroadcaster``
（阶段 4 B3 修复件）的**业务层登记表**，覆盖任务书 d 组三要点：

- **重复加入**：同一 session 对象二次 :meth:`SubscriptionRegistry.register`
  直接拒绝（防订阅者表错位；同 IP 不同 session 允许并存——客户端重连时
  旧会话可能尚未 EOF，各订阅各队列互不影响）；
- **断开回收**：统一汇流到 broadcaster 的 ``on_remove`` hook ——
  ① 服务主动 :meth:`unregister`（EOF/关流）；② 发送失败 broadcaster 自动
  注销（慢/死客户端）。hook 内注销 session 后台接收任务并通知监听器；
- **新订阅强制关键帧**：register 成功即调 ``on_new_subscriber``（服务层接
  :meth:`ScreenPipeline.request_keyframe`——新客户端从 I 帧起解，避免花屏）。

**所有权（D23 兑现）**：broadcaster 由本表**构造并持有**（``on_remove``
构造期即绑定 :meth:`handle_remove`，消除鸡生蛋依赖）；装配层把
:attr:`broadcaster` 交给 :class:`~rdc.server.pipeline.ScreenPipeline`，
``pipeline.close()`` 不关闭 broadcaster，其生命周期归本表
:meth:`shutdown`——正是阶段 6 报告"broadcaster 所有权在会话层（阶段 9
装配）"的落点。

订阅者 id 由本表单调分配（``sub-<seq>``），与 IP 解耦：同 IP 多会话时
broadcaster 的 ``subscriber_id`` 仍是唯一键。

零 IO、零 Qt：外部依赖只有回调注入（关键帧请求 / 注销通知），
集成测试用真实帧流（loopback socket）构造会话。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Final

from rdc.logging import get_logger
from rdc.transport.backpressure import DEFAULT_QUEUE_SIZE
from rdc.transport.broadcaster import ScreenBroadcaster, SubscriberInfo
from rdc.transport.server import ScreenSession

__all__ = ["REASON_CLOSED", "SubscriptionRegistry"]

_LOG = get_logger("server.broadcaster")

#: 服务主动注销的默认原因。
REASON_CLOSED: Final = "closed"

#: 注销通知回调：``fn(subscriber_id, reason)``。
RemoveListener = Callable[[str, str], None]
#: 新订阅建立回调（服务层接 pipeline.request_keyframe）。
NewSubscriberHook = Callable[[], None]


class SubscriptionRegistry:
    """ScreenSession ↔ ScreenBroadcaster 的订阅登记表（持有广播器）。

    Args:
        queue_size: 透传给 broadcaster 的每订阅者队列深度。
        on_new_subscriber: 新订阅建立回调（请求关键帧）。
        on_removed: 注销通知（审计/清理接线），异常隔离不外逸。
    """

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        on_new_subscriber: NewSubscriberHook | None = None,
        on_removed: RemoveListener | None = None,
    ) -> None:
        self._bc = ScreenBroadcaster(queue_size=queue_size, on_remove=self.handle_remove)
        self._on_new = on_new_subscriber
        self._on_removed = on_removed
        self._seq = 0
        self._sessions: dict[str, ScreenSession] = {}
        self._close_tasks: set[asyncio.Task[None]] = set()

    @property
    def broadcaster(self) -> ScreenBroadcaster:
        """本表持有的广播器（装配层注入 pipeline）。"""
        return self._bc

    # ------------------------------------------------------------------ 登记

    def register(self, session: ScreenSession) -> str:
        """登记一个画面会话并开始接收广播帧。

        Returns:
            分配的订阅者 id（:meth:`unregister` 用）。

        Raises:
            ValueError: 同一 session 对象重复登记。
            RuntimeError: 广播器已停止，或 session 尚未绑定帧流。
        """
        if any(existing is session for existing in self._sessions.values()):
            raise ValueError("同一 ScreenSession 重复登记订阅")
        if not session.is_connected:
            raise RuntimeError("ScreenSession 帧流未绑定/已断开，拒绝登记")
        self._seq += 1
        sid = f"sub-{self._seq}"
        self._sessions[sid] = session
        try:
            self._bc.add(
                SubscriberInfo(subscriber_id=sid, stream=session.stream, hmac_key=session.hmac_key)
            )
        except Exception:
            self._sessions.pop(sid, None)  # add 失败不留半登记状态
            raise
        _LOG.info(
            "screen_subscriber_registered",
            subscriber=sid,
            ip=session.client_ip,
            count=len(self._sessions),
        )
        if self._on_new is not None:
            self._on_new()  # 新客户端：下一帧强制关键帧
        return sid

    async def unregister(self, subscriber_id: str, reason: str = REASON_CLOSED) -> None:
        """主动注销（幂等：未知 id 静默返回）。"""
        if subscriber_id not in self._sessions:
            return
        await self._bc.remove(subscriber_id, reason)

    # ------------------------------------------------------------------ 回收汇流

    def handle_remove(self, subscriber_id: str, reason: str) -> None:
        """broadcaster ``on_remove`` hook：统一注销记账（幂等）。

        覆盖两条路径：主动 ``unregister`` 与发送失败自动注销；
        会话后台任务的清理调度为独立 task，不阻塞广播器注销路径。
        """
        session = self._sessions.pop(subscriber_id, None)
        if session is None:
            return
        task = asyncio.get_running_loop().create_task(
            session.close(), name=f"sub-close-{subscriber_id}"
        )
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)
        _LOG.info(
            "screen_subscriber_removed",
            subscriber=subscriber_id,
            reason=reason,
            count=len(self._sessions),
        )
        if self._on_removed is not None:
            try:
                self._on_removed(subscriber_id, reason)
            except Exception as exc:  # 监听器异常隔离（约束 6：记录不静默）
                _LOG.error("subscribers_removed_listener_failed", error=repr(exc))

    # ------------------------------------------------------------------ 观测/收尾

    def subscriber_ids(self) -> list[str]:
        """当前登记的订阅者 id（顺序稳定）。"""
        return list(self._sessions)

    def count(self) -> int:
        """当前订阅者数量。"""
        return len(self._sessions)

    async def await_close_tasks(self) -> None:
        """等待全部会话清理任务收尾（测试/关停路径用）。"""
        while self._close_tasks:
            tasks = list(self._close_tasks)
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            self._close_tasks.difference_update(tasks)

    async def shutdown(self) -> None:
        """全部注销（幂等）；之后 broadcaster 拒绝新订阅。"""
        await self._bc.shutdown()  # 逐个 remove → handle_remove 清理本表
        await self.await_close_tasks()

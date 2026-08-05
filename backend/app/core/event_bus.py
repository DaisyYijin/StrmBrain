"""
事件总线 - 进程内发布/订阅，支持同步与异步发布

供 SSE 实时推送、跨模块解耦通知使用。
事件发布方（如 sync_service / organize_service）无需关心谁在监听，
监听方（如 SSE 端点）按需订阅感兴趣的事件类型。

设计要点：
- publish 为同步方法，可在工作线程中安全调用（如 asyncio.to_thread 内的同步函数）。
  对于异步回调，会自动调度到事件循环执行。
- publish_async 通过 asyncio.create_task 异步发布，适用于异步上下文。
- 订阅者回调签名：callback(event_type: str, data: dict) -> None
- SSE 端点通过注册一个使用 loop.call_soon_threadsafe 的同步回调来跨线程投递事件。
"""
import asyncio
import threading
import time
from typing import Callable, Optional

from app.core.logbuffer import get_logger

logger = get_logger("app.core.event_bus")


class EventBus:
    """
    进程内事件总线（单例）。

    支持同步发布（publish）和异步发布（publish_async）。
    订阅者回调可为同步函数或协程函数。
    """

    # ===== 事件类型常量 =====
    FILE_MUTATED = "file_mutated"                      # 文件变更（增/删/改）
    SYNC_COMPLETED = "sync_completed"                  # 同步完成
    ORGANIZE_COMPLETED = "organize_completed"          # 整理完成
    ACCOUNT_AUTH_FAILED = "account_auth_failed"        # 账号认证失败
    ACCOUNT_AUTH_RECOVERED = "account_auth_recovered"  # 账号认证恢复
    NOTIFICATION_CREATED = "notification_created"      # 通知已创建

    # 全部事件类型列表（便于 SSE 端点统一订阅）
    ALL_EVENTS = (
        FILE_MUTATED,
        SYNC_COMPLETED,
        ORGANIZE_COMPLETED,
        ACCOUNT_AUTH_FAILED,
        ACCOUNT_AUTH_RECOVERED,
        NOTIFICATION_CREATED,
    )

    _instance: Optional["EventBus"] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._subscribers: dict[str, list[Callable]] = {}
                    inst._lock = threading.RLock()
                    inst._loop: Optional[asyncio.AbstractEventLoop] = None
                    cls._instance = inst
        return cls._instance

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """设置主事件循环引用，供工作线程跨线程调度异步回调使用"""
        self._loop = loop

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """获取已注册的事件循环"""
        return self._loop

    def subscribe(self, event_type: str, callback: Callable):
        """
        订阅事件。

        callback 签名：callback(event_type: str, data: dict) -> None
        callback 可为同步函数或协程函数。
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        """取消订阅事件"""
        with self._lock:
            cbs = self._subscribers.get(event_type)
            if not cbs:
                return
            try:
                cbs.remove(callback)
            except ValueError:
                pass
            if not cbs:
                self._subscribers.pop(event_type, None)

    def subscribe_all(self, callback: Callable):
        """订阅全部已知事件类型"""
        for et in self.ALL_EVENTS:
            self.subscribe(et, callback)

    def unsubscribe_all(self, callback: Callable):
        """取消订阅全部事件类型"""
        for et in self.ALL_EVENTS:
            self.unsubscribe(et, callback)

    def publish(self, event_type: str, data: dict):
        """
        同步发布事件：依次调用所有订阅者回调。

        可在任意线程（含工作线程）中安全调用：
        - 同步回调直接执行
        - 异步回调调度到事件循环执行（优先使用已注册的 loop，否则尝试获取当前运行循环）
        """
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))

        if not callbacks:
            return

        for cb in callbacks:
            try:
                result = cb(event_type, data)
                # 回调返回协程时，调度到事件循环执行
                if asyncio.iscoroutine(result):
                    self._schedule_coro(result)
            except Exception as e:
                logger.warning(f"[event-bus] 订阅者回调异常 ({event_type}): {e}")

    def publish_async(self, event_type: str, data: dict):
        """
        异步发布事件：通过 asyncio.create_task 在当前事件循环中调度。

        仅可在异步上下文（有运行中的事件循环）中调用。
        """
        coro = self._async_publish(event_type, data)
        try:
            asyncio.create_task(coro)
        except RuntimeError:
            # 无运行中的事件循环，降级为同步发布
            logger.debug(f"[event-bus] 无事件循环，降级同步发布: {event_type}")
            self.publish(event_type, data)

    async def _async_publish(self, event_type: str, data: dict):
        """异步发布内部实现：调用异步订阅者并 await"""
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))

        if not callbacks:
            return

        for cb in callbacks:
            try:
                result = cb(event_type, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[event-bus] 异步订阅者回调异常 ({event_type}): {e}")

    def _schedule_coro(self, coro):
        """将协程调度到事件循环执行（支持跨线程）"""
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        # 尝试获取当前运行的事件循环
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # 无运行中的事件循环，关闭协程避免警告
            coro.close()
            logger.debug("[event-bus] 无可用事件循环，丢弃异步回调")

    def subscriber_count(self, event_type: str = "") -> int:
        """获取订阅者数量（用于调试/状态展示）"""
        with self._lock:
            if event_type:
                return len(self._subscribers.get(event_type, []))
            return sum(len(cbs) for cbs in self._subscribers.values())


def get_event_bus() -> EventBus:
    """获取事件总线全局单例"""
    return EventBus()

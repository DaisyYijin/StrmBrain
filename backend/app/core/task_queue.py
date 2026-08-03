"""
任务队列管理系统
================

参考 qmediasync 的 NewSyncQueueManager 设计，实现通用的异步任务队列。

特性：
    - 按任务类型分组，每种类型独立队列、串行执行
    - 任务去重：相同 source_id + task_type 的任务不重复入队
    - 队列满时返回错误而不是阻塞
    - 支持暂停 / 恢复
    - 任务执行回调支持异步函数，try/except 防崩溃
    - 执行完成后自动清理 current_task

任务类型：
    - strm_sync（STRM 同步）
    - scrape（刮削整理）
    - upload（上传）
    - download（下载）

典型用法::

    from app.core.task_queue import init_task_queue_manager, Task, TaskType

    # 在 FastAPI startup 中初始化
    manager = init_task_queue_manager()

    # 添加任务
    task = Task(
        id="strm_sync_1",
        task_type=TaskType.STRM_SYNC,
        name="全量同步 - 影视目录",
        source_id=1,
        callback=my_async_function,
        args=("arg1",),
        kwargs={"key": "value"},
    )
    manager.add_task(task)

    # 检查状态
    status = manager.check_task_status(1, TaskType.STRM_SYNC)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.core.logbuffer import get_logger

logger = get_logger("app.core.task_queue")


# ===== 常量 =====

QUEUE_MAX_SIZE: int = 50
"""每种任务类型队列的最大容量。"""

# 队列运行状态常量
QUEUE_STATUS_RUNNING: str = "running"
QUEUE_STATUS_PAUSED: str = "paused"
QUEUE_STATUS_STOPPED: str = "stopped"


# ===== 枚举 =====


class TaskType(str, Enum):
    """
    任务类型枚举。

    每种类型对应一个独立的任务队列，队列内任务串行执行。
    """

    STRM_SYNC = "strm_sync"
    """STRM 同步任务。"""

    SCRAPE = "scrape"
    """刮削整理任务。"""

    UPLOAD = "upload"
    """上传任务。"""

    DOWNLOAD = "download"
    """下载任务。"""


class TaskStatus(int, Enum):
    """
    任务状态枚举。

    用于 :meth:`TaskQueuePerType.check_task_status` 和
    :meth:`TaskQueueManager.check_task_status` 的返回值。
    """

    NONE = 0
    """无任务（source_id 对应的任务不存在）。"""

    WAITING = 1
    """等待中（任务已入队但尚未开始执行）。"""

    RUNNING = 2
    """执行中（任务正在被回调函数处理）。"""


# ===== 异常 =====


class TaskQueueFullError(Exception):
    """队列已满异常。"""

    pass


# ===== 数据类 =====


@dataclass
class Task:
    """
    任务数据类。

    Attributes:
        id: 唯一标识（如 ``"strm_sync_1"``）。
        task_type: 任务类型（对应 :class:`TaskType` 枚举值）。
        name: 显示名称。
        source_id: 关联的目录 ID。
        callback: 可选的回调函数（支持异步和同步）。
        args: 传给回调的位置参数。
        kwargs: 传给回调的关键字参数。
        created_at: 创建时间戳。

    典型用法::

        task = Task(
            id="strm_sync_1",
            task_type="strm_sync",
            name="全量同步",
            source_id=1,
            callback=async_sync_function,
            args=("cid_123",),
            kwargs={"mode": "full"},
        )
    """

    id: str
    task_type: str
    name: str
    source_id: int
    callback: Optional[Callable[..., Any]] = None
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


# ===== 单类型队列 =====


class TaskQueuePerType:
    """
    按任务类型分组的队列。

    每种任务类型拥有独立的队列，队列内任务串行执行（一次只执行一个任务）。
    支持去重、满队拒绝、暂停 / 恢复。

    Attributes:
        task_type: 任务类型字符串。
        task_chan: 异步队列（最大容量 50）。
        waiting_queue: 等待中的任务映射（task_id -> Task）。
        current_task: 当前正在执行的任务（无任务时为 None）。
        status: 队列运行状态（running / paused / stopped）。
    """

    def __init__(self, task_type: str, max_size: int = QUEUE_MAX_SIZE) -> None:
        """
        Args:
            task_type: 任务类型字符串。
            max_size: 队列最大容量，默认 50。
        """
        self.task_type: str = task_type
        self.task_chan: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self.waiting_queue: dict[str, Task] = {}
        self.current_task: Optional[Task] = None
        self.status: str = QUEUE_STATUS_RUNNING

    def add_task(self, task: Task) -> bool:
        """
        添加任务到队列。

        - 去重：相同 source_id 的任务不重复入队（检查等待队列和当前任务）。
        - 满队拒绝：队列已满时返回 False，不阻塞。

        Args:
            task: 要添加的任务。

        Returns:
            是否成功入队（去重或满队时返回 False）。
        """
        # 去重：检查当前执行中的任务
        if self.current_task is not None and self.current_task.source_id == task.source_id:
            logger.info(
                f"任务已在执行中，跳过入队: {task.id} "
                f"(source_id={task.source_id}, type={self.task_type})"
            )
            return False

        # 去重：检查等待队列
        for waiting_task in self.waiting_queue.values():
            if waiting_task.source_id == task.source_id:
                logger.info(
                    f"任务已在队列中，跳过入队: {task.id} "
                    f"(source_id={task.source_id}, type={self.task_type})"
                )
                return False

        # 满队拒绝（非阻塞）
        if self.task_chan.full():
            logger.warning(
                f"队列已满，拒绝任务: {task.id} "
                f"(type={self.task_type}, size={self.task_chan.qsize()})"
            )
            return False

        # 入队：先加入等待队列，再放入异步队列
        self.waiting_queue[task.id] = task
        try:
            self.task_chan.put_nowait(task)
        except asyncio.QueueFull:
            # 并发场景下的二次保护
            self.waiting_queue.pop(task.id, None)
            logger.warning(
                f"队列已满，拒绝任务: {task.id} (type={self.task_type})"
            )
            return False

        logger.info(
            f"任务已加入队列: {task.id} ({task.name}), "
            f"type={self.task_type}, 等待数={len(self.waiting_queue)}"
        )
        return True

    def cancel_task(self, task_id: str) -> bool:
        """
        取消等待中的任务。

        从等待队列中移除指定任务。正在执行中的任务无法取消。
        注意：由于 asyncio.Queue 不支持移除中间元素，被取消的任务
        在 :meth:`process` 取出时会被自动跳过。

        Args:
            task_id: 任务 ID。

        Returns:
            是否成功取消（任务正在执行或不存在时返回 False）。
        """
        if task_id in self.waiting_queue:
            task: Task = self.waiting_queue.pop(task_id)
            logger.info(f"任务已取消: {task_id} ({task.name})")
            return True

        if self.current_task is not None and self.current_task.id == task_id:
            logger.warning(f"任务正在执行中，无法取消: {task_id}")
            return False

        logger.warning(f"任务不在队列中: {task_id}")
        return False

    def check_task_status(self, source_id: int) -> TaskStatus:
        """
        检查指定 source_id 的任务状态。

        Args:
            source_id: 关联的目录 ID。

        Returns:
            任务状态（NONE / WAITING / RUNNING）。
        """
        # 检查当前执行中的任务
        if self.current_task is not None and self.current_task.source_id == source_id:
            return TaskStatus.RUNNING

        # 检查等待队列
        for task in self.waiting_queue.values():
            if task.source_id == source_id:
                return TaskStatus.WAITING

        return TaskStatus.NONE

    def pause(self) -> None:
        """暂停队列处理。

        当前正在执行的任务会继续执行至完成，但不会从队列中取出新任务。
        """
        if self.status != QUEUE_STATUS_PAUSED:
            self.status = QUEUE_STATUS_PAUSED
            logger.info(f"任务队列已暂停: type={self.task_type}")

    def resume(self) -> None:
        """恢复队列处理。"""
        if self.status == QUEUE_STATUS_PAUSED:
            self.status = QUEUE_STATUS_RUNNING
            logger.info(f"任务队列已恢复: type={self.task_type}")

    def stop(self) -> None:
        """停止队列处理。

        停止后 :meth:`process` 循环将退出，不再处理任何任务。
        """
        self.status = QUEUE_STATUS_STOPPED
        logger.info(f"任务队列已停止: type={self.task_type}")

    def get_status(self) -> dict[str, Any]:
        """
        获取队列状态信息。

        Returns:
            包含队列状态、当前任务、等待任务列表的字典::

                {
                    "task_type": "strm_sync",
                    "status": "running",
                    "current_task": {"id": "...", "name": "...", ...} | None,
                    "waiting_count": 3,
                    "waiting_tasks": [{"id": "...", "name": "...", ...}, ...],
                    "queue_size": 3,
                    "queue_maxsize": 50
                }
        """
        return {
            "task_type": self.task_type,
            "status": self.status,
            "current_task": (
                {
                    "id": self.current_task.id,
                    "name": self.current_task.name,
                    "source_id": self.current_task.source_id,
                    "task_type": self.current_task.task_type,
                }
                if self.current_task is not None
                else None
            ),
            "waiting_count": len(self.waiting_queue),
            "waiting_tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "source_id": t.source_id,
                    "task_type": t.task_type,
                    "created_at": t.created_at,
                }
                for t in self.waiting_queue.values()
            ],
            "queue_size": self.task_chan.qsize(),
            "queue_maxsize": self.task_chan.maxsize,
        }

    async def process(self) -> None:
        """
        任务处理循环。

        从队列中取出任务并执行，循环运行直到队列状态变为 ``stopped``。
        暂停状态下等待恢复，不取出新任务。

        此方法应通过 ``asyncio.create_task`` 或 ``loop.create_task``
        在后台运行，由 :class:`TaskQueueManager` 自动管理。

        处理流程：
            1. 检查暂停/停止状态
            2. 从队列取出任务（带 1 秒超时，便于周期性检查状态）
            3. 检查任务是否已被取消（从 waiting_queue 中移除）
            4. 从等待队列移除，设为当前任务
            5. 执行任务
            6. 清理 current_task
        """
        logger.info(f"任务处理循环已启动: type={self.task_type}")

        while self.status != QUEUE_STATUS_STOPPED:
            # 暂停状态下等待
            if self.status == QUEUE_STATUS_PAUSED:
                await asyncio.sleep(0.5)
                continue

            # 从队列取出任务（带超时，便于周期性检查暂停/停止状态）
            try:
                task: Task = await asyncio.wait_for(
                    self.task_chan.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info(f"任务处理循环被取消: type={self.task_type}")
                break

            # 检查任务是否已被取消（从 waiting_queue 中移除）
            if task.id not in self.waiting_queue:
                logger.info(f"任务已被取消，跳过执行: {task.id}")
                self.task_chan.task_done()
                continue

            # 从等待队列移除，设为当前任务
            self.waiting_queue.pop(task.id, None)
            self.current_task = task

            try:
                await self.execute_task(task)
            finally:
                # 执行完成后自动清理 current_task
                self.current_task = None
                self.task_chan.task_done()

        logger.info(f"任务处理循环已退出: type={self.task_type}")

    async def execute_task(self, task: Task) -> None:
        """
        执行单个任务。

        支持异步和同步回调函数：
        - 异步函数：直接 await 执行
        - 同步函数：通过 ``asyncio.to_thread`` 在线程池中执行，避免阻塞事件循环

        使用 try/except 防止任务崩溃影响后续任务的执行。

        Args:
            task: 要执行的任务。
        """
        logger.info(f"开始执行任务: {task.id} ({task.name})")

        if task.callback is None:
            logger.warning(f"任务无回调函数，跳过: {task.id}")
            return

        try:
            if asyncio.iscoroutinefunction(task.callback):
                # 异步回调：直接 await
                await task.callback(*task.args, **task.kwargs)
            else:
                # 同步回调：在线程池中执行，避免阻塞事件循环
                await asyncio.to_thread(task.callback, *task.args, **task.kwargs)
            logger.info(f"任务执行完成: {task.id} ({task.name})")
        except Exception as e:
            logger.warning(
                f"任务执行失败 [{task.id}] ({task.name}): {e}",
                exc_info=True,
            )


# ===== 全局队列管理器 =====


class TaskQueueManager:
    """
    全局任务队列管理器。

    管理所有任务类型的队列，按类型分发任务到对应队列。
    每种任务类型（:class:`TaskType`）拥有独立的 :class:`TaskQueuePerType`
    实例和处理循环。

    Attributes:
        queues: 任务类型字符串 -> :class:`TaskQueuePerType` 的映射。
    """

    def __init__(self) -> None:
        self.queues: dict[str, TaskQueuePerType] = {}
        self._process_tasks: dict[str, asyncio.Task] = {}
        self._initialized: bool = False

    def start(self) -> None:
        """
        初始化所有任务类型的队列并启动处理循环。

        需在事件循环中调用（如 FastAPI startup 事件）。
        为每种 :class:`TaskType` 创建独立的队列和后台处理任务。

        Raises:
            RuntimeError: 若不在事件循环中调用。
        """
        if self._initialized:
            logger.warning("任务队列管理器已初始化，跳过重复初始化")
            return

        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

        for task_type in TaskType:
            type_name: str = task_type.value
            queue: TaskQueuePerType = TaskQueuePerType(type_name)
            self.queues[type_name] = queue
            # 为每个队列启动后台处理循环
            self._process_tasks[type_name] = loop.create_task(
                queue.process(), name=f"task_queue_{type_name}"
            )
            logger.info(f"任务队列已启动: type={type_name}")

        self._initialized = True
        logger.info(f"任务队列管理器已初始化: 类型数={len(self.queues)}")

    def add_task(self, task: Task) -> bool:
        """
        按任务类型分发任务到对应队列。

        根据 ``task.task_type`` 将任务添加到对应类型的队列中。
        队列内会进行去重和满队检查。

        Args:
            task: 要添加的任务。

        Returns:
            是否成功入队。
        """
        if not self._initialized:
            logger.warning("任务队列管理器未初始化，无法添加任务")
            return False

        queue: Optional[TaskQueuePerType] = self.queues.get(task.task_type)
        if queue is None:
            logger.warning(f"未知的任务类型: {task.task_type}")
            return False

        return queue.add_task(task)

    def cancel_task(self, source_id: int, task_type: str) -> bool:
        """
        取消指定类型的任务。

        在对应类型的队列中查找并取消与 ``source_id`` 关联的等待任务。
        正在执行中的任务无法取消。

        Args:
            source_id: 关联的目录 ID。
            task_type: 任务类型字符串。

        Returns:
            是否成功取消。
        """
        queue: Optional[TaskQueuePerType] = self.queues.get(task_type)
        if queue is None:
            logger.warning(f"未知的任务类型: {task_type}")
            return False

        # 构造可能的任务 ID（约定格式：{task_type}_{source_id}）
        task_id: str = f"{task_type}_{source_id}"

        # 优先通过 task_id 取消
        if queue.cancel_task(task_id):
            return True

        # 如果 task_id 不匹配（自定义 ID 格式），遍历等待队列按 source_id 取消
        for tid, task in list(queue.waiting_queue.items()):
            if task.source_id == source_id:
                return queue.cancel_task(tid)

        logger.info(
            f"未找到可取消的任务: source_id={source_id}, type={task_type}"
        )
        return False

    def check_task_status(self, source_id: int, task_type: str) -> TaskStatus:
        """
        检查指定任务的执行状态。

        Args:
            source_id: 关联的目录 ID。
            task_type: 任务类型字符串。

        Returns:
            任务状态（NONE / WAITING / RUNNING）。
        """
        queue: Optional[TaskQueuePerType] = self.queues.get(task_type)
        if queue is None:
            return TaskStatus.NONE

        return queue.check_task_status(source_id)

    def pause_all(self) -> None:
        """暂停所有任务队列。

        当前正在执行的任务会继续执行至完成，但所有队列都不会取出新任务。
        """
        for queue in self.queues.values():
            queue.pause()
        logger.info("所有任务队列已暂停")

    def resume_all(self) -> None:
        """恢复所有任务队列。"""
        for queue in self.queues.values():
            queue.resume()
        logger.info("所有任务队列已恢复")

    def get_all_status(self) -> dict[str, Any]:
        """
        获取所有队列的状态信息。

        Returns:
            包含初始化状态和各队列状态的字典::

                {
                    "initialized": True,
                    "queues": {
                        "strm_sync": {...},
                        "scrape": {...},
                        ...
                    }
                }
        """
        return {
            "initialized": self._initialized,
            "queues": {
                task_type: queue.get_status()
                for task_type, queue in self.queues.items()
            },
        }

    def get_queue_status(self, task_type: str) -> dict[str, Any]:
        """
        获取指定任务类型的队列状态。

        Args:
            task_type: 任务类型字符串。

        Returns:
            该队列的状态字典；若类型不存在则返回空字典。
        """
        queue = self.queues.get(task_type)
        if queue is None:
            return {}
        return queue.get_status()

    async def shutdown(self) -> None:
        """
        停止所有队列的处理循环。

        将所有队列状态设为 stopped，并取消后台处理任务。
        应在应用关闭时调用（如 FastAPI shutdown 事件）。
        """
        for queue in self.queues.values():
            queue.stop()

        # 取消所有处理循环任务
        for task_type, process_task in self._process_tasks.items():
            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            logger.info(f"任务处理循环已终止: type={task_type}")

        self._process_tasks.clear()
        self._initialized = False
        logger.info("任务队列管理器已关闭")


# ===== 全局单例 =====

global_task_queue_manager: Optional[TaskQueueManager] = None


def init_task_queue_manager() -> TaskQueueManager:
    """
    初始化全局任务队列管理器单例。

    需在事件循环中调用（如 FastAPI startup 事件）。
    首次调用时创建管理器并启动所有队列的处理循环；
    重复调用时直接返回已有实例。

    Returns:
        TaskQueueManager 实例。

    Raises:
        RuntimeError: 若不在事件循环中调用。

    典型用法::

        @app.on_event("startup")
        async def startup():
            init_task_queue_manager()
    """
    global global_task_queue_manager
    if global_task_queue_manager is not None and global_task_queue_manager._initialized:
        return global_task_queue_manager

    global_task_queue_manager = TaskQueueManager()
    global_task_queue_manager.start()
    return global_task_queue_manager


def get_task_queue_manager() -> Optional[TaskQueueManager]:
    """
    获取全局任务队列管理器单例。

    与 :func:`init_task_queue_manager` 不同，此函数不会自动初始化。

    Returns:
        TaskQueueManager 实例，未初始化时返回 None。
    """
    return global_task_queue_manager

"""
WebSocket 进度管理器

用于整理/同步任务实时推送进度到前端。
支持任务锁防止并发执行同一类型任务，完成状态延迟清除。
"""
import json
import time
import asyncio
from typing import Optional
from fastapi import WebSocket

from app.core.logbuffer import get_logger

logger = get_logger("app.core.progress")


class ProgressManager:
    """
    进度管理器：管理 WebSocket 连接，推送任务进度。
    单例模式，全局共享。
    通过 asyncio.Lock 保证任务状态变更的原子性，防止并发冲突。
    """

    _instance: Optional["ProgressManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._connections: list[WebSocket] = []
            cls._instance._current_task: Optional[dict] = None
            cls._instance._task_lock = asyncio.Lock()
            cls._instance._clear_task: Optional[asyncio.Task] = None
        return cls._instance

    @property
    def connections(self):
        return self._connections

    async def connect(self, websocket: WebSocket):
        """接受新的 WebSocket 连接"""
        await websocket.accept()
        self._connections.append(websocket)
        logger.info(f"[progress] WebSocket 已连接，当前连接数: {len(self._connections)}")
        # 如果有正在进行的任务，立即推送当前状态
        if self._current_task:
            try:
                await websocket.send_text(json.dumps(self._current_task, ensure_ascii=False))
            except Exception:
                pass

    def disconnect(self, websocket: WebSocket):
        """断开 WebSocket 连接"""
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info(f"[progress] WebSocket 已断开，当前连接数: {len(self._connections)}")

    async def broadcast(self, data: dict):
        """向所有连接广播消息"""
        message = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

    async def start_task(self, task_type: str, total: int, title: str = "") -> bool:
        """
        开始一个任务。通过锁保证原子性。
        如果已有任务在运行，返回 False（调用方应拒绝重复触发）。
        """
        async with self._task_lock:
            if self._current_task is not None:
                logger.warning(f"[progress] 任务已在运行，拒绝重复启动: {task_type}")
                return False

            # 取消之前的延迟清除任务
            if self._clear_task and not self._clear_task.done():
                self._clear_task.cancel()
                self._clear_task = None

            self._current_task = {
                "type": "progress",
                "task_type": task_type,        # organize / full_sync / incremental_sync
                "status": "running",            # running / completed / error
                "title": title or task_type,
                "total": total,
                "current": 0,
                "current_file": "",
                "started_at": time.time(),
                "message": "",
            }
            await self.broadcast(self._current_task)
            return True

    async def update_progress(self, current: int, current_file: str = "", message: str = ""):
        """更新进度"""
        if not self._current_task:
            return
        self._current_task["current"] = current
        if current_file:
            self._current_task["current_file"] = current_file
        if message:
            self._current_task["message"] = message
        await self.broadcast(self._current_task)

    async def update_total(self, total: int):
        """更新任务总数（扫描完成后调用）"""
        if not self._current_task:
            return
        self._current_task["total"] = total
        await self.broadcast(self._current_task)

    async def complete_task(self, summary: str = ""):
        """完成任务，保留状态 5 秒后自动清除"""
        async with self._task_lock:
            if not self._current_task:
                return
            self._current_task["status"] = "completed"
            self._current_task["message"] = summary or "任务完成"
            await self.broadcast(self._current_task)
            # 延迟清除，让前端有时间显示最终状态
            self._clear_task = asyncio.create_task(self._delayed_clear(5.0))

    async def error_task(self, error: str):
        """任务出错，保留状态 5 秒后自动清除"""
        async with self._task_lock:
            if not self._current_task:
                return
            self._current_task["status"] = "error"
            self._current_task["message"] = error
            await self.broadcast(self._current_task)
            self._clear_task = asyncio.create_task(self._delayed_clear(5.0))

    async def _delayed_clear(self, delay: float):
        """延迟清除当前任务状态"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._task_lock:
            self._current_task = None
            self._clear_task = None

    def is_running(self) -> bool:
        """检查是否有任务正在运行"""
        return self._current_task is not None and self._current_task.get("status") == "running"


# 全局单例
progress_manager = ProgressManager()

"""
API 路由 - 任务队列管理（查看状态、暂停/恢复/取消）
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.tasks")

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(require_auth)])


@router.get("/status", response_model=ApiResponse)
async def get_all_task_status():
    """获取所有任务队列状态"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(data={"queues": {}})
    return ApiResponse(data={"queues": global_task_queue_manager.get_all_status()})


@router.get("/status/{task_type}", response_model=ApiResponse)
async def get_task_status(task_type: str):
    """获取指定类型的任务队列状态"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(data={})
    status = global_task_queue_manager.get_queue_status(task_type)
    return ApiResponse(data=status)


class CancelTaskIn(BaseModel):
    source_id: int
    task_type: str


@router.post("/cancel", response_model=ApiResponse)
async def cancel_task(payload: CancelTaskIn):
    """取消指定任务"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(code=500, message="任务队列未初始化")
    ok = global_task_queue_manager.cancel_task(payload.source_id, payload.task_type)
    if ok:
        return ApiResponse(message="任务已取消")
    return ApiResponse(code=404, message="任务未找到")


class CheckStatusIn(BaseModel):
    source_id: int
    task_type: str


@router.post("/check", response_model=ApiResponse)
async def check_task_status(payload: CheckStatusIn):
    """检查任务状态"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(data={"status": 0, "status_text": "空闲"})
    status = global_task_queue_manager.check_task_status(payload.source_id, payload.task_type)
    status_map = {0: "空闲", 1: "等待中", 2: "运行中"}
    return ApiResponse(data={"status": status, "status_text": status_map.get(status, "未知")})


@router.post("/pause-all", response_model=ApiResponse)
async def pause_all_tasks():
    """暂停所有任务队列"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(code=500, message="任务队列未初始化")
    global_task_queue_manager.pause_all()
    return ApiResponse(message="所有任务队列已暂停")


@router.post("/resume-all", response_model=ApiResponse)
async def resume_all_tasks():
    """恢复所有任务队列"""
    from app.core.task_queue import global_task_queue_manager
    if global_task_queue_manager is None:
        return ApiResponse(code=500, message="任务队列未初始化")
    global_task_queue_manager.resume_all()
    return ApiResponse(message="所有任务队列已恢复")


@router.get("/types", response_model=ApiResponse)
async def get_task_types():
    """获取所有任务类型"""
    return ApiResponse(data={
        "types": [
            {"value": "strm_sync", "label": "STRM同步"},
            {"value": "scrape", "label": "刮削整理"},
            {"value": "upload", "label": "上传"},
            {"value": "download", "label": "下载"},
        ]
    })

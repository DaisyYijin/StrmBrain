"""
媒体库删除级联控制面板 API
===========================

提供级联删除的事件队列状态、删除历史查询与清空能力：
- GET    /api/sync-del/status   队列大小 + 历史条数（功能始终启用）
- GET    /api/sync-del/history  查询删除历史（最近 limit 条，倒序）
- DELETE /api/sync-del/history  清空删除历史
"""
from fastapi import APIRouter, Depends, Query

from app.core.auth import require_auth
from app.core.logbuffer import get_logger
from app.services.mediasyncdel_service import get_mediasync_del_service

logger = get_logger("app.api.sync_del")

router = APIRouter(
    prefix="/api/sync-del",
    tags=["sync-del"],
    dependencies=[Depends(require_auth)],
)


@router.get("/status")
async def sync_del_status():
    """返回级联删除队列大小与历史条数。"""
    svc = get_mediasync_del_service()
    return {
        "queue_size": svc.get_queue_size(),
        "history_count": svc.get_history_count(),
    }


@router.get("/history")
async def sync_del_history(limit: int = Query(50, ge=1, le=500)):
    """查询删除历史，返回最近 limit 条（倒序，最新在前）。"""
    svc = get_mediasync_del_service()
    return {"records": svc.get_history(limit)}


@router.delete("/history")
async def sync_del_clear_history():
    """清空删除历史文件（写空数组）。"""
    svc = get_mediasync_del_service()
    svc.clear_history()
    return {"cleared": True}

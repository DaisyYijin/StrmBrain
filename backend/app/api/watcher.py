"""
API 路由 - 文件系统监控管理
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, List, Callable, Any

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.watcher")

router = APIRouter(prefix="/api/watcher", tags=["watcher"], dependencies=[Depends(require_auth)])


class WatchStartIn(BaseModel):
    path: str
    extensions: Optional[List[str]] = None
    event_type: str = "created"  # created/modified/deleted/moved


@router.get("/paths", response_model=ApiResponse)
async def get_watched_paths():
    """获取所有监控路径"""
    from app.services.folder_watcher import global_watcher_manager
    if global_watcher_manager is None:
        return ApiResponse(data={"paths": []})
    return ApiResponse(data={"paths": global_watcher_manager.get_watched_paths()})


@router.post("/start", response_model=ApiResponse)
async def start_watching(payload: WatchStartIn):
    """开始监控指定路径"""
    from app.services.folder_watcher import global_watcher_manager
    if global_watcher_manager is None:
        return ApiResponse(code=500, message="监控管理器未初始化")

    from pathlib import Path
    if not Path(payload.path).exists():
        return ApiResponse(code=404, message="路径不存在")

    def _on_event(event_info: dict):
        logger.info(f"文件监控事件: {event_info}")

    ok = global_watcher_manager.start_watching(
        payload.path,
        extensions=payload.extensions,
        callback=_on_event,
    )
    if ok:
        return ApiResponse(message=f"已开始监控: {payload.path}")
    return ApiResponse(code=400, message="启动监控失败（可能未安装 watchdog 库）")


@router.post("/stop", response_model=ApiResponse)
async def stop_watching(payload: dict):
    """停止监控指定路径"""
    from app.services.folder_watcher import global_watcher_manager
    if global_watcher_manager is None:
        return ApiResponse(code=500, message="监控管理器未初始化")
    path = payload.get("path", "")
    if not path:
        return ApiResponse(code=400, message="请指定路径")
    global_watcher_manager.stop_watching(path)
    return ApiResponse(message=f"已停止监控: {path}")


@router.post("/stop-all", response_model=ApiResponse)
async def stop_all_watching():
    """停止所有监控"""
    from app.services.folder_watcher import global_watcher_manager
    if global_watcher_manager is None:
        return ApiResponse(code=500, message="监控管理器未初始化")
    global_watcher_manager.stop_all()
    return ApiResponse(message="已停止所有监控")


@router.get("/status", response_model=ApiResponse)
async def get_watcher_status():
    """获取监控状态"""
    from app.services.folder_watcher import global_watcher_manager
    if global_watcher_manager is None:
        return ApiResponse(data={"paths": [], "is_running": False})
    paths = global_watcher_manager.get_watched_paths()
    return ApiResponse(data={
        "paths": paths,
        "is_running": len(paths) > 0,
        "count": len(paths),
    })

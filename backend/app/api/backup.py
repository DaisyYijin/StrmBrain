"""
API 路由 - 数据备份与恢复
"""
import asyncio
from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel
from pathlib import Path

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.config import DATA_DIR
from app.core.logbuffer import get_logger

logger = get_logger("app.api.backup")

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(require_auth)])


class BackupStartIn(BaseModel):
    backup_type: str = "manual"
    reason: str = ""


@router.get("/list", response_model=ApiResponse)
async def list_backups():
    """获取所有备份记录"""
    from app.services.backup_service import backup_service
    records = backup_service.list_backups()
    return ApiResponse(data={"records": records})


@router.post("/start", response_model=ApiResponse)
async def start_backup(payload: BackupStartIn):
    """启动备份（异步执行）"""
    from app.services.backup_service import backup_service
    if backup_service.is_running():
        return ApiResponse(code=400, message="备份或恢复任务正在运行中")
    # 在后台执行
    asyncio.create_task(backup_service.backup(payload.backup_type, payload.reason))
    return ApiResponse(message="备份任务已启动")


@router.get("/status", response_model=ApiResponse)
async def get_backup_status():
    """获取备份/恢复进度"""
    from app.services.backup_service import backup_service
    return ApiResponse(data=backup_service.get_running_status())


@router.delete("/{backup_id}", response_model=ApiResponse)
async def delete_backup(backup_id: int):
    """删除备份记录及文件"""
    from app.services.backup_service import backup_service
    ok = backup_service.delete_backup(backup_id)
    if not ok:
        return ApiResponse(code=404, message="备份记录不存在")
    return ApiResponse(message="备份已删除")


@router.post("/cleanup", response_model=ApiResponse)
async def cleanup_backups(max_count: int = 10):
    """清理旧备份"""
    from app.services.backup_service import backup_service
    deleted = backup_service.cleanup_old_backups(max_count)
    return ApiResponse(message=f"已清理 {deleted} 个旧备份")


@router.post("/restore", response_model=ApiResponse)
async def restore_backup(payload: dict):
    """从备份恢复（通过备份ID或文件路径）"""
    from app.services.backup_service import backup_service
    if backup_service.is_running():
        return ApiResponse(code=400, message="备份或恢复任务正在运行中")

    backup_id = payload.get("backup_id")
    zip_path = payload.get("zip_path", "")

    if backup_id:
        records = backup_service.list_backups()
        record = next((r for r in records if r.get("id") == backup_id), None)
        if not record:
            return ApiResponse(code=404, message="备份记录不存在")
        zip_path = record.get("file_path", "")

    if not zip_path:
        return ApiResponse(code=400, message="未指定备份文件")

    p = Path(zip_path)
    if not p.is_absolute():
        p = DATA_DIR / "backups" / zip_path
    if not p.exists():
        return ApiResponse(code=404, message=f"备份文件不存在: {p}")

    asyncio.create_task(backup_service.restore(str(p)))
    return ApiResponse(message="恢复任务已启动")


@router.post("/restore/upload", response_model=ApiResponse)
async def restore_from_upload(file: UploadFile = File(...)):
    """上传备份文件并恢复"""
    from app.services.backup_service import backup_service
    if backup_service.is_running():
        return ApiResponse(code=400, message="备份或恢复任务正在运行中")

    # 保存上传的文件
    upload_dir = DATA_DIR / "backups" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / file.filename
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    asyncio.create_task(backup_service.restore(str(save_path)))
    return ApiResponse(message="恢复任务已启动")


@router.post("/test", response_model=ApiResponse)
async def test_backup():
    """快速测试备份功能（不保存文件）"""
    from app.services.backup_service import backup_service
    json_files = list(DATA_DIR.glob("*.json"))
    return ApiResponse(data={
        "data_dir": str(DATA_DIR),
        "json_file_count": len(json_files),
        "json_files": [f.name for f in json_files],
        "backup_dir_exists": (DATA_DIR / "backups").exists(),
    })

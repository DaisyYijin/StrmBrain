"""
API 路由 - API Key 管理
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.apikeys")

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"], dependencies=[Depends(require_auth)])


class CreateAPIKeyIn(BaseModel):
    name: str


class UpdateStatusIn(BaseModel):
    is_active: bool


@router.get("/list", response_model=ApiResponse)
async def list_api_keys():
    """获取API Key列表（不包含完整密钥）"""
    from app.core.api_key import list_api_keys as _list
    keys = _list()
    return ApiResponse(data={"keys": keys})


@router.post("/create", response_model=ApiResponse)
async def create_api_key(payload: CreateAPIKeyIn):
    """创建API Key（完整密钥仅返回一次）"""
    from app.core.api_key import create_api_key as _create
    if not payload.name.strip():
        return ApiResponse(code=400, message="名称不能为空")
    result = _create(payload.name.strip())
    return ApiResponse(
        data=result,
        message="API Key创建成功，请妥善保管密钥，此密钥仅显示一次"
    )


@router.delete("/{key_id}", response_model=ApiResponse)
async def delete_api_key(key_id: int):
    """删除API Key"""
    from app.core.api_key import delete_api_key as _delete
    ok = _delete(key_id)
    if not ok:
        return ApiResponse(code=404, message="API Key不存在")
    return ApiResponse(message="已删除")


@router.put("/{key_id}/status", response_model=ApiResponse)
async def update_api_key_status(key_id: int, payload: UpdateStatusIn):
    """启用/禁用API Key"""
    from app.core.api_key import update_api_key_status as _update
    ok = _update(key_id, payload.is_active)
    if not ok:
        return ApiResponse(code=404, message="API Key不存在")
    status_text = "启用" if payload.is_active else "禁用"
    return ApiResponse(message=f"API Key已{status_text}")

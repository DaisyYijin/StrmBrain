"""
API 路由 - AI 设置管理
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"], dependencies=[Depends(require_auth)])


class AISettingsIn(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn"
    model_name: str = "Qwen/Qwen2.5-7B-Instruct"
    timeout: int = 60
    enabled: bool = True  # 保留字段向后兼容，实际由 ai_mode 控制


@router.get("/settings", response_model=ApiResponse)
async def get_ai_settings():
    """获取AI配置"""
    from app.services.ai_service import get_ai_config
    return ApiResponse(data=get_ai_config())


@router.post("/settings", response_model=ApiResponse)
async def save_ai_settings(payload: AISettingsIn):
    """保存AI配置"""
    from app.services.ai_service import save_ai_config, init_ai_client
    save_ai_config({
        "api_key": payload.api_key,
        "base_url": payload.base_url,
        "model_name": payload.model_name,
        "timeout": payload.timeout,
        "enabled": payload.enabled,
    })
    init_ai_client()
    return ApiResponse(message="已保存")


class AIExtractIn(BaseModel):
    filename: str
    media_type: str = "movie"  # movie / tv


@router.post("/extract", response_model=ApiResponse)
async def ai_extract(payload: AIExtractIn):
    """AI提取媒体信息"""
    from app.services.ai_service import get_ai_client
    client = get_ai_client()
    if client is None:
        return ApiResponse(code=400, message="AI服务未配置或未启用")
    try:
        if payload.media_type == "tv":
            result = await client.extract_tv_name(payload.filename)
        else:
            result = await client.extract_movie_name(payload.filename)
        return ApiResponse(data=result)
    except Exception as e:
        logger.warning(f"AI提取失败: {e}")
        return ApiResponse(code=500, message=str(e))


@router.post("/test", response_model=ApiResponse)
async def test_ai():
    """测试AI连接"""
    from app.services.ai_service import get_ai_client
    client = get_ai_client()
    if client is None:
        return ApiResponse(code=400, message="AI服务未配置或未启用")
    try:
        result = await client.extract_movie_name("Test.Movie.2023.1080p.BluRay.mkv")
        return ApiResponse(data=result, message="AI连接正常")
    except Exception as e:
        return ApiResponse(code=500, message=f"AI连接失败: {e}")

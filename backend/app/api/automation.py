"""
API 路由 - 自动化规则引擎（G7）

规则 = 触发器（cron/webhook）+ 动作序列（strm_sync/scrape/organize/emby_refresh）。
- 管理接口（/rules、/run/{id}）需要认证（require_auth）
- webhook 触发端点（/webhook/{token}）为公开端点：单独 router，不加 require_auth 依赖，
  并在 main.py 的公开路径前缀中加入 /api/automation/webhook/ 以绕过全局认证中间件。
"""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.services.automation_service import get_automation_service

# 管理接口：需要认证
router = APIRouter(
    prefix="/api/automation",
    tags=["automation"],
    dependencies=[Depends(require_auth)],
)

# Webhook 触发端点：公开（不依赖 require_auth）
webhook_router = APIRouter(
    prefix="/api/automation",
    tags=["automation-webhook"],
)


class ActionItem(BaseModel):
    """动作项：type + enabled + config"""
    type: str
    enabled: bool = True
    config: dict = {}


class RuleRequest(BaseModel):
    """规则创建/更新请求体（更新时字段可缺省，仅更新显式提供的字段）"""
    name: str = ""
    trigger_type: str = ""
    cron: str = ""
    webhook_token: str = ""
    actions: List[ActionItem] = []
    is_enabled: bool = True


def _reschedule_automation_jobs():
    """规则变更后重注册 cron 定时任务（asyncio.create_task 包裹，避免阻塞请求）"""
    try:
        from app.core.scheduler import register_automation_jobs
        asyncio.create_task(register_automation_jobs())
    except RuntimeError:
        # 无运行中的事件循环（理论上不会发生，路由均为异步），忽略
        pass


# ==================== 规则管理 ====================

@router.get("/rules", response_model=ApiResponse)
async def list_rules():
    """规则列表"""
    service = get_automation_service()
    return ApiResponse(data={"rules": service.list_rules()})


@router.post("/rules", response_model=ApiResponse)
async def create_rule(payload: RuleRequest):
    """创建规则"""
    service = get_automation_service()
    rule = service.add_rule(payload.model_dump())
    _reschedule_automation_jobs()
    return ApiResponse(data={"rule": rule})


@router.put("/rules/{rule_id}", response_model=ApiResponse)
async def update_rule(rule_id: int, payload: RuleRequest):
    """更新规则（body 同上，部分字段可缺省）"""
    service = get_automation_service()
    # exclude_unset：仅更新客户端显式提供的字段，未提供字段保留原值
    patch = payload.model_dump(exclude_unset=True, mode="json")
    rule = service.update_rule(rule_id, patch)
    if rule is None:
        return ApiResponse(code=404, message="规则不存在")
    _reschedule_automation_jobs()
    return ApiResponse(data={"rule": rule})


@router.delete("/rules/{rule_id}", response_model=ApiResponse)
async def delete_rule(rule_id: int):
    """删除规则"""
    service = get_automation_service()
    if not service.delete_rule(rule_id):
        return ApiResponse(code=404, message="规则不存在")
    _reschedule_automation_jobs()
    return ApiResponse(data={"deleted": True})


@router.post("/run/{rule_id}", response_model=ApiResponse)
async def run_rule(rule_id: int):
    """手动立即执行规则（后台执行，不阻塞请求）"""
    service = get_automation_service()
    rule = service.get_rule(rule_id)
    if not rule:
        return ApiResponse(code=404, message="规则不存在")
    service._run_rule_async(rule_id, context="manual")
    return ApiResponse(data={"started": True, "name": rule.get("name", "")})


# ==================== Webhook 触发（公开端点） ====================

@webhook_router.post("/webhook/{token}")
async def automation_webhook(token: str):
    """公开端点：按 webhook_token 匹配规则并触发执行。

    返回 {"matched": bool, "rule": str|""}；未匹配到规则时 matched=false。
    """
    service = get_automation_service()
    rule_name = service.handle_webhook(token)
    if rule_name is None:
        return {"matched": False, "rule": ""}
    return {"matched": True, "rule": rule_name}

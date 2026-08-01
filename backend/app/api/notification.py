"""
API 路由 - 通知管理（多渠道配置 + 事件规则 + 测试发送）
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, List, Any

from app.core.auth import require_auth
from app.schemas import ApiResponse
from app.core.logbuffer import get_logger

logger = get_logger("app.api.notification")

router = APIRouter(prefix="/api/notification", tags=["notification"], dependencies=[Depends(require_auth)])


# ===== 渠道管理 =====

class ChannelIn(BaseModel):
    name: str
    type: str  # telegram/bark/serverchan/meow/webhook/wechat_work
    config: dict
    is_enabled: bool = True


@router.get("/channels", response_model=ApiResponse)
async def list_channels():
    """获取所有通知渠道"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(data={"channels": []})
    channels = mgr.get_channels()
    return ApiResponse(data={"channels": channels})


@router.post("/channels", response_model=ApiResponse)
async def create_channel(payload: ChannelIn):
    """创建通知渠道"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    channel = mgr.add_channel({
        "name": payload.name,
        "type": payload.type,
        "config": payload.config,
        "is_enabled": payload.is_enabled,
    })
    return ApiResponse(data=channel, message="渠道创建成功")


@router.put("/channels/{channel_id}", response_model=ApiResponse)
async def update_channel(channel_id: int, payload: ChannelIn):
    """更新通知渠道"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    channel = mgr.update_channel(channel_id, {
        "name": payload.name,
        "type": payload.type,
        "config": payload.config,
        "is_enabled": payload.is_enabled,
    })
    if channel is None:
        return ApiResponse(code=404, message="渠道不存在")
    return ApiResponse(data=channel, message="已更新")


@router.delete("/channels/{channel_id}", response_model=ApiResponse)
async def delete_channel(channel_id: int):
    """删除通知渠道"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    ok = mgr.delete_channel(channel_id)
    if not ok:
        return ApiResponse(code=404, message="渠道不存在")
    return ApiResponse(message="已删除")


# ===== 规则管理 =====

class RuleIn(BaseModel):
    event_type: str  # sync_complete/scrape_complete/system_alert/error
    channel_ids: List[int]
    is_enabled: bool = True


@router.get("/rules", response_model=ApiResponse)
async def list_rules():
    """获取所有通知规则"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(data={"rules": []})
    return ApiResponse(data={"rules": mgr.get_rules()})


@router.post("/rules", response_model=ApiResponse)
async def create_rule(payload: RuleIn):
    """创建通知规则"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    rule = mgr.add_rule({
        "event_type": payload.event_type,
        "channel_ids": payload.channel_ids,
        "is_enabled": payload.is_enabled,
    })
    return ApiResponse(data=rule, message="规则创建成功")


@router.put("/rules/{rule_id}", response_model=ApiResponse)
async def update_rule(rule_id: int, payload: RuleIn):
    """更新通知规则"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    rule = mgr.update_rule(rule_id, {
        "event_type": payload.event_type,
        "channel_ids": payload.channel_ids,
        "is_enabled": payload.is_enabled,
    })
    if rule is None:
        return ApiResponse(code=404, message="规则不存在")
    return ApiResponse(data=rule, message="已更新")


@router.delete("/rules/{rule_id}", response_model=ApiResponse)
async def delete_rule(rule_id: int):
    """删除通知规则"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    ok = mgr.delete_rule(rule_id)
    if not ok:
        return ApiResponse(code=404, message="规则不存在")
    return ApiResponse(message="已删除")


# ===== 测试发送 =====

class TestSendIn(BaseModel):
    event_type: str = "system_alert"
    title: str = "测试通知"
    content: str = "这是一条来自 STRMhub 的测试通知"
    image: str = ""


@router.post("/test", response_model=ApiResponse)
async def test_send(payload: TestSendIn):
    """发送测试通知"""
    from app.services.notification_manager import global_notification_manager
    mgr = global_notification_manager
    if mgr is None:
        return ApiResponse(code=500, message="通知管理器未初始化")
    results = await mgr.send_notification(
        event_type=payload.event_type,
        title=payload.title,
        content=payload.content,
        image=payload.image,
    )
    return ApiResponse(data={"results": results}, message=f"发送完成，成功 {sum(1 for r in results if r.get('success'))} / {len(results)} 个渠道")


# ===== 事件类型列表 =====

@router.get("/event-types", response_model=ApiResponse)
async def get_event_types():
    """获取所有支持的事件类型"""
    return ApiResponse(data={
        "event_types": [
            {"value": "sync_complete", "label": "同步完成"},
            {"value": "scrape_complete", "label": "刮削完成"},
            {"value": "system_alert", "label": "系统告警"},
            {"value": "error", "label": "异常告警"},
        ],
        "channel_types": [
            {"value": "telegram", "label": "Telegram", "fields": ["bot_token", "chat_id", "proxy_url"]},
            {"value": "bark", "label": "Bark", "fields": ["server_url", "device_key", "sound", "icon"]},
            {"value": "serverchan", "label": "Server酱", "fields": ["sckey", "endpoint"]},
            {"value": "meow", "label": "MeoW", "fields": ["nickname", "endpoint"]},
            {"value": "webhook", "label": "自定义Webhook", "fields": ["endpoint", "method", "format", "template", "auth_type", "auth_token", "auth_user", "auth_pass", "auth_header_key", "auth_query_key", "query_param", "headers"]},
            {"value": "wechat_work", "label": "企业微信", "fields": []},
        ]
    })

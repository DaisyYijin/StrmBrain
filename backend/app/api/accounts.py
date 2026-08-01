"""
API 路由 - 账号管理
"""
from fastapi import APIRouter

from app.core.json_storage import (
    read_accounts, find_account, upsert_account, delete_account as _delete_account,
    find_account_by_user_id,
)
from app.services import Client115Service
from app.schemas import ApiResponse, AccountOut, AccountCreate

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=ApiResponse)
async def list_accounts():
    """获取账号列表"""
    accounts = read_accounts()
    # 按 ID 降序
    accounts_sorted = sorted(accounts, key=lambda a: a.get("id", 0), reverse=True)
    return ApiResponse(
        data=[AccountOut(**{k: a.get(k) for k in AccountOut.model_fields}).model_dump() for a in accounts_sorted]
    )


@router.post("", response_model=ApiResponse)
async def create_account(data: AccountCreate):
    """手动添加账号（通过 cookies）"""
    # 验证 cookies
    is_valid, user_info = Client115Service.check_cookies_valid(data.cookies)

    if not is_valid:
        return ApiResponse(code=400, message=f"Cookies invalid: {user_info.get('error', '')}")

    # 检查是否已存在
    user_id = user_info.get("user_id", "")
    if user_id and find_account_by_user_id(user_id):
        return ApiResponse(code=400, message="Account already exists")

    # 创建账号
    account_data = {
        "name": data.name,
        "cookies": data.cookies,
        "user_id": user_id,
        "username": user_info.get("username", ""),
        "status": 1,
    }
    saved = upsert_account(account_data)

    return ApiResponse(data=AccountOut(**{k: saved.get(k) for k in AccountOut.model_fields}).model_dump())


@router.get("/{account_id}", response_model=ApiResponse)
async def get_account(account_id: int):
    """获取账号详情"""
    account = find_account(account_id)

    if not account:
        return ApiResponse(code=404, message="Account not found")

    return ApiResponse(data=AccountOut(**{k: account.get(k) for k in AccountOut.model_fields}).model_dump())


@router.delete("/{account_id}", response_model=ApiResponse)
async def delete_account(account_id: int):
    """删除账号"""
    account = find_account(account_id)

    if not account:
        return ApiResponse(code=404, message="Account not found")

    _delete_account(account_id)

    # 清除客户端缓存（按 cookies 精确清除）
    Client115Service.remove_client(cookies=account.get("cookies", ""))

    return ApiResponse(message="Deleted")


@router.post("/{account_id}/check", response_model=ApiResponse)
async def check_account(account_id: int):
    """检测账号状态，并刷新详细信息"""
    account = find_account(account_id)

    if not account:
        return ApiResponse(code=404, message="Account not found")

    cookies = account.get("cookies", "")
    is_valid, user_info = Client115Service.check_cookies_valid(cookies)

    # 更新状态
    update_data = {"id": account_id, "status": 1 if is_valid else 0}
    if is_valid:
        update_data["username"] = user_info.get("username") or account.get("username", "")
        update_data["vip_level"] = user_info.get("vip_level", account.get("vip_level", 0))
        update_data["space_used"] = user_info.get("space_used", account.get("space_used", 0))
        update_data["space_total"] = user_info.get("space_total", account.get("space_total", 0))
        # 头像为空时才覆盖，避免清掉扫码时已保存的头像
        if not account.get("avatar_url") and user_info.get("avatar_url"):
            update_data["avatar_url"] = user_info["avatar_url"]

    saved = upsert_account(update_data)

    return ApiResponse(
        data={
            "is_valid": is_valid,
            "user_info": user_info if is_valid else None
        }
    )

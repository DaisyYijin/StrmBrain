"""
API 路由 - 系统信息（版本号、实时日志、本地账号、本地目录浏览、登录认证）
"""
import os
from pathlib import Path
from fastapi import APIRouter, Query, Depends
from pydantic import BaseModel
import bcrypt

from app.core.json_storage import read_local_account, save_local_account
from app.core.auth import (
    create_access_token, authenticate_user, require_auth, is_auth_enabled,
    verify_password,
)
from app.schemas import ApiResponse
from app.config import VERSION
from app.core.logbuffer import ring_handler, SOURCE_LABELS, get_logger

logger = get_logger("app.api.system")

router = APIRouter(prefix="/api", tags=["system"])


class LocalAccountIn(BaseModel):
    username: str
    old_password: str = ""
    new_password: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


@router.get("/version", response_model=ApiResponse)
async def get_version():
    """获取版本号"""
    return ApiResponse(data={"version": VERSION, "auth_enabled": is_auth_enabled()})


@router.post("/login", response_model=ApiResponse)
async def login(payload: LoginIn):
    """登录认证，返回 JWT token"""
    user = authenticate_user(payload.username, payload.password)
    if not user:
        logger.warning(f"登录失败: username={payload.username}")
        return ApiResponse(code=401, message="用户名或密码错误")
    token = create_access_token(user)
    logger.info(f"登录成功: username={payload.username}")
    return ApiResponse(data={"token": token, "username": user["sub"]})


@router.get("/auth/check", response_model=ApiResponse)
async def auth_check(user: dict = Depends(require_auth)):
    """检查当前 token 是否有效"""
    return ApiResponse(data={"username": user.get("sub", "")})


@router.get("/logs", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def get_logs(since: int = Query(default=0)):
    """
    获取实时日志（结构化，带级别和来源信息）。
    since > 0 时仅返回序列号大于 since 的增量日志，减少传输量。
    """
    entries = ring_handler.get_entries(since=since)
    # 最新日志在前
    entries.reverse()
    return ApiResponse(data={
        "entries": entries,
        "sources": SOURCE_LABELS,
        "latest_seq": ring_handler.latest_seq,
    })


@router.post("/logs/clear", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def clear_logs():
    """清空日志缓冲"""
    ring_handler.clear()
    return ApiResponse(message="日志已清空")


@router.get("/local-account", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def get_local_account_api():
    """获取本地管理账号信息（仅返回用户名）"""
    acct = read_local_account()
    return ApiResponse(data={"username": acct.get("username", "") if acct else ""})


@router.post("/local-account", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def save_local_account_api(payload: LocalAccountIn):
    """
    更新本地管理账号。
    - 修改用户名：直接更新
    - 修改密码：需提供 old_password 验证通过后，用 new_password 替换
    """
    acct = read_local_account()
    username = payload.username.strip()
    if not username:
        return ApiResponse(code=400, message="用户名不能为空")

    # 判断是否需要修改密码
    if payload.new_password:
        # 需要验证原密码
        if not payload.old_password:
            return ApiResponse(code=400, message="修改密码需要提供原密码")
        if not acct or not acct.get("password_hash"):
            return ApiResponse(code=400, message="当前未设置账号，无法验证原密码")
        if not verify_password(payload.old_password, acct["password_hash"]):
            logger.warning(f"修改密码失败：原密码验证不通过, username={username}")
            return ApiResponse(code=400, message="原密码不正确")
        # 验证通过，使用新密码
        pwd_bytes = payload.new_password.encode("utf-8")[:72]
        pwd_hash = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")
    elif acct and acct.get("password_hash"):
        # 不改密码，保留原密码哈希
        pwd_hash = acct["password_hash"]
    else:
        # 首次设置且未提供密码
        return ApiResponse(code=400, message="首次设置账号需要提供新密码")

    save_local_account(username, pwd_hash)
    logger.info(f"本地账号已更新: {username}")
    return ApiResponse(message="已保存")


@router.get("/local/browse", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def browse_local_dirs(path: str = Query(default="/")):
    """
    浏览本地文件系统目录，返回子目录列表。
    用于前端本地目录选择器。
    """
    # 规范化路径
    if not path or path == "":
        path = "/"
    try:
        target = Path(path).resolve()
    except Exception:
        return ApiResponse(code=400, message="路径无效")

    # 路径不存在时，逐级向上查找最近的存在目录
    original = target
    while not target.exists():
        if str(target) == target.anchor or target.parent == target:
            # 已到根目录仍不存在
            return ApiResponse(code=404, message=f"路径不存在: {original}")
        target = target.parent

    if not target.is_dir():
        target = target.parent if target.parent != target else Path("/")

    try:
        dirs = []
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({
                    "name": entry.name,
                    "path": str(entry),
                })
        # 计算父目录
        parent = str(target.parent) if str(target) != target.anchor else ""
        return ApiResponse(data={
            "current": str(target),
            "parent": parent,
            "dirs": dirs,
        })
    except PermissionError:
        return ApiResponse(code=403, message=f"无权限访问: {target}")
    except Exception as e:
        return ApiResponse(code=500, message=f"浏览目录失败: {str(e)}")

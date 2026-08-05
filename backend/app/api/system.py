"""
API 路由 - 系统信息（版本号、实时日志、本地账号、本地目录浏览、登录认证）
"""
import os
import time
from pathlib import Path
from fastapi import APIRouter, Query, Depends, Request
from pydantic import BaseModel
import bcrypt

from app.core.json_storage import read_local_account, save_local_account
from app.core.auth import (
    create_access_token, authenticate_user, require_auth, is_auth_enabled,
    verify_password, has_local_account,
)
from app.schemas import ApiResponse
from app.config import VERSION
from app.core.logbuffer import ring_handler, SOURCE_LABELS, get_logger

logger = get_logger("app.api.system")

router = APIRouter(prefix="/api", tags=["system"])

# ===== 登录速率限制 =====
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 分钟锁定
_login_attempts: dict[str, list[float]] = {}  # IP -> [timestamp, ...]


def _check_login_rate_limit(client_ip: str) -> tuple[bool, str]:
    """检查登录速率限制，返回 (是否允许, 提示消息)"""
    now = time.time()
    cutoff = now - _LOGIN_LOCKOUT_SECONDS
    # 清理过期记录
    if client_ip in _login_attempts:
        _login_attempts[client_ip] = [t for t in _login_attempts[client_ip] if t > cutoff]
    else:
        _login_attempts[client_ip] = []
    attempts = _login_attempts[client_ip]
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        remaining = int(attempts[-1] + _LOGIN_LOCKOUT_SECONDS - now)
        return False, f"登录失败次数过多，请 {remaining} 秒后重试"
    return True, ""


def _record_login_failure(client_ip: str) -> None:
    """记录一次登录失败"""
    if client_ip not in _login_attempts:
        _login_attempts[client_ip] = []
    _login_attempts[client_ip].append(time.time())


class LocalAccountIn(BaseModel):
    username: str
    old_password: str = ""
    new_password: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str


@router.get("/version", response_model=ApiResponse)
async def get_version():
    """获取版本号及更新检查信息"""
    from app.services.version_service import get_version_info
    info = get_version_info()
    return ApiResponse(data={
        "version": VERSION,
        "auth_enabled": is_auth_enabled(),
        # 是否已注册本地管理账号（未注册时前端显示注册页）
        "registered": has_local_account(),
        "latest_version": info.get("latest_version"),
        "has_update": info.get("has_update", False),
        "release_url": info.get("release_url"),
        "release_notes": info.get("release_notes"),
        "checked_at": info.get("checked_at"),
        "error": info.get("error"),
    })


@router.post("/version/check", response_model=ApiResponse)
async def check_version():
    """主动触发版本检查（请求 GitHub Releases API）"""
    from app.services.version_service import check_latest_version
    info = await check_latest_version()
    return ApiResponse(data={
        "version": VERSION,
        "latest_version": info.get("latest_version"),
        "has_update": info.get("has_update", False),
        "release_url": info.get("release_url"),
        "release_notes": info.get("release_notes"),
        "checked_at": info.get("checked_at"),
        "error": info.get("error"),
    })


@router.post("/login", response_model=ApiResponse)
async def login(payload: LoginIn, request: Request):
    """登录认证，返回 JWT token"""
    client_ip = request.client.host if request.client else "unknown"

    # 未注册本地账号时，提示用户先注册（前端会切换到注册页）
    if not has_local_account():
        return ApiResponse(code=400, message="尚未注册管理账号，请先注册", data={"need_register": True})

    allowed, msg = _check_login_rate_limit(client_ip)
    if not allowed:
        return ApiResponse(code=429, message=msg)
    user = authenticate_user(payload.username, payload.password)
    if not user:
        _record_login_failure(client_ip)
        logger.warning(f"登录失败: username={payload.username}, ip={client_ip}")
        return ApiResponse(code=401, message="用户名或密码错误")
    token = create_access_token(user)
    logger.info(f"登录成功: username={payload.username}, ip={client_ip}")
    return ApiResponse(data={"token": token, "username": user["sub"]})


@router.post("/register", response_model=ApiResponse)
async def register(payload: RegisterIn, request: Request):
    """首次部署注册管理账号。
    仅当本地账号不存在时可用；已注册后返回 409 拒绝再次注册。
    账号密码存储在 local_account.json（持久化，升级/重启不丢失）。
    """
    client_ip = request.client.host if request.client else "unknown"

    # 已存在本地账号 → 拒绝重复注册
    if has_local_account():
        return ApiResponse(code=409, message="管理账号已注册，请直接登录")

    username = (payload.username or "").strip()
    password = payload.password or ""
    if not username:
        return ApiResponse(code=400, message="用户名不能为空")
    if len(username) < 2 or len(username) > 32:
        return ApiResponse(code=400, message="用户名长度需在 2-32 个字符之间")
    if len(password) < 6:
        return ApiResponse(code=400, message="密码长度至少 6 位")
    if len(password) > 72:
        return ApiResponse(code=400, message="密码长度不能超过 72 位")

    # 生成 bcrypt 哈希并保存
    pwd_bytes = password.encode("utf-8")[:72]
    pwd_hash = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")
    save_local_account(username, pwd_hash)

    logger.info(f"管理账号注册成功: username={username}, ip={client_ip}")
    # 注册成功直接发放 token，无需再次登录
    token = create_access_token({"sub": username})
    return ApiResponse(data={"token": token, "username": username}, message="注册成功")


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


# ===== 115 生活事件监控 =====

class LifeEventMonitorIn(BaseModel):
    enabled: bool = False
    interval: int = 0  # 轮询间隔（秒），0=保持当前值


@router.get("/life-event-monitor", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def get_life_event_monitor_api():
    """获取 115 生活事件监控状态与配置"""
    from app.services.life_event_monitor import get_life_event_monitor
    return ApiResponse(data=get_life_event_monitor().get_status())


@router.post("/life-event-monitor", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def set_life_event_monitor_api(payload: LifeEventMonitorIn):
    """启用/停用 115 生活事件监控，并实时生效。
    启用后后台轮询 115 生活事件，感知网盘文件变化并增量同步到本地 STRM。"""
    from app.services.life_event_monitor import get_life_event_monitor
    monitor = get_life_event_monitor()

    interval = payload.interval if payload.interval > 0 else None
    status = monitor.set_enabled(payload.enabled, interval)

    if payload.enabled:
        ok = monitor.start()
        if not ok:
            return ApiResponse(code=500, message="生活事件监控启动失败，请检查日志")
    else:
        await monitor.stop()

    state = "已启动" if payload.enabled else "已停止"
    return ApiResponse(message=f"115 生活事件监控{state}", data=status)


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
    过滤系统敏感目录，防止路径遍历风险。
    """
    # 规范化路径
    if not path or path == "":
        path = "/"
    try:
        target = Path(path).resolve()
    except Exception:
        return ApiResponse(code=400, message="路径无效")

    # 系统敏感目录黑名单（Windows/Linux 均覆盖）
    _SENSITIVE_DIRS = {
        "windows": {"windows", "system32", "syswow64", "system volume information", "$recycle.bin"},
        "linux": {"proc", "sys", "dev", "boot", "etc", "root", "var/log", "var/lib/docker"},
    }

    def _is_sensitive(p: Path) -> bool:
        name_lower = p.name.lower()
        full_lower = str(p).lower().replace("\\", "/")
        for s in _SENSITIVE_DIRS["windows"]:
            if name_lower == s:
                return True
        for s in _SENSITIVE_DIRS["linux"]:
            if full_lower.endswith("/" + s) or full_lower == s or ("/" + s + "/") in full_lower:
                return True
        return False

    # 路径不存在时，逐级向上查找最近的存在目录
    original = target
    while not target.exists():
        if str(target) == target.anchor or target.parent == target:
            return ApiResponse(code=404, message=f"路径不存在: {original}")
        target = target.parent

    if not target.is_dir():
        target = target.parent if target.parent != target else Path("/")

    try:
        dirs = []
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith(".") and not _is_sensitive(entry):
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

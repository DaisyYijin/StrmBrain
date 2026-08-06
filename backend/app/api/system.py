"""
API 路由 - 系统信息（版本号、实时日志、本地账号、本地目录浏览、登录认证）
含 SSE 实时事件推送（#23）和 MCP Server 端点（#13）。
"""
import os
import json
import threading
import time
import asyncio
from pathlib import Path
from fastapi import APIRouter, Query, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import bcrypt

from app.core.json_storage import read_local_account, save_local_account, read_setting
from app.core.auth import (
    create_access_token, authenticate_user, require_auth, is_auth_enabled,
    verify_password, has_local_account,
)
from app.core.event_bus import get_event_bus, EventBus
from app.schemas import ApiResponse
from app.config import VERSION
from app.core.logbuffer import ring_handler, SOURCE_LABELS, get_logger

logger = get_logger("app.api.system")

router = APIRouter(prefix="/api", tags=["system"])

# ===== 登录速率限制 =====
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 分钟锁定
_login_attempts: dict[str, list[float]] = {}  # IP -> [timestamp, ...]
_login_attempts_lock = threading.Lock()

# 注册互斥锁：保证"检查是否已注册 → 写入账号"是原子操作，
# 防止并发请求同时通过检查导致重复注册（单人注册后其他人无法再注册）
_register_lock = threading.Lock()


def _check_login_rate_limit(client_ip: str) -> tuple[bool, str]:
    """检查登录速率限制，返回 (是否允许, 提示消息)"""
    now = time.time()
    cutoff = now - _LOGIN_LOCKOUT_SECONDS
    with _login_attempts_lock:
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
    with _login_attempts_lock:
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
    """首次部署注册管理账号（单次注册制）。
    仅当本地账号不存在时可用；已注册后返回 409 拒绝再次注册。
    使用互斥锁保证"检查-写入"原子性：并发请求下也只会成功一次，
    一旦注册成功，任何其他人（含新部署实例）都无法再注册。
    账号密码存储在 local_account.json（持久化，升级/重启不丢失）。
    """
    client_ip = request.client.host if request.client else "unknown"

    # 已存在本地账号 → 拒绝重复注册（快速路径，无需拿锁）
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

    # 加锁执行"再次检查 + 写入"，杜绝并发重复注册
    with _register_lock:
        # 锁内二次检查：并发请求同时通过上方快速检查时，这里只有一个能通过
        if has_local_account():
            logger.warning(f"并发注册被拒绝: username={username}, ip={client_ip}")
            return ApiResponse(code=409, message="管理账号已注册，请直接登录")

        # 生成 bcrypt 哈希并保存
        pwd_bytes = password.encode("utf-8")[:72]
        pwd_hash = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")
        ok = save_local_account(username, pwd_hash)
        if not ok:
            logger.error(f"管理账号保存失败: username={username}")
            return ApiResponse(code=500, message="账号保存失败，请检查 config 目录权限")

    logger.info(f"管理账号注册成功: username={username}, ip={client_ip}")
    # 注册成功直接发放 token，无需再次登录
    token = create_access_token({"sub": username})
    return ApiResponse(data={"token": token, "username": username}, message="注册成功")


@router.get("/auth/check", response_model=ApiResponse)
async def auth_check(user: dict = Depends(require_auth)):
    """检查当前 token 是否有效"""
    return ApiResponse(data={"username": user.get("sub", "")})


@router.get("/setup/status", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def setup_status():
    """feature #82: 首启配置向导——汇总各步骤完成状态，供向导判断从哪步开始。

    返回每步的 done 布尔 + 整体 completed。步骤：115 账号 / STRM 服务器 / Emby / TMDB。
    """
    from app.core.json_storage import read_accounts

    accounts = read_accounts()
    has_account = any(a.get("status") == 1 for a in accounts)

    strm_cfg = read_setting("strm") or {}
    has_strm = bool((strm_cfg.get("server_url") or "").strip())

    emby_cfg = read_setting("emby") or {}
    has_emby = bool((emby_cfg.get("host") or "").strip() and (emby_cfg.get("api_key") or "").strip())

    tmdb_cfg = read_setting("tmdb") or {}
    has_tmdb = bool((tmdb_cfg.get("api_key") or "").strip())

    # 是否已手动完成/跳过向导（持久化标记）
    wiz = read_setting("setup_wizard") or {}
    dismissed = bool(wiz.get("dismissed", False))

    steps = [
        {"key": "account", "name": "登录 115 账号", "done": has_account, "required": True},
        {"key": "strm", "name": "配置 STRM 服务器地址", "done": has_strm, "required": True},
        {"key": "emby", "name": "配置 Emby（可选）", "done": has_emby, "required": False},
        {"key": "tmdb", "name": "配置 TMDB（可选，用于整理）", "done": has_tmdb, "required": False},
    ]
    # 必填项全部完成即算 completed
    completed = all(s["done"] for s in steps if s["required"])
    return ApiResponse(data={
        "steps": steps,
        "completed": completed,
        "dismissed": dismissed,
        # 向导是否应展示：未完成必填 且 未手动关闭
        "show_wizard": (not completed) and (not dismissed),
    })


class SetupWizardUpdate(BaseModel):
    dismissed: bool = True


@router.post("/setup/dismiss", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def dismiss_setup_wizard(payload: SetupWizardUpdate):
    """标记首启向导为已完成/已跳过（不再自动弹出）。"""
    from app.core.json_storage import save_setting
    save_setting("setup_wizard", {"dismissed": bool(payload.dismissed)})
    return ApiResponse(message="已保存")


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


# ===== #23: SSE 实时事件推送 =====

@router.get("/events/sse")
async def events_sse(request: Request):
    """
    SSE 端点：实时推送事件总线事件。

    - 订阅事件总线的全部事件类型
    - 有事件时推送 `data: {json}\n\n`
    - 每 30 秒发送心跳 `: keepalive\n\n`
    - 客户端断开时取消订阅
    """
    bus = get_event_bus()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event_type: str, data: dict):
        """事件回调：跨线程安全投递到 SSE 队列"""
        event = {
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        }
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            # 事件循环已关闭（应用关闭中），忽略
            pass

    # 订阅全部事件类型
    bus.subscribe_all(_on_event)

    async def _event_stream():
        try:
            while True:
                # 客户端断开时退出
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 30 秒无事件，发送心跳保持连接
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe_all(_on_event)
            logger.debug("[sse] 事件 SSE 连接已断开，已取消订阅")

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 反代时禁用缓冲
        },
    )


# ===== #13: MCP Server（AI 助手控制）=====

def _mcp_enabled() -> bool:
    """检查 MCP Server 是否启用（默认关闭）"""
    cfg = read_setting("mcp_server")
    return bool(cfg.get("enabled", False))


@router.get("/mcp/sse")
async def mcp_sse(request: Request):
    """
    MCP Server SSE 端点：返回 MCP 协议的 SSE 流。

    客户端通过此 SSE 接收服务端消息（JSON-RPC 响应），
    通过 POST /api/mcp/messages 发送 JSON-RPC 请求。
    需在设置中启用 mcp_server.enabled。
    """
    if not _mcp_enabled():
        return ApiResponse(code=403, message="MCP Server 未启用")

    from app.services.mcp_server import get_mcp_server

    server = get_mcp_server()
    loop = asyncio.get_running_loop()
    session_id = server.create_session(loop)

    async def _mcp_stream():
        try:
            # 首条消息：告知客户端消息提交端点
            endpoint_url = "/api/mcp/messages"
            yield f"event: endpoint\ndata: {endpoint_url}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                msg = await server.wait_message(session_id, timeout=30.0)
                if msg is not None:
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            server.close_session(session_id)
            logger.debug(f"[mcp] SSE 会话已断开: {session_id}")

    return StreamingResponse(
        _mcp_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/mcp/messages")
async def mcp_messages(request: Request):
    """
    MCP Server POST 端点：处理 JSON-RPC 请求。

    - `initialize` -> 返回服务器能力
    - `tools/list` -> 返回工具列表
    - `tools/call` -> 执行指定工具
    需在设置中启用 mcp_server.enabled。
    """
    if not _mcp_enabled():
        return ApiResponse(code=403, message="MCP Server 未启用")

    from app.services.mcp_server import get_mcp_server

    try:
        body = await request.json()
    except Exception:
        return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}

    server = get_mcp_server()
    result = await server.handle_message(body)
    return result


class McpSettingsUpdate(BaseModel):
    """MCP Server 配置更新"""
    enabled: bool = False


@router.get("/mcp/settings", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def get_mcp_settings(request: Request):
    """G6: 获取 MCP Server 配置与状态。

    返回 {enabled, sse_url, messages_url, tools:[{name,description}], tool_count}。
    sse_url 基于当前请求 host 拼接，方便用户直接复制到 AI 客户端。
    """
    from app.services.mcp_server import get_mcp_server

    cfg = read_setting("mcp_server") or {}
    enabled = bool(cfg.get("enabled", False))

    # 基于当前请求拼接可访问的 SSE 地址
    base = str(request.base_url).rstrip("/")
    tools = []
    try:
        tools = get_mcp_server().get_tools_meta()
    except Exception as e:
        logger.warning(f"[mcp] 获取工具列表失败: {e}")

    return ApiResponse(data={
        "enabled": enabled,
        "sse_url": f"{base}/api/mcp/sse",
        "messages_url": f"{base}/api/mcp/messages",
        "tools": tools,
        "tool_count": len(tools),
    })


@router.post("/mcp/settings", response_model=ApiResponse, dependencies=[Depends(require_auth)])
async def save_mcp_settings(payload: McpSettingsUpdate):
    """G6: 保存 MCP Server 配置（启用/禁用）。"""
    from app.core.json_storage import save_setting

    cfg = read_setting("mcp_server") or {}
    cfg["enabled"] = bool(payload.enabled)
    save_setting("mcp_server", cfg)
    logger.info(f"[mcp] MCP Server {'已启用' if payload.enabled else '已禁用'}")
    return ApiResponse(message="已保存", data={"enabled": bool(payload.enabled)})

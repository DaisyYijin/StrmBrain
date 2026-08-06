"""
STRMhub 主入口
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from app.api import (v115_router, accounts_router, settings_router,
                     system_router, dashboard_router, organize_router,
                     tools_router, wechat_router,
                     ai_router, watcher_router,
                     emby_webhook_router, clouddownload_router,
                     notification_router, tasks_router,
                     sync_del_router)
from app.api.automation import router as automation_router
from app.api.automation import webhook_router as automation_webhook_router
from app.config import HOST, PORT, CORS_ORIGINS, AUTH_ENABLED, VERSION
from app.core.auth import verify_token
from app.core.logbuffer import setup_logging, get_logger
from app.core.progress import progress_manager

# 不需要认证的公开路径（精确匹配）
_PUBLIC_EXACT = frozenset({
    "/api/health",
    "/api/version",
    "/api/version/check",
    "/api/login",
    "/api/register",          # 首次部署注册管理账号（无 token 时也必须可访问）
    "/api/wechat/callback",  # 企微回调（GET 验证 + POST 消息）
    "/api/emby/webhook",     # Emby Webhook（POST 入库事件）
})

# 不需要认证的公开路径前缀
_PUBLIC_PREFIXES = (
    "/api/115/url/",   # 302 下载重定向（Emby 直接访问）
    "/ws/progress",    # WebSocket 进度通道
    "/api/automation/webhook/",  # 自动化规则 Webhook 触发（公开端点，token 鉴权）
    "/api/events/sse", # SSE 实时事件推送（EventSource 无法设置 Authorization 头）
    "/api/mcp/sse",    # MCP Server SSE 流（同上）
    "/api/mcp/messages",  # MCP Server JSON-RPC 消息（SSE 客户端 POST 提交）
    "/dav",            # WebDAV 只读访问（自带 HTTP Basic 认证，见 webdav_service）
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    setup_logging()
    logger = get_logger()

    # 启用 SQLite WAL 模式（提升并发读写性能，失败不影响启动）
    try:
        from app.core.db_helper import enable_wal_mode
        enable_wal_mode()
    except Exception as e:
        logger.warning(f"启用数据库 WAL 模式失败: {e}")

    from app.core.scheduler import init_scheduler, shutdown_scheduler

    # 注册事件总线的 event loop 引用（供工作线程跨线程发布事件）
    import asyncio as _asyncio
    from app.core.event_bus import get_event_bus
    get_event_bus().set_loop(_asyncio.get_running_loop())

    _version_task = None
    await init_scheduler()

    # 初始化新功能模块
    try:
        from app.services.ai_service import init_ai_client
        init_ai_client()
        logger.info("AI服务已初始化")
    except Exception as e:
        logger.warning(f"AI服务初始化失败: {e}")

    try:
        from app.services.folder_watcher import global_watcher_manager
        logger.info("文件监控管理器已就绪")
    except Exception as e:
        logger.warning(f"文件监控管理器初始化失败: {e}")

    # 初始化 115 生活事件监控（后台轮询网盘变化，事件驱动增量同步）
    try:
        from app.services.life_event_monitor import get_life_event_monitor
        get_life_event_monitor().start()
        logger.info("115 生活事件监控已初始化")
    except Exception as e:
        logger.warning(f"115 生活事件监控初始化失败: {e}")

    # 初始化媒体库删除级联服务（Emby 删除事件 → 115 网盘级联删除）
    try:
        from app.services.mediasyncdel_service import get_mediasync_del_service
        get_mediasync_del_service().start()
        logger.info("媒体库删除级联服务已启动")
    except Exception as e:
        logger.warning(f"媒体库删除级联服务初始化失败: {e}")

    # 初始化通知管理器
    try:
        from app.services.notification_manager import init_notification_manager
        init_notification_manager()
        logger.info("通知管理器已初始化")
    except Exception as e:
        logger.warning(f"通知管理器初始化失败: {e}")

    # 初始化 Telegram Bot 双向控制服务
    try:
        from app.services.telegram_bot import get_telegram_bot
        get_telegram_bot().start()
        logger.info("Telegram Bot 服务已初始化")
    except Exception as e:
        logger.warning(f"Telegram Bot 服务初始化失败: {e}")

    # 初始化上传队列
    try:
        from app.services.upload_queue import init_upload_queue
        init_upload_queue()
        logger.info("上传队列已初始化")
    except Exception as e:
        logger.warning(f"上传队列初始化失败: {e}")

    # 启动版本检查后台任务
    try:
        import asyncio
        from app.services.version_service import start_version_check_loop
        _version_task = asyncio.create_task(start_version_check_loop())
        _version_task.add_done_callback(
            lambda t: t.exception() and logger.warning(f"版本检查后台任务异常退出: {t.exception()}")
        )
        logger.info("版本检查后台任务已启动")
    except Exception as e:
        logger.warning(f"版本检查后台任务启动失败: {e}", exc_info=True)

    # 启动 Emby 反代服务（后台线程执行，绝不影响主应用启动流程）
    try:
        import threading as _threading

        def _start_proxy_bg():
            try:
                from app.services.emby_proxy import start_proxy
                status = start_proxy()
                if status.get("running"):
                    logger.info(f"Emby 反代服务已启动，端口 {status.get('current_port')}")
            except Exception as e:
                logger.warning(f"Emby 反代服务启动失败: {e}", exc_info=True)

        _proxy_thread = _threading.Thread(target=_start_proxy_bg, daemon=True, name="emby-proxy-starter")
        _proxy_thread.start()
    except Exception as e:
        logger.warning(f"启动 Emby 反代服务线程异常: {e}")

    logger.info("STRMhub 应用已启动（登录验证已开启）" if AUTH_ENABLED else "STRMhub 应用已启动（登录验证未开启）")
    yield
    await shutdown_scheduler()

    # 停止版本检查后台任务
    try:
        if _version_task and not _version_task.done():
            _version_task.cancel()
            try:
                await _version_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.warning(f"停止版本检查任务时异常: {e}")

    # 停止所有文件监控
    try:
        from app.services.folder_watcher import global_watcher_manager
        if global_watcher_manager:
            global_watcher_manager.stop_all()
    except Exception as e:
        logger.warning(f"停止文件监控时异常: {e}", exc_info=True)

    # 停止 115 生活事件监控
    try:
        from app.services.life_event_monitor import get_life_event_monitor
        await get_life_event_monitor().stop()
    except Exception as e:
        logger.warning(f"停止 115 生活事件监控时异常: {e}")

    # 停止 Telegram Bot 服务
    try:
        from app.services.telegram_bot import get_telegram_bot
        await get_telegram_bot().stop()
    except Exception as e:
        logger.warning(f"停止 Telegram Bot 服务时异常: {e}")

    # 停止媒体库删除级联服务
    try:
        from app.services.mediasyncdel_service import get_mediasync_del_service
        get_mediasync_del_service().stop()
    except Exception as e:
        logger.warning(f"停止媒体库删除级联服务时异常: {e}")

    # 停止上传队列
    try:
        from app.services.upload_queue import shutdown_upload_queue
        shutdown_upload_queue()
    except Exception as e:
        logger.warning(f"停止上传队列时异常: {e}")

    # 停止 Emby 反代服务
    try:
        from app.services.emby_proxy import stop_proxy
        stop_proxy()
    except Exception as e:
        logger.warning(f"停止 Emby 反代服务时异常: {e}")

    logger.info("STRMhub 应用已关闭")


app = FastAPI(
    title="STRMhub",
    description="115 网盘 STRM 生成工具",
    version=VERSION,
    lifespan=lifespan
)

# CORS（本工具前后端同源，仅允许通过 CORS_ORIGINS 环境变量配置的源跨域）
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )


def _is_public(path: str) -> bool:
    """判断路径是否为公开路径（不需要认证）"""
    if path in _PUBLIC_EXACT:
        return True
    for p in _PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    全局认证中间件：
    - AUTH_ENABLED=false 时跳过
    - 公开路径（健康检查、登录、下载重定向等）跳过
    - 其余 /api/ 路径要求有效的 Bearer token
    """
    if not AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path

    # 非 API 路径放行（静态文件、首页）
    if not path.startswith("/api/"):
        return await call_next(request)

    # 公开 API 路径放行
    if _is_public(path):
        return await call_next(request)

    # 提取 Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # JWT token 认证
        try:
            verify_token(token)
            return await call_next(request)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "认证失败，请重新登录", "data": None},
            )

    return JSONResponse(
        status_code=401,
        content={"code": 401, "message": "未提供认证凭证，请先登录", "data": None},
    )


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    """给所有 /api/ 响应禁用缓存，避免浏览器返回旧数据"""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "STRMhub"}


@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    """WebSocket 端点：实时推送整理/同步任务进度"""
    connected = await progress_manager.connect(ws)
    if not connected:
        return
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        progress_manager.disconnect(ws)
    except Exception:
        progress_manager.disconnect(ws)


# 注册 API 路由
app.include_router(v115_router)
app.include_router(accounts_router)
app.include_router(settings_router)
app.include_router(system_router)
app.include_router(dashboard_router)
app.include_router(organize_router)
app.include_router(tools_router)
app.include_router(wechat_router)
app.include_router(ai_router)
app.include_router(watcher_router)
app.include_router(emby_webhook_router)
app.include_router(clouddownload_router)
app.include_router(notification_router)
app.include_router(tasks_router)
app.include_router(sync_del_router)
app.include_router(automation_router)
app.include_router(automation_webhook_router)


# WebDAV 只读访问（G5）：支持 OPTIONS/PROPFIND/GET/HEAD 等方法，
# 用通用路由注册（Starlette route 支持自定义方法集）。
from app.services.webdav_service import handle_webdav as _handle_webdav

_DAV_METHODS = ["GET", "HEAD", "OPTIONS", "PROPFIND", "PUT", "DELETE",
                "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"]
app.add_route("/dav", _handle_webdav, methods=_DAV_METHODS)
app.add_route("/dav/{path:path}", _handle_webdav, methods=_DAV_METHODS)


# 静态文件（前端）- 必须放在最后
frontend_path = Path(__file__).parent.parent / "static"


@app.get("/")
async def index():
    """首页"""
    resp = FileResponse(frontend_path / "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/logo.png")
async def logo():
    """Logo 图片"""
    logo_file = frontend_path / "logo.png"
    if logo_file.exists():
        return FileResponse(logo_file, media_type="image/png")
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)

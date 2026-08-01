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
                     ai_router, apikeys_router, watcher_router,
                     emby_webhook_router)
from app.config import HOST, PORT, CORS_ORIGINS, AUTH_ENABLED
from app.core.auth import verify_token
from app.core.logbuffer import setup_logging, get_logger
from app.core.progress import progress_manager

# 不需要认证的公开路径（精确匹配）
_PUBLIC_EXACT = frozenset({
    "/api/health",
    "/api/version",
    "/api/login",
    "/api/wechat/callback",  # 企微回调（GET 验证 + POST 消息）
    "/api/emby/webhook",     # Emby Webhook（POST 入库事件）
})

# 不需要认证的公开路径前缀
_PUBLIC_PREFIXES = (
    "/api/115/url/",   # 302 下载重定向（Emby 直接访问）
    "/ws/progress",    # WebSocket 进度通道
    "/",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    setup_logging()
    logger = get_logger()
    from app.core.scheduler import init_scheduler, shutdown_scheduler
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

    logger.info(f"STRMhub started (AUTH_ENABLED={AUTH_ENABLED})")
    yield
    await shutdown_scheduler()

    # 停止所有文件监控
    try:
        from app.services.folder_watcher import global_watcher_manager
        if global_watcher_manager:
            global_watcher_manager.stop_all()
    except Exception:
        pass

    logger.info("Application closed")


app = FastAPI(
    title="STRMhub",
    description="115 网盘 STRM 生成工具",
    version="0.1.0",
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
        # 先尝试 API Key 认证（sk- 开头）
        if token.startswith("sk-"):
            try:
                from app.core.api_key import validate_api_key
                if validate_api_key(token):
                    return await call_next(request)
            except Exception:
                pass
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "API Key无效或已禁用", "data": None},
            )
        # JWT token 认证
        try:
            verify_token(token)
            return await call_next(request)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"code": 401, "message": "认证失败，请重新登录", "data": None},
            )

    # 支持 X-API-Key 头
    api_key_header = request.headers.get("X-API-Key", "")
    if api_key_header.startswith("sk-"):
        try:
            from app.core.api_key import validate_api_key
            if validate_api_key(api_key_header):
                return await call_next(request)
        except Exception:
            pass
        return JSONResponse(
            status_code=401,
            content={"code": 401, "message": "API Key无效或已禁用", "data": None},
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
    await progress_manager.connect(ws)
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
app.include_router(apikeys_router)
app.include_router(watcher_router)
app.include_router(emby_webhook_router)


# 静态文件（前端）- 必须放在最后
frontend_path = Path(__file__).parent.parent / "static"


@app.get("/")
async def index():
    """首页"""
    return FileResponse(frontend_path / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)

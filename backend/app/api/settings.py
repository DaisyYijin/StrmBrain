"""
API 路由 - 系统设置（Emby / TMDB / STRM / 通知 / API 间隔）
数据存储在 settings.json，不依赖数据库。
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.json_storage import read_setting, save_setting
from app.schemas import ApiResponse

router = APIRouter(prefix="/api/settings", tags=["settings"])


class EmbySettings(BaseModel):
    host: str = ""
    api_key: str = ""


class TmdbSettings(BaseModel):
    api_key: str = ""
    api_domain: str = ""
    image_domain: str = ""
    language: str = "both"  # both=中文+英文, zh=仅中文, en=仅英文


@router.get("/emby", response_model=ApiResponse)
async def get_emby_settings():
    """获取 Emby 设置"""
    data = read_setting("emby")
    return ApiResponse(data={
        "host": data.get("host", ""),
        "api_key": data.get("api_key", ""),
    })


@router.post("/emby", response_model=ApiResponse)
async def save_emby_settings(payload: EmbySettings):
    """保存 Emby 设置（同时清除仪表盘缓存）"""
    save_setting("emby", payload.model_dump())
    from app.core.cache import invalidate
    invalidate()
    return ApiResponse(message="已保存")


@router.post("/emby/test", response_model=ApiResponse)
async def test_emby(payload: EmbySettings):
    """测试 Emby 连接"""
    from app.services.emby import test_connection
    ok, msg = await test_connection(payload.host, payload.api_key)
    return ApiResponse(code=0 if ok else 500, message=msg)


# ===== Emby 反代配置 =====

class EmbyProxySettings(BaseModel):
    """Emby 反代配置"""
    enabled: bool = False
    port: int = 8787


@router.get("/emby-proxy", response_model=ApiResponse)
async def get_emby_proxy_settings():
    """获取 Emby 反代配置与状态"""
    from app.services.emby_proxy import get_status
    status = get_status()
    return ApiResponse(data={
        "enabled": status.get("enabled", False),
        "port": status.get("port", 8787),
        "running": status.get("running", False),
        "emby_configured": status.get("emby_configured", False),
        "current_port": status.get("current_port", 0),
    })


@router.post("/emby-proxy", response_model=ApiResponse)
async def save_emby_proxy_settings(payload: EmbyProxySettings):
    """保存 Emby 反代配置并应用（变更端口或启停时自动重启服务）"""
    if payload.port < 1 or payload.port > 65535:
        return ApiResponse(code=400, message="端口范围无效（1-65535）")
    from app.services.emby_proxy import save_config
    status = save_config(payload.enabled, payload.port)
    running = status.get("running", False)
    if payload.enabled and not running:
        return ApiResponse(code=500, message="反代服务启动失败，请检查 Emby 配置和日志")
    return ApiResponse(message="已保存" if not payload.enabled else "反代服务已启动")


@router.post("/emby-proxy/restart", response_model=ApiResponse)
async def restart_emby_proxy():
    """重启 Emby 反代服务"""
    from app.services.emby_proxy import get_status, start_proxy, stop_proxy
    status = get_status()
    if not status.get("enabled"):
        return ApiResponse(code=400, message="反代未启用，请先启用")
    stop_proxy()
    status = start_proxy()
    if status.get("running"):
        return ApiResponse(message="反代服务已重启")
    return ApiResponse(code=500, message="重启失败，请检查日志")


@router.get("/tmdb", response_model=ApiResponse)
async def get_tmdb_settings():
    """获取 TMDB 设置"""
    data = read_setting("tmdb")
    from app.services.tmdb_service import DEFAULT_API_DOMAIN, DEFAULT_IMAGE_DOMAIN
    return ApiResponse(data={
        "api_key": data.get("api_key", ""),
        "api_domain": data.get("api_domain", "") or DEFAULT_API_DOMAIN,
        "image_domain": data.get("image_domain", "") or DEFAULT_IMAGE_DOMAIN,
        "language": data.get("language", "both"),
    })


@router.post("/tmdb", response_model=ApiResponse)
async def save_tmdb_settings(payload: TmdbSettings):
    """保存 TMDB 设置（API Key + 域名）"""
    save_setting("tmdb", payload.model_dump())
    # 清空 TMDB 搜索缓存
    from app.services.tmdb_service import TmdbService
    TmdbService.clear_cache()
    return ApiResponse(message="已保存")


@router.post("/tmdb/test", response_model=ApiResponse)
async def test_tmdb(payload: TmdbSettings):
    """测试 TMDB API 连通性"""
    if not payload.api_key:
        return ApiResponse(code=400, message="请填写 API Key")
    from app.services.tmdb_service import DEFAULT_API_DOMAIN
    domain = (payload.api_domain or "").strip().rstrip("/") or DEFAULT_API_DOMAIN
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=5, write=5, pool=5)) as client:
            resp = await client.get(
                f"{domain}/3/configuration",
                params={"api_key": payload.api_key}
            )
            if resp.status_code == 200:
                return ApiResponse(message="TMDB API 连接成功")
            else:
                return ApiResponse(code=500, message=f"连接失败 (HTTP {resp.status_code})")
    except httpx.ConnectTimeout:
        return ApiResponse(code=500, message=f"连接超时，请检查域名 {domain} 是否可访问")
    except httpx.ConnectError:
        return ApiResponse(code=500, message=f"无法连接到 {domain}，请检查域名或网络")
    except httpx.TimeoutException:
        return ApiResponse(code=500, message=f"请求超时，请检查域名 {domain} 是否可访问")
    except Exception as e:
        return ApiResponse(code=500, message=f"测试失败: {str(e)}")


# ===== STRM 配置 =====

class StrmSettings(BaseModel):
    """STRM 直链配置"""
    server_url: str = ""
    server_port: str = ""
    overwrite_mode: str = "skip"


@router.get("/strm", response_model=ApiResponse)
async def get_strm_settings():
    """获取 STRM 配置"""
    data = read_setting("strm")
    return ApiResponse(data={
        "server_url": data.get("server_url", ""),
        "server_port": data.get("server_port", ""),
        "overwrite_mode": data.get("overwrite_mode", "skip"),
    })


@router.post("/strm", response_model=ApiResponse)
async def save_strm_settings(payload: StrmSettings):
    """保存 STRM 配置"""
    save_setting("strm", payload.model_dump())
    return ApiResponse(message="已保存")


# ===== STRM 播放安全配置 =====

@router.get("/strm-security", response_model=ApiResponse)
async def get_strm_security():
    """获取 STRM 播放安全配置"""
    from app.services.strm_token import get_security_info
    return ApiResponse(data=get_security_info())


class StrmSecurityUpdate(BaseModel):
    """STRM 安全配置更新"""
    enabled: bool = True


@router.post("/strm-security", response_model=ApiResponse)
async def save_strm_security(payload: StrmSecurityUpdate):
    """更新 STRM 播放安全配置（启用/禁用 Token 验证）"""
    from app.services.strm_token import set_enabled
    set_enabled(payload.enabled)
    return ApiResponse(message="已保存")


@router.post("/strm-security/rotate", response_model=ApiResponse)
async def rotate_strm_token():
    """轮换 STRM 播放 Token（旧 Token 立即失效，需重新生成 STRM 文件）"""
    from app.services.strm_token import rotate_token
    rotate_token()
    return ApiResponse(message="Token 已轮换，请重新执行全量同步以更新 STRM 文件")


# ===== Emby 入库刷新配置 =====

class EmbySyncSettings(BaseModel):
    """Emby 入库刷新配置"""
    auto_refresh: bool = True       # 同步完成后自动刷新 Emby 媒体库
    auto_upload: bool = False       # Emby 刮削后自动上传 nfo/图片到网盘
    upload_delay: int = 60          # 刷新后等待秒数（等待 Emby 刮削完成）


@router.get("/emby-sync", response_model=ApiResponse)
async def get_emby_sync_settings():
    """获取 Emby 入库刷新配置"""
    data = read_setting("emby_sync")
    return ApiResponse(data={
        "auto_refresh": data.get("auto_refresh", True),
        "auto_upload": data.get("auto_upload", False),
        "upload_delay": data.get("upload_delay", 60),
    })


@router.post("/emby-sync", response_model=ApiResponse)
async def save_emby_sync_settings(payload: EmbySyncSettings):
    """保存 Emby 入库刷新配置"""
    save_setting("emby_sync", payload.model_dump())
    return ApiResponse(message="已保存")


# ===== Emby 入库通知配置 =====

class EmbyNotifySettings(BaseModel):
    """Emby 入库通知配置"""
    notify_on_sync: bool = True        # 同步完成后发送通知
    notify_on_organize: bool = True    # 整理完成后发送通知
    notify_on_error: bool = True       # 任务异常时发送通知
    notify_on_emby_add: bool = True    # Emby 入库时发送通知（通过 Webhook 触发）
    webhook_token: str = ""            # Emby Webhook 认证 token


@router.get("/emby-notify", response_model=ApiResponse)
async def get_emby_notify_settings():
    """获取 Emby 入库通知配置"""
    data = read_setting("emby_notify")
    return ApiResponse(data={
        "notify_on_sync": data.get("notify_on_sync", True),
        "notify_on_organize": data.get("notify_on_organize", True),
        "notify_on_error": data.get("notify_on_error", True),
        "notify_on_emby_add": data.get("notify_on_emby_add", True),
        "webhook_token": data.get("webhook_token", ""),
    })


@router.post("/emby-notify", response_model=ApiResponse)
async def save_emby_notify_settings(payload: EmbyNotifySettings):
    """保存 Emby 入库通知配置"""
    save_setting("emby_notify", payload.model_dump())
    return ApiResponse(message="已保存")


# ===== API 请求间隔配置 =====

class ApiIntervalSettings(BaseModel):
    """115 API 请求间隔配置"""
    interval: float = 0.3


@router.get("/api-interval", response_model=ApiResponse)
async def get_api_interval_settings():
    """获取 API 请求间隔配置"""
    data = read_setting("api_interval")
    return ApiResponse(data={
        "interval": data.get("interval", 0.3),
    })


@router.post("/api-interval", response_model=ApiResponse)
async def save_api_interval_settings(payload: ApiIntervalSettings):
    """保存 API 请求间隔配置"""
    save_setting("api_interval", payload.model_dump())
    return ApiResponse(message="已保存")


# ===== 通知配置 =====

class NotificationSettings(BaseModel):
    """通知设置 - 企业微信自建应用 / Telegram / QQ 机器人"""
    # 企业微信自建应用
    wechat_enabled: bool = False
    wechat_corp_id: str = ""
    wechat_agent_secret: str = ""
    wechat_agent_id: str = ""
    wechat_api_base: str = "https://qyapi.weixin.qq.com"
    wechat_callback_token: str = ""
    wechat_callback_aes_key: str = ""
    wechat_default_user: str = "@all"
    # Telegram
    tg_enabled: bool = False
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    # QQ 机器人
    qq_enabled: bool = False
    qq_api_url: str = ""
    qq_access_token: str = ""
    qq_user_id: str = ""
    qq_group_id: str = ""


@router.get("/notification", response_model=ApiResponse)
async def get_notification_settings():
    """获取通知设置"""
    data = read_setting("notification")
    return ApiResponse(data={
        "wechat_enabled": data.get("wechat_enabled", False),
        "wechat_corp_id": data.get("wechat_corp_id", ""),
        "wechat_agent_secret": data.get("wechat_agent_secret", ""),
        "wechat_agent_id": data.get("wechat_agent_id", ""),
        "wechat_api_base": data.get("wechat_api_base", "https://qyapi.weixin.qq.com"),
        "wechat_callback_token": data.get("wechat_callback_token", ""),
        "wechat_callback_aes_key": data.get("wechat_callback_aes_key", ""),
        "wechat_default_user": data.get("wechat_default_user", "@all"),
        "tg_enabled": data.get("tg_enabled", False),
        "tg_bot_token": data.get("tg_bot_token", ""),
        "tg_chat_id": data.get("tg_chat_id", ""),
        "qq_enabled": data.get("qq_enabled", False),
        "qq_api_url": data.get("qq_api_url", ""),
        "qq_access_token": data.get("qq_access_token", ""),
        "qq_user_id": data.get("qq_user_id", ""),
        "qq_group_id": data.get("qq_group_id", ""),
    })


@router.post("/notification", response_model=ApiResponse)
async def save_notification_settings(payload: NotificationSettings):
    """保存通知设置"""
    save_setting("notification", payload.model_dump())
    return ApiResponse(message="已保存")


class TestNotificationRequest(BaseModel):
    """测试通知请求"""
    channel: str = "wechat"
    wechat_corp_id: str = ""
    wechat_agent_secret: str = ""
    wechat_agent_id: str = ""
    wechat_api_base: str = "https://qyapi.weixin.qq.com"
    wechat_default_user: str = "@all"
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    qq_api_url: str = ""
    qq_access_token: str = ""
    qq_user_id: str = ""
    qq_group_id: str = ""


@router.post("/notification/test", response_model=ApiResponse)
async def test_notification(payload: TestNotificationRequest):
    """测试通知发送（指定渠道）"""
    test_content = "> 这是一条测试消息，说明通知配置正常。\n> 如果你收到了这条消息，说明机器人已配置成功。"

    if payload.channel == "wechat":
        if not payload.wechat_corp_id or not payload.wechat_agent_secret or not payload.wechat_agent_id:
            return ApiResponse(code=400, message="请填写企业ID、应用Secret、应用ID")
        from app.services.wechat_app import WeChatAppService
        WeChatAppService._token_cache = {}
        test_settings = {
            "wechat_enabled": True,
            "wechat_corp_id": payload.wechat_corp_id,
            "wechat_agent_secret": payload.wechat_agent_secret,
            "wechat_agent_id": payload.wechat_agent_id,
            "wechat_api_base": payload.wechat_api_base or "https://qyapi.weixin.qq.com",
            "wechat_default_user": payload.wechat_default_user or "@all",
        }
        original = WeChatAppService._get_settings
        WeChatAppService._get_settings = classmethod(lambda cls: test_settings)
        try:
            ok = await WeChatAppService.send_markdown(
                f"### STRMhub 通知测试\n{test_content}"
            )
        finally:
            WeChatAppService._get_settings = original
    elif payload.channel == "telegram":
        if not payload.tg_bot_token or not payload.tg_chat_id:
            return ApiResponse(code=400, message="请填写 Bot Token 和 Chat ID")
        from app.services.notification_service import NotificationService
        ok = await NotificationService.send_telegram_message(
            payload.tg_bot_token, payload.tg_chat_id, "STRMhub 通知测试", test_content
        )
    elif payload.channel == "qq":
        if not payload.qq_api_url or (not payload.qq_user_id and not payload.qq_group_id):
            return ApiResponse(code=400, message="请填写 API 地址和 QQ 号/群号")
        from app.services.notification_service import NotificationService
        ok = await NotificationService.send_qq_message(
            payload.qq_api_url, payload.qq_access_token,
            payload.qq_user_id, payload.qq_group_id,
            "STRMhub 通知测试", test_content
        )
    else:
        return ApiResponse(code=400, message="未知通知渠道")

    return ApiResponse(code=0 if ok else 500, message="测试通知发送成功" if ok else "测试通知发送失败")

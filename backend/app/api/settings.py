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
    """保存 Emby 设置（自动补全协议，同时清除仪表盘缓存，并自动启动反代服务）"""
    host = (payload.host or "").strip()
    # 自动补全缺失的 http:// 协议前缀（用户可能只填 IP:端口）
    if host and not host.lower().startswith(("http://", "https://")):
        host = "http://" + host
    save_setting("emby", {"host": host, "api_key": payload.api_key.strip()})
    from app.core.cache import invalidate
    invalidate()
    # 配置 Emby 后自动启动反代（302 播放），无需手动开启
    try:
        from app.services.emby_proxy import start_proxy
        status = start_proxy()
        if status.get("running"):
            return ApiResponse(message="已保存，反代服务已启动")
    except Exception as e:
        from app.core.logbuffer import get_logger
        get_logger().warning(f"[settings] 保存 Emby 后启动反代失败: {e}")
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
    port: int = 6086


@router.get("/emby-proxy", response_model=ApiResponse)
async def get_emby_proxy_settings():
    """获取 Emby 反代配置与状态"""
    from app.services.emby_proxy import get_status
    status = get_status()
    return ApiResponse(data={
        "enabled": status.get("enabled", True),
        "port": status.get("port", 6086),
        "running": status.get("running", False),
        "emby_configured": status.get("emby_configured", False),
        "current_port": status.get("current_port", 0),
    })


@router.post("/emby-proxy", response_model=ApiResponse)
async def save_emby_proxy_settings(payload: EmbyProxySettings):
    """保存 Emby 反代配置并应用（反代为内置功能，仅保存端口并重启服务）"""
    if payload.port < 1 or payload.port > 65535:
        return ApiResponse(code=400, message="端口范围无效（1-65535）")
    from app.services.emby_proxy import save_config
    status = save_config(True, payload.port)
    running = status.get("running", False)
    if not running:
        return ApiResponse(code=500, message="反代服务启动失败，请检查 Emby 配置和日志")
    return ApiResponse(message="反代服务已重启")


@router.post("/emby-proxy/restart", response_model=ApiResponse)
async def restart_emby_proxy():
    """重启 Emby 反代服务"""
    from app.services.emby_proxy import start_proxy, stop_proxy
    stop_proxy()
    status = start_proxy()
    if status.get("running"):
        return ApiResponse(message="反代服务已重启")
    return ApiResponse(code=500, message="重启失败，请检查日志")


# ===== P1-6: Emby 反代路由规则 =====

class RouteRulesPayload(BaseModel):
    """路由规则配置（P1-6）"""
    route_rules: list[dict] = []


@router.get("/emby-proxy/route-rules", response_model=ApiResponse)
async def get_route_rules():
    """获取 Emby 反代路由规则"""
    data = read_setting("emby_proxy")
    return ApiResponse(data={
        "route_rules": data.get("route_rules", []),
    })


@router.post("/emby-proxy/route-rules", response_model=ApiResponse)
async def save_route_rules(payload: RouteRulesPayload):
    """保存 Emby 反代路由规则（合并到现有 emby_proxy 配置，不覆盖端口等字段）"""
    data = read_setting("emby_proxy")
    data["route_rules"] = payload.route_rules
    save_setting("emby_proxy", data)
    return ApiResponse(message="路由规则已保存")


@router.get("/tmdb", response_model=ApiResponse)
async def get_tmdb_settings():
    """获取 TMDB 设置"""
    data = read_setting("tmdb")
    from app.services.tmdb_service import DEFAULT_API_DOMAIN, DEFAULT_IMAGE_DOMAIN
    return ApiResponse(data={
        "api_key": data.get("api_key", ""),
        "api_domain": data.get("api_domain", "") or DEFAULT_API_DOMAIN,
        "image_domain": data.get("image_domain", "") or DEFAULT_IMAGE_DOMAIN,
        "language": data.get("language", "zh"),
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
    server_port: str = "6060"
    overwrite_mode: str = "skip"


@router.get("/strm", response_model=ApiResponse)
async def get_strm_settings():
    """获取 STRM 配置"""
    data = read_setting("strm")
    return ApiResponse(data={
        "server_url": data.get("server_url", ""),
        "server_port": data.get("server_port", "6060"),
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
    interval: float = 3.0
    retry_cooldown: float = 30.0  # 限流/错误重试的冷却等待时间（秒）


@router.get("/api-interval", response_model=ApiResponse)
async def get_api_interval_settings():
    """获取 API 请求间隔配置"""
    data = read_setting("api_interval")
    return ApiResponse(data={
        "interval": data.get("interval", 3.0),
        "retry_cooldown": data.get("retry_cooldown", 30.0),
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


# ===== P1-10: 缓存预热配置 =====

class CacheWarmerSettings(BaseModel):
    """缓存预热配置"""
    enabled: bool = False
    cron: str = "0 3 * * *"
    max_files: int = 100


@router.get("/cache-warmer", response_model=ApiResponse)
async def get_cache_warmer_settings():
    """获取缓存预热配置与状态"""
    from app.services.cache_warmer import get_cache_warmer
    cfg = get_cache_warmer().get_config()
    status = get_cache_warmer().get_status()
    return ApiResponse(data={
        "enabled": cfg.get("enabled", False),
        "cron": cfg.get("cron", "0 3 * * *"),
        "max_files": cfg.get("max_files", 100),
        "status": status,
    })


@router.post("/cache-warmer", response_model=ApiResponse)
async def save_cache_warmer_settings(payload: CacheWarmerSettings):
    """保存缓存预热配置并重注册定时任务"""
    from app.services.cache_warmer import get_cache_warmer
    get_cache_warmer().save_config(
        enabled=payload.enabled,
        cron=payload.cron,
        max_files=payload.max_files,
    )
    # 重注册定时任务
    try:
        from app.core.scheduler import register_cache_warmer
        await register_cache_warmer()
    except Exception as e:
        from app.core.logbuffer import get_logger
        get_logger().warning(f"[settings] 重注册缓存预热任务失败: {e}")
    return ApiResponse(message="已保存")


@router.post("/cache-warmer/run", response_model=ApiResponse)
async def run_cache_warmer():
    """立即执行一次缓存预热"""
    from app.services.cache_warmer import get_cache_warmer
    try:
        result = await get_cache_warmer().warm_once()
        return ApiResponse(data=result)
    except Exception as e:
        return ApiResponse(code=500, message=f"预热失败: {str(e)}")


# ===== P1-8: Telegram Bot 双向控制配置 =====

class TelegramBotSettings(BaseModel):
    """Telegram Bot 配置"""
    enabled: bool = False
    token: str = ""
    allowed_chat_ids: list[int] = []


@router.get("/telegram-bot", response_model=ApiResponse)
async def get_telegram_bot_settings():
    """获取 Telegram Bot 配置与状态"""
    from app.services.telegram_bot import get_telegram_bot
    cfg = get_telegram_bot().get_config()
    status = get_telegram_bot().get_status()
    return ApiResponse(data={
        "enabled": cfg.get("enabled", False),
        "token": cfg.get("token", ""),
        "allowed_chat_ids": cfg.get("allowed_chat_ids", []),
        "status": status,
    })


@router.post("/telegram-bot", response_model=ApiResponse)
async def save_telegram_bot_settings(payload: TelegramBotSettings):
    """保存 Telegram Bot 配置并重启 Bot"""
    from app.services.telegram_bot import get_telegram_bot
    get_telegram_bot().save_config(
        enabled=payload.enabled,
        token=payload.token,
        allowed_chat_ids=payload.allowed_chat_ids,
    )
    # 重启 Bot（先停止再启动，配置变更后生效）
    try:
        get_telegram_bot().restart()
    except Exception as e:
        from app.core.logbuffer import get_logger
        get_logger().warning(f"[settings] 重启 Telegram Bot 失败: {e}")
    return ApiResponse(message="已保存")


@router.post("/telegram-bot/test", response_model=ApiResponse)
async def test_telegram_bot():
    """发送测试消息到第一个允许的 Chat ID"""
    from app.services.telegram_bot import get_telegram_bot
    bot = get_telegram_bot()
    cfg = bot.get_config()
    token = cfg.get("token", "")
    allowed_ids = cfg.get("allowed_chat_ids", [])

    if not token:
        return ApiResponse(code=400, message="请先配置 Bot Token")
    if not allowed_ids:
        return ApiResponse(code=400, message="请先配置允许的 Chat ID")

    chat_id = allowed_ids[0]
    test_text = (
        "<b>STRMhub Telegram Bot 测试</b>\n"
        "如果你收到了这条消息，说明 Bot 配置正常。"
    )
    ok = await bot.send_message_async(chat_id, test_text)
    return ApiResponse(
        code=0 if ok else 500,
        message="测试消息发送成功" if ok else "测试消息发送失败",
    )


# ===== #19: 路径映射配置 =====

class PathMappingRule(BaseModel):
    """单条路径映射规则"""
    op: str = "replace"          # replace / replaceAll / prefix / suffix
    source: str = "all"          # local / strm_rel / strm_url / all
    from_: str = ""              # 源子串（replace/replaceAll 用）
    to: str = ""                 # 目标子串


class PathMappingPayload(BaseModel):
    """路径映射配置载荷"""
    rules: list[dict] = []


@router.get("/path-mapping", response_model=ApiResponse)
async def get_path_mapping():
    """获取路径映射规则列表"""
    from app.services.path_mapper import PathMapper
    rules = PathMapper.get_config()
    return ApiResponse(data={"rules": rules})


@router.post("/path-mapping", response_model=ApiResponse)
async def save_path_mapping(payload: PathMappingPayload):
    """保存路径映射规则列表（写 STRM 内容前按规则顺序应用）"""
    from app.services.path_mapper import PathMapper
    PathMapper.save_config(payload.rules)
    return ApiResponse(message="路径映射规则已保存")


# ===== #16: 每目录独立配置覆盖 =====

class DirOverridePayload(BaseModel):
    """目录覆盖配置载荷"""
    dir_overrides: list[dict] = []  # [{"path": str, "config": {...}}]


@router.get("/dir-overrides", response_model=ApiResponse)
async def get_dir_overrides():
    """获取每目录独立配置覆盖列表"""
    data = read_setting("organize_dirs")
    return ApiResponse(data={
        "dir_overrides": data.get("dir_overrides", []) if isinstance(data, dict) else [],
    })


@router.post("/dir-overrides", response_model=ApiResponse)
async def save_dir_overrides(payload: DirOverridePayload):
    """保存每目录独立配置覆盖列表（合并到现有 organize_dirs 配置，不覆盖其他字段）"""
    data = read_setting("organize_dirs")
    if not isinstance(data, dict):
        data = {}
    data["dir_overrides"] = payload.dir_overrides
    save_setting("organize_dirs", data)
    return ApiResponse(message="目录覆盖配置已保存")


# ===== #14: Emby 媒体信息上传/下载 =====

@router.get("/emby-media-info", response_model=ApiResponse)
async def get_emby_media_info_status():
    """获取 Emby 媒体信息上传/下载状态。

    返回累计上传/下载次数、缓存命中率、Emby 配置状态。
    """
    from app.services.emby_media_info import get_emby_media_info_service
    service = get_emby_media_info_service()
    return ApiResponse(data=service.get_status())


# ===== #30: 内置自动更新 =====

class UpdaterSettings(BaseModel):
    """自动更新配置"""
    enabled: bool = False
    check_interval: int = 24  # 检查间隔（小时）
    auto_install: bool = False
    pre_release: bool = False


@router.get("/updater", response_model=ApiResponse)
async def get_updater_settings():
    """获取自动更新配置与状态"""
    from app.services.updater import get_updater
    data = read_setting("updater")
    updater = get_updater()
    status = updater.get_status()
    return ApiResponse(data={
        "enabled": data.get("enabled", False),
        "check_interval": data.get("check_interval", 24),
        "auto_install": data.get("auto_install", False),
        "pre_release": data.get("pre_release", False),
        "status": status,
    })


@router.post("/updater", response_model=ApiResponse)
async def save_updater_settings(payload: UpdaterSettings):
    """保存自动更新配置"""
    save_setting("updater", payload.model_dump())
    return ApiResponse(message="更新设置已保存")


@router.post("/updater/check", response_model=ApiResponse)
async def check_update():
    """主动触发版本检查"""
    from app.services.updater import get_updater
    result = await get_updater().check_latest()
    return ApiResponse(data=result)


@router.get("/updater/status", response_model=ApiResponse)
async def get_updater_status():
    """获取当前版本与最新版本状态"""
    from app.services.updater import get_updater
    return ApiResponse(data=get_updater().get_status())


# ===== #35: alist Sign 配置 =====

class AlistSignSettings(BaseModel):
    """alist URL 签名配置"""
    enabled: bool = False
    secret_key: str = ""
    expire_seconds: int = 7200


@router.get("/alist-sign", response_model=ApiResponse)
async def get_alist_sign_settings():
    """获取 alist 签名配置"""
    from app.services.path_mapper import AlistSigner
    return ApiResponse(data=AlistSigner.get_config())


@router.post("/alist-sign", response_model=ApiResponse)
async def save_alist_sign_settings(payload: AlistSignSettings):
    """保存 alist 签名配置"""
    from app.services.path_mapper import AlistSigner
    AlistSigner.save_config(
        enabled=payload.enabled,
        secret_key=payload.secret_key,
        expire_seconds=payload.expire_seconds,
    )
    return ApiResponse(message="alist 签名配置已保存")


# ===== #26: 外部播放器脚本配置 =====

class ExternalPlayerSettings(BaseModel):
    """外部播放器配置"""
    default_player: str = "potplayer"
    custom_templates: dict = {}


@router.get("/external-player", response_model=ApiResponse)
async def get_external_player_settings():
    """获取外部播放器配置与支持的播放器列表"""
    from app.services.external_player import ExternalPlayerService
    svc = ExternalPlayerService()
    return ApiResponse(data={
        "default_player": svc.get_default_player(),
        "custom_templates": svc.get_custom_templates(),
        "supported_players": svc.get_supported_players(),
    })


@router.post("/external-player", response_model=ApiResponse)
async def save_external_player_settings(payload: ExternalPlayerSettings):
    """保存外部播放器配置"""
    from app.services.external_player import ExternalPlayerService
    svc = ExternalPlayerService()
    svc.set_default_player(payload.default_player)
    svc.save_custom_templates(payload.custom_templates)
    return ApiResponse(message="外部播放器配置已保存")


# ===== #34: 整理覆盖检查配置 =====

class OrganizeOverwriteSettings(BaseModel):
    """整理覆盖策略配置"""
    overwrite_policy: str = "skip"  # skip / replace / rename


@router.get("/organize-overwrite", response_model=ApiResponse)
async def get_organize_overwrite_settings():
    """获取整理覆盖策略"""
    data = read_setting("organize_dirs")
    return ApiResponse(data={
        "overwrite_policy": data.get("overwrite_policy", "skip") if isinstance(data, dict) else "skip",
    })


@router.post("/organize-overwrite", response_model=ApiResponse)
async def save_organize_overwrite_settings(payload: OrganizeOverwriteSettings):
    """保存整理覆盖策略（合并到 organize_dirs 配置）"""
    data = read_setting("organize_dirs")
    if not isinstance(data, dict):
        data = {}
    data["overwrite_policy"] = payload.overwrite_policy
    save_setting("organize_dirs", data)
    return ApiResponse(message="覆盖策略已保存")

"""
API 路由 - 仪表盘
"""
import os
import platform
import shutil
import time

from fastapi import APIRouter

from app.core.json_storage import read_setting, get_first_valid_account, read_accounts
from app.schemas import ApiResponse
from app.services.emby import EmbyClient
from app.core.cache import cached, invalidate

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# 缓存 TTL（秒）
TTL_SHORT = 60       # 列表/统计类：1 分钟
TTL_MEDIUM = 120     # 趋势/分布类：2 分钟

# 应用启动时间（用于计算运行时长）
_APP_START_TS = time.time()


async def _get_emby_client() -> EmbyClient | None:
    cfg = read_setting("emby")
    if not cfg.get("host") or not cfg.get("api_key"):
        return None
    return EmbyClient(cfg["host"], cfg["api_key"])


def _collect_system_info() -> dict:
    """收集本机系统信息（CPU/内存/磁盘/运行时长）。"""
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
        "uptime": int(time.time() - _APP_START_TS),
    }

    # 内存信息（优先用 psutil，无则跳过）
    try:
        import psutil
        mem = psutil.virtual_memory()
        info["mem_total"] = mem.total
        info["mem_used"] = mem.used
        info["mem_percent"] = mem.percent
        info["cpu_percent"] = psutil.cpu_percent(interval=None)  # 非阻塞，返回上次调用以来的平均值
    except ImportError:
        pass

    # 磁盘信息（挂载点 / 或 cwd 所在分区）
    try:
        disk = shutil.disk_usage("/")
        info["disk_total"] = disk.total
        info["disk_used"] = disk.used
        info["disk_free"] = disk.free
        info["disk_percent"] = round(disk.used / disk.total * 100, 1) if disk.total else 0
    except Exception:
        # Windows 下 / 可能无效，尝试 cwd
        try:
            disk = shutil.disk_usage(os.getcwd())
            info["disk_total"] = disk.total
            info["disk_used"] = disk.used
            info["disk_free"] = disk.free
            info["disk_percent"] = round(disk.used / disk.total * 100, 1) if disk.total else 0
        except Exception:
            pass

    return info


@router.get("/overview", response_model=ApiResponse)
async def overview():
    """仪表盘概览：Emby 媒体库统计 + 服务器状态 + 115 容量 + 系统信息"""
    result = {"emby": None, "emby_status": None, "account": None, "accounts": [], "system": None}

    client = await _get_emby_client()
    if client:
        info = await client.system_info()
        counts = await client.item_counts()
        if info:
            result["emby"] = {
                "connected": True,
                "server_name": info.get("ServerName", ""),
                "version": info.get("Version", ""),
                "operating_system": info.get("OperatingSystemDisplayName", ""),
                "movie_count": (counts or {}).get("MovieCount", 0),
                "series_count": (counts or {}).get("SeriesCount", 0),
                "episode_count": (counts or {}).get("EpisodeCount", 0),
            }
            try:
                result["emby_status"] = await client.server_status()
            except Exception:
                result["emby_status"] = None
        else:
            result["emby"] = {"connected": False}

    # 115 账号信息（第一个有效账号 + 所有账号列表）
    all_accounts = read_accounts()
    valid_accounts = [acc for acc in all_accounts if acc.get("status") == 1]
    acc = valid_accounts[0] if valid_accounts else (all_accounts[0] if all_accounts else None)
    if acc:
        result["account"] = {
            "id": acc.get("id"),
            "name": acc.get("name", ""),
            "username": acc.get("username", ""),
            "user_id": acc.get("user_id", ""),
            "vip_level": acc.get("vip_level", 0),
            "space_used": acc.get("space_used", 0),
            "space_total": acc.get("space_total", 0),
            "avatar_url": acc.get("avatar_url", ""),
            "app": acc.get("app", ""),
        }
        # 所有有效账号的简要信息
        result["accounts"] = [{
            "id": a.get("id"),
            "name": a.get("name", ""),
            "username": a.get("username", ""),
            "vip_level": a.get("vip_level", 0),
            "space_used": a.get("space_used", 0),
            "space_total": a.get("space_total", 0),
            "avatar_url": a.get("avatar_url", ""),
        } for a in valid_accounts]

    # 系统信息
    result["system"] = _collect_system_info()

    # 上传队列状态
    try:
        from app.services.upload_queue import get_upload_queue
        result["upload_queue"] = get_upload_queue().get_status()
    except Exception:
        result["upload_queue"] = None

    return ApiResponse(data=result)


@router.get("/emby/library-stats", response_model=ApiResponse)
async def emby_library_stats():
    """各媒体库入库数量统计（带封面图，缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    stats = await cached("library_stats", TTL_SHORT, client.library_stats)
    # 补充封面图 URL（从缓存取的 stats 不含图片地址，每次重新拼）
    libs = await cached("libraries", TTL_SHORT, client.libraries)
    for s in stats:
        lib = next((l for l in libs if l.get("Name") == s.get("name")), None)
        item_id = lib.get("ItemId") if lib else None
        s["item_id"] = item_id
        s["image_url"] = client.image_url(item_id) if item_id else None
        s["collection_type"] = lib.get("CollectionType", "") if lib else ""
    return ApiResponse(data=stats)


@router.get("/emby/genre-stats", response_model=ApiResponse)
async def emby_genre_stats(item_type: str = "Movie"):
    """按类型统计影片分布（缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    stats = await cached(f"genre_stats:{item_type}", TTL_SHORT, lambda: client.genre_stats(item_type))
    return ApiResponse(data=stats)


@router.get("/emby/latest", response_model=ApiResponse)
async def emby_latest(item_type: str = "Movie", limit: int = 12):
    """获取 Emby 最新入库内容（缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby，请先在仪表盘设置")

    items = await cached(f"latest:{item_type}:{limit}", TTL_SHORT, lambda: client.latest_items(item_type, limit))
    data = [{
        "id": it.get("Id"),
        "name": it.get("Name"),
        "year": it.get("ProductionYear"),
        "type": it.get("Type"),
        "rating": it.get("CommunityRating"),
        "overview": it.get("Overview", ""),
        "image_url": client.image_url(it.get("Id")) if it.get("Id") else None,
    } for it in items]
    return ApiResponse(data=data)


@router.get("/emby/libraries", response_model=ApiResponse)
async def emby_libraries():
    """获取 Emby 媒体库列表"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")

    libs = await client.libraries()
    data = [{
        "name": lib.get("Name"),
        "type": lib.get("CollectionType", ""),
        "paths": lib.get("Locations", []),
        "item_id": lib.get("ItemId"),
        "image_url": client.image_url(lib.get("ItemId")) if lib.get("ItemId") else None,
    } for lib in libs]
    return ApiResponse(data=data)


@router.get("/emby/recently-played", response_model=ApiResponse)
async def emby_recently_played(limit: int = 12):
    """最近播放内容（缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    items = await cached(f"recently_played:{limit}", TTL_SHORT, lambda: client.recently_played(limit))
    data = [{
        "id": it.get("Id"),
        "name": it.get("Name"),
        "year": it.get("ProductionYear"),
        "type": it.get("Type"),
        "rating": it.get("CommunityRating"),
        "overview": it.get("Overview", ""),
        "date_played": it.get("DatePlayed", ""),
        "play_count": it.get("PlayCount", 0),
        "image_url": client.image_url(it.get("Id")) if it.get("Id") else None,
    } for it in items]
    return ApiResponse(data=data)


@router.get("/emby/popular", response_model=ApiResponse)
async def emby_popular(limit: int = 12):
    """热门内容（按播放次数，缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    items = await cached(f"popular:{limit}", TTL_SHORT, lambda: client.popular_items(limit))
    data = [{
        "id": it.get("Id"),
        "name": it.get("Name"),
        "year": it.get("ProductionYear"),
        "type": it.get("Type"),
        "rating": it.get("CommunityRating"),
        "overview": it.get("Overview", ""),
        "play_count": it.get("UserData", {}).get("PlayCount", 0) if it.get("UserData") else it.get("PlayCount", 0),
        "image_url": client.image_url(it.get("Id")) if it.get("Id") else None,
    } for it in items]
    return ApiResponse(data=data)


@router.get("/emby/upcoming", response_model=ApiResponse)
async def emby_upcoming(limit: int = 12):
    """即将上映/未播出内容（缓存 60s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    items = await cached(f"upcoming:{limit}", TTL_SHORT, lambda: client.upcoming(limit))
    data = [{
        "id": it.get("Id"),
        "name": it.get("Name"),
        "series_name": it.get("SeriesName", ""),
        "year": it.get("ProductionYear"),
        "type": it.get("Type"),
        "overview": it.get("Overview", ""),
        "premiere_date": it.get("PremiereDate", ""),
        "image_url": client.image_url(it.get("Id")) if it.get("Id") else None,
    } for it in items]
    return ApiResponse(data=data)


@router.get("/emby/play-trend", response_model=ApiResponse)
async def emby_play_trend(days: int = 30):
    """入库趋势（按日，缓存 120s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    data = await cached(f"play_trend:{days}", TTL_MEDIUM, lambda: client.play_trend(days))
    return ApiResponse(data=data)


@router.get("/emby/storage-stats", response_model=ApiResponse)
async def emby_storage_stats():
    """存储分布（缓存 120s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    data = await cached("storage_stats", TTL_MEDIUM, client.storage_stats)
    return ApiResponse(data=data)


@router.get("/emby/library-detail/{item_id}", response_model=ApiResponse)
async def emby_library_detail(item_id: str):
    """媒体库详情：分类/年代/分辨率/评分分布（缓存 120s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    data = await cached(f"library_detail:{item_id}", TTL_MEDIUM, lambda: client.library_detail(item_id))
    return ApiResponse(data=data)


@router.get("/emby/server-status", response_model=ApiResponse)
async def emby_server_status():
    """Emby 服务器状态（缓存 30s）"""
    client = await _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="未配置 Emby")
    data = await cached("server_status", 30, client.server_status)
    return ApiResponse(data=data)


@router.post("/emby/invalidate-cache", response_model=ApiResponse)
async def emby_invalidate_cache():
    """手动清除仪表盘缓存（Emby 重新配置后调用）"""
    invalidate()
    return ApiResponse(data={"ok": True})

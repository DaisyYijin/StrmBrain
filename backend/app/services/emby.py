"""
Emby 客户端服务

用于连接 Emby 服务器，查询媒体库统计和内容信息。
"""
from typing import Optional

import httpx

from app.core.logbuffer import get_logger

logger = get_logger("app.services.emby")


class EmbyClient:
    """Emby API 客户端"""

    def __init__(self, host: str, api_key: str):
        # 规范化地址：去掉尾部斜杠，自动补全缺失的 http:// 协议前缀
        host = (host or "").strip()
        if host and not host.lower().startswith(("http://", "https://")):
            host = "http://" + host
        self.host = host.rstrip("/")
        self.api_key = api_key

    async def _get(self, path: str, params: Optional[dict] = None, timeout: float = 30.0) -> Optional[dict]:
        """发起 GET 请求"""
        params = params or {}
        params["api_key"] = self.api_key
        url = f"{self.host}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    return r.json()
                return None
        except Exception as e:
            # 单行日志即可定位问题，避免完整堆栈刷屏
            logger.warning(f"请求失败 {path}: {e}")
            return None

    async def system_info(self) -> Optional[dict]:
        """服务器信息"""
        return await self._get("/System/Info")

    async def item_counts(self) -> Optional[dict]:
        """各类型条目数量统计"""
        return await self._get("/Items/Counts")

    async def libraries(self) -> list[dict]:
        """媒体库列表"""
        data = await self._get("/Library/VirtualFolders")
        return data if isinstance(data, list) else []

    async def get_library_items_sorted(
        self,
        lib_id: str,
        item_type: str = "Movie",
        sort_by: str = "DateCreated",
        sort_order: str = "Descending",
        limit: int = 9,
    ) -> list[dict]:
        """
        获取媒体库中带有封面图片的条目，按指定方式排序。
        sort_by: DateCreated / SortName / PremiereDate / CommunityRating / Random
        sort_order: Descending / Ascending
        返回: [{"id", "name", "year", "rating", "image_url"}]
        """
        params = {
            "ParentId": lib_id,
            "Recursive": "true",
            "Limit": limit * 3,  # 多取一些，过滤掉没有封面的
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "Fields": "ProductionYear,CommunityRating,ImageTags",
            "EnableImageTypes": "Primary",
        }
        if item_type:
            params["IncludeItemTypes"] = item_type

        data = await self._get("/Items", params, timeout=60.0)
        items = data.get("Items", []) if data else []
        result = []
        for it in items:
            image_tags = it.get("ImageTags", {}) or {}
            if not image_tags.get("Primary"):
                continue  # 跳过没有封面的
            result.append({
                "id": it.get("Id", ""),
                "name": it.get("Name", ""),
                "year": it.get("ProductionYear", ""),
                "rating": it.get("CommunityRating", 0) or 0,
                "image_url": self.image_url(it.get("Id", ""), "Primary"),
            })
            if len(result) >= limit:
                break
        return result

    async def library_stats(self) -> list[dict]:
        """
        各媒体库的条目数量统计（并发查询，避免大库超时）
        返回: [{"name", "type", "count"}]
        """
        import asyncio

        libs = await self.libraries()

        async def _count_one(lib: dict) -> dict:
            item_id = lib.get("ItemId")
            collection_type = lib.get("CollectionType", "")
            if collection_type == "movies":
                include_type = "Movie"
            elif collection_type == "tvshows":
                include_type = "Series"
            else:
                include_type = ""

            params = {
                "ParentId": item_id,
                "Recursive": "true",
                "Limit": 0,
                "EnableTotalRecordCount": "true",
            }
            if include_type:
                params["IncludeItemTypes"] = include_type
            data = await self._get("/Items", params, timeout=30.0)
            count = data.get("TotalRecordCount", 0) if data else 0
            return {
                "name": lib.get("Name", ""),
                "type": collection_type or "mixed",
                "count": count,
            }

        # 并发执行所有库的统计
        results = await asyncio.gather(*[_count_one(lib) for lib in libs])
        return list(results)

    async def genre_stats(self, item_type: str = "Movie", limit: int = 15) -> list[dict]:
        """
        按类型（genre）统计影片分布
        返回: [{"name", "count"}]
        """
        data = await self._get("/Genres", {
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "SortBy": "SortName",
            "Fields": "ItemCounts",
            "Limit": 200,
        })
        items = data.get("Items", []) if data else []
        result = []
        for g in items:
            counts = g.get("ChildCount") or 0
            # MovieCount/SeriesCount 更精确
            if item_type == "Movie":
                counts = g.get("MovieCount", counts)
            elif item_type == "Series":
                counts = g.get("SeriesCount", counts)
            result.append({"name": g.get("Name", ""), "count": counts})
        # 按数量降序，取前 N
        result.sort(key=lambda x: x["count"], reverse=True)
        return result[:limit]

    async def latest_items(self, item_type: str = "Movie", limit: int = 12) -> list[dict]:
        """
        最新入库内容
        item_type: Movie / Series / Episode
        """
        data = await self._get("/Items", {
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": limit,
            "Fields": "ProductionYear,Overview,CommunityRating",
            "ImageTypeLimit": 1,
        })
        if data and "Items" in data:
            return data["Items"]
        return []

    async def recently_played(self, limit: int = 12) -> list[dict]:
        """最近播放内容"""
        data = await self._get("/Items", {
            "Recursive": "true",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Filters": "IsPlayed",
            "Limit": limit,
            "Fields": "ProductionYear,Overview,CommunityRating,DatePlayed",
            "ImageTypeLimit": 1,
        })
        return data.get("Items", []) if data else []

    async def popular_items(self, limit: int = 12) -> list[dict]:
        """热门内容（按播放次数）"""
        data = await self._get("/Items", {
            "Recursive": "true",
            "SortBy": "PlayCount",
            "SortOrder": "Descending",
            "Limit": limit,
            "Fields": "ProductionYear,Overview,CommunityRating,PlayCount",
            "ImageTypeLimit": 1,
            "Filters": "IsPlayed",
        })
        return data.get("Items", []) if data else []

    async def upcoming(self, limit: int = 12) -> list[dict]:
        """即将上映/未播出内容"""
        data = await self._get("/Items", {
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "SortBy": "PremiereDate",
            "SortOrder": "Ascending",
            "IsUnaired": "true",
            "Limit": limit,
            "Fields": "ProductionYear,Overview,SeriesName,PremiereDate",
            "ImageTypeLimit": 1,
        })
        return data.get("Items", []) if data else []

    async def play_trend(self, days: int = 30) -> list[dict]:
        """
        入库趋势：按日期统计新增条目数
        返回: [{"date": "2024-01-01", "count": 5}]
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        data = await self._get("/Items", {
            "Recursive": "true",
            "MinDateCreated": start,
            "Limit": 5000,
            "Fields": "DateCreated",
        }, timeout=60.0)
        items = data.get("Items", []) if data else []
        # 按日期分组统计
        from collections import Counter
        counter = Counter()
        for it in items:
            dc = it.get("DateCreated")
            if not dc:
                continue
            try:
                # 形如 2024-01-01T00:00:00.0000000Z
                day = dc[:10]
                counter[day] += 1
            except Exception:
                continue
        # 填充空日期
        result = []
        cur = now - timedelta(days=days)
        end = now
        while cur <= end:
            d = cur.strftime("%Y-%m-%d")
            result.append({"date": d, "count": counter.get(d, 0)})
            cur += timedelta(days=1)
        return result

    async def storage_stats(self) -> list[dict]:
        """
        存储分布：各媒体库的条目数（Emby 不直接返回库总大小，只统计数量）
        返回: [{"name", "type", "count"}]
        """
        # 与 library_stats 逻辑一致，直接委托
        return await self.library_stats()

    async def library_detail(self, lib_id: str) -> dict:
        """
        媒体库详情：分类、年代、分辨率、评分分布
        """
        import asyncio

        async def _genre():
            data = await self._get("/Genres", {
                "ParentId": lib_id,
                "Recursive": "true",
                "Limit": 50,
                "Fields": "ItemCounts",
            })
            items = data.get("Items", []) if data else []
            return [{"name": g.get("Name", ""), "count": g.get("MovieCount", 0) + g.get("SeriesCount", 0) + g.get("EpisodeCount", 0)} for g in items]

        async def _year():
            # 用 Years 端点直接获取年代分布
            data = await self._get("/Items/Years", {
                "ParentId": lib_id,
                "Recursive": "true",
            }, timeout=30.0)
            items = data.get("Items", []) if data else []
            return [{"year": y.get("Name", ""), "count": y.get("Size", 0)} for y in items if y.get("Name")]

        async def _resolution():
            # 按分辨率（视频 4K/1080P/720P）统计，限制 2000 项加快速度
            data = await self._get("/Items", {
                "ParentId": lib_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Limit": 2000,
                "Fields": "MediaStreams",
            }, timeout=45.0)
            items = data.get("Items", []) if data else []
            from collections import Counter
            counter = Counter()
            for it in items:
                for ms in it.get("MediaStreams", []) or []:
                    if ms.get("Type") == "Video":
                        h = ms.get("Height", 0) or 0
                        if h >= 2160:
                            counter["4K"] += 1
                        elif h >= 1080:
                            counter["1080P"] += 1
                        elif h >= 720:
                            counter["720P"] += 1
                        elif h > 0:
                            counter["SD"] += 1
                        break
            return [{"name": k, "count": v} for k, v in counter.items()]

        async def _rating():
            data = await self._get("/Items", {
                "ParentId": lib_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Limit": 5000,
                "Fields": "CommunityRating",
            }, timeout=60.0)
            items = data.get("Items", []) if data else []
            from collections import Counter
            counter = Counter()
            for it in items:
                r = it.get("CommunityRating")
                if r is None:
                    counter["未评"] += 1
                elif r >= 8:
                    counter["8-10"] += 1
                elif r >= 6:
                    counter["6-8"] += 1
                elif r >= 4:
                    counter["4-6"] += 1
                else:
                    counter["0-4"] += 1
            return [{"name": k, "count": v} for k, v in counter.items()]

        genres, years, resolutions, ratings = await asyncio.gather(_genre(), _year(), _resolution(), _rating())
        # 年代按降序，取前 15
        years_sorted = sorted(years, key=lambda x: x["year"], reverse=True)[:15]
        return {
            "genres": genres,
            "years": years_sorted,
            "resolutions": resolutions,
            "ratings": ratings,
        }

    async def server_status(self) -> dict:
        """服务器状态：服务器信息 + 在线用户"""
        info = await self._get("/System/Info") or {}
        endpoint = await self._get("/GetEndpointInfo") or {}
        # 在线用户数需要管理员权限，可能失败
        sessions = await self._get("/Sessions") or []
        active_users = len([s for s in sessions if s.get("IsActive")]) if isinstance(sessions, list) else 0
        return {
            "server_name": info.get("ServerName", ""),
            "version": info.get("Version", ""),
            "operating_system": info.get("OperatingSystemDisplayName", ""),
            "has_pending_restart": info.get("HasPendingRestart", False),
            "supports_running_shutdown": info.get("CanSelfRestart", False),
            "active_users": active_users,
            "session_count": len(sessions) if isinstance(sessions, list) else 0,
            "is_local": endpoint.get("IsLocal", True),
        }

    def image_url(self, item_id: str, image_type: str = "Primary") -> str:
        """拼接条目图片 URL（带 api_key）"""
        return f"{self.host}/Items/{item_id}/Images/{image_type}?api_key={self.api_key}"

    async def refresh_library(self) -> bool:
        """
        触发 Emby 全库刷新（扫描新增的 STRM 文件）
        调用 POST /Library/Refresh
        """
        url = f"{self.host}/Library/Refresh"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, params={"api_key": self.api_key})
                return r.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"刷新媒体库失败: {e}")
            return False

    async def refresh_library_by_path(self, path: str) -> bool:
        """
        按路径刷新 Emby 媒体库（比全库刷新更快）
        通过查找该路径对应的媒体库，然后刷新该库
        """
        libs = await self.libraries()
        for lib in libs:
            lib_path = lib.get("Locations", [])
            if isinstance(lib_path, str):
                lib_path = [lib_path]
            # 检查路径是否在该库下
            for lp in lib_path:
                if path.startswith(lp):
                    item_id = lib.get("ItemId")
                    if item_id:
                        url = f"{self.host}/Items/{item_id}/Refresh"
                        params = {
                            "api_key": self.api_key,
                            "Recursive": "true",
                            "MetadataRefreshMode": "FullRefresh",
                            "ImageRefreshMode": "FullRefresh",
                            "ReplaceAllMetadata": "true",
                        }
                        try:
                            async with httpx.AsyncClient(timeout=30.0) as client:
                                r = await client.post(url, params=params)
                                return r.status_code in (200, 204)
                        except Exception as e:
                            logger.warning(f"按路径刷新失败: {e}")
                            return False
        # 未找到匹配的库，执行全库刷新
        return await self.refresh_library()


    async def get_items_missing_images(self, lib_id: str = "", item_type: str = "") -> list[dict]:
        """
        获取缺少封面图片的媒体条目。
        通过 ImageTags 字段判断是否缺少 Primary 封面。
        返回: [{"id", "name", "type", "year", "has_primary"}]
        """
        params = {
            "Recursive": "true",
            "Fields": "ImageTags,ProductionYear,Overview",
            "Limit": 5000,
        }
        if lib_id:
            params["ParentId"] = lib_id
        if item_type:
            params["IncludeItemTypes"] = item_type
        data = await self._get("/Items", params, timeout=60.0)
        items = data.get("Items", []) if data else []
        result = []
        for it in items:
            image_tags = it.get("ImageTags", {}) or {}
            has_primary = bool(image_tags.get("Primary"))
            if not has_primary:
                result.append({
                    "id": it.get("Id", ""),
                    "name": it.get("Name", ""),
                    "type": it.get("Type", ""),
                    "year": it.get("ProductionYear", ""),
                    "has_primary": has_primary,
                })
        return result

    async def get_tv_shows(self, lib_id: str = "") -> list[dict]:
        """
        获取所有电视剧条目（用于缺集扫描）。
        返回: [{"id", "name", "year", "provider_ids"}]
        """
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "Fields": "ProviderIds,ProductionYear,Overview",
            "Limit": 5000,
        }
        if lib_id:
            params["ParentId"] = lib_id
        data = await self._get("/Items", params, timeout=60.0)
        items = data.get("Items", []) if data else []
        result = []
        for it in items:
            provider_ids = it.get("ProviderIds", {}) or {}
            result.append({
                "id": it.get("Id", ""),
                "name": it.get("Name", ""),
                "year": it.get("ProductionYear", ""),
                "tmdb_id": provider_ids.get("Tmdb", ""),
                "tvdb_id": provider_ids.get("Tvdb", ""),
                "imdb_id": provider_ids.get("Imdb", ""),
            })
        return result

    async def get_season_episodes(self, series_id: str) -> list[dict]:
        """
        获取某部电视剧在 Emby 中的所有季和集。
        返回: [{"season": int, "episodes": [int, ...]}]
        """
        # 先获取所有季
        seasons_data = await self._get(f"/Shows/{series_id}/Seasons", {
            "Fields": "IndexNumber",
        }, timeout=30.0)
        seasons = seasons_data.get("Items", []) if seasons_data else []

        result = []
        for season in seasons:
            season_num = season.get("IndexNumber", 0) or 0
            if season_num <= 0:
                continue
            # 获取该季的所有集
            eps_data = await self._get(f"/Shows/{series_id}/Episodes", {
                "SeasonId": season.get("Id", ""),
                "Fields": "IndexNumber",
            }, timeout=30.0)
            episodes = eps_data.get("Items", []) if eps_data else []
            ep_nums = sorted([
                ep.get("IndexNumber", 0) or 0
                for ep in episodes
                if ep.get("IndexNumber")
            ])
            result.append({
                "season": season_num,
                "episodes": ep_nums,
                "episode_count": len(ep_nums),
            })
        return result

    async def upload_image(self, item_id: str, image_data: bytes, image_type: str = "Primary") -> bool:
        """
        上传图片到 Emby 条目。
        image_type: Primary / Backdrop / Logo / Thumb
        """
        url = f"{self.host}/Items/{item_id}/Images/{image_type}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    url,
                    params={"api_key": self.api_key},
                    content=image_data,
                    headers={"Content-Type": "image/jpeg"},
                )
                return r.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"上传图片失败 {item_id}: {e}")
            return False


async def test_connection(host: str, api_key: str) -> tuple[bool, str]:
    """测试 Emby 连接"""
    if not host or not api_key:
        return False, "请填写 Emby 地址和 API Key"
    client = EmbyClient(host, api_key)
    info = await client.system_info()
    if info and info.get("ServerName"):
        return True, f"连接成功: {info.get('ServerName')} (版本 {info.get('Version', '未知')})"
    return False, "无法连接 Emby，请检查地址和 API Key"


async def trigger_emby_refresh(path: str = "") -> bool:
    """
    同步完成后自动触发 Emby 媒体库刷新。
    从数据库读取 Emby 配置，若未配置则跳过。
    path: 指定刷新路径，为空则全库刷新。
    """
    try:
        from app.core.db_helper import read_setting

        settings = read_setting("emby")
        if not settings:
            logger.debug("未配置 Emby，跳过媒体库刷新")
            return False

        host = (settings.get("host") or "").strip()
        api_key = (settings.get("api_key") or "").strip()

        if not host or not api_key:
            logger.debug("Emby 配置不完整，跳过媒体库刷新")
            return False

        client = EmbyClient(host, api_key)
        if path:
            logger.info(f"[emby] 按路径刷新媒体库: {path}")
            ok = await client.refresh_library_by_path(path)
        else:
            logger.info("[emby] 全库刷新媒体库")
            ok = await client.refresh_library()

        if ok:
            logger.info("[emby] 媒体库刷新已触发")
        else:
            logger.warning("[emby] 媒体库刷新失败")
        return ok
    except Exception as e:
        logger.warning(f"触发 Emby 刷新异常: {e}")
        return False


async def sync_media_info(item_id: str = "") -> dict:
    """通过 Emby Items/RemoteInfo 接口同步媒体信息

    获取 Emby 中指定项目（或所有最近项目）的媒体信息，
    包括 Path、MediaSources 等，用于与本地 STRM 文件做交叉校验。
    """
    try:
        from app.core.db_helper import read_setting

        settings = read_setting("emby")
        if not settings:
            return {"success": False, "error": "Emby 未配置"}

        host = (settings.get("host") or "").strip()
        api_key = (settings.get("api_key") or "").strip()

        if not host or not api_key:
            return {"success": False, "error": "Emby 未配置"}

        # 规范化地址：补全协议前缀，去掉尾部斜杠
        if not host.lower().startswith(("http://", "https://")):
            host = "http://" + host
        host = host.rstrip("/")

        headers = {"X-Emby-Token": api_key}
        raw_items: list[dict] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            if item_id:
                # 获取单个项目的详细信息（含 MediaSources）
                url = f"{host}/Items/{item_id}"
                params = {"Fields": "Path,MediaSources"}
                r = await client.get(url, params=params, headers=headers)
                if r.status_code != 200:
                    return {
                        "success": False,
                        "error": f"获取项目信息失败: HTTP {r.status_code}",
                    }
                raw_items = [r.json()]
            else:
                # 获取最近添加的项目列表（按创建时间倒序）
                url = f"{host}/Items"
                params = {
                    "SortBy": "DateCreated",
                    "SortOrder": "Descending",
                    "Recursive": "true",
                    "Fields": "Path,MediaSources",
                    "Limit": 100,
                }
                r = await client.get(url, params=params, headers=headers)
                if r.status_code != 200:
                    return {
                        "success": False,
                        "error": f"获取项目列表失败: HTTP {r.status_code}",
                    }
                data = r.json()
                raw_items = data.get("Items", []) if data else []

        # 提取所需字段：Id / Name / Path / Type / Size
        result: list[dict] = []
        for it in raw_items:
            media_sources = it.get("MediaSources", []) or []
            path = it.get("Path", "")
            size = 0
            if media_sources:
                # 取第一个 MediaSource 作为主文件信息
                ms = media_sources[0]
                if not path:
                    path = ms.get("Path", "")
                size = ms.get("Size", 0) or 0
            result.append({
                "id": it.get("Id", ""),
                "name": it.get("Name", ""),
                "path": path,
                "type": it.get("Type", ""),
                "size": size,
            })

        return {
            "success": True,
            "total": len(result),
            "items": result,
        }
    except httpx.HTTPError as e:
        logger.warning(f"同步媒体信息网络错误: {e}")
        return {"success": False, "error": f"网络错误: {e}"}
    except Exception as e:
        logger.warning(f"同步媒体信息异常: {e}")
        return {"success": False, "error": f"同步媒体信息异常: {e}"}

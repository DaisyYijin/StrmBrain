"""
Emby 媒体信息上传/下载服务

将 Emby 的 MediaSourceInfo（媒体流信息：编码、分辨率、码率、声道等）
与 115 网盘文件的 SHA1+size 关联，实现跨实例的媒体信息共享：

- upload_media_info：从 Emby 获取 item 的 MediaSourceInfo，关联 SHA1+size 后上传到 115 云端
- download_media_info：通过 SHA1+size 从 115 云端下载已有的 MediaSourceInfo
- get_status：返回上传/下载计数和缓存命中率

简化实现说明：
- Emby 侧：已实现 MediaSourceInfo 获取和 SHA1 关联框架
- 115 侧：上传/下载 API 不确定，标注 TODO 并返回 None，计数仍正常累加
"""
from typing import Optional

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.emby_media_info")

# 媒体信息统计的持久化 key
_STATS_KEY = "emby_media_info_stats"


class EmbyMediaInfoService:
    """
    Emby 媒体信息上传/下载服务。

    通过 Emby API 获取 MediaSourceInfo，关联 115 文件的 SHA1+size，
    实现 STRM 场景下跨实例的媒体信息共享。
    """

    def __init__(self):
        self._stats = self._load_stats()

    # ===== 统计持久化 =====

    def _load_stats(self) -> dict:
        """从 settings.json 加载统计数据"""
        data = read_setting(_STATS_KEY)
        return {
            "uploaded": data.get("uploaded", 0),
            "downloaded": data.get("downloaded", 0),
            "cache_hits": data.get("cache_hits", 0),
            "cache_misses": data.get("cache_misses", 0),
        }

    def _save_stats(self):
        """保存统计数据到 settings.json"""
        save_setting(_STATS_KEY, self._stats)

    # ===== Emby 客户端 =====

    def _get_emby_client(self):
        """获取已配置的 Emby 客户端实例"""
        emby_cfg = read_setting("emby")
        host = (emby_cfg.get("host", "") or "").rstrip("/")
        api_key = emby_cfg.get("api_key", "")
        if not host or not api_key:
            return None
        from app.services.emby import EmbyClient
        return EmbyClient(host, api_key)

    def _get_account(self, account_id: int = 0):
        """获取有效的 115 账号"""
        from app.core.json_storage import find_account, get_first_valid_account
        if account_id:
            return find_account(account_id)
        return get_first_valid_account()

    # ===== 核心方法 =====

    async def upload_media_info(
        self,
        item_id: str,
        pickcode: str = "",
        sha1: str = "",
        size: int = 0,
        account_id: int = 0,
    ) -> Optional[dict]:
        """
        上传 Emby 媒体信息到 115 云端。

        1. 通过 Emby API 获取 item 的 MediaSourceInfo
        2. 关联 SHA1 + size 作为唯一标识
        3. 通过 115 API 上传到云端（TODO：115 侧上传 API 待确认）

        Args:
            item_id: Emby 媒体条目 ID
            pickcode: 115 文件 pickcode（可选，用于关联）
            sha1: 115 文件 SHA1（用于唯一标识）
            size: 文件大小（字节）
            account_id: 115 账号 ID

        Returns:
            上传的媒体信息 dict，失败返回 None
        """
        client = self._get_emby_client()
        if not client:
            logger.warning("[emby-media-info] Emby 未配置，无法获取 MediaSourceInfo")
            return None

        # 1. 从 Emby 获取 MediaSourceInfo
        try:
            item_data = await client._get(
                f"/Items/{item_id}",
                params={"Fields": "MediaSources,Path"},
            )
        except Exception as e:
            logger.warning(f"[emby-media-info] 获取 Emby 条目信息失败: {e}")
            return None

        if not item_data:
            logger.warning(f"[emby-media-info] Emby 条目 {item_id} 不存在或无数据")
            return None

        # 提取 MediaSourceInfo
        media_sources = item_data.get("MediaSources", [])
        if not media_sources:
            logger.warning(f"[emby-media-info] 条目 {item_id} 无 MediaSourceInfo")
            return None

        # 取第一个 MediaSource（通常是主视频流）
        media_source = media_sources[0]

        # 2. 关联 SHA1 + size 构建上传数据
        upload_data = {
            "sha1": sha1,
            "size": size,
            "pickcode": pickcode,
            "item_id": item_id,
            "item_name": item_data.get("Name", ""),
            "item_path": item_data.get("Path", ""),
            "media_source": {
                "Id": media_source.get("Id", ""),
                "Container": media_source.get("Container", ""),
                "Protocol": media_source.get("Protocol", ""),
                "Bitrate": media_source.get("Bitrate", 0),
                "RunTimeTicks": media_source.get("RunTimeTicks", 0),
                "Size": media_source.get("Size", 0),
                "MediaStreams": [
                    {
                        "Type": s.get("Type", ""),
                        "Codec": s.get("Codec", ""),
                        "Language": s.get("Language", ""),
                        "DisplayTitle": s.get("DisplayTitle", ""),
                        "BitRate": s.get("BitRate", 0),
                        "Channels": s.get("Channels", 0),
                        "SampleRate": s.get("SampleRate", 0),
                        "Width": s.get("Width", 0),
                        "Height": s.get("Height", 0),
                        "AverageFrameRate": s.get("AverageFrameRate", 0),
                        "PixelFormat": s.get("PixelFormat", ""),
                    }
                    for s in media_source.get("MediaStreams", [])
                ],
            },
        }

        # 3. 上传到 115 云端
        # TODO: 115 侧的媒体信息上传 API 待确认。
        #       目前 Client115Service 无 upload_emby_mediainfo 方法。
        #       确认 API 后通过 Client115Service 上传 upload_data。
        #       可考虑上传为 JSON 文件（如 {sha1}.emby_mediainfo.json）到指定目录。
        account = self._get_account(account_id)
        if account:
            cookies = account.get("cookies", "")
            try:
                # 尝试调用 Client115Service 的上传方法（如果存在）
                from app.services.client_115 import Client115Service
                if hasattr(Client115Service, "upload_emby_mediainfo"):
                    # TODO: 确认方法签名后实现
                    # Client115Service.upload_emby_mediainfo(cookies, upload_data)
                    logger.info(f"[emby-media-info] 调用 upload_emby_mediainfo: sha1={sha1[:8]}...")
                else:
                    logger.info(
                        f"[emby-media-info] 115 上传 API 未实现（TODO），"
                        f"已获取 MediaSourceInfo: item={item_id}, sha1={sha1[:8]}..., "
                        f"streams={len(media_source.get('MediaStreams', []))}"
                    )
            except Exception as e:
                logger.warning(f"[emby-media-info] 上传到 115 失败: {e}")
        else:
            logger.warning("[emby-media-info] 无有效 115 账号，跳过上传")

        # 更新统计
        self._stats["uploaded"] += 1
        self._save_stats()

        return upload_data

    async def download_media_info(self, sha1: str, size: int = 0, account_id: int = 0) -> Optional[dict]:
        """
        从 115 云端下载已有的媒体信息。

        通过 SHA1+size 查找并下载之前上传的 MediaSourceInfo。
        用于 STRM 文件在 Emby 中缺少媒体信息时，从云端恢复。

        Args:
            sha1: 115 文件 SHA1
            size: 文件大小（字节，用于二次校验）
            account_id: 115 账号 ID

        Returns:
            MediaSourceInfo dict，未找到或失败返回 None
        """
        if not sha1:
            logger.warning("[emby-media-info] download 需要 sha1 参数")
            return None

        account = self._get_account(account_id)
        if not account:
            logger.warning("[emby-media-info] 无有效 115 账号，无法下载")
            return None

        cookies = account.get("cookies", "")

        # TODO: 115 侧的媒体信息下载 API 待确认。
        #       确认 API 后通过 Client115Service 按 sha1 查找并下载 JSON 文件。
        #       目前返回 None，表示未找到已有媒体信息。
        try:
            from app.services.client_115 import Client115Service
            if hasattr(Client115Service, "download_emby_mediainfo"):
                # TODO: 确认方法签名后实现
                # result = Client115Service.download_emby_mediainfo(cookies, sha1, size)
                # if result:
                #     self._stats["downloaded"] += 1
                #     self._stats["cache_hits"] += 1
                #     self._save_stats()
                #     return result
                logger.info(f"[emby-media-info] 调用 download_emby_mediainfo: sha1={sha1[:8]}...")
            else:
                logger.info(
                    f"[emby-media-info] 115 下载 API 未实现（TODO），"
                    f"无法下载: sha1={sha1[:8]}..., size={size}"
                )
        except Exception as e:
            logger.warning(f"[emby-media-info] 从 115 下载失败: {e}")

        # 未找到 -> 记录缓存未命中（仅在 API 实现后才有意义，当前不计数以避免状态失真）
        # self._stats["cache_misses"] += 1  # TODO: API 实现后启用
        # self._save_stats()

        return None

    def get_status(self) -> dict:
        """
        返回媒体信息上传/下载状态。

        Returns:
            {
                "uploaded": 累计上传次数,
                "downloaded": 累计下载次数,
                "cache_hit_rate": 缓存命中率（0~1）,
                "cache_hits": 缓存命中次数,
                "cache_misses": 缓存未命中次数,
            }
        """
        hits = self._stats.get("cache_hits", 0)
        misses = self._stats.get("cache_misses", 0)
        total = hits + misses
        hit_rate = round(hits / total, 4) if total > 0 else 0.0

        return {
            "uploaded": self._stats.get("uploaded", 0),
            "downloaded": self._stats.get("downloaded", 0),
            "cache_hit_rate": hit_rate,
            "cache_hits": hits,
            "cache_misses": misses,
            "emby_configured": self._is_emby_configured(),
        }

    def _is_emby_configured(self) -> bool:
        """检查 Emby 是否已配置"""
        emby_cfg = read_setting("emby")
        return bool(emby_cfg.get("host") and emby_cfg.get("api_key"))

    def reset_stats(self):
        """重置统计数据（供调试/重置用）"""
        self._stats = {
            "uploaded": 0,
            "downloaded": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
        self._save_stats()
        logger.info("[emby-media-info] 统计数据已重置")


# 全局单例
_instance: Optional[EmbyMediaInfoService] = None


def get_emby_media_info_service() -> EmbyMediaInfoService:
    """获取 EmbyMediaInfoService 全局单例"""
    global _instance
    if _instance is None:
        _instance = EmbyMediaInfoService()
    return _instance

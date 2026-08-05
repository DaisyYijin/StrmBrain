"""
缓存预热服务
============

定时预加载 115 网盘目录树和下载链接到缓存，消除首次播放延迟。

工作原理：
- 遍历已配置的全量同步目录，调用 Client115Service.list_all_files 预加载文件列表
- 对前 N 个视频文件调用 get_download_url 预填充直链缓存（_DOWNLOAD_URL_CACHE）
- 用户首次播放时直链已在缓存中，无需实时请求 115 API，消除卡顿

配置（存于 settings.json 的 cache_warmer 键）：
- enabled: bool 是否启用（默认 False）
- cron: str 定时表达式（默认 "0 3 * * *"，每天凌晨 3 点）
- max_files: int 单次预热最大文件数（默认 100）

架构：
- CacheWarmer 单例，warm_once 可被调度器 cron 触发或 API 手动触发
- 在 app/core/scheduler.py 中注册 cron 任务（参考 register_daily_checkin 模式）
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, save_setting, get_first_valid_account

logger = get_logger("app.services.cache_warmer")

# ===== 常量 =====

SETTINGS_KEY = "cache_warmer"
"""配置在 settings.json 中的存储键。"""

DEFAULT_CRON = "0 3 * * *"
"""默认 cron 表达式（每天凌晨 3 点）。"""

DEFAULT_MAX_FILES = 100
"""单次预热最大文件数。"""

# 视频扩展名（与 organize_service.VIDEO_EXTS 保持一致）
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".m4v", ".ts", ".m2ts", ".rmvb", ".iso",
}


class CacheWarmer:
    """
    缓存预热器（单例）。

    通过定时任务预加载 115 网盘文件列表和下载直链到内存缓存，
    消除用户首次播放时的 API 请求延迟。

    典型用法::

        from app.services.cache_warmer import get_cache_warmer
        warmer = get_cache_warmer()
        await warmer.warm_once()       # 手动触发一次预热
        status = warmer.get_status()   # 获取状态
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False          # 后台长轮询任务是否运行（预留）
        self._warming: bool = False          # 是否正在执行预热
        self._last_run: str = ""             # 最近一次预热时间
        self._total_warmed: int = 0          # 累计预热文件数
        self._last_error: str = ""           # 最近一次错误信息

    # ===== 配置读取 =====

    @staticmethod
    def get_config() -> dict:
        """读取缓存预热配置，返回合并默认值后的完整配置。"""
        cfg = read_setting(SETTINGS_KEY) or {}
        return {
            "enabled": cfg.get("enabled", False),
            "cron": cfg.get("cron", DEFAULT_CRON) or DEFAULT_CRON,
            "max_files": cfg.get("max_files", DEFAULT_MAX_FILES) or DEFAULT_MAX_FILES,
        }

    @staticmethod
    def save_config(enabled: bool, cron: str, max_files: int) -> dict:
        """保存缓存预热配置到 settings.json。"""
        cfg = {
            "enabled": bool(enabled),
            "cron": (cron or DEFAULT_CRON).strip(),
            "max_files": max(int(max_files), 1) if max_files else DEFAULT_MAX_FILES,
        }
        save_setting(SETTINGS_KEY, cfg)
        return cfg

    def is_enabled(self) -> bool:
        """是否已启用。"""
        return bool(self.get_config().get("enabled", False))

    # ===== 生命周期 =====

    def start(self) -> bool:
        """启动后台任务（幂等）。

        当前实现中预热由调度器 cron 驱动，start 主要标记运行状态。
        需在事件循环中调用。
        """
        if self._running:
            return True
        if not self.is_enabled():
            logger.info("[cache-warmer] 缓存预热未启用（enabled=false），不启动")
            return False
        self._running = True
        logger.info("[cache-warmer] 缓存预热服务已就绪")
        return True

    async def stop(self) -> None:
        """停止后台任务（幂等）。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("[cache-warmer] 缓存预热服务已停止")

    # ===== 核心预热逻辑 =====

    async def warm_once(self) -> dict:
        """执行一次缓存预热。

        遍历已配置的全量同步目录，预加载文件列表和前 N 个视频的下载直链。

        Returns:
            预热结果字典::
                {
                    "warmed": int,        # 本次预热的直链数
                    "scanned": int,       # 扫描到的文件总数
                    "skipped": int,       # 跳过的文件数（无 pickcode 等）
                    "errors": [str],      # 错误信息列表
                    "duration": float,    # 耗时（秒）
                }
        """
        if self._warming:
            logger.warning("[cache-warmer] 预热正在进行中，跳过本次触发")
            return {"warmed": 0, "scanned": 0, "skipped": 0, "errors": ["预热正在进行中"], "duration": 0}

        self._warming = True
        start_ts = time.time()
        result = {"warmed": 0, "scanned": 0, "skipped": 0, "errors": [], "duration": 0.0}

        try:
            # 获取有效账号
            account = get_first_valid_account()
            if not account or account.get("status") == 0:
                msg = "未找到有效账号或 cookies 已失效"
                logger.warning(f"[cache-warmer] {msg}")
                result["errors"].append(msg)
                return result

            cookies = account.get("cookies", "")
            account_id = account.get("id", 0)

            # 读取同步配置，获取同步目录和后缀
            from app.services.sync_service import SyncService
            sync_config = SyncService.load_schedule()
            source_cid = sync_config.get("source_cid", "")
            if not source_cid:
                msg = "未配置全量同步目录，请先在全量同步页面设置源目录"
                logger.warning(f"[cache-warmer] {msg}")
                result["errors"].append(msg)
                return result

            # 解析视频后缀
            video_exts_str = sync_config.get("video_exts_str", "")
            if video_exts_str:
                video_exts = _parse_exts(video_exts_str)
            else:
                video_exts = set(_VIDEO_EXTS)

            max_files = self.get_config().get("max_files", DEFAULT_MAX_FILES)

            logger.info(
                f"[cache-warmer] 开始预热: source_cid={source_cid}, "
                f"max_files={max_files}, video_exts={video_exts}"
            )

            # 第一步：预加载文件列表（调用 list_all_files 遍历目录树）
            from app.services.client_115 import Client115Service
            all_files = await asyncio.to_thread(
                Client115Service.list_all_files,
                cookies=cookies,
                cid=source_cid,
                video_exts=video_exts,
            )
            result["scanned"] = len(all_files)
            logger.info(f"[cache-warmer] 文件列表预加载完成: 共 {len(all_files)} 个视频文件")

            # 第二步：对前 N 个视频文件预填充直链缓存
            to_warm = all_files[:max_files]
            warmed = 0
            skipped = 0
            errors = []

            for i, f in enumerate(to_warm):
                pickcode = f.get("pickcode", "")
                file_name = f.get("name", "")
                if not pickcode:
                    skipped += 1
                    continue
                try:
                    url = await asyncio.to_thread(
                        Client115Service.get_download_url,
                        cookies=cookies,
                        pickcode=pickcode,
                        account_id=account_id,
                        context=file_name,
                    )
                    if url:
                        warmed += 1
                    else:
                        errors.append(f"获取直链失败: {file_name}")
                except Exception as e:
                    errors.append(f"{file_name}: {str(e)}")
                    logger.warning(f"[cache-warmer] 预热直链失败 {file_name}: {e}")

                # 每 10 个输出一次进度
                if (i + 1) % 10 == 0:
                    logger.info(f"[cache-warmer] 预热进度: {i + 1}/{len(to_warm)}")

            result["warmed"] = warmed
            result["skipped"] = skipped
            result["errors"] = errors
            result["duration"] = round(time.time() - start_ts, 2)

            # 更新状态
            self._last_run = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            self._total_warmed += warmed
            self._last_error = errors[-1] if errors else ""

            logger.info(
                f"[cache-warmer] 预热完成: 直链 {warmed} 个, 跳过 {skipped} 个, "
                f"耗时 {result['duration']}s"
            )
            return result

        except Exception as e:
            self._last_error = str(e)
            logger.warning(f"[cache-warmer] 预热异常: {e}", exc_info=True)
            result["errors"].append(str(e))
            result["duration"] = round(time.time() - start_ts, 2)
            return result
        finally:
            self._warming = False

    # ===== 状态查询 =====

    def get_status(self) -> dict:
        """获取缓存预热状态（供 API 使用）。

        Returns:
            状态字典::
                {
                    "running": bool,       # 服务是否运行中
                    "warming": bool,       # 是否正在执行预热
                    "last_run": str,       # 最近一次预热时间
                    "total_warmed": int,   # 累计预热文件数
                    "next_run": str,       # 下次预计运行时间
                    "last_error": str,     # 最近错误信息
                }
        """
        next_run = ""
        # 尝试从调度器获取下次运行时间
        try:
            from app.core.scheduler import get_scheduler, CACHE_WARMER_JOB_ID
            scheduler = get_scheduler()
            if scheduler:
                job = scheduler.get_job(CACHE_WARMER_JOB_ID)
                if job and job.next_run_time:
                    next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        return {
            "running": self._running,
            "warming": self._warming,
            "last_run": self._last_run,
            "total_warmed": self._total_warmed,
            "next_run": next_run,
            "last_error": self._last_error,
        }


# ===== 全局单例 =====

_global_cache_warmer: Optional[CacheWarmer] = None


def get_cache_warmer() -> CacheWarmer:
    """获取全局缓存预热器单例。"""
    global _global_cache_warmer
    if _global_cache_warmer is None:
        _global_cache_warmer = CacheWarmer()
    return _global_cache_warmer


# ===== 工具函数 =====

def _parse_exts(exts_str: str) -> set:
    """解析后缀字符串为集合（与 scheduler._parse_exts 一致）。"""
    if not exts_str or not exts_str.strip():
        return set()
    result = set()
    for e in exts_str.split(","):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        result.add(e)
    return result

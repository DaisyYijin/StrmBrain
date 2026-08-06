"""
115 生活事件监控服务
====================

参考 qmediasync 的 media_sync 模块：通过轮询 115 的"生活事件"接口
（life_recent_operations），感知网盘中的文件变化（新增/删除/移动/重命名），
事件驱动地增量同步到本地 STRM 目录，替代全量扫描对比。

优势：
- 秒级感知网盘变化（默认轮询间隔 30s），无需全量扫描 115 API
- 大幅减少 115 请求量：只处理变化的文件，而非每次扫描整个目录
- 离线下载完成 → 自动触发增量同步 + 整理，形成完整闭环

架构：
- 后台 asyncio 任务轮询 life_recent_operations
- 记录 last_data（last_time / last_count / total_count）实现增量拉取
- 解析事件类型：browse_video（忽略）/ move_file / copy_file / file_rename /
  new_folder / folder_rename / delete_file / copy_folder 等
- 事件应用到本地：新增→生成 STRM / 移动→迁移 STRM / 重命名→重写 STRM /
  删除→清理 STRM
- 变更累计后触发 Emby 媒体库刷新

配置（存于 settings.json 的 life_event 键）：
- enabled: bool 是否启用（默认 True，自动后台运行）
- interval: int 轮询间隔秒数（默认 30，最小 10）
- sync_after_changes: bool 有变更时是否触发增量同步兜底（默认 True，
  防止事件遗漏导致本地与网盘不一致）
"""
import asyncio
import threading
import time
from typing import Optional

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, save_setting

logger = get_logger("app.services.life_event_monitor")

# ===== 常量 =====
SETTINGS_KEY = "life_event"
DEFAULT_INTERVAL = 30          # 默认轮询间隔（秒）
MIN_INTERVAL = 10              # 最小轮询间隔（秒）
MAX_BATCH = 1000               # 单次拉取最大条数
STATE_FILE_KEY = "last_data"   # settings 中保存的 last_data（增量游标）

# 需要处理的文件操作类型（浏览类事件忽略）
# behavior_type 定义参考 p115client life_recent_operation_items
_EVENT_TYPES_FILE = {
    "upload_file": "created",        # 上传文件
    "upload_image_file": "created",  # 上传图片
    "move_file": "moved",            # 移动文件
    "move_image_file": "moved",      # 移动图片
    "copy_file": "created",          # 复制文件（新副本视为新增）
    "copy_folder": "created",        # 复制目录
    "file_rename": "renamed",        # 文件改名
    "delete_file": "deleted",        # 删除文件
    "new_folder": "created",         # 新增目录
    "folder_rename": "renamed",      # 目录改名
    "receive_files": "created",      # 接收文件（分享转存）
}
# 忽略的事件类型（浏览/播放等不改变文件结构）
_IGNORE_EVENT_TYPES = {
    "browse_video", "browse_image", "browse_audio", "browse_document",
    "star_file", "star_image", "folder_label",
}

# 视频扩展名（与 organize_service.VIDEO_EXTS 保持一致）
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}
# 元数据扩展名（nfo/字幕/图片等，事件中同样需要同步）
_DATA_EXTS = {".nfo", ".srt", ".ass", ".ssa", ".sub", ".jpg", ".jpeg", ".png", ".webp"}


# ===== 全局状态 =====

class LifeEventMonitor:
    """115 生活事件监控器（后台任务）。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event = threading.Event()
        self._last_data: Optional[dict] = None   # 增量游标
        self._last_check_ts: float = 0.0
        self._running = False
        self._stats = {
            "total_polls": 0,        # 轮询次数
            "events_seen": 0,        # 累计事件数
            "events_handled": 0,     # 已处理事件数
            "last_event_ts": 0,      # 最近事件时间戳
            "last_error": "",
        }
        self._pending_changes = 0    # 未触发的变更数（批量刷新用）

    # ===== 配置 =====

    @staticmethod
    def _get_config() -> dict:
        cfg = read_setting(SETTINGS_KEY) or {}
        return cfg

    def _get_interval(self) -> int:
        try:
            interval = int(self._get_config().get("interval", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        return max(interval, MIN_INTERVAL)

    def is_enabled(self) -> bool:
        return bool(self._get_config().get("enabled", True))

    # ===== 生命周期 =====

    def start(self) -> bool:
        """启动后台轮询任务（幂等）。需在事件循环中调用。"""
        if self._running:
            return True
        if not self.is_enabled():
            logger.info("[life-event] 115 生活事件监控未启用（enabled=false），不启动")
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[life-event] 启动失败：不在事件循环中")
            return False
        self._task = loop.create_task(self._run_loop())
        self._running = True
        logger.info(f"[life-event] 115 生活事件监控已启动，轮询间隔 {self._get_interval()}s")
        return True

    async def stop(self) -> None:
        """停止后台任务（幂等）。"""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("[life-event] 115 生活事件监控已停止")

    def get_status(self) -> dict:
        """获取监控状态（供 API 使用）。"""
        cfg = self._get_config()
        return {
            "enabled": cfg.get("enabled", True),
            "running": self._running,
            "interval": self._get_interval(),
            "last_data": self._last_data,
            "stats": self._stats,
        }

    def set_enabled(self, enabled: bool, interval: Optional[int] = None) -> dict:
        """设置开关与轮询间隔（需调用方随后 start/stop 生效，或本方法直接应用）。"""
        cfg = self._get_config()
        cfg["enabled"] = bool(enabled)
        if interval is not None:
            cfg["interval"] = max(int(interval), MIN_INTERVAL)
        save_setting(SETTINGS_KEY, cfg)
        return self.get_status()

    # ===== 主循环 =====

    async def _run_loop(self) -> None:
        """后台轮询主循环。"""
        logger.info("[life-event] 轮询循环已启动")
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["last_error"] = str(e)
                logger.warning(f"[life-event] 轮询异常: {e}")
            # 等待下一个周期
            try:
                await asyncio.sleep(self._get_interval())
            except asyncio.CancelledError:
                break
        logger.info("[life-event] 轮询循环已退出")

    async def _poll_once(self) -> None:
        """单次轮询：拉取事件 → 解析 → 应用。"""
        from app.core.json_storage import get_first_valid_account

        account = get_first_valid_account()
        if not account or account.get("status") == 0:
            self._stats["last_error"] = "未找到有效账号或 cookies 已失效"
            return

        cookies = account.get("cookies", "")
        from app.services.client_115 import Client115Service
        client = Client115Service.create_client_from_cookies(cookies)

        self._stats["total_polls"] += 1
        self._last_check_ts = time.time()

        # 拉取最近操作（增量游标）
        payload = {"limit": MAX_BATCH}
        if self._last_data:
            import json as _json
            payload["last_data"] = _json.dumps(self._last_data)

        try:
            resp = client.life_recent_operations(payload)
        except Exception as e:
            self._stats["last_error"] = f"life_recent_operations: {e}"
            logger.warning(f"[life-event] 拉取生活事件失败: {e}")
            return

        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            self._stats["last_error"] = f"life_recent_operations 返回异常: {str(resp)[:200]}"
            logger.warning(f"[life-event] 生活事件返回异常: {str(resp)[:200]}")
            return

        # 更新增量游标
        new_last_data = data.get("last_data") or data.get("last") or {}
        if isinstance(new_last_data, dict):
            self._last_data = new_last_data

        events = data.get("list") or data.get("events") or data.get("data") or []
        if isinstance(events, dict):
            events = events.get("list") or []
        if not events:
            return

        self._stats["events_seen"] += len(events)
        self._stats["last_event_ts"] = time.time()

        # 按时间正序处理（接口返回倒序）
        events = list(events)
        events.reverse()

        handled = await self._process_events(client, cookies, events)
        self._stats["events_handled"] += handled

        # 有变更时触发 Emby 刷新（防抖：短时间合并）
        if handled > 0:
            self._pending_changes += handled
            await self._maybe_refresh_emby()
            # O11: 去抖动汇总通知——批量事件平息后只发一条汇总，避免逐条刷屏
            try:
                from app.services.notification_manager import get_notification_manager
                await get_notification_manager().send_notification_debounced(
                    "sync_complete",
                    "115 生活事件增量同步",
                    counters={"变更文件": handled},
                    delay=60.0,
                    group_key="life_event",
                )
            except Exception as e:
                logger.debug(f"[life-event] 去抖动通知登记失败: {e}")

    async def _process_events(self, client, cookies: str, events: list) -> int:
        """
        解析并应用事件列表，返回实际处理（产生本地变更）的事件数。
        client: p115client 实例（用于获取事件详情）
        """
        handled = 0
        # 事件可能批量到达，先合并同类事件避免重复处理
        for ev in events:
            try:
                if await self._handle_event(client, cookies, ev):
                    handled += 1
            except Exception as e:
                logger.warning(f"[life-event] 处理事件异常 {ev}: {e}")
        return handled

    async def _handle_event(self, client, cookies: str, ev: dict) -> bool:
        """处理单条事件，返回是否产生本地变更。
        ev: 生活事件条目（含 behavior_type / file_id / parent_id / file_name 等）"""
        behavior_type = ev.get("behavior_type") or ev.get("type") or ""
        if not behavior_type:
            return False
        if behavior_type in _IGNORE_EVENT_TYPES:
            return False

        action = _EVENT_TYPES_FILE.get(behavior_type)
        if not action:
            logger.debug(f"[life-event] 忽略事件类型: {behavior_type}")
            return False

        # 事件条目中可能不含完整文件信息，尝试获取详情
        file_name = ev.get("file_name") or ev.get("name") or ""
        file_id = ev.get("file_id") or ""
        parent_id = ev.get("parent_id") or ""
        ext = ("." + file_name.rsplit(".", 1)[-1]).lower() if "." in file_name else ""
        is_dir = ev.get("file_category") == 0 or (ev.get("fid") is None and file_name)

        # 只处理视频与元数据文件（目录事件除外，目录内文件会单独有事件）
        if not is_dir and ext and ext not in _VIDEO_EXTS and ext not in _DATA_EXTS:
            return False

        logger.info(
            f"[life-event] 事件: {behavior_type}({action}) '{file_name}' "
            f"file_id={file_id} parent_id={parent_id}"
        )

        # 委托给同步服务应用变更（新增/删除/移动/重命名统一走增量同步对比）
        changed = await self._apply_event_to_sync(cookies, file_id, file_name, parent_id, behavior_type, action)
        return changed

    async def _apply_event_to_sync(
        self,
        cookies: str,
        file_id: str,
        file_name: str,
        parent_id: str,
        behavior_type: str,
        action: str,
    ) -> bool:
        """
        将事件应用到本地 STRM 目录。
        策略：事件驱动的精确操作 + 增量同步兜底。
        - created/moved/renamed：定位文件所在目录 → 增量同步该目录
        - deleted：从本地清单删除对应条目 → 清理本地文件
        返回是否产生本地变更。
        """
        from app.services.sync_service import SyncService

        # 读取当前同步计划配置
        config = SyncService.load_schedule()
        if not config:
            return False
        source_cid = config.get("source_cid", "")
        local_media_dir = config.get("local_media_dir", "")
        if not source_cid or not local_media_dir:
            return False

        if action == "deleted" and file_id:
            # 删除事件：直接从清单清理（精确，无需扫描）
            return SyncService.remove_from_manifest_by_file_id(local_media_dir, file_id)

        # 其他事件（新增/移动/重命名/复制）：触发一次轻量增量同步
        # 复用增量同步的清单对比逻辑，自动处理新增/迁移/重命名
        loop = asyncio.get_running_loop()
        try:
            video_exts = _parse_exts(config.get("video_exts_str", "")) or _VIDEO_EXTS
            image_exts = _parse_exts(config.get("image_exts_str", ""))
            data_exts = _parse_exts(config.get("data_exts_str", "")) or _DATA_EXTS
            min_video_size_mb = config.get("min_video_size_mb", 0)

            result = await asyncio.to_thread(
                SyncService.incremental_sync,
                cookies=cookies,
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                video_exts=video_exts,
                image_exts=image_exts,
                data_exts=data_exts,
                min_video_size_mb=min_video_size_mb,
                account_id=0,
                loop=loop,
            )
            synced_count = len(result.get("synced", []))
            if synced_count > 0:
                logger.info(f"[life-event] 事件触发增量同步完成: 变更 {synced_count} 个文件")
            return synced_count > 0
        except Exception as e:
            logger.warning(f"[life-event] 事件触发增量同步失败: {e}")
            return False

    async def _maybe_refresh_emby(self) -> None:
        """有变更时触发 Emby 媒体库刷新（防抖 5 秒合并多次事件）。"""
        try:
            await asyncio.sleep(5)
            if self._pending_changes <= 0:
                return
            pending = self._pending_changes
            self._pending_changes = 0

            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True):
                from app.services.emby import trigger_emby_refresh
                await trigger_emby_refresh()
                logger.info(f"[life-event] 已触发 Emby 刷新（{pending} 项变更）")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[life-event] 触发 Emby 刷新失败: {e}")


# ===== 全局单例 =====

global_life_event_monitor: Optional[LifeEventMonitor] = None


def get_life_event_monitor() -> LifeEventMonitor:
    """获取全局生活事件监控器单例。"""
    global global_life_event_monitor
    if global_life_event_monitor is None:
        global_life_event_monitor = LifeEventMonitor()
    return global_life_event_monitor


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

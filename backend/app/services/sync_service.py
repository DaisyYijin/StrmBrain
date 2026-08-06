"""
同步服务 - 全量同步、增量同步、上传同步
全量同步：扫描指定目录下所有匹配后缀的文件，生成 STRM 文件到本地目录
增量同步：仅同步新增/变更的文件
上传同步：将本地元数据文件（nfo、图片、字幕等）上传到 115 网盘
"""
import json
import time
import asyncio
import shutil
import hashlib
import sqlite3
import threading
import os
from typing import Optional
from pathlib import Path

from app.services.client_115 import Client115Service
from app.config import DATA_DIR, CONFIG_DIR
from app.core.logbuffer import get_logger
from app.core.db_helper import get_api_intervals

logger = get_logger("app.services.sync_service")


def _compare_strm_content(existing_content: str, new_content: str) -> bool:
    """S3: STRM 内容级比对。

    参考 qmediasync sync_strm.go 的 CompareStrm 思路（简化版）：
    目标文件已存在时，若其内容与待写入内容一致（去首尾空白后比较），
    则无需重新写入，避免无效 I/O 与 mtime 抖动。
    返回 True 表示内容相同（可跳过写入）。
    """
    try:
        return (existing_content or "").strip() == (new_content or "").strip()
    except Exception:
        return False


# 默认视频后缀
DEFAULT_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}

# 同步计划配置文件
SCHEDULE_FILE = CONFIG_DIR / "sync_schedule.json"

# 清单目录（保存在后端数据目录，不污染用户的媒体目录）
MANIFEST_DIR = DATA_DIR / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

# SQLite 清单数据库（替代 JSON，支持路径索引和高效查询）
MANIFEST_DB = MANIFEST_DIR / "manifests.db"
_manifest_db_lock = threading.Lock()

# 同步并发锁：防止全量/增量同步被并发触发
_sync_lock = threading.Lock()


def _manifest_path(local_root: Path) -> Path:
    """根据本地媒体目录路径生成清单文件路径（按路径哈希命名，避免特殊字符）"""
    key = hashlib.md5(str(local_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return MANIFEST_DIR / f"manifest_{key}.json"


def _manifest_hash(local_root: Path) -> str:
    """根据本地媒体目录路径生成哈希键（用于 SQLite 分区）"""
    return hashlib.md5(str(local_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _init_manifest_db():
    """初始化 SQLite 清单数据库（线程安全，仅初始化一次）"""
    with _manifest_db_lock:
        conn = sqlite3.connect(str(MANIFEST_DB))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manifest_entries (
                    local_root_hash TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    sha1 TEXT,
                    pickcode TEXT,
                    parent_path TEXT,
                    name TEXT,
                    size INTEGER,
                    PRIMARY KEY (local_root_hash, file_id)
                )
            """)
            # 路径索引：支持按目录路径快速查询（增量同步中按目录筛选）
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_manifest_path
                ON manifest_entries (local_root_hash, parent_path)
            """)
            conn.commit()
        finally:
            conn.close()


# 模块加载时初始化数据库
_init_manifest_db()


class SyncService:
    """网盘文件同步服务"""

    @staticmethod
    def _safe_schedule(loop: Optional[asyncio.AbstractEventLoop], coro):
        """
        从工作线程安全地向事件循环提交协程。
        loop 为 None 时静默跳过（不支持进度上报的环境）。
        """
        if loop is None:
            coro.close()  # 避免协程未消费警告
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()

    @classmethod
    def _notify_sync_failures(cls, errors: list, sync_type: str, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Q5: 扫描失败聚合通知。

        同步结束后若 errors 非空，取前 10 条错误（每条 {"name", "error"}）拼成文本，
        调用 NotificationService.notify 推送"同步失败提醒"。整体 try/except 包裹，
        通知失败不影响同步主流程。
        """
        if not errors:
            return
        try:
            # 拼接聚合文本：[STRMhub] {sync_type} 同步失败 {N} 项\n1. xxx: 错误
            top = errors[:10]
            lines = [f"[STRMhub] {sync_type} 同步失败 {len(errors)} 项"]
            for i, e in enumerate(top, 1):
                name = (e or {}).get("name", "") or "未知文件"
                err = (e or {}).get("error", "") or "未知错误"
                lines.append(f"{i}. {name}: {err}")
            content = "\n".join(lines)

            async def _do_notify():
                try:
                    from app.services.notification_service import NotificationService
                    await NotificationService.notify("同步失败提醒", content)
                except Exception:
                    pass

            # 优先调度到主事件循环（同步方法通常运行在工作线程）；无可用循环时独立运行
            if loop is not None and loop.is_running():
                cls._safe_schedule(loop, _do_notify())
            else:
                asyncio.run(_do_notify())
            logger.info(f"[sync] 已发送同步失败聚合通知: {sync_type} {len(errors)} 项")
        except Exception as e:
            logger.warning(f"[sync] 发送同步失败通知失败: {e}")

    # ============ 全量同步 ============

    @classmethod
    def full_sync(
        cls,
        cookies: str,
        source_cid: str,
        local_media_dir: str,
        video_exts: set[str],
        image_exts: Optional[set] = None,
        data_exts: Optional[set] = None,
        min_video_size_mb: int = 0,
        account_id: int = 0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> dict:
        """
        全量同步：扫描源目录下所有匹配后缀的文件，生成 STRM 到本地目录

        - 视频文件：生成 .strm 文件（内容为 115 下载链接）
        - 图片文件：直接下载到本地
        - 数据文件（nfo/srt 等）：直接下载到本地
        - loop: 主事件循环，用于从工作线程提交进度上报协程
        """
        from app.core.progress import progress_manager

        # 并发保护：同一时刻只允许一个同步任务执行
        if not _sync_lock.acquire(blocking=False):
            logger.warning("[sync] 已有同步任务正在运行，跳过本次全量同步")
            return {"total": 0, "synced": [], "skipped": 0, "errors": [], "skipped_concurrent": True}

        image_exts = image_exts or set()
        data_exts = data_exts or set()
        all_exts = video_exts | image_exts | data_exts
        min_size_bytes = min_video_size_mb * 1024 * 1024 if min_video_size_mb > 0 else 0

        logger.info(f"[sync] 全量同步开始: source_cid={source_cid}, local_dir={local_media_dir}, video_exts={video_exts}, image_exts={image_exts}, data_exts={data_exts}, min_size={min_size_bytes}")
        cls._log_rate_config()
        _start_ts = time.time()

        result = {
            "total": 0,
            "synced": [],
            "skipped": 0,
            "errors": [],
        }

        local_root = Path(local_media_dir)
        local_root.mkdir(parents=True, exist_ok=True)

        # 预加载 STRM 设置，避免每个文件都读 JSON
        strm_settings = cls._load_strm_settings()

        # 扫描源目录所有文件
        all_files = Client115Service.list_all_files_with_meta(
            cookies, source_cid, all_exts, min_size=0, recursive=True
        )
        logger.info(f"[sync] 扫描到 {len(all_files)} 个匹配文件")

        # 过滤视频文件的最小大小
        filtered = []
        for f in all_files:
            ext = cls._get_ext(f["name"])
            if ext in video_exts and min_size_bytes > 0 and f.get("size", 0) < min_size_bytes:
                result["skipped"] += 1
                continue
            filtered.append(f)

        result["total"] = len(filtered)
        if not filtered:
            cls._save_manifest(local_root, {})
            _sync_lock.release()
            return result

        # 更新进度总数（start_task 已由调用方在 API 层完成）
        cls._safe_schedule(loop, progress_manager.update_total(len(filtered)))

        # 生成 STRM / 下载文件
        manifest = {}
        # 收集延迟下载任务（数据/图片文件），主循环结束后批量下载
        _pending_downloads: list[dict] = []
        for idx, f in enumerate(filtered):
            cls._safe_schedule(loop, progress_manager.update_progress(idx, f["name"]))
            _cur_no = idx + 1
            _cur_name = f.get("name", "")
            _cur_dir = f.get("parent_path", "")

            # 每个文件都输出处理日志，含目录信息
            _interval = get_api_intervals().get("sync_file_interval", 3.0)
            logger.info(f"[sync] 正在处理第 {_cur_no}/{len(filtered)} 个文件: {_cur_name}")

            synced, entry = cls._sync_single_file(
                cookies, f, local_root, video_exts, image_exts, data_exts,
                account_id, strm_settings, defer_download=True,
            )
            if synced:
                if synced.get("type") == "skipped":
                    result["skipped"] += 1
                elif synced.get("type") == "pending_download":
                    # 收集到批量下载列表，稍后统一下载
                    _pending_downloads.append({
                        "pickcode": synced["pickcode"],
                        "local_path": synced["local_path"],
                        "context": synced["name"],
                        "_manifest_key": f["file_id"],
                        "_synced_entry": {
                            "name": synced["name"], "type": "download",
                            "path": synced["path"],
                        },
                    })
                else:
                    result["synced"].append(synced)
                if entry:
                    manifest[f["file_id"]] = entry
            elif entry:
                result["errors"].append(entry)

            # 文件间等待（仅对需要 API 调用的操作，STRM 生成无 API 调用可跳过）
            if _interval > 0:
                logger.info(f"[sync] 115 API 暂停 {_interval:.1f}s...")
                time.sleep(_interval)

        # 批量下载数据/图片文件（串行获取链接 + 并发下载内容）
        if _pending_downloads:
            logger.info(f"[sync] 开始批量下载 {_pending_downloads.__len__()} 个数据/图片文件...")
            cls._safe_schedule(loop, progress_manager.update_progress(
                len(filtered), f"批量下载 {len(_pending_downloads)} 个文件"))
            batch_results = Client115Service.download_files_batch(cookies, _pending_downloads)
            for i, br in enumerate(batch_results):
                if br["success"]:
                    result["synced"].append({
                        "name": _pending_downloads[i]["context"],
                        "type": "download",
                        "path": _pending_downloads[i]["local_path"],
                    })
                else:
                    result["errors"].append({
                        "name": _pending_downloads[i]["context"],
                        "error": br.get("error", "下载失败"),
                    })
                    # 失败的文件从清单中移除，下次同步会重试
                    mk = _pending_downloads[i].get("_manifest_key", "")
                    if mk and mk in manifest:
                        del manifest[mk]
            logger.info(f"[sync] 批量下载完成: 成功 {sum(1 for r in batch_results if r['success'])}, "
                        f"失败 {sum(1 for r in batch_results if not r['success'])}")

        # 保存清单
        cls._save_manifest(local_root, manifest)

        summary = f"共 {result['total']} 个文件，成功 {len(result['synced'])}，跳过 {result['skipped']}，失败 {len(result['errors'])}"
        logger.info(f"[sync] 全量同步完成: {summary}, 耗时 {time.time() - _start_ts:.1f}s")
        cls._safe_schedule(loop, progress_manager.complete_task(summary))

        # #23: 发布 SYNC_COMPLETED 事件到事件总线
        try:
            from app.core.event_bus import get_event_bus, EventBus
            get_event_bus().publish(EventBus.SYNC_COMPLETED, {
                "sync_type": "full",
                "summary": summary,
                "total": result["total"],
                "synced": len(result["synced"]),
                "skipped": result["skipped"],
                "errors": len(result["errors"]),
                "local_media_dir": local_media_dir,
            })
        except Exception:
            pass

        # Q5: 扫描失败聚合通知（errors 非空时推送前 10 条错误）
        if result["errors"]:
            cls._notify_sync_failures(result["errors"], "全量", loop)

        _sync_lock.release()
        return result

    # ============ 增量同步 ============

    @classmethod
    def incremental_sync(
        cls,
        cookies: str,
        source_cid: str,
        local_media_dir: str,
        video_exts: Optional[set] = None,
        image_exts: Optional[set] = None,
        data_exts: Optional[set] = None,
        min_video_size_mb: int = 0,
        account_id: int = 0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> dict:
        """
        增量同步：仅同步新增/变更的文件

        通过对比本地清单（.strmhub_manifest.json）中的 file_id 和 sha1，
        只处理新增或变更的文件。
        loop: 主事件循环，用于从工作线程提交进度上报协程
        """
        from app.core.progress import progress_manager

        # 并发保护：同一时刻只允许一个同步任务执行
        if not _sync_lock.acquire(blocking=False):
            logger.warning("[sync] 已有同步任务正在运行，跳过本次增量同步")
            return {"total": 0, "synced": [], "skipped": 0, "errors": [], "skipped_concurrent": True}

        video_exts = video_exts or DEFAULT_VIDEO_EXTS
        image_exts = image_exts or set()
        data_exts = data_exts or set()
        all_exts = video_exts | image_exts | data_exts
        min_size_bytes = min_video_size_mb * 1024 * 1024 if min_video_size_mb > 0 else 0

        logger.info(f"[sync] 增量同步开始: source_cid={source_cid}, local_dir={local_media_dir}")
        cls._log_rate_config()
        _start_ts = time.time()

        result = {
            "total": 0,
            "synced": [],
            "skipped": 0,
            "errors": [],
        }

        local_root = Path(local_media_dir)

        if not local_root.exists():
            try:
                local_root.mkdir(parents=True, exist_ok=True)
                logger.info(f"[sync] 增量同步：本地媒体目录不存在，已自动创建 {local_media_dir}")
            except Exception as e:
                logger.warning(f"[sync] 增量同步跳过：无法创建本地媒体目录 {local_media_dir} - {e}")
                return {
                    "total": 0, "synced": [], "skipped": 0,
                    "errors": [{"name": "", "error": f"无法创建本地媒体目录: {e}"}],
                }

        # 预加载 STRM 设置
        strm_settings = cls._load_strm_settings()

        # 加载上次同步清单
        last_manifest = cls._load_manifest(local_root)

        # 一次性扫描本地目录
        logger.info(f"[sync] 扫描本地目录已有文件...")
        local_existing_files: set = set()
        for item in local_root.rglob("*"):
            if item.is_file():
                local_existing_files.add(str(item.relative_to(local_root)).replace("\\", "/"))
        logger.info(f"[sync] 本地已有 {len(local_existing_files)} 个文件")

        # 扫描源目录
        # S2: 优先尝试 115 导出目录树快速扫描（export_dir），失败/不支持则回退递归扫描
        all_files = []
        try:
            tree = Client115Service.export_dir_tree(cookies, source_cid)
        except Exception as e:
            tree = {}
            logger.warning(f"[sync] export_dir 树快速扫描异常，回退递归扫描: {e}")
        if tree and tree.get("files"):
            # 基于树结构生成 all_files 等价列表（字段与 list_all_files_with_meta 兼容）
            for f in tree["files"]:
                name = f.get("name", "")
                if cls._get_ext(name) not in all_exts:
                    continue
                all_files.append({
                    "file_id": f.get("file_id", ""),
                    "pickcode": f.get("pickcode", ""),
                    "name": name,
                    "size": f.get("size", 0) or 0,
                    "parent_path": f.get("parent_path", ""),
                    "sha1": f.get("sha1", "") or "",
                })
            logger.info(f"[sync] 使用 export_dir 树快速扫描，{len(all_files)} 个文件")
        if not all_files:
            # 回退：递归扫描（与原有逻辑一致）
            all_files = Client115Service.list_all_files_with_meta(
                cookies, source_cid, all_exts, min_size=0, recursive=True
            )
            logger.info(f"[sync] 扫描到 {len(all_files)} 个匹配文件")
        result["total"] = len(all_files)

        # 防误删保护：远端扫描返回 0 结果但本地清单有记录时，
        # 极大概率是 115 API 异常（限流/cookies 过期/网络问题），而非文件真的全部被删。
        # 此时中止清理逻辑，保留本地 STRM 文件不被误删。
        if len(all_files) == 0 and len(last_manifest) > 0:
            logger.warning(
                f"[sync] 远端扫描返回 0 结果但本地清单有 {len(last_manifest)} 条记录，"
                f"疑似 115 API 异常，跳过删除清理以保护本地文件"
            )
            result["errors"].append({
                "name": "",
                "error": "远端扫描 0 结果，已中止清理（疑似 API 异常）",
            })
            summary = f"远端扫描 0 结果，已跳过清理保护 {len(last_manifest)} 个本地文件"
            logger.info(f"[sync] 增量同步完成（防误删保护触发）: {summary}, 耗时 {time.time() - _start_ts:.1f}s")
            cls._safe_schedule(loop, progress_manager.complete_task(summary))
            _sync_lock.release()
            return result

        # 更新进度总数
        cls._safe_schedule(loop, progress_manager.update_total(len(all_files)))

        # 对比清单
        new_manifest = {}
        processed = 0
        for f in all_files:
            name = f["name"]
            ext = cls._get_ext(name)
            file_id = f.get("file_id", "")
            sha1 = f.get("sha1", "")
            parent_path = f.get("parent_path", "")
            pickcode = f.get("pickcode", "")

            processed += 1
            _interval = get_api_intervals().get("sync_file_interval", 3.0)
            logger.info(f"[sync] 正在处理第 {processed}/{len(all_files)} 个文件: {name}")
            if processed % 10 == 0:
                cls._safe_schedule(loop, progress_manager.update_progress(processed, name))

            # 视频文件最小大小过滤
            if ext in video_exts and min_size_bytes > 0 and f.get("size", 0) < min_size_bytes:
                result["skipped"] += 1
                continue

            # 检查是否已同步
            existing = last_manifest.get(file_id)
            if existing and existing.get("sha1") == sha1 and sha1:
                # 文件内容未变更
                old_parent_path = existing.get("parent_path", "")
                old_name = existing.get("name", "")
                if old_parent_path != parent_path or old_name != name:
                    # 文件被移动或重命名
                    cls._relocate_local_file(
                        local_root, old_parent_path, old_name,
                        parent_path, name, ext, video_exts, image_exts, data_exts,
                        cookies, pickcode, result, account_id, strm_settings,
                    )
                    new_manifest[file_id] = {
                        "name": name, "size": f.get("size", 0), "sha1": sha1,
                        "pickcode": pickcode, "parent_path": parent_path,
                    }
                else:
                    # 检查本地文件是否实际存在且有效
                    if ext in video_exts:
                        local_rel = (f"{parent_path}/{name}.strm") if parent_path else f"{name}.strm"
                    else:
                        local_rel = f"{parent_path}/{name}" if parent_path else name
                    local_file_valid = local_rel in local_existing_files
                    # STRM 文件需额外检查内容是否为空（空文件视为缺失，触发重新生成）
                    if local_file_valid and ext in video_exts:
                        local_file = local_root / local_rel
                        if not local_file.exists() or local_file.stat().st_size == 0:
                            local_file_valid = False
                            logger.info(f"[sync] STRM 文件内容为空，重新生成: {local_rel}")
                    if local_file_valid:
                        result["skipped"] += 1
                        new_manifest[file_id] = existing
                    else:
                        # 本地文件被删除或为空，重新同步
                        logger.info(f"[sync] 本地文件缺失，重新同步: {local_rel}")
                        synced, entry = cls._sync_single_file(
                            cookies, f, local_root, video_exts, image_exts, data_exts,
                            account_id, strm_settings,
                        )
                        if synced:
                            if synced.get("type") == "skipped":
                                result["skipped"] += 1
                            else:
                                result["synced"].append(synced)
                        elif entry:
                            result["errors"].append(entry)
                        new_manifest[file_id] = {
                            "name": name, "size": f.get("size", 0), "sha1": sha1,
                            "pickcode": pickcode, "parent_path": parent_path,
                        }
                continue

            # 新增或变更文件
            synced, entry = cls._sync_single_file(
                cookies, f, local_root, video_exts, image_exts, data_exts,
                account_id, strm_settings,
            )
            if synced:
                if synced.get("type") == "skipped":
                    result["skipped"] += 1
                else:
                    result["synced"].append(synced)
                new_manifest[file_id] = {
                    "name": name, "size": f.get("size", 0), "sha1": sha1,
                    "pickcode": pickcode, "parent_path": parent_path,
                }
            elif entry:
                result["errors"].append(entry)

            # 文件间等待（_interval 已在循环开头定义）
            if _interval > 0:
                logger.info(f"[sync] 115 API 暂停 {_interval:.1f}s...")
                time.sleep(_interval)

        # 检测已删除的文件
        cls._cleanup_deleted_files(local_root, last_manifest, new_manifest, video_exts, image_exts, data_exts, result=result, strm_settings=strm_settings)

        # 保存更新后的清单
        cls._save_manifest(local_root, new_manifest)

        summary = f"共 {result['total']} 个文件，新增同步 {len(result['synced'])}，跳过 {result['skipped']}，失败 {len(result['errors'])}"
        logger.info(f"[sync] 增量同步完成: {summary}, 耗时 {time.time() - _start_ts:.1f}s")
        cls._safe_schedule(loop, progress_manager.complete_task(summary))

        # #23: 发布 SYNC_COMPLETED 事件到事件总线
        try:
            from app.core.event_bus import get_event_bus, EventBus
            get_event_bus().publish(EventBus.SYNC_COMPLETED, {
                "sync_type": "incremental",
                "summary": summary,
                "total": result["total"],
                "synced": len(result["synced"]),
                "skipped": result["skipped"],
                "errors": len(result["errors"]),
                "local_media_dir": local_media_dir,
            })
        except Exception:
            pass

        # Q5: 扫描失败聚合通知（errors 非空时推送前 10 条错误）
        if result["errors"]:
            cls._notify_sync_failures(result["errors"], "增量", loop)

        _sync_lock.release()
        return result

    # ============ 同步计划管理 ============

    @classmethod
    def save_schedule(
        cls,
        account_id: int,
        source_cid: str,
        local_media_dir: str,
        cron: str = "",
        video_exts_str: str = "",
        image_exts_str: str = "",
        data_exts_str: str = "",
        min_video_size_mb: int = 0,
        preserve_cron: bool = False,
        source_path: str = "",
    ) -> bool:
        """保存同步计划配置到本地文件"""
        try:
            if preserve_cron:
                existing = cls.load_schedule()
                cron = existing.get("cron", "")
                if not source_path:
                    source_path = existing.get("source_path", "")

            config = {
                "account_id": account_id,
                "source_cid": source_cid,
                "source_path": source_path,
                "local_media_dir": local_media_dir,
                "cron": cron,
                "video_exts_str": video_exts_str,
                "image_exts_str": image_exts_str,
                "data_exts_str": data_exts_str,
                "min_video_size_mb": min_video_size_mb,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            SCHEDULE_FILE.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except Exception as e:
            logger.warning(f"[sync] 保存同步计划失败: {e}")
            return False

    @classmethod
    def load_schedule(cls) -> dict:
        """加载同步计划配置"""
        try:
            if SCHEDULE_FILE.exists():
                return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[sync] 加载同步计划失败: {e}")
        return {}

    # ============ 核心同步方法（消除重复代码） ============

    @classmethod
    def _sync_single_file(
        cls,
        cookies: str,
        f: dict,
        local_root: Path,
        video_exts: set,
        image_exts: set,
        data_exts: set,
        account_id: int,
        strm_settings: dict,
        defer_download: bool = False,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        同步单个文件：生成 STRM 或下载文件。
        返回 (synced_entry, error_entry)，成功时 error_entry 为 None。
        defer_download=True 时，数据/图片文件不立即下载，返回 pending_download 类型。
        """
        name = f["name"]
        ext = cls._get_ext(name)
        parent_path = f.get("parent_path", "")
        pickcode = f.get("pickcode", "")
        file_id = f.get("file_id", "")
        size = f.get("size", 0)
        sha1 = f.get("sha1", "")

        local_dir = local_root / parent_path if parent_path else local_root

        # 路径长度防护：Windows MAX_PATH=260，预留余量取 250。
        # 超长路径会导致文件创建失败（FilesystemError），跳过并记录。
        local_name = name + ".strm" if ext in video_exts else name
        full_path = local_dir / local_name
        if len(str(full_path)) > 250:
            logger.warning(
                f"[sync] 路径长度 {len(str(full_path))} 超过 250 字符限制，跳过: {parent_path}/{name}"
            )
            return None, {"name": name, "error": f"路径过长（{len(str(full_path))}字符），已跳过"}

        local_dir.mkdir(parents=True, exist_ok=True)

        manifest_entry = {
            "name": name, "size": size, "sha1": sha1,
            "pickcode": pickcode, "parent_path": parent_path,
        }

        try:
            if ext in video_exts:
                # 视频文件：生成 .strm（保留原始后缀，如 钢铁侠.mkv.strm）
                strm_name = name + ".strm"
                strm_path = local_dir / strm_name

                # #19: 路径映射 - 应用到本地相对路径（可能改变 STRM 落盘位置）
                from app.services.path_mapper import PathMapper
                _path_mapper = PathMapper(PathMapper.get_config())
                _rel_path_str = f"{parent_path}/{strm_name}" if parent_path else strm_name
                _rel_path_str = _path_mapper.apply(_rel_path_str, "strm_rel")
                strm_path = local_root / _rel_path_str
                # 应用到本地绝对路径（local 映射可能将文件写到 local_root 之外）
                _local_mapped = _path_mapper.apply(str(strm_path), "local")
                if _local_mapped and _local_mapped != str(strm_path):
                    strm_path = Path(_local_mapped)
                strm_path.parent.mkdir(parents=True, exist_ok=True)

                # overwrite_mode=skip 时，已存在且非空的 STRM 文件跳过写入
                overwrite_mode = (strm_settings or {}).get("overwrite_mode", "skip")
                if overwrite_mode == "skip" and strm_path.exists() and strm_path.stat().st_size > 0:
                    return {"name": name, "type": "skipped"}, manifest_entry

                content = cls._generate_strm_content(name, parent_path, pickcode, account_id, strm_settings)
                if not content:
                    logger.warning(f"[sync] STRM 内容为空（server_url 未配置），跳过: {parent_path}/{strm_name}")
                    return None, {"name": name, "error": "server_url 未配置，无法生成 STRM"}

                # #19: 路径映射 - 应用到 STRM 内容（播放 URL），写 STRM 内容前生效
                content = _path_mapper.apply(content, "strm_url")

                # S3: 内容级比对 + mtime 保持（参考 qmediasync CompareStrm）
                # 目标 STRM 已存在时，先读取其内容与待写入内容比对：
                # - 内容相同 → 跳过写入，并尝试把 mtime 设为远端文件的时间（取不到则用当前时间）
                # - 内容不同 → 正常写入
                if strm_path.exists():
                    try:
                        existing_content = strm_path.read_text(encoding="utf-8")
                    except Exception:
                        existing_content = None
                    if existing_content is not None and _compare_strm_content(existing_content, content):
                        try:
                            mtime = cls._extract_remote_mtime(f)
                            os.utime(strm_path, (mtime, mtime))
                        except Exception:
                            pass
                        logger.info(f"[sync] STRM 内容未变更，跳过写入并保持 mtime: {parent_path}/{strm_name}")
                        # 跳过写入不应算作"新增"，按 skipped 计数（由调用方统一累加）
                        return {"name": name, "type": "skipped"}, manifest_entry
                strm_path.write_text(content, encoding="utf-8")

                # #19: 映射后的路径可能位于 local_root 之外，relative_to 容错处理
                try:
                    _return_path = str(strm_path.relative_to(local_root))
                except ValueError:
                    _return_path = str(strm_path)
                return {
                    "name": name, "type": "strm",
                    "path": _return_path,
                }, manifest_entry

            elif ext in image_exts or ext in data_exts:
                # 图片/数据文件：直接下载或延迟批量下载
                local_path = local_dir / name

                if defer_download:
                    # 返回下载任务信息，由调用方批量下载
                    return {
                        "name": name, "type": "pending_download",
                        "path": str(local_path.relative_to(local_root)),
                        "pickcode": pickcode, "local_path": str(local_path),
                    }, manifest_entry

                ok = Client115Service.download_file(cookies, pickcode, str(local_path), context=name)
                if ok:
                    return {
                        "name": name, "type": "download",
                        "path": str(local_path.relative_to(local_root)),
                    }, manifest_entry
                else:
                    return None, {"name": name, "error": "下载失败"}

            return None, None

        except Exception as e:
            return None, {"name": name, "error": str(e)}

    # ============ 辅助方法 ============

    @staticmethod
    def _get_ext(name: str) -> str:
        """从文件名提取小写扩展名（带点）"""
        if "." in name:
            return ("." + name.rsplit(".", 1)[-1]).lower()
        return ""

    @staticmethod
    def _extract_remote_mtime(f: dict) -> float:
        """S3: 从远端文件元数据中提取 mtime（时间戳秒）。

        优先使用文件元数据里的 time / updated_at 字段；取不到时间字段时用当前时间。
        兼容毫秒级时间戳（大于 10^11 视为毫秒，转换为秒）。
        """
        ts = f.get("time") or f.get("updated_at")
        if ts:
            try:
                ts = float(ts)
                if ts > 1e11:
                    ts = ts / 1000.0
                return ts
            except (TypeError, ValueError):
                pass
        return time.time()

    @staticmethod
    def _load_strm_settings() -> dict:
        """从数据库读取 STRM 直链配置"""
        from app.core.db_helper import read_setting
        return read_setting("strm")

    @classmethod
    def _log_rate_config(cls):
        """记录当前生效的 API 请求间隔配置（仅日志，不影响业务）"""
        try:
            from app.core.db_helper import get_api_intervals
            iv = get_api_intervals()
            logger.info(
                f"[sync] API 请求间隔: 列表 {iv.get('file_list_interval', 3.0)}s / "
                f"文件间 {iv.get('sync_file_interval', 3.0)}s / "
                f"写操作 {iv.get('download_url_interval', 3.0)}s，"
                f"限流冷却 {iv.get('retry_cooldown', 30.0)}s"
            )
        except Exception as e:
            logger.warning(f"[sync] 读取 API 间隔配置失败: {e}")

    @classmethod
    def _generate_strm_content(
        cls, file_name: str, parent_path: str,
        pickcode: str, account_id: int, settings: Optional[dict] = None,
    ) -> str:
        """
        生成 302 跳转模式的 .strm 文件内容。
        STRM 文件指向本服务接口，播放时实时获取 115 直链并 302 重定向。
        account_id 固定使用 0（自动选择第一个有效账号），避免账号删除/重建后 STRM 失效。
        URL 包含 token 参数用于安全验证，防止未授权访问。
        """
        settings = settings or cls._load_strm_settings()

        server_url = (settings.get("server_url") or "").strip().rstrip("/")
        server_port = (settings.get("server_port") or "").strip()
        if not server_url:
            return ""
        base = f"{server_url}:{server_port}" if server_port else server_url
        # URL 中只放文件名（含扩展名），供 Emby 识别视频类型。
        # 文件实际标识是 pickcode，完整目录路径不需要出现在 URL 中。
        # 保留中文明文（只编码会破坏 URL 解析的特殊字符），让用户能直接辨认影视名。
        # 参考 Alist 等工具的做法：URL 路径中中文不编码，播放器和 HTTP 服务器都能正确处理。
        encoded_path = file_name.replace("?", "%3F").replace("#", "%23").replace("&", "%26").replace("=", "%3D")

        # 获取 STRM 播放 Token（自动生成，写入 URL 供播放时验证）
        # A5: 使用 HMAC-SHA256 路径签名替代明文 token，token 不暴露在 URL 中
        from app.services.strm_token import get_token, is_enabled, sign_path
        sig_part = ""
        if is_enabled():
            signature = sign_path(pickcode)
            sig_part = f"&s={signature}"

        return f"{base}/api/115/url/{encoded_path}?pickcode={pickcode}&account_id=0{sig_part}"

    @classmethod
    def _save_manifest(cls, local_root: Path, manifest: dict):
        """保存同步清单到 SQLite 数据库（支持路径索引，高效查询）"""
        root_hash = _manifest_hash(local_root)
        try:
            with _manifest_db_lock:
                conn = sqlite3.connect(str(MANIFEST_DB))
                try:
                    # 使用事务：先删除该 local_root 的所有旧条目，再批量插入新条目
                    conn.execute("DELETE FROM manifest_entries WHERE local_root_hash = ?", (root_hash,))
                    rows = []
                    for file_id, entry in manifest.items():
                        rows.append((
                            root_hash,
                            str(file_id),
                            entry.get("sha1", ""),
                            entry.get("pickcode", ""),
                            entry.get("parent_path", ""),
                            entry.get("name", ""),
                            entry.get("size", 0),
                        ))
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO manifest_entries "
                            "(local_root_hash, file_id, sha1, pickcode, parent_path, name, size) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            rows,
                        )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"[sync] 保存清单到 SQLite 失败: {e}")
            # 降级到 JSON 存储
            try:
                manifest_path = _manifest_path(local_root)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

    @classmethod
    def _load_manifest(cls, local_root: Path) -> dict:
        """加载上次同步清单（优先从 SQLite 加载，自动迁移旧 JSON 清单）"""
        root_hash = _manifest_hash(local_root)
        manifest = {}

        # 优先从 SQLite 加载
        try:
            with _manifest_db_lock:
                conn = sqlite3.connect(str(MANIFEST_DB))
                try:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.execute(
                        "SELECT file_id, sha1, pickcode, parent_path, name, size "
                        "FROM manifest_entries WHERE local_root_hash = ?",
                        (root_hash,),
                    )
                    for row in cursor:
                        manifest[row["file_id"]] = {
                            "sha1": row["sha1"] or "",
                            "pickcode": row["pickcode"] or "",
                            "parent_path": row["parent_path"] or "",
                            "name": row["name"] or "",
                            "size": row["size"] or 0,
                        }
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"[sync] 从 SQLite 加载清单失败: {e}")

        # 如果 SQLite 中有数据，直接返回
        if manifest:
            return manifest

        # 自动迁移：SQLite 中无数据但旧 JSON 清单存在时，导入到 SQLite
        try:
            manifest_path = _manifest_path(local_root)
            if manifest_path.exists():
                old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if old_manifest:
                    logger.info(f"[sync] 迁移 JSON 清单到 SQLite: {len(old_manifest)} 条记录")
                    cls._save_manifest(local_root, old_manifest)
                    # 迁移成功后删除旧 JSON 文件
                    try:
                        manifest_path.unlink()
                    except Exception:
                        pass
                    return old_manifest
        except Exception as e:
            logger.warning(f"[sync] 迁移旧 JSON 清单失败: {e}")

        return {}

    @classmethod
    def _relocate_local_file(
        cls,
        local_root: Path,
        old_parent_path: str,
        old_name: str,
        new_parent_path: str,
        new_name: str,
        ext: str,
        video_exts: set,
        image_exts: set,
        data_exts: set,
        cookies: str,
        pickcode: str,
        result: dict,
        account_id: int = 0,
        strm_settings: Optional[dict] = None,
    ):
        """
        将本地文件从旧位置迁移到新位置（整理后文件被移动或重命名时调用）。
        旧文件存在则移动，不存在则通过 _sync_single_file 重新生成。
        """
        # 读取整理文件方式（move/copy/hardlink/softlink），用于本地文件迁移
        from app.services.organize_service import _organize_file, _get_organize_method
        _organize_method = _get_organize_method()
        old_local_dir = local_root / old_parent_path if old_parent_path else local_root
        new_local_dir = local_root / new_parent_path if new_parent_path else local_root
        new_local_dir.mkdir(parents=True, exist_ok=True)

        # 构造文件信息 dict，供 _sync_single_file 使用
        file_info = {
            "name": new_name,
            "parent_path": new_parent_path,
            "pickcode": pickcode,
            "file_id": "",
            "size": 0,
            "sha1": "",
        }

        try:
            if ext in video_exts:
                old_strm_name = old_name + ".strm"
                new_strm_name = new_name + ".strm"
                old_strm_path = old_local_dir / old_strm_name
                new_strm_path = new_local_dir / new_strm_name

                if old_strm_path.exists():
                    if old_strm_path.resolve() != new_strm_path.resolve():
                        _organize_file(str(old_strm_path), str(new_strm_path), _organize_method)
                    # 迁移后重新生成 STRM 内容，确保 URL 中的文件名和路径与网盘一致
                    content = cls._generate_strm_content(new_name, new_parent_path, pickcode, account_id, strm_settings or cls._load_strm_settings())
                    if content:
                        new_strm_path.write_text(content, encoding="utf-8")
                    logger.info(f"[sync] STRM 迁移: {old_parent_path}/{old_strm_name} -> {new_parent_path}/{new_strm_name}")
                    result["synced"].append({
                        "name": new_name, "type": "strm_relocated",
                        "path": str(new_strm_path.relative_to(local_root)),
                    })
                    # 清理迁移后可能变空的旧目录
                    if old_parent_path and old_local_dir != new_local_dir:
                        cls._cleanup_empty_dirs(local_root, old_local_dir)
                else:
                    # 旧 STRM 不存在，重新生成（复用 _sync_single_file）
                    logger.info(f"[sync] 旧 STRM 不存在，重新生成: {new_parent_path}/{new_strm_name}")
                    synced, entry = cls._sync_single_file(
                        cookies, file_info, local_root, video_exts, image_exts, data_exts,
                        account_id, strm_settings or cls._load_strm_settings(),
                    )
                    if synced:
                        if synced.get("type") == "skipped":
                            result["skipped"] += 1
                        else:
                            result["synced"].append(synced)
                    elif entry:
                        result["errors"].append(entry)

            elif ext in image_exts or ext in data_exts:
                old_file_path = old_local_dir / old_name
                new_file_path = new_local_dir / new_name

                if old_file_path.exists():
                    if old_file_path.resolve() != new_file_path.resolve():
                        _organize_file(str(old_file_path), str(new_file_path), _organize_method)
                    logger.info(f"[sync] 文件迁移: {old_parent_path}/{old_name} -> {new_parent_path}/{new_name}")
                    result["synced"].append({
                        "name": new_name, "type": "relocated",
                        "path": str(new_file_path.relative_to(local_root)),
                    })
                    # 清理迁移后可能变空的旧目录
                    if old_parent_path and old_local_dir != new_local_dir:
                        cls._cleanup_empty_dirs(local_root, old_local_dir)
                else:
                    # 旧文件不存在，重新下载（复用 _sync_single_file）
                    logger.info(f"[sync] 旧文件不存在，重新下载: {new_parent_path}/{new_name}")
                    synced, entry = cls._sync_single_file(
                        cookies, file_info, local_root, video_exts, image_exts, data_exts,
                        account_id, strm_settings or cls._load_strm_settings(),
                    )
                    if synced:
                        result["synced"].append(synced)
                    elif entry:
                        result["errors"].append(entry)
        except Exception as e:
            logger.warning(f"[sync] 文件迁移失败: {old_parent_path}/{old_name} -> {new_parent_path}/{new_name}: {e}")
            result["errors"].append({"name": new_name, "error": f"迁移失败: {e}"})

    @classmethod
    def _cleanup_deleted_files(
        cls,
        local_root: Path,
        last_manifest: dict,
        new_manifest: dict,
        video_exts: set,
        image_exts: set,
        data_exts: set,
        result: Optional[dict] = None,
        strm_settings: Optional[dict] = None,
    ):
        """检测已从网盘删除的文件，清理本地对应的 STRM/图片/数据文件，并删除空目录。

        O1 统计式防误删保护：
          除"远端返回 0 结果"的极端保护（在调用方 incremental_sync 中）外，
          此处再加一道删除比例阈值。当本次拟删除的文件数占上次清单的比例超过
          阈值（默认 50%）时，极大概率是 115 API 返回了"部分结果"（列了一半就断），
          而非文件真的被大批量删除。此时中止清理，保留本地文件不被误删。

          阈值可通过 strm 设置的 max_delete_ratio 配置（0~100，单位%，
          0 或 >=100 表示关闭该保护）。
        """
        deleted_ids = set(last_manifest.keys()) - set(new_manifest.keys())
        if not deleted_ids:
            return

        # O1: 删除比例阈值保护
        last_count = len(last_manifest)
        del_count = len(deleted_ids)
        if last_count > 0:
            try:
                cfg = strm_settings if isinstance(strm_settings, dict) else cls._load_strm_settings()
                max_ratio = float((cfg or {}).get("max_delete_ratio", 50) or 0)
            except Exception:
                max_ratio = 50.0
            del_ratio = del_count / last_count * 100.0
            # max_ratio<=0 或 >=100 视为关闭保护
            if 0 < max_ratio < 100 and del_ratio > max_ratio:
                msg = (
                    f"拟删除 {del_count}/{last_count} 个文件（{del_ratio:.1f}%），"
                    f"超过防误删阈值 {max_ratio:.0f}%，疑似 115 API 返回部分结果，已中止清理保护本地文件"
                )
                logger.warning(f"[sync] {msg}")
                if isinstance(result, dict):
                    result.setdefault("errors", []).append({"name": "", "error": msg})
                return

        cleaned = 0
        affected_dirs: set = set()
        for fid in deleted_ids:
            old = last_manifest[fid]
            old_parent_path = old.get("parent_path", "")
            old_name = old.get("name", "")
            old_ext = cls._get_ext(old_name)
            old_local_dir = local_root / old_parent_path if old_parent_path else local_root

            try:
                if old_ext in video_exts:
                    old_strm_name = old_name + ".strm"
                    old_strm_path = old_local_dir / old_strm_name
                    if old_strm_path.exists():
                        old_strm_path.unlink()
                        cleaned += 1
                        logger.info(f"[sync] 删除已移除的 STRM: {old_parent_path}/{old_strm_name}")
                elif old_ext in image_exts or old_ext in data_exts:
                    old_file_path = old_local_dir / old_name
                    if old_file_path.exists():
                        old_file_path.unlink()
                        cleaned += 1
                        logger.info(f"[sync] 删除已移除的文件: {old_parent_path}/{old_name}")
                if old_parent_path:
                    affected_dirs.add(old_local_dir)
            except Exception as e:
                logger.warning(f"[sync] 清理已删除文件失败: {old_parent_path}/{old_name}: {e}")

        # 清理空目录（从最深层的受影响目录开始向上检查）
        for dir_path in sorted(affected_dirs, key=lambda p: len(p.parts), reverse=True):
            cls._cleanup_empty_dirs(local_root, dir_path)

        if cleaned:
            logger.info(f"[sync] 清理了 {cleaned} 个已从网盘移除的本地文件")

    # ============ 目录树导出 ============

    @classmethod
    def export_directory_tree(cls, local_root: Path) -> dict:
        """
        导出本地媒体目录的目录树结构，用于增量同步结果的展示。

        扫描 local_root 下的所有文件和目录，构建嵌套的树形结构：
        - 目录节点：type="dir"，包含 children 列表
        - .strm 文件节点：type="strm"，包含 size
        - 其他文件节点：type="file"，包含 size

        返回格式：
        {
            "name": "root_dir_name",
            "path": "",
            "type": "dir",
            "children": [...],
            "stats": {"total_dirs": N, "total_files": N, "total_strm_files": N, "total_other_files": N}
        }
        """
        local_root = Path(local_root)
        root_name = local_root.name or str(local_root)

        stats = {
            "total_dirs": 0,
            "total_files": 0,
            "total_strm_files": 0,
            "total_other_files": 0,
        }

        if not local_root.exists():
            logger.warning(f"[sync] 导出目录树失败：目录不存在 {local_root}")
            return {
                "name": root_name,
                "path": "",
                "type": "dir",
                "children": [],
                "stats": stats,
            }

        # 扁平化节点表：rel_path_str -> node dict
        # 先收集所有 rglob 条目，再按路径关系组装父子层级
        nodes: dict[str, dict] = {}

        for item in local_root.rglob("*"):
            try:
                rel = item.relative_to(local_root)
            except ValueError:
                continue
            rel_str = str(rel).replace("\\", "/")

            if item.is_dir():
                node = {
                    "name": rel.name,
                    "path": rel_str,
                    "type": "dir",
                    "children": [],
                }
                nodes[rel_str] = node
                stats["total_dirs"] += 1
            else:
                # 区分 .strm 文件和其他文件
                is_strm = item.suffix.lower() == ".strm"
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                node = {
                    "name": rel.name,
                    "path": rel_str,
                    "type": "strm" if is_strm else "file",
                    "size": size,
                }
                nodes[rel_str] = node
                stats["total_files"] += 1
                if is_strm:
                    stats["total_strm_files"] += 1
                else:
                    stats["total_other_files"] += 1

        # 构建父子关系：将每个节点挂到父节点的 children 列表
        root_children: list[dict] = []
        for rel_str, node in nodes.items():
            if "/" in rel_str:
                parent_path = rel_str.rsplit("/", 1)[0]
                parent = nodes.get(parent_path)
                if parent is not None:
                    parent["children"].append(node)
                else:
                    # 父目录不在节点表中（理论上不会发生），挂到根节点
                    root_children.append(node)
            else:
                # 直接位于根目录下的条目
                root_children.append(node)

        # 排序：目录在前、文件在后，各自按名称排序，保证输出稳定可读
        def _sort_key(n: dict):
            type_order = 0 if n["type"] == "dir" else 1
            return (type_order, n["name"])

        def _sort_tree(n: dict):
            if n.get("type") == "dir" and n.get("children"):
                n["children"].sort(key=_sort_key)
                for child in n["children"]:
                    _sort_tree(child)

        root_children.sort(key=_sort_key)
        for child in root_children:
            _sort_tree(child)

        logger.info(
            f"[sync] 目录树导出完成: 目录 {stats['total_dirs']}，"
            f"文件 {stats['total_files']}（STRM {stats['total_strm_files']}，其他 {stats['total_other_files']}）"
        )

        return {
            "name": root_name,
            "path": "",
            "type": "dir",
            "children": root_children,
            "stats": stats,
        }

    @staticmethod
    def _cleanup_empty_dirs(local_root: Path, start_dir: Path):
        """从 start_dir 开始向上删除空目录，直到 local_root 或遇到非空目录为止。"""
        try:
            current = start_dir
            while current != local_root and current.is_relative_to(local_root):
                if not current.exists():
                    break
                # 目录非空则停止
                try:
                    next(current.iterdir())
                    break
                except StopIteration:
                    # 空目录，删除
                    current.rmdir()
                    logger.info(f"[sync] 清理空目录: {current.relative_to(local_root)}")
                    current = current.parent
        except Exception as e:
            logger.warning(f"[sync] 清理空目录失败: {e}")

    @classmethod
    def remove_from_manifest_by_file_id(cls, local_media_dir: str, file_id: str) -> bool:
        """
        根据 file_id 从同步清单中移除条目，并删除对应的本地文件（STRM/图片/数据）。
        用于生活事件监控的删除事件（网盘文件被删除时精确清理本地）。
        返回是否清理了本地文件。
        """
        from pathlib import Path as _Path
        local_root = _Path(local_media_dir)
        manifest = cls._load_manifest(local_root)
        entry = manifest.get(file_id)
        if not entry:
            return False

        name = entry.get("name", "")
        parent_path = entry.get("parent_path", "")
        ext = cls._get_ext(name)
        local_dir = local_root / parent_path if parent_path else local_root

        cleaned = False
        try:
            if ext in cls.DEFAULT_VIDEO_EXTS:
                strm_path = local_dir / (name + ".strm")
                if strm_path.exists():
                    strm_path.unlink()
                    cleaned = True
                    logger.info(f"[life-event] 删除事件清理 STRM: {parent_path}/{name}.strm")
            else:
                file_path = local_dir / name
                if file_path.exists():
                    file_path.unlink()
                    cleaned = True
                    logger.info(f"[life-event] 删除事件清理文件: {parent_path}/{name}")
            if cleaned:
                manifest.pop(file_id, None)
                cls._save_manifest(local_root, manifest)
                # 清理可能变空的目录
                if parent_path:
                    cls._cleanup_empty_dirs(local_root, local_dir)
        except Exception as e:
            logger.warning(f"[sync] 删除事件清理失败 {parent_path}/{name}: {e}")

        return cleaned

    # ============ 上传同步（队列化） ============

    # 默认上传后缀（Emby 刮削产生的元数据文件）
    DEFAULT_UPLOAD_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".srt", ".ass", ".ssa", ".sub", ".idx"}

    @classmethod
    def upload_sync(
        cls,
        cookies: str,
        source_cid: str,
        local_media_dir: str,
        upload_exts: Optional[set] = None,
        overwrite: bool = False,
        account_id: int = 0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> dict:
        """
        上传同步：将本地目录中的元数据文件（nfo、图片、字幕等）加入持久化上传队列

        - 扫描本地目录下匹配 upload_exts 的文件
        - 跳过 .strm 文件
        - 对比 115 网盘已有文件，跳过已存在的（或按 overwrite 配置覆盖）
        - 自动创建不存在的网盘目录
        - 将待上传文件加入持久化队列，由后台 worker 异步执行上传
        - loop: 主事件循环，用于进度上报
        """
        from app.core.progress import progress_manager
        from app.services.upload_queue import UploadQueue, UploadTask

        upload_exts = upload_exts or cls.DEFAULT_UPLOAD_EXTS
        logger.info(f"[sync-upload] 上传同步开始（队列模式）: source_cid={source_cid}, local_dir={local_media_dir}, exts={upload_exts}, overwrite={overwrite}")

        result = {
            "total": 0,
            "queued": [],
            "skipped": 0,
            "errors": [],
        }

        local_root = Path(local_media_dir)
        if not local_root.exists():
            try:
                local_root.mkdir(parents=True, exist_ok=True)
                logger.info(f"[sync-upload] 本地媒体目录不存在，已自动创建: {local_media_dir}")
            except Exception as e:
                logger.warning(f"[sync-upload] 无法创建本地媒体目录: {local_media_dir} - {e}")
                return {
                    "total": 0, "queued": [], "skipped": 0,
                    "errors": [{"name": "", "error": f"无法创建本地媒体目录: {e}"}],
                }

        # 1. 扫描本地匹配文件
        local_files: list[Path] = []
        for item in local_root.rglob("*"):
            if not item.is_file():
                continue
            # 跳过 .strm 文件（本地专用，不上传）
            if item.suffix.lower() == ".strm":
                continue
            ext = ("." + item.suffix.lstrip(".").lower()) if item.suffix else ""
            if ext in upload_exts:
                local_files.append(item)

        result["total"] = len(local_files)
        logger.info(f"[sync-upload] 本地扫描到 {len(local_files)} 个待上传文件")

        if not local_files:
            cls._safe_schedule(loop, progress_manager.complete_task("无需上传的文件"))
            return result

        # 2. 列出 115 网盘已有文件（用于去重判断）
        logger.info(f"[sync-upload] 正在扫描 115 网盘已有文件...")
        existing_files: set[str] = set()  # "parent_path/filename"
        try:
            remote_files = Client115Service.list_all_files_with_meta(
                cookies, source_cid, upload_exts, min_size=0, recursive=True
            )
            for f in remote_files:
                key = f"{f.get('parent_path', '')}/{f['name']}"
                existing_files.add(key)
            logger.info(f"[sync-upload] 115 网盘已有 {len(existing_files)} 个匹配文件")
        except Exception as e:
            logger.warning(f"[sync-upload] 扫描 115 文件列表失败: {e}")

        # 3. 目录 CID 缓存（避免重复创建/查找目录）
        dir_cid_cache: dict[str, str] = {"": source_cid}

        # 更新进度总数
        cls._safe_schedule(loop, progress_manager.update_total(len(local_files)))

        # 4. 生成上传任务并加入队列
        queue = UploadQueue.get_instance()
        tasks_to_add: list[UploadTask] = []

        for idx, local_file in enumerate(local_files):
            rel_path = local_file.relative_to(local_root)
            rel_path_str = str(rel_path).replace("\\", "/")
            parent_path = "/".join(rel_path.parts[:-1])
            filename = rel_path.name

            cls._safe_schedule(loop, progress_manager.update_progress(idx, filename))

            # 检查是否已存在
            exist_key = f"{parent_path}/{filename}"
            if exist_key in existing_files and not overwrite:
                result["skipped"] += 1
                continue

            try:
                # 查找或创建目标目录
                if parent_path in dir_cid_cache:
                    dest_cid = dir_cid_cache[parent_path]
                else:
                    parts = [p for p in parent_path.split("/") if p]
                    dest_cid = Client115Service.ensure_path(cookies, parts, source_cid)
                    if dest_cid:
                        dir_cid_cache[parent_path] = dest_cid
                    else:
                        result["errors"].append({"name": filename, "error": "创建网盘目录失败"})
                        continue

                # 获取文件大小
                try:
                    file_size = local_file.stat().st_size
                except Exception:
                    file_size = 0

                # 创建上传任务
                task = UploadTask(
                    local_path=str(local_file),
                    filename=filename,
                    parent_path=parent_path,
                    dest_cid=dest_cid,
                    cookies=cookies,
                    account_id=account_id,
                    file_size=file_size,
                    overwrite=overwrite,
                )
                tasks_to_add.append(task)
                result["queued"].append({"name": filename, "path": rel_path_str})

            except Exception as e:
                result["errors"].append({"name": filename, "error": str(e)})

        # 批量加入队列
        if tasks_to_add:
            added = queue.add_tasks(tasks_to_add)
            logger.info(f"[sync-upload] {added}/{len(tasks_to_add)} 个任务已加入上传队列（去重后）")

        summary = f"共 {result['total']} 个文件，入队 {len(result['queued'])}，跳过 {result['skipped']}，失败 {len(result['errors'])}"
        logger.info(f"[sync-upload] 上传同步完成: {summary}")
        cls._safe_schedule(loop, progress_manager.complete_task(summary))

        return result

    # ============ 刮削后自动上传 ============

    @classmethod
    async def trigger_auto_upload(
        cls,
        cookies: str,
        source_cid: str,
        local_media_dir: str,
        account_id: int = 0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> Optional[dict]:
        """
        Emby 刷新后自动上传本地元数据文件（nfo/图片/字幕）到网盘。

        读取 emby_sync 配置中的 auto_upload 和 upload_delay：
        - auto_upload=False 时直接返回
        - 等待 upload_delay 秒让 Emby 完成刮削
        - 调用 upload_sync 将文件加入持久化上传队列，由后台 worker 异步执行
        """
        from app.core.db_helper import read_setting

        sync_cfg = read_setting("emby_sync") or {}
        if not sync_cfg.get("auto_upload", False):
            return None

        if not source_cid or not local_media_dir:
            logger.warning("[sync-upload] 自动上传跳过：source_cid 或 local_media_dir 为空")
            return None

        delay = sync_cfg.get("upload_delay", 60)
        if delay > 0:
            logger.info(f"[sync-upload] 等待 {delay} 秒后开始上传（等待 Emby 刮削完成）")
            await asyncio.sleep(delay)

        logger.info("[sync-upload] 开始执行刮削后自动上传（队列模式）")
        try:
            result = await asyncio.to_thread(
                cls.upload_sync,
                cookies=cookies,
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                account_id=account_id,
                loop=loop,
            )
            logger.info(
                f"[sync-upload] 自动上传任务已入队: 入队 {len(result.get('queued', []))}, "
                f"跳过 {result.get('skipped', 0)}, 失败 {len(result.get('errors', []))}"
            )
            return result
        except Exception as e:
            logger.warning(f"[sync-upload] 自动上传失败: {e}")
            return None

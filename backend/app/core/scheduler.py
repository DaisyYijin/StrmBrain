"""
定时任务调度器 - 基于 APScheduler

支持同步归档的定时增量同步（Cron 表达式）。
"""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


_scheduler: AsyncIOScheduler = None

# 同步任务的 Job ID
SYNC_JOB_ID = "sync_incremental_schedule"


async def init_scheduler():
    """初始化调度器"""
    global _scheduler
    if _scheduler is not None:
        return

    from app.core.logbuffer import get_logger
    logger = get_logger()

    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    logger.info("定时任务调度器已启动")

    # 加载已保存的同步计划
    await register_sync_schedule()


async def shutdown_scheduler():
    """关闭调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> AsyncIOScheduler:
    """获取调度器实例"""
    return _scheduler


async def register_sync_schedule():
    """
    根据保存的同步计划配置注册/更新定时任务
    使用标准 5 段式 Cron 表达式
    """
    global _scheduler
    if _scheduler is None:
        return

    from app.core.logbuffer import get_logger
    from app.services.sync_service import SyncService
    logger = get_logger()

    # 移除旧的同步任务
    try:
        _scheduler.remove_job(SYNC_JOB_ID)
    except Exception:
        pass  # 任务不存在，忽略

    config = SyncService.load_schedule()
    if not config:
        return

    cron_str = config.get("cron", "").strip()
    if not cron_str:
        logger.info("同步定时计划未启用（Cron 为空）")
        return

    # 使用 CronTrigger.from_crontab 解析标准 5 段式 cron
    try:
        trigger = CronTrigger.from_crontab(cron_str)
    except Exception as e:
        logger.warning(f"Cron 表达式无效 '{cron_str}': {e}")
        return

    # 注册任务
    _scheduler.add_job(
        _run_scheduled_sync,
        trigger=trigger,
        id=SYNC_JOB_ID,
        replace_existing=True,
        kwargs={"config": config},
    )
    logger.info(f"同步定时计划已注册: cron='{cron_str}'")


async def _run_scheduled_sync(config: dict):
    """
    定时任务执行函数：执行增量同步
    """
    from app.core.logbuffer import get_logger
    from app.services.sync_service import SyncService
    from app.core.json_storage import find_account, get_first_valid_account

    logger = get_logger()
    account_id = config.get("account_id")
    source_cid = config.get("source_cid", "")
    local_media_dir = config.get("local_media_dir", "")

    if not source_cid or not local_media_dir:
        logger.warning("同步计划配置不完整，跳过执行")
        return

    # 获取账号：优先使用配置中的 account_id，否则取第一个有效账号
    account = None
    if account_id:
        account = find_account(account_id)
    else:
        account = get_first_valid_account()

    if not account:
        logger.warning("未找到有效账号，跳过同步")
        return

    if account.get("status") == 0:
        logger.warning(f"账号 {account.get('id')} cookies 已失效，跳过同步")
        return

    logger.info(f"开始执行定时增量同步: account={account.get('id')}, source={source_cid}")

    try:
        # 解析扩展名
        video_exts = _parse_exts(config.get("video_exts_str", ""))
        image_exts = _parse_exts(config.get("image_exts_str", ""))
        data_exts = _parse_exts(config.get("data_exts_str", ""))
        min_video_size_mb = config.get("min_video_size_mb", 0)

        cookies = account.get("cookies", "")
        _loop = asyncio.get_running_loop()

        # 在工作线程中执行同步，避免阻塞事件循环
        sync_result = await asyncio.to_thread(
            SyncService.incremental_sync,
            cookies=cookies,
            source_cid=source_cid,
            local_media_dir=local_media_dir,
            video_exts=video_exts,
            image_exts=image_exts,
            data_exts=data_exts,
            min_video_size_mb=min_video_size_mb,
            account_id=account.get("id", 0),
            loop=_loop,
        )

        logger.info(
            f"定时增量同步完成: 共 {sync_result['total']} 个文件, "
            f"新增同步 {len(sync_result['synced'])} 个, "
            f"跳过 {sync_result['skipped']} 个"
        )

        # 同步完成后根据配置触发 Emby 媒体库刷新
        from app.core.db_helper import read_setting
        sync_cfg = read_setting("emby_sync") or {}
        if sync_cfg.get("auto_refresh", True) and sync_result.get("synced"):
            from app.services.emby import trigger_emby_refresh
            await trigger_emby_refresh(local_media_dir)

            # Emby 刮削后自动上传 nfo/图片到网盘
            await SyncService.trigger_auto_upload(
                cookies=cookies,
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                account_id=account.get("id", 0),
                loop=_loop,
            )

        # 根据配置发送通知
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_sync", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_sync_complete("incremental", sync_result, local_media_dir)

    except Exception as e:
        logger.error(f"定时增量同步失败: {e}")
        # 根据配置发送异常通知
        from app.core.db_helper import read_setting
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_error", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_error("定时增量同步", str(e))


def _parse_exts(exts_str: str) -> set:
    """解析后缀字符串为集合"""
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

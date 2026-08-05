"""
定时任务调度器 - 基于 APScheduler

支持同步归档的定时整理+增量同步（Cron 表达式）。
"""
import asyncio
import json
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
    logger.info(f"定时任务已注册（整理+增量同步）: cron='{cron_str}'")


async def _run_scheduled_sync(config: dict):
    """
    定时任务执行函数：先执行整理，再执行增量同步
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
        logger.warning("未找到有效账号，跳过执行")
        return

    if account.get("status") == 0:
        logger.warning(f"账号 {account.get('id')} cookies 已失效，跳过执行")
        return

    cookies = account.get("cookies", "")
    _loop = asyncio.get_running_loop()

    # ========== 第一步：执行整理 ==========
    organize_result = None
    try:
        organize_result = await _run_scheduled_organize(
            cookies=cookies,
            target_cid=source_cid,  # 全量同步目录作为整理目标
            logger=logger,
        )
    except Exception as e:
        logger.warning(f"定时整理失败（继续执行增量同步）: {e}")

    # ========== 第二步：执行增量同步 ==========
    logger.info(f"开始执行定时增量同步: account={account.get('id')}, source={source_cid}")

    try:
        # 解析扩展名
        video_exts = _parse_exts(config.get("video_exts_str", ""))
        image_exts = _parse_exts(config.get("image_exts_str", ""))
        data_exts = _parse_exts(config.get("data_exts_str", ""))
        min_video_size_mb = config.get("min_video_size_mb", 0)

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
        logger.warning(f"定时增量同步失败: {e}")
        # 根据配置发送异常通知
        from app.core.db_helper import read_setting
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_error", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_error("定时增量同步", str(e))


async def _run_scheduled_organize(cookies: str, target_cid: str, logger) -> dict | None:
    """
    定时整理：从已保存的整理配置中读取参数，执行整理。
    target_cid: 全量同步目录 cid（整理目标目录）
    返回整理结果 dict，无配置或无文件时返回 None。
    """
    from app.config import CONFIG_DIR
    from app.services.organize_service import OrganizeService

    # 读取整理目录配置
    dirs_file = CONFIG_DIR / "organize_dirs.json"
    if not dirs_file.exists():
        logger.info("定时整理：未找到整理目录配置，跳过整理")
        return None

    try:
        dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"定时整理：读取整理目录配置失败: {e}")
        return None

    organize_source_cid = dirs_cfg.get("source_cid", "").strip()
    if not organize_source_cid:
        logger.info("定时整理：未配置等待整理目录，跳过整理")
        return None

    existing_cid = dirs_cfg.get("existing_cid", "")
    redundant_cid = dirs_cfg.get("redundant_cid", "")
    unrecognized_cid = dirs_cfg.get("unrecognized_cid", "")
    use_ffprobe = dirs_cfg.get("use_ffprobe", False)
    skip_no_info = dirs_cfg.get("skip_no_info", False)
    prefer_filename = dirs_cfg.get("prefer_filename", False)
    min_organize_size_mb = dirs_cfg.get("min_organize_size_mb", 0)
    organize_blacklist = dirs_cfg.get("organize_blacklist", "")

    # 读取二级分类配置
    classify_config = ""
    category_roots = None
    classify_file = CONFIG_DIR / "classify_config.json"
    if classify_file.exists():
        try:
            classify_data = json.loads(classify_file.read_text(encoding="utf-8"))
            classify_config = classify_data.get("classify_config", "")
            category_roots = classify_data.get("category_roots")
        except (json.JSONDecodeError, OSError):
            pass

    # 读取洗版策略
    wash_config = None
    wash_file = CONFIG_DIR / "wash_config.json"
    if wash_file.exists():
        try:
            wash_config = json.loads(wash_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # 读取重命名规则
    rename_rules = None
    rename_file = CONFIG_DIR / "rename_rules.json"
    if rename_file.exists():
        try:
            rename_rules = json.loads(rename_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    logger.info(
        f"开始执行定时整理: source={organize_source_cid}, target={target_cid}, "
        f"existing={existing_cid}, redundant={redundant_cid}, unrecognized={unrecognized_cid}"
    )

    result = await OrganizeService.scan_and_organize(
        cookies=cookies,
        source_cid=organize_source_cid,
        target_cid=target_cid,
        existing_cid=existing_cid,
        redundant_cid=redundant_cid,
        unrecognized_cid=unrecognized_cid,
        classify_config=classify_config,
        category_roots=category_roots,
        rename_rules=rename_rules,
        wash_config=wash_config,
        use_ffprobe=use_ffprobe,
        skip_no_info=skip_no_info,
        prefer_filename=prefer_filename,
        min_organize_size_mb=min_organize_size_mb,
        organize_blacklist=organize_blacklist,
        dry_run=False,
    )

    organized = len(result.get("organized", []))
    redundant = len(result.get("redundant", []))
    unrecognized = len(result.get("unrecognized", []))
    errors = len(result.get("errors", []))

    logger.info(
        f"定时整理完成: 成功 {organized}, 冗余 {redundant}, "
        f"无法识别 {unrecognized}, 失败 {errors}"
    )

    # 整理完成后根据配置触发 Emby 刷新和通知
    from app.core.db_helper import read_setting
    sync_cfg = read_setting("emby_sync") or {}
    if sync_cfg.get("auto_refresh", True) and organized > 0:
        from app.services.emby import trigger_emby_refresh
        await trigger_emby_refresh()

    notify_cfg = read_setting("emby_notify") or {}
    if notify_cfg.get("notify_on_organize", True) and organized > 0:
        from app.services.notification_service import NotificationService
        await NotificationService.notify_organize_complete(result)

    return result


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


# ===== 下载完成自动整理（参考 qmediasync：离线下载 → 自动整理） =====

# 自动整理相关配置 key（存于 clouddownload_config）
AUTO_ORGANIZE_KEY = "auto_organize"         # bool：是否启用下载完成自动整理
AUTO_ORGANIZE_DELAY_KEY = "auto_organize_delay"  # int：延迟秒数（等待下载开始/转存完成）

# 视频扩展名（与 organize_service.VIDEO_EXTS 保持一致）
_ORGANIZE_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}


def schedule_auto_organize_after_download(cookies: str, save_cid: str, retry: int = 0) -> bool:
    """
    下载任务添加成功后，注册一次性自动整理任务。
    参考 qmediasync：添加离线下载 → 增加一次性任务-自动整理 → 移动转存数据 → 整理待刮削库。

    Args:
        cookies: 115 账号 cookies
        save_cid: 转存/保存目录 cid（下载完成后文件所在目录）
        retry: 重试次数（转存目录暂无文件时递增重试）

    Returns:
        是否成功注册
    """
    global _scheduler
    if _scheduler is None:
        return False

    from app.core.logbuffer import get_logger
    from app.core.db_helper import read_setting

    logger = get_logger()

    # 读取配置：默认启用，延迟 60 秒（给 115 离线下载/转存留出时间）
    cfg = read_setting("clouddownload_config") or {}
    if not cfg.get(AUTO_ORGANIZE_KEY, True):
        logger.info("下载完成自动整理未启用（auto_organize=false），跳过注册")
        return False

    # 基础延迟取配置值；重试时按 retry 递增（60s / 120s / 180s）
    try:
        base_delay = int(cfg.get(AUTO_ORGANIZE_DELAY_KEY, 60) or 60)
    except (TypeError, ValueError):
        base_delay = 60
    if base_delay < 10:
        base_delay = 10  # 最小 10 秒，避免配置错误导致立即执行
    delay = base_delay * (retry + 1)

    try:
        from datetime import datetime, timedelta, timezone
        from apscheduler.triggers.date import DateTrigger
        run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        job_id = f"auto_organize_dl_{int(time.time())}"
        _scheduler.add_job(
            _run_auto_organize_after_download,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            kwargs={"cookies": cookies, "save_cid": save_cid, "retry": retry + 1},
        )
        logger.info(f"已注册下载完成自动整理任务: {job_id}, {delay}s 后执行 (save_cid={save_cid}, retry={retry})")
        return True
    except Exception as e:
        logger.warning(f"注册下载完成自动整理任务失败: {e}")
        return False


async def _run_auto_organize_after_download(cookies: str, save_cid: str, retry: int = 1):
    """
    下载完成自动整理执行体：
    1. 检查转存目录是否有文件（无文件且重试次数内则延迟再试）
    2. 移动转存数据到等待整理目录（organize_dirs.json 的 source_cid）
    3. 执行整理（复用 _run_scheduled_organize）
    参考 qmediasync：移动转存数据 → 整理待刮削库。
    retry: 当前重试次数（schedule 时已 +1，默认 1 表示首次执行）。
    """
    from app.core.logbuffer import get_logger
    from app.core.json_storage import get_first_valid_account
    from app.config import CONFIG_DIR

    logger = get_logger()

    # 读取整理目录配置（等待整理目录 = organize source_cid）
    dirs_file = CONFIG_DIR / "organize_dirs.json"
    if not dirs_file.exists():
        logger.info("自动整理：未找到整理目录配置，跳过")
        return
    try:
        dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"自动整理：读取整理目录配置失败: {e}")
        return

    organize_source_cid = dirs_cfg.get("source_cid", "").strip()
    if not organize_source_cid:
        logger.info("自动整理：未配置等待整理目录，跳过")
        return

    # 使用当前有效账号（cookies 可能已切换）
    account = get_first_valid_account()
    if not account or account.get("status") == 0:
        logger.warning("自动整理：未找到有效账号，跳过")
        return
    cookies = account.get("cookies", "")

    from app.services.client_115 import Client115Service

    # 1. 检查转存目录是否有文件（下载可能尚未完成）
    if save_cid:
        try:
            listing = Client115Service.list_files(cookies, save_cid, 0, 10)
            items = listing.get("data", []) if isinstance(listing, dict) else []
            if not items:
                if retry <= 3:
                    # 暂无文件，延迟再试（下载/转存需要时间）
                    logger.info(f"自动整理：转存目录暂无文件，重试 ({retry}/3)...")
                    schedule_auto_organize_after_download(cookies, save_cid, retry)
                else:
                    logger.warning("自动整理：转存目录持续无文件，放弃自动整理")
                return
        except Exception as e:
            logger.warning(f"自动整理：检查转存目录失败: {e}")
            return

        # 2. 移动转存数据到等待整理目录（保持目录结构：目录整体移动，文件单独移动）
        if save_cid != organize_source_cid:
            try:
                listing = Client115Service.list_files(cookies, save_cid, 0, 1000)
                items = listing.get("data", []) if isinstance(listing, dict) else []
                moved = 0
                for it in items:
                    item_name = it.get("n", "")
                    if not item_name:
                        continue
                    if not it.get("fid"):
                        # 子目录：整体移动（保留结构）
                        ok = Client115Service.move(cookies, [it.get("cid")], organize_source_cid, context=item_name)
                    else:
                        # 视频文件：移动到等待整理目录
                        ext = ("." + item_name.rsplit(".", 1)[-1]).lower() if "." in item_name else ""
                        if ext not in _ORGANIZE_VIDEO_EXTS:
                            continue
                        ok = Client115Service.move(cookies, [it.get("fid")], organize_source_cid, context=item_name)
                    if ok:
                        moved += 1
                if moved > 0:
                    logger.info(f"自动整理-移动转存数据: 移动 {moved} 项到等待整理目录")
                else:
                    logger.info("自动整理：转存目录无可移动的视频文件")
            except Exception as e:
                logger.warning(f"自动整理：移动转存数据失败: {e}")
                return
    else:
        logger.info("自动整理：未指定转存目录（save_cid 为空），直接整理等待整理目录")

    # 3. 执行整理
    logger.info("自动整理：开始整理待刮削库...")
    await _run_scheduled_organize(cookies=cookies, target_cid=organize_source_cid, logger=logger)

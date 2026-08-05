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

    # E2: 注册每日 115 签到任务
    try:
        await register_daily_checkin()
    except Exception as e:
        logger.warning(f"注册每日 115 签到任务失败: {e}")

    # G7: 注册自动化规则 cron 触发器
    try:
        await register_automation_jobs()
    except Exception as e:
        logger.warning(f"注册自动化规则定时任务失败: {e}")

    # P1-10: 注册缓存预热定时任务
    try:
        await register_cache_warmer()
    except Exception as e:
        logger.warning(f"注册缓存预热定时任务失败: {e}")


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


# ===== E2: 115 每日签到（cron 可配置，由特色工具页管理） =====

# 每日签到的 Job ID
DAILY_CHECKIN_JOB_ID = "daily_115_checkin"

# 签到配置的 settings.json key
CHECKIN_SETTING_KEY = "checkin"

# 默认签到 cron（每天 00:05）
DEFAULT_CHECKIN_CRON = "5 0 * * *"


async def register_daily_checkin():
    """注册 115 签到定时任务（cron 从配置读取，默认 '5 0 * * *'）

    配置存储于 settings.json 的 checkin 字段：{"enabled": bool, "cron": str}。
    - enabled=false 或 cron 为空时不注册
    - 特色工具页修改配置后调用本函数重注册
    """
    global _scheduler
    if _scheduler is None:
        return

    from app.core.logbuffer import get_logger
    from app.core.json_storage import read_setting
    logger = get_logger()

    # 先移除旧任务（保证重注册生效）
    try:
        _scheduler.remove_job(DAILY_CHECKIN_JOB_ID)
    except Exception:
        pass

    try:
        cfg = read_setting(CHECKIN_SETTING_KEY) or {}
        enabled = cfg.get("enabled", True)
        cron_str = str(cfg.get("cron") or DEFAULT_CHECKIN_CRON).strip()
        if not enabled or not cron_str:
            logger.info("115 签到定时任务未启用（enabled=false 或 cron 为空），跳过注册")
            return
        trigger = CronTrigger.from_crontab(cron_str)
        _scheduler.add_job(
            _run_daily_checkin,
            trigger=trigger,
            id=DAILY_CHECKIN_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"已注册 115 签到定时任务 (id={DAILY_CHECKIN_JOB_ID}, cron='{cron_str}')")
    except Exception as e:
        logger.warning(f"注册 115 签到定时任务失败: {e}")


def _is_checkin_success(result: dict) -> bool:
    """判断签到结果是否成功（115 签到接口返回 {"state": True, ...} 或 {"data": {...}}）"""
    if not result:
        return False
    if result.get("error"):
        return False
    if result.get("state") is False:
        return False
    return True


async def _run_daily_checkin():
    """每日 115 签到执行体：遍历所有有效账号（status==1）逐个签到，日志记录成功/失败。

    - 账号 cookies 为空或签到失败（cookies 可能已失效）时跳过并警告
    - 逐个账号独立签到，单个账号失败不影响其它账号
    """
    from app.core.logbuffer import get_logger
    from app.core.json_storage import read_accounts
    from app.services.client_115 import Client115Service

    logger = get_logger()
    accounts = read_accounts()
    valid = [acc for acc in accounts if acc.get("status") == 1]
    if not valid:
        logger.info("每日 115 签到：无有效账号，跳过")
        return

    ok = 0
    fail = 0
    for acc in valid:
        account_id = acc.get("id", 0)
        cookies = acc.get("cookies", "")
        if not cookies:
            logger.warning(f"每日 115 签到：账号 {account_id} cookies 为空，跳过")
            fail += 1
            continue
        try:
            result = await asyncio.to_thread(Client115Service.daily_checkin, cookies)
        except Exception as e:
            logger.warning(f"每日 115 签到：账号 {account_id} 异常: {e}")
            fail += 1
            continue

        if not isinstance(result, dict) or not _is_checkin_success(result):
            err = result.get("error") if isinstance(result, dict) else str(result)
            # cookies 失效等场景：跳过并警告
            logger.warning(f"每日 115 签到：账号 {account_id} 失败（cookies 可能已失效）: {err}")
            fail += 1
            continue

        ok += 1
        # 记录签到结果（含"已签到"等状态信息）
        data = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(data, dict):
            msg = data.get("error_msg") or data.get("msg") or data.get("message") or "OK"
            logger.info(f"每日 115 签到：账号 {account_id} 成功 ({msg})")
        else:
            logger.info(f"每日 115 签到：账号 {account_id} 成功")

    logger.info(f"每日 115 签到完成: 成功 {ok} 个账号, 失败 {fail} 个账号")


# ===== G7: 自动化规则 cron 触发器 =====

# 自动化规则 Job ID 前缀（实际 id=automation_{rule_id}）
AUTOMATION_JOB_PREFIX = "automation_"


async def register_automation_jobs():
    """注册/更新自动化规则的 cron 触发器任务。

    遍历 automation_rules.json 中 trigger_type=cron 且启用的规则，
    为每条规则注册 APScheduler job（id=automation_{rule_id}）。
    规则变更（增/删/改）后需重新调用本函数。
    """
    global _scheduler
    if _scheduler is None:
        return

    from app.core.logbuffer import get_logger
    from app.services.automation_service import get_automation_service

    logger = get_logger()

    # 先移除旧的自动化规则任务（保证规则变更后重注册生效）
    try:
        for job in _scheduler.get_jobs():
            if job.id.startswith(AUTOMATION_JOB_PREFIX):
                _scheduler.remove_job(job.id)
    except Exception:
        pass

    try:
        rules = get_automation_service().list_rules()
    except Exception as e:
        logger.warning(f"读取自动化规则失败: {e}")
        return

    for rule in rules:
        if not rule.get("is_enabled", True):
            continue
        if rule.get("trigger_type") != "cron":
            continue
        cron_str = (rule.get("cron") or "").strip()
        if not cron_str:
            continue
        rule_id = rule.get("id")
        if rule_id is None:
            continue
        try:
            trigger = CronTrigger.from_crontab(cron_str)
            _scheduler.add_job(
                _run_automation_rule,
                trigger=trigger,
                id=f"{AUTOMATION_JOB_PREFIX}{rule_id}",
                replace_existing=True,
                kwargs={"rule_id": rule_id},
            )
            logger.info(f"已注册自动化规则定时任务: 规则 {rule_id} ({rule.get('name', '')}), cron='{cron_str}'")
        except Exception as e:
            logger.warning(f"注册自动化规则 {rule_id} 定时任务失败: {e}")


async def _run_automation_rule(rule_id):
    """自动化规则 cron 触发执行体：调 get_automation_service().run_rule 执行动作序列"""
    from app.core.logbuffer import get_logger
    from app.services.automation_service import get_automation_service

    logger = get_logger()
    logger.info(f"自动化规则 {rule_id} 定时触发执行")
    try:
        result = await get_automation_service().run_rule(rule_id, context="cron")
        logger.info(f"自动化规则 {rule_id} 执行完成: success={result.get('success')}")
    except Exception as e:
        logger.warning(f"自动化规则 {rule_id} 执行异常: {e}")


# ===== P1-10: 缓存预热（cron 可配置，由设置页管理） =====

# 缓存预热的 Job ID
CACHE_WARMER_JOB_ID = "cache_warmer_schedule"

# 缓存预热配置的 settings.json key
CACHE_WARMER_SETTING_KEY = "cache_warmer"

# 默认缓存预热 cron（每天凌晨 3 点）
DEFAULT_CACHE_WARMER_CRON = "0 3 * * *"


async def register_cache_warmer():
    """注册缓存预热定时任务（cron 从配置读取，默认 '0 3 * * *'）

    配置存储于 settings.json 的 cache_warmer 字段：{"enabled": bool, "cron": str, "max_files": int}。
    - enabled=false 或 cron 为空时不注册
    - 设置页修改配置后调用本函数重注册
    """
    global _scheduler
    if _scheduler is None:
        return

    from app.core.logbuffer import get_logger
    from app.core.json_storage import read_setting
    logger = get_logger()

    # 先移除旧任务（保证重注册生效）
    try:
        _scheduler.remove_job(CACHE_WARMER_JOB_ID)
    except Exception:
        pass

    try:
        cfg = read_setting(CACHE_WARMER_SETTING_KEY) or {}
        enabled = cfg.get("enabled", False)
        cron_str = str(cfg.get("cron") or DEFAULT_CACHE_WARMER_CRON).strip()
        if not enabled or not cron_str:
            logger.info("缓存预热定时任务未启用（enabled=false 或 cron 为空），跳过注册")
            return
        trigger = CronTrigger.from_crontab(cron_str)
        _scheduler.add_job(
            _run_cache_warmer,
            trigger=trigger,
            id=CACHE_WARMER_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"已注册缓存预热定时任务 (id={CACHE_WARMER_JOB_ID}, cron='{cron_str}')")
    except Exception as e:
        logger.warning(f"注册缓存预热定时任务失败: {e}")


async def _run_cache_warmer():
    """缓存预热 cron 触发执行体：调用 CacheWarmer.warm_once 预加载缓存"""
    from app.core.logbuffer import get_logger
    from app.services.cache_warmer import get_cache_warmer

    logger = get_logger()
    logger.info("缓存预热定时任务触发")
    try:
        result = await get_cache_warmer().warm_once()
        logger.info(
            f"缓存预热完成: 直链 {result.get('warmed', 0)} 个, "
            f"扫描 {result.get('scanned', 0)} 个, 耗时 {result.get('duration', 0)}s"
        )
    except Exception as e:
        logger.warning(f"缓存预热执行异常: {e}")

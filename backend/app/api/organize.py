"""
API 路由 - 整理功能 + 同步归档
"""
import asyncio
import json
import time
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

from app.core.json_storage import find_account, get_first_valid_account
from app.schemas import ApiResponse
from app.config import DATA_DIR, CONFIG_DIR
from app.services.organize_service import OrganizeService
from app.services.sync_service import SyncService
from app.services.media_probe import is_ffprobe_available

router = APIRouter(prefix="/api/organize", tags=["organize"])

# 整理配置持久化 —— 每类配置独立文件（存放在 config/ 目录）
ORGANIZE_DIRS_FILE = CONFIG_DIR / "organize_dirs.json"
CLASSIFY_CONFIG_FILE = CONFIG_DIR / "classify_config.json"
WASH_CONFIG_FILE = CONFIG_DIR / "wash_config.json"
RENAME_RULES_FILE = CONFIG_DIR / "rename_rules.json"

# type → 文件路径 映射
_CONFIG_FILE_MAP = {
    "dirs": ORGANIZE_DIRS_FILE,
    "classify": CLASSIFY_CONFIG_FILE,
    "wash": WASH_CONFIG_FILE,
    "rename": RENAME_RULES_FILE,
}


class RenameRules(BaseModel):
    movie_folder: str = "{first_letter}-{title}-{year}"
    movie_file: str = "{title}.{year}<.{resource_pix}><.{fps}><.{resource_version}><.{resource_source}><.{resource_type}><.{resource_effect}><.{video_encode}><.{audio_encode}><-{resource_team}>{ext}"
    tv_folder: str = "{first_letter}-{title} ({year})"
    season_folder: str = "Season {season_num:02d}"
    episode_file: str = "{title} - S{season_num:02d}<E{episode_num:02d}>< - {episode_name}><.{resource_pix}><.{fps}><.{resource_version}><.{resource_source}><.{resource_type}><.{resource_effect}><.{video_encode}><.{audio_encode}><-{resource_team}>{ext}"


class WashConfig(BaseModel):
    enabled: bool = True
    wash_yaml: str = ""  # YAML 格式的洗版策略配置
    # 新格式：电影/电视剧两组独立配置
    wash_movie: Optional[dict] = None
    wash_tv: Optional[dict] = None
    # 旧格式字段（向后兼容）
    wash_media_type: str = ""  # movie / tv / 空=全部
    wash_scope: str = "all"  # all / group
    wash_size_mode: str = "disabled"  # disabled / max_size / min_size / skip / coexist
    wash_pix_list: List[str] = []  # 分辨率优先级
    wash_source_list: List[str] = []  # 来源优先级
    wash_vcodec_list: List[str] = []  # 视频编码优先级
    wash_effect_list: List[str] = []  # 特效优先级
    wash_acodec_list: List[str] = []  # 音频编码优先级
    wash_fps_list: List[str] = []  # 帧率优先级
    wash_dimension_list: List[str] = []  # 比较维度顺序


class OrganizeRequest(BaseModel):
    account_id: int = 0  # 0=自动选择第一个有效账号
    source_cid: str
    source_path: str = ""
    existing_cid: str = ""
    existing_path: str = ""
    redundant_cid: str = ""
    redundant_path: str = ""
    unrecognized_cid: str = ""
    unrecognized_path: str = ""
    classify_config: str = ""  # YAML 格式的分类配置字符串
    category_roots: Optional[dict] = None  # 自定义根目录名称 {"movie":"电影", "tv":"电视剧", "av":"AV"}
    rename_rules: RenameRules = RenameRules()
    wash_config: WashConfig = WashConfig()
    use_ffprobe: bool = False  # 是否用 ffprobe 探测文件内容补充资源信息
    skip_no_info: bool = False  # True=文件名和ffprobe均无资源信息时跳过重命名
    prefer_filename: bool = False  # True=文件名优先，仅补充缺失字段；False=ffprobe优先，探测值覆盖文件名值
    min_organize_size_mb: int = 0  # 小于此大小的视频不整理（MB），0=不限制
    organize_blacklist: str = ""  # 整理黑名单，每行一个正则，文件名匹配则跳过
    ai_mode: str = "off"  # off=关闭AI, assist=TMDB失败时辅助AI, force=强制使用AI
    dry_run: bool = False  # True=仅预览，不实际移动文件


class FullSyncRequest(BaseModel):
    account_id: int = 0  # 0=自动选择第一个有效账号
    source_cid: str
    local_media_dir: str
    video_exts: list[str] = []
    image_exts: list[str] = []
    data_exts: list[str] = []
    min_video_size_mb: int = 0


class IncrementalSyncRequest(BaseModel):
    account_id: int = 0  # 0=自动选择第一个有效账号


class UploadSyncRequest(BaseModel):
    account_id: int = 0  # 0=自动选择第一个有效账号
    upload_exts: list[str] = []  # 需要上传的文件后缀
    overwrite: bool = False  # 是否覆盖已存在的文件


class SyncScheduleRequest(BaseModel):
    account_id: int
    source_cid: str
    source_path: str = ""
    local_media_dir: str
    cron: str = ""  # 标准 5 段式 cron，留空不启用
    video_exts_str: str = ""
    image_exts_str: str = ""
    data_exts_str: str = ""
    min_video_size_mb: int = 0


def _get_account(account_id: int) -> Optional[dict]:
    """获取账号：指定 ID 或第一个有效账号"""
    if account_id:
        return find_account(account_id)
    return get_first_valid_account()


@router.post("/run", response_model=ApiResponse)
async def run_organize(payload: OrganizeRequest):
    """
    执行整理：扫描源目录，分类文件，移动到全量同步目录
    - 整理后文件 → 全量同步目录（从 sync_schedule.json 读取）
    - 全量同步目录已存在的文件 → 已存在影视的目录
    - 冗余文件 → 冗余文件存在的目录
    - 无法识别的文件 → 识别不准的目录
    """
    account = _get_account(payload.account_id)

    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    if not payload.source_cid:
        return ApiResponse(code=400, message="请选择需要整理的源目录")

    # 从 sync_schedule.json 读取全量同步目录作为整理目标
    target_cid = ""
    target_path = ""
    sync_schedule_file = CONFIG_DIR / "sync_schedule.json"
    if sync_schedule_file.exists():
        try:
            import json as _json
            schedule = _json.loads(sync_schedule_file.read_text(encoding="utf-8"))
            target_cid = schedule.get("source_cid", "")
            target_path = schedule.get("source_path", "")
        except (json.JSONDecodeError, OSError) as e:
            from app.core.logbuffer import get_logger
            _logger = get_logger()
            _logger.warning(f"[organize] 读取同步配置文件失败: {e}")
            return ApiResponse(code=500, message="同步配置文件损坏，请重新配置全量同步目录")

    if not target_cid:
        return ApiResponse(code=400, message="未配置全量同步目录，请先在全量同步页面设置源目录")

    try:
        # 进度回调
        from app.core.progress import progress_manager

        async def _progress_cb(current, total, filename):
            await progress_manager.update_progress(current, total, filename)

        if not await progress_manager.start_task("organize", 0, "影视整理"):
            return ApiResponse(code=409, message="已有任务正在运行，请等待完成后再试")
        result = await OrganizeService.scan_and_organize(
            cookies=account.get("cookies", ""),
            source_cid=payload.source_cid,
            target_cid=target_cid,
            existing_cid=payload.existing_cid,
            redundant_cid=payload.redundant_cid,
            unrecognized_cid=payload.unrecognized_cid,
            classify_config=payload.classify_config,
            category_roots=payload.category_roots,
            rename_rules=payload.rename_rules.model_dump(),
            wash_config=payload.wash_config.model_dump(),
            use_ffprobe=payload.use_ffprobe,
            skip_no_info=payload.skip_no_info,
            prefer_filename=payload.prefer_filename,
            min_organize_size_mb=payload.min_organize_size_mb,
            organize_blacklist=payload.organize_blacklist,
            ai_mode=payload.ai_mode,
            dry_run=payload.dry_run,
            progress_callback=_progress_cb,
        )
        await progress_manager.complete_task(
            f"整理完成: 成功 {len(result.get('organized', []))}，"
            f"冗余 {len(result.get('redundant', []))}，"
            f"无法识别 {len(result.get('unrecognized', []))}"
        )
        # 仅在实际执行（非预览）时触发 Emby 刷新和通知
        if not payload.dry_run:
            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True):
                from app.services.emby import trigger_emby_refresh
                await trigger_emby_refresh()
            notify_cfg = read_setting("emby_notify") or {}
            if notify_cfg.get("notify_on_organize", True):
                from app.services.notification_service import NotificationService
                await NotificationService.notify_organize_complete(result)
        return ApiResponse(data=result)
    except Exception as e:
        await progress_manager.error_task(f"整理失败: {str(e)}")
        return ApiResponse(code=500, message=f"整理失败: {str(e)}")


@router.post("/sync/full", response_model=ApiResponse)
async def full_sync(payload: FullSyncRequest):
    """
    全量同步：扫描源目录下所有匹配后缀的文件，生成 STRM 到本地目录
    - 视频文件：生成 .strm 文件
    - 图片文件：直接下载到本地
    - 数据文件（nfo/srt 等）：直接下载到本地
    """
    account = _get_account(payload.account_id)

    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    if not payload.source_cid:
        return ApiResponse(code=400, message="请选择需要全量同步的目录")

    if not payload.local_media_dir.strip():
        return ApiResponse(code=400, message="请填写本地媒体目录")

    if not payload.video_exts:
        return ApiResponse(code=400, message="请填写视频文件后缀")

    # 调试日志：确认收到的路径
    from app.core.logbuffer import get_logger
    _logger = get_logger()
    _logger.info(f"[sync] 全量同步请求: local_media_dir='{payload.local_media_dir}', source_cid='{payload.source_cid}'")

    # 提前创建本地媒体目录，避免同步过程中才发现路径问题
    from pathlib import Path
    try:
        media_dir = Path(payload.local_media_dir.strip())
        media_dir.mkdir(parents=True, exist_ok=True)
        _logger.info(f"[sync] 本地媒体目录已就绪: {media_dir.resolve()}")
    except Exception as e:
        _logger.warning(f"[sync] 创建本地媒体目录失败: {payload.local_media_dir} - {e}")
        return ApiResponse(code=400, message=f"无法创建本地媒体目录 '{payload.local_media_dir}'：{e}")

    try:
        from app.core.progress import progress_manager
        _loop = asyncio.get_running_loop()
        if not await progress_manager.start_task("full_sync", 0, "全量同步"):
            return ApiResponse(code=409, message="已有任务正在运行，请等待完成后再试")

        video_exts = set(e.lower() if e.startswith(".") else f".{e.lower()}" for e in payload.video_exts if e.strip())
        image_exts = set(e.lower() if e.startswith(".") else f".{e.lower()}" for e in payload.image_exts if e.strip())
        data_exts = set(e.lower() if e.startswith(".") else f".{e.lower()}" for e in payload.data_exts if e.strip())

        # 全量同步时自动保存配置，供增量同步使用（保留已有 cron 设置）
        SyncService.save_schedule(
            account_id=payload.account_id,
            source_cid=payload.source_cid,
            local_media_dir=payload.local_media_dir.strip(),
            cron="",
            video_exts_str=",".join(sorted(video_exts)),
            image_exts_str=",".join(sorted(image_exts)),
            data_exts_str=",".join(sorted(data_exts)),
            min_video_size_mb=payload.min_video_size_mb,
            preserve_cron=True,
        )

        result = await asyncio.to_thread(
            SyncService.full_sync,
            cookies=account.get("cookies", ""),
            source_cid=payload.source_cid,
            local_media_dir=payload.local_media_dir.strip(),
            video_exts=video_exts,
            image_exts=image_exts,
            data_exts=data_exts,
            min_video_size_mb=payload.min_video_size_mb,
            account_id=account.get("id", 0),
            loop=_loop,
        )
        await progress_manager.complete_task(
            f"全量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
        )
        # 同步完成后根据配置触发 Emby 媒体库刷新
        from app.core.db_helper import read_setting
        sync_cfg = read_setting("emby_sync") or {}
        if sync_cfg.get("auto_refresh", True) and result.get("synced"):
            from app.services.emby import trigger_emby_refresh
            await trigger_emby_refresh(payload.local_media_dir.strip())

            # Emby 刮削后自动上传 nfo/图片到网盘
            await SyncService.trigger_auto_upload(
                cookies=account.get("cookies", ""),
                source_cid=payload.source_cid,
                local_media_dir=payload.local_media_dir.strip(),
                account_id=account.get("id", 0),
                loop=_loop,
            )
        # 根据配置发送通知
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_sync", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_sync_complete("full", result, payload.local_media_dir.strip())
        return ApiResponse(data=result)
    except Exception as e:
        await progress_manager.error_task(f"全量同步失败: {str(e)}")
        return ApiResponse(code=500, message=f"全量同步失败: {str(e)}")


@router.post("/sync/incremental", response_model=ApiResponse)
async def incremental_sync(payload: IncrementalSyncRequest):
    """
    增量同步：从已保存的同步配置中读取目录和后缀，仅同步新增/变更的文件
    """
    account = _get_account(payload.account_id)

    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    # 从已保存的配置中读取同步参数
    schedule = SyncService.load_schedule()
    source_cid = schedule.get("source_cid", "")
    local_media_dir = schedule.get("local_media_dir", "")

    if not source_cid:
        return ApiResponse(code=400, message="请先执行一次全量同步以保存目录配置")

    if not local_media_dir:
        return ApiResponse(code=400, message="请先执行一次全量同步以保存本地媒体目录")

    try:
        from app.core.progress import progress_manager
        _loop = asyncio.get_running_loop()
        if not await progress_manager.start_task("incremental_sync", 0, "增量同步"):
            return ApiResponse(code=409, message="已有任务正在运行，请等待完成后再试")

        video_exts = _parse_exts_str(schedule.get("video_exts_str", ""))
        image_exts = _parse_exts_str(schedule.get("image_exts_str", ""))
        data_exts = _parse_exts_str(schedule.get("data_exts_str", ""))
        min_video_size_mb = schedule.get("min_video_size_mb", 0)

        result = await asyncio.to_thread(
            SyncService.incremental_sync,
            cookies=account.get("cookies", ""),
            source_cid=source_cid,
            local_media_dir=local_media_dir,
            video_exts=video_exts,
            image_exts=image_exts,
            data_exts=data_exts,
            min_video_size_mb=min_video_size_mb,
            account_id=account.get("id", 0),
            loop=_loop,
        )
        await progress_manager.complete_task(
            f"增量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
        )
        # 同步完成后根据配置触发 Emby 媒体库刷新
        from app.core.db_helper import read_setting
        sync_cfg = read_setting("emby_sync") or {}
        if sync_cfg.get("auto_refresh", True) and result.get("synced"):
            from app.services.emby import trigger_emby_refresh
            await trigger_emby_refresh(local_media_dir)

            # Emby 刮削后自动上传 nfo/图片到网盘
            await SyncService.trigger_auto_upload(
                cookies=account.get("cookies", ""),
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                account_id=account.get("id", 0),
                loop=_loop,
            )
        # 根据配置发送通知
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_sync", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_sync_complete("incremental", result, local_media_dir)
        return ApiResponse(data=result)
    except Exception as e:
        await progress_manager.error_task(f"增量同步失败: {str(e)}")
        return ApiResponse(code=500, message=f"增量同步失败: {str(e)}")


@router.post("/sync/upload", response_model=ApiResponse)
async def upload_sync(payload: UploadSyncRequest):
    """
    上传同步：将本地元数据文件（nfo、图片、字幕等）上传到 115 网盘
    - 从已保存的同步配置中读取网盘目录和本地目录
    - 自动跳过 .strm 文件和视频文件
    - 支持覆盖模式
    """
    account = _get_account(payload.account_id)

    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    # 从已保存的配置中读取同步参数
    schedule = SyncService.load_schedule()
    source_cid = schedule.get("source_cid", "")
    local_media_dir = schedule.get("local_media_dir", "")

    if not source_cid:
        return ApiResponse(code=400, message="请先执行一次全量同步以保存目录配置")

    if not local_media_dir:
        return ApiResponse(code=400, message="请先执行一次全量同步以保存本地媒体目录")

    try:
        from app.core.progress import progress_manager
        _loop = asyncio.get_running_loop()
        if not await progress_manager.start_task("upload_sync", 0, "上传同步"):
            return ApiResponse(code=409, message="已有任务正在运行，请等待完成后再试")

        # 解析上传后缀
        if payload.upload_exts:
            upload_exts = set(
                (e.lower() if e.startswith(".") else f".{e.lower()}")
                for e in payload.upload_exts if e.strip()
            )
        else:
            upload_exts = SyncService.DEFAULT_UPLOAD_EXTS

        result = await asyncio.to_thread(
            SyncService.upload_sync,
            cookies=account.get("cookies", ""),
            source_cid=source_cid,
            local_media_dir=local_media_dir,
            upload_exts=upload_exts,
            overwrite=payload.overwrite,
            account_id=account.get("id", 0),
            loop=_loop,
        )
        await progress_manager.complete_task(
            f"上传同步完成: 入队 {len(result.get('queued', []))}，跳过 {result.get('skipped', 0)}"
        )
        # 根据配置发送通知
        from app.core.db_helper import read_setting
        notify_cfg = read_setting("emby_notify") or {}
        if notify_cfg.get("notify_on_sync", True):
            from app.services.notification_service import NotificationService
            await NotificationService.notify_sync_complete("upload", result, local_media_dir)
        return ApiResponse(data=result)
    except Exception as e:
        await progress_manager.error_task(f"上传同步失败: {str(e)}")
        return ApiResponse(code=500, message=f"上传同步失败: {str(e)}")


@router.post("/sync/schedule", response_model=ApiResponse)
async def save_sync_schedule(payload: SyncScheduleRequest):
    """
    保存同步定时计划（Cron 表达式）
    """
    account = find_account(payload.account_id)

    if not account:
        return ApiResponse(code=404, message="账号不存在")

    try:
        ok = SyncService.save_schedule(
            account_id=payload.account_id,
            source_cid=payload.source_cid,
            source_path=payload.source_path,
            local_media_dir=payload.local_media_dir.strip(),
            cron=payload.cron.strip(),
            video_exts_str=payload.video_exts_str,
            image_exts_str=payload.image_exts_str,
            data_exts_str=payload.data_exts_str,
            min_video_size_mb=payload.min_video_size_mb,
        )
        if ok:
            # 重新注册定时任务
            from app.core.scheduler import register_sync_schedule
            await register_sync_schedule()
            return ApiResponse(data={"saved": True})
        return ApiResponse(code=500, message="保存失败")
    except Exception as e:
        return ApiResponse(code=500, message=f"保存定时计划失败: {str(e)}")


@router.get("/sync/schedule", response_model=ApiResponse)
async def get_sync_schedule():
    """获取已保存的同步定时计划"""
    config = SyncService.load_schedule()
    return ApiResponse(data=config)


# ============ 上传队列状态 ============

@router.get("/upload-queue/status", response_model=ApiResponse)
async def get_upload_queue_status():
    """获取上传队列状态统计"""
    from app.services.upload_queue import get_upload_queue
    queue = get_upload_queue()
    return ApiResponse(data=queue.get_status())


@router.get("/upload-queue/tasks", response_model=ApiResponse)
async def get_upload_queue_tasks(limit: int = 20):
    """获取上传队列最近的任务列表"""
    from app.services.upload_queue import get_upload_queue
    queue = get_upload_queue()
    return ApiResponse(data=queue.get_recent_tasks(limit))


@router.post("/upload-queue/clear", response_model=ApiResponse)
async def clear_upload_queue():
    """清除已完成/已跳过/已失败的上传任务"""
    from app.services.upload_queue import get_upload_queue
    queue = get_upload_queue()
    cleared = queue.clear_finished()
    return ApiResponse(data={"cleared": cleared})


@router.post("/upload-queue/retry", response_model=ApiResponse)
async def retry_failed_upload_tasks():
    """重试所有失败的上传任务"""
    from app.services.upload_queue import get_upload_queue
    queue = get_upload_queue()
    count = queue.retry_failed()
    return ApiResponse(data={"retried": count})


def _parse_exts_str(exts_str: str) -> set:
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


class SaveOrganizeConfigRequest(BaseModel):
    """保存整理配置（支持按类型单独保存）"""
    type: str = "all"  # dirs / classify / wash / rename / all
    source_cid: str = ""
    source_path: str = ""
    existing_cid: str = ""
    existing_path: str = ""
    redundant_cid: str = ""
    redundant_path: str = ""
    unrecognized_cid: str = ""
    unrecognized_path: str = ""
    classify_config: str = ""
    category_roots: Optional[dict] = None  # 自定义根目录名称
    rename_rules: Optional[RenameRules] = None
    wash_config: Optional[WashConfig] = None
    use_ffprobe: bool = False  # 是否用 ffprobe 探测文件内容补充资源信息
    skip_no_info: bool = False  # True=文件名和ffprobe均无资源信息时跳过重命名
    prefer_filename: bool = False  # True=文件名优先，仅补充缺失字段；False=ffprobe优先，探测值覆盖文件名值
    min_organize_size_mb: int = 0  # 小于此大小的视频不整理（MB），0=不限制
    organize_blacklist: str = ""  # 整理黑名单，每行一个正则，文件名匹配则跳过
    ai_mode: str = "off"  # off=关闭AI, assist=TMDB失败时辅助AI, force=强制使用AI


@router.post("/config/save", response_model=ApiResponse)
async def save_organize_config(payload: SaveOrganizeConfigRequest):
    """保存整理功能配置（按类型写入独立 JSON 文件）"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        saved_files = []

        # 整理目录
        if payload.type in ("dirs", "all"):
            data = {
                "source_cid": payload.source_cid,
                "source_path": payload.source_path,
                "existing_cid": payload.existing_cid,
                "existing_path": payload.existing_path,
                "redundant_cid": payload.redundant_cid,
                "redundant_path": payload.redundant_path,
                "unrecognized_cid": payload.unrecognized_cid,
                "unrecognized_path": payload.unrecognized_path,
                "use_ffprobe": payload.use_ffprobe,
                "skip_no_info": payload.skip_no_info,
                "prefer_filename": payload.prefer_filename,
                "min_organize_size_mb": payload.min_organize_size_mb,
                "organize_blacklist": payload.organize_blacklist,
                "ai_mode": payload.ai_mode,
                "updated_at": ts,
            }
            ORGANIZE_DIRS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_files.append("organize_dirs.json")

        # 二级分类
        if payload.type in ("classify", "all"):
            data = {
                "classify_config": payload.classify_config,
                "category_roots": payload.category_roots or {"movie": "电影", "tv": "电视剧", "av": "AV"},
                "updated_at": ts,
            }
            CLASSIFY_CONFIG_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_files.append("classify_config.json")

        # 洗版策略
        if payload.type in ("wash", "all") and payload.wash_config:
            data = payload.wash_config.model_dump()
            data["updated_at"] = ts
            WASH_CONFIG_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_files.append("wash_config.json")

        # 重命名规则
        if payload.type in ("rename", "all") and payload.rename_rules:
            data = payload.rename_rules.model_dump()
            data["updated_at"] = ts
            RENAME_RULES_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved_files.append("rename_rules.json")

        return ApiResponse(data={"saved": True, "files": saved_files})
    except Exception as e:
        return ApiResponse(code=500, message=f"保存配置失败: {str(e)}")


@router.get("/config/load", response_model=ApiResponse)
async def load_organize_config():
    """加载全部整理功能配置（合并 4 个独立文件返回）"""
    try:
        result = {}

        # 整理目录
        if ORGANIZE_DIRS_FILE.exists():
            dirs = json.loads(ORGANIZE_DIRS_FILE.read_text(encoding="utf-8"))
            result.update(dirs)

        # 二级分类
        if CLASSIFY_CONFIG_FILE.exists():
            classify = json.loads(CLASSIFY_CONFIG_FILE.read_text(encoding="utf-8"))
            result.update(classify)

        # 洗版策略
        if WASH_CONFIG_FILE.exists():
            wash = json.loads(WASH_CONFIG_FILE.read_text(encoding="utf-8"))
            result["wash_config"] = wash

        # 重命名规则
        if RENAME_RULES_FILE.exists():
            rename = json.loads(RENAME_RULES_FILE.read_text(encoding="utf-8"))
            result["rename_rules"] = rename

        return ApiResponse(data=result)
    except Exception as e:
        return ApiResponse(code=500, message=f"加载配置失败: {str(e)}")


@router.get("/ffprobe/status", response_model=ApiResponse)
async def check_ffprobe_status():
    """检查系统是否安装了 ffprobe"""
    return ApiResponse(data={"available": is_ffprobe_available()})

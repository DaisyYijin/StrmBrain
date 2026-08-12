"""
API 路由 - 转存下载（115 离线下载）
1. 添加离线下载任务（磁力/HTTP/FTP 链接）
2. 获取任务列表
3. 删除任务
4. 清空任务
5. 保存目录配置持久化
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

from app.core.json_storage import get_first_valid_account, read_setting, save_setting
from app.schemas import ApiResponse
from app.services.client_115 import Client115Service

router = APIRouter(prefix="/api/clouddownload", tags=["clouddownload"])


class AddTaskRequest(BaseModel):
    account_id: int = 0  # 0=自动选择第一个有效账号
    urls: List[str] = []  # 下载链接列表
    save_cid: str = ""  # 保存到的目录 cid（留空=根目录）


class ParseLinksRequest(BaseModel):
    """N1: 从消息文本解析磁力/ed2k/种子链接"""
    account_id: int = 0
    text: str = ""


class TorrentInfoRequest(BaseModel):
    """N1: 解析磁力/种子的文件清单（供勾选）"""
    account_id: int = 0
    torrent_url: str = ""   # 磁力链接或种子 url
    torrent_sha1: str = ""  # 或已上传种子的 sha1


class AddBtRequest(BaseModel):
    """N1: 提交 BT 任务，仅下载选中文件"""
    account_id: int = 0
    info_hash: str = ""
    wanted_indexes: List[int] = []  # 空=全部
    save_cid: str = ""
    torrent_sha1: str = ""


def _parse_offline_links(raw: str) -> List[str]:
    """N1: 从一段文本按出现顺序提取磁力/ed2k/种子链接（去零宽/RTL、去重）。

    参考 MoviePilot p115strmhelper OfflineLinkResolver。
    """
    import re
    if not raw or not isinstance(raw, str):
        return []
    s = raw.replace("\uff5c", "|").strip()
    if not s:
        return []
    _STRIP = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF))
    ed2k_re = re.compile(
        r"(ed2k://\|file\|[^|]+\|\d+\|[0-9A-Fa-f]{32}(?:\|(?:h|p)=[^|]+)?\|/)",
        re.IGNORECASE,
    )
    spans = []
    for m in ed2k_re.finditer(s):
        spans.append((m.start(), m.end(), m.group(1)))
    for m in re.finditer(r"(magnet:\?[^\s]+)", s, re.IGNORECASE):
        spans.append((m.start(), m.end(), m.group(1)))
    for m in re.finditer(r"(https?://[^\s]+\.torrent(?:\?[^\s]*)?)", s, re.IGNORECASE):
        spans.append((m.start(), m.end(), m.group(1)))
    spans.sort(key=lambda x: x[0])
    seen = set()
    out: List[str] = []
    last_end = -1
    for st, en, txt in spans:
        if st < last_end:
            continue
        c = txt.translate(_STRIP)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
        last_end = en
    return out


class ListTaskRequest(BaseModel):
    account_id: int = 0
    page: int = 1
    page_size: int = 30


class DelTaskRequest(BaseModel):
    account_id: int = 0
    info_hashes: List[str] = []
    delete_source: bool = False  # 是否同时删除源文件


class ClearTaskRequest(BaseModel):
    account_id: int = 0
    flag: int = 0  # 0=已完成 1=全部 2=已失败 3=进行中


class CheckStatusRequest(BaseModel):
    account_id: int = 0
    info_hashes: List[str] = []


class TaskDetailRequest(BaseModel):
    account_id: int = 0
    info_hash: str = ""


class ShareCreateRequest(BaseModel):
    account_id: int = 0
    file_ids: List[str] = []  # 要分享的文件/目录 id 列表
    password: str = ""        # 访问密码（留空=无密码）
    expire_days: int = 0      # 有效期天数（0=长期）


class ShareListRequest(BaseModel):
    account_id: int = 0
    page: int = 1
    page_size: int = 20


class ShareCancelRequest(BaseModel):
    account_id: int = 0
    share_code: str = ""


def _get_account(account_id: int) -> Optional[dict]:
    if account_id:
        from app.core.json_storage import find_account
        return find_account(account_id)
    return get_first_valid_account()


@router.post("/add", response_model=ApiResponse)
async def add_task(payload: AddTaskRequest):
    """添加离线下载任务"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.urls:
        return ApiResponse(code=400, message="请输入下载链接")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_add_urls(cookies, payload.urls, payload.save_cid)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"添加失败: {result['error']}")

    # 下载任务添加成功后，注册一次性自动整理任务（参考 qmediasync：下载完成 → 自动整理）
    # 从保存的目录配置读取 save_cid（请求未指定时），文件下载完成后自动移动并整理
    if isinstance(result, dict) and result.get("state"):
        try:
            from app.core.scheduler import schedule_auto_organize_after_download
            save_cid = payload.save_cid or (read_setting("clouddownload_config") or {}).get("save_cid", "")
            if save_cid:
                schedule_auto_organize_after_download(cookies, save_cid)
        except Exception as e:
            from app.core.logbuffer import get_logger
            get_logger().warning(f"[clouddownload] 注册自动整理失败: {e}")

    return ApiResponse(data=result)


@router.post("/parse-links", response_model=ApiResponse)
async def parse_links(payload: ParseLinksRequest):
    """N1: 从粘贴的文本中提取磁力/ed2k/种子链接（去零宽字符、按序去重）。"""
    links = _parse_offline_links(payload.text)
    return ApiResponse(data={"links": links, "count": len(links)})


@router.post("/torrent-info", response_model=ApiResponse)
async def torrent_info(payload: TorrentInfoRequest):
    """N1: 解析磁力/种子的文件清单，供用户勾选后只下载选中项。"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not (payload.torrent_url or payload.torrent_sha1):
        return ApiResponse(code=400, message="请提供磁力链接或种子")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_torrent_info(
        cookies, payload.torrent_url, payload.torrent_sha1
    )
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"解析失败: {result['error']}")
    return ApiResponse(data=result)


@router.post("/add-bt", response_model=ApiResponse)
async def add_bt(payload: AddBtRequest):
    """N1: 提交 BT 离线任务，仅下载勾选的文件；成功后注册自动整理。"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.info_hash:
        return ApiResponse(code=400, message="缺少 info_hash")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_add_bt(
        cookies, payload.info_hash, payload.wanted_indexes,
        payload.save_cid, payload.torrent_sha1,
    )
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"添加失败: {result['error']}")

    # 成功后注册一次性自动整理（与 add_task 一致）
    if isinstance(result, dict) and result.get("state"):
        try:
            from app.core.scheduler import schedule_auto_organize_after_download
            save_cid = payload.save_cid or (read_setting("clouddownload_config") or {}).get("save_cid", "")
            if save_cid:
                schedule_auto_organize_after_download(cookies, save_cid)
        except Exception as e:
            from app.core.logbuffer import get_logger
            get_logger().warning(f"[clouddownload] BT 注册自动整理失败: {e}")

    return ApiResponse(data=result)


class InstantUploadRequest(BaseModel):
    """N3: 跨云秒传——用远端直链 SHA1 秒传进 115"""
    account_id: int = 0
    download_url: str = ""   # 源文件可 Range 访问的直链
    filename: str = ""
    file_size: int = 0
    full_sha1: str = ""      # 整文件 SHA1（源云盘 API 提供）
    save_cid: str = "0"


@router.post("/instant-upload", response_model=ApiResponse)
async def instant_upload(payload: InstantUploadRequest):
    """N3: 跨云秒传。用远端直链（如阿里云盘分享直链）的 SHA1 尝试秒传进 115，
    命中则不下载字节即完成。需调用方提供整文件 SHA1（源云盘通常在文件信息中提供）。
    """
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not (payload.download_url and payload.filename and payload.full_sha1 and payload.file_size > 0):
        return ApiResponse(code=400, message="参数不完整（需 download_url/filename/file_size/full_sha1）")

    cookies = account.get("cookies", "")
    import asyncio
    from app.services.client_115 import _executor
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: Client115Service.instant_upload_from_url(
            cookies, payload.download_url, payload.filename,
            payload.file_size, payload.full_sha1, payload.save_cid,
        ),
    )
    if result.get("status") == "error":
        return ApiResponse(code=500, message=f"秒传失败: {result.get('message', '')}")
    return ApiResponse(data=result)


@router.get("/quota", response_model=ApiResponse)
async def get_quota(account_id: int = 0):
    """N1: 获取离线下载配额信息。"""
    account = _get_account(account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_quota(cookies)
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"获取配额失败: {result['error']}")
    return ApiResponse(data=result)


# ===== 目录文件就绪检测 =====

class CheckDirReadyRequest(BaseModel):
    account_id: int = 0
    cid: str = ""


@router.post("/check_dir_ready", response_model=ApiResponse)
async def check_dir_ready(payload: CheckDirReadyRequest):
    """检查指定 115 目录下是否有文件（转存/下载就绪检测）"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.cid:
        return ApiResponse(code=400, message="请提供目录 cid")

    cookies = account.get("cookies", "")
    result = Client115Service.list_files(cookies, payload.cid, 0, 10)

    if isinstance(result, dict) and result.get("_error"):
        return ApiResponse(code=500, message=f"检测失败: {result['_error']}")

    # 115 返回格式: {data: [...], count: N}
    data_list = result.get("data", []) if isinstance(result, dict) else []
    file_count = len(data_list) if isinstance(data_list, list) else 0

    return ApiResponse(data={
        "ready": file_count > 0,
        "file_count": file_count,
    })


@router.post("/list", response_model=ApiResponse)
async def list_tasks(payload: ListTaskRequest):
    """获取离线下载任务列表"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_list(cookies, payload.page, payload.page_size)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"获取失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/del", response_model=ApiResponse)
async def del_tasks(payload: DelTaskRequest):
    """删除离线下载任务"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.info_hashes:
        return ApiResponse(code=400, message="请选择要删除的任务")

    cookies = account.get("cookies", "")
    flag = 1 if payload.delete_source else 0
    result = Client115Service.clouddownload_del(cookies, payload.info_hashes, flag)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"删除失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/clear", response_model=ApiResponse)
async def clear_tasks(payload: ClearTaskRequest):
    """清空离线下载任务"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_clear(cookies, payload.flag)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"清空失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/check_status", response_model=ApiResponse)
async def check_download_status(payload: CheckStatusRequest):
    """检查离线下载任务的完成状态（前端轮询用）"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.info_hashes:
        return ApiResponse(code=400, message="请提供要检查的任务 hash")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_check_status(cookies, payload.info_hashes)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"检查失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/detail", response_model=ApiResponse)
async def task_detail(payload: TaskDetailRequest):
    """获取离线下载任务明细（名称/状态/进度/速度/文件列表）"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.info_hash:
        return ApiResponse(code=400, message="请提供任务 hash")

    cookies = account.get("cookies", "")
    result = Client115Service.clouddownload_task_details(cookies, payload.info_hash)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"获取详情失败: {result['error']}")

    return ApiResponse(data={"task": result})


# ===== 保存目录配置持久化 =====

class SaveDirConfigRequest(BaseModel):
    save_cid: str = ""
    save_path: str = ""
    auto_organize: bool = True


@router.post("/config/save", response_model=ApiResponse)
async def save_dir_config(payload: SaveDirConfigRequest):
    """保存转存下载的目录配置"""
    save_setting("clouddownload_config", {
        "save_cid": payload.save_cid,
        "save_path": payload.save_path,
        "auto_organize": payload.auto_organize,
    })
    return ApiResponse(data={"saved": True})


@router.get("/config/load", response_model=ApiResponse)
async def load_dir_config():
    """加载转存下载的目录配置"""
    config = read_setting("clouddownload_config")
    if config:
        config.setdefault("auto_organize", True)
        return ApiResponse(data=config)
    return ApiResponse(data={"save_cid": "", "save_path": "", "auto_organize": True})


# ===== 分享链接转存 =====

class ShareSnapRequest(BaseModel):
    account_id: int = 0
    share_url: str


class ShareReceiveRequest(BaseModel):
    account_id: int = 0
    share_url: str
    file_ids: List[str] = []  # 要转存的文件/目录 id 列表
    target_cid: str = ""  # 保存到自己的网盘目录 cid（留空=根目录）


@router.post("/share/snap", response_model=ApiResponse)
async def share_snap(payload: ShareSnapRequest):
    """获取分享链接中的文件列表"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.share_url:
        return ApiResponse(code=400, message="请输入分享链接")

    cookies = account.get("cookies", "")
    result = Client115Service.share_snap(cookies, payload.share_url)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"获取失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/share/dedup-probe", response_model=ApiResponse)
async def share_dedup_probe(payload: ShareSnapRequest):
    """O12: 转存前秒传 dry-run 探测——预估分享中有多少文件已在网盘（可秒传/跳过）。"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.share_url:
        return ApiResponse(code=400, message="请输入分享链接")

    cookies = account.get("cookies", "")
    result = Client115Service.share_dedup_probe(cookies, payload.share_url)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"探测失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/share/receive", response_model=ApiResponse)
async def share_receive(payload: ShareReceiveRequest):
    """转存分享链接中的文件到自己的网盘"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.share_url:
        return ApiResponse(code=400, message="请输入分享链接")
    if not payload.file_ids:
        return ApiResponse(code=400, message="请选择要转存的文件")

    cookies = account.get("cookies", "")
    result = Client115Service.share_receive(cookies, payload.share_url, payload.file_ids, payload.target_cid)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"转存失败: {result['error']}")

    return ApiResponse(data=result)


# ===== 分享链接管理（创建/列表/取消） =====

@router.post("/share/create", response_model=ApiResponse)
async def share_create(payload: ShareCreateRequest):
    """创建分享链接（我发出的分享）"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.file_ids:
        return ApiResponse(code=400, message="请选择要分享的文件")

    cookies = account.get("cookies", "")
    result = Client115Service.share_create(
        cookies, payload.file_ids, "all", payload.password, payload.expire_days,
    )

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"创建分享失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/share/list", response_model=ApiResponse)
async def share_list(payload: ShareListRequest):
    """获取我发出的分享列表"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    cookies = account.get("cookies", "")
    result = Client115Service.share_list_created(cookies, payload.page, payload.page_size)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"获取分享列表失败: {result['error']}")

    return ApiResponse(data=result)


@router.post("/share/cancel", response_model=ApiResponse)
async def share_cancel(payload: ShareCancelRequest):
    """取消分享"""
    account = _get_account(payload.account_id)
    if not account:
        return ApiResponse(code=404, message="未找到有效账号，请先登录 115")
    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")
    if not payload.share_code:
        return ApiResponse(code=400, message="缺少 share_code")

    cookies = account.get("cookies", "")
    result = Client115Service.share_cancel(cookies, payload.share_code)

    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(code=500, message=f"取消分享失败: {result['error']}")

    return ApiResponse(data=result)

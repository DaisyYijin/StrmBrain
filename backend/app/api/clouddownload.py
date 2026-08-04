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


# ===== 保存目录配置持久化 =====

class SaveDirConfigRequest(BaseModel):
    save_cid: str = ""
    save_path: str = ""


@router.post("/config/save", response_model=ApiResponse)
async def save_dir_config(payload: SaveDirConfigRequest):
    """保存转存下载的目录配置"""
    save_setting("clouddownload_config", {
        "save_cid": payload.save_cid,
        "save_path": payload.save_path,
    })
    return ApiResponse(data={"saved": True})


@router.get("/config/load", response_model=ApiResponse)
async def load_dir_config():
    """加载转存下载的目录配置"""
    config = read_setting("clouddownload_config")
    return ApiResponse(data=config if config else {"save_cid": "", "save_path": ""})


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

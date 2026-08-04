"""
API 路由 - 115 扫码登录
"""
from fastapi import APIRouter, HTTPException

from app.core.json_storage import upsert_account, find_account, find_account_by_user_id, get_first_valid_account
from app.services import Client115Service
from app.schemas import ApiResponse

router = APIRouter(prefix="/api/115", tags=["115"])

# 115 扫码登录支持的设备类型
LOGIN_APPS = [
    {"value": "web", "label": "115生活_网页端"},
    {"value": "android", "label": "115生活_安卓端"},
    {"value": "ios", "label": "115生活_苹果端"},
    {"value": "ipad", "label": "115生活_苹果平板端"},
    {"value": "tv", "label": "115生活_安卓电视端"},
    {"value": "apple_tv", "label": "115生活_苹果电视端"},
    {"value": "os_windows", "label": "115生活_Windows端"},
    {"value": "os_mac", "label": "115生活_macOS端"},
    {"value": "os_linux", "label": "115生活_Linux端"},
    {"value": "wechatmini", "label": "115生活_微信小程序端"},
    {"value": "alipaymini", "label": "115生活_支付宝小程序端"},
]


@router.get("/apps", response_model=ApiResponse)
async def list_login_apps():
    """获取支持的登录设备类型列表"""
    return ApiResponse(data=LOGIN_APPS)


@router.get("/qrcode", response_model=ApiResponse)
async def get_login_qrcode(app: str = "web"):
    """获取 115 扫码登录二维码"""
    try:
        qrcode_url, uid = await Client115Service.get_qrcode_for_login(app)
        return ApiResponse(data={"qrcode_url": qrcode_url, "uid": uid})
    except Exception as e:
        return ApiResponse(code=500, message=f"获取二维码失败: {str(e)}")


@router.get("/qrcode/status", response_model=ApiResponse)
async def check_qrcode_status(uid: str, name: str = "默认账号"):
    """检查二维码扫描状态"""
    result = await Client115Service.check_qrcode_status(uid)

    # 登录成功，保存账号
    if result.get("status") == 2 and result.get("cookies"):
        user_id = result.get("user_id", "")
        # 扫码时使用的设备类型（由 Client115Service 内部缓存提供）
        login_app = Client115Service.get_qrcode_app(uid) or "web"

        account_data = {
            "name": result.get("username") or name,
            "cookies": result["cookies"],
            "user_id": user_id,
            "username": result.get("username", ""),
            "app": login_app,
            "avatar_url": result.get("avatar_url", ""),
            "status": 1,
        }

        saved = upsert_account(account_data)
        result["account_id"] = saved.get("id")
        result["message"] = "账号已更新" if saved.get("updated_at", 0) > 0 and find_account_by_user_id(user_id) else "账号已添加"

        # 不返回 cookies 给前端
        result.pop("cookies", None)

    return ApiResponse(data=result)


@router.get("/files", response_model=ApiResponse)
async def list_files(
    account_id: int,
    cid: str = "0",
    offset: int = 0,
    limit: int = 100,
):
    """获取 115 文件列表"""
    account = find_account(account_id)

    if not account:
        return ApiResponse(code=404, message="账号不存在")

    cookies = account.get("cookies", "")
    files = Client115Service.list_files(cookies, cid, offset, limit)

    # 仅当封装层返回了非空 error（异常）时才报错
    err = files.get("_error")
    if err:
        return ApiResponse(code=500, message=err)

    return ApiResponse(data=files)


@router.get("/url/{filename:path}")
async def get_download_url(
    filename: str,
    pickcode: str,
    account_id: int,
    t: str = "",
):
    """
    获取 115 文件下载链接（302 重定向）
    URL 格式: /api/115/url/video.mp4?pickcode=xxx&account_id=0&t=token
    路径中的文件名（含扩展名）供 Emby 识别视频类型，pickcode 才是真正的文件标识。
    t 参数为 STRM 播放 Token，用于安全验证（防止未授权 URL 被外部直接调用）。
    """
    from fastapi.responses import RedirectResponse

    # Token 安全验证
    from app.services.strm_token import verify_token
    if not verify_token(t):
        raise HTTPException(status_code=403, detail="Token 验证失败，请重新生成 STRM 文件")

    # account_id=0 时自动取第一个有效账号
    if account_id:
        account = find_account(account_id)
    else:
        account = get_first_valid_account()

    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    # cookies 失效检测
    if account.get("status") == 0:
        raise HTTPException(status_code=503, detail="账号 cookies 已失效，请重新登录")

    cookies = account.get("cookies", "")
    url = Client115Service.get_download_url(cookies, pickcode, account.get("id", 0))

    if not url:
        # 获取失败可能是 cookies 过期，标记账号需要检查
        raise HTTPException(status_code=502, detail="获取下载链接失败，cookies 可能已过期")

    # 禁止缓存 302 响应，避免过期直链被缓存
    response = RedirectResponse(url=url, status_code=302)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

"""
API 路由 - 115 扫码登录
"""
from fastapi import APIRouter, HTTPException, Request

from app.core.json_storage import upsert_account, find_account, find_account_by_user_id, get_first_valid_account
from app.core.logbuffer import get_logger
from app.services import Client115Service
from app.schemas import ApiResponse

logger = get_logger("app.api.v115")

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


@router.get("/rate-stats", response_model=ApiResponse)
async def get_rate_limit_stats():
    """P0-2: 获取 115 速率限制和请求统计

    返回 QPS/QPM/QPH/平均延迟/限流次数/缓存命中率/缓存大小等指标，
    供运维监控和限流策略调优使用。
    """
    stats = Client115Service.get_rate_limit_stats()
    return ApiResponse(data=stats)


@router.get("/url/{filename:path}")
async def get_download_url(
    filename: str,
    pickcode: str,
    account_id: int,
    t: str = "",
    s: str = "",
    request: Request = None,
):
    """
    获取 115 文件下载链接（302 重定向）
    URL 格式（新）: /api/115/url/video.mp4?pickcode=xxx&account_id=0&s=signature
    URL 格式（旧）: /api/115/url/video.mp4?pickcode=xxx&account_id=0&t=token
    路径中的文件名（含扩展名）供 Emby 识别视频类型，pickcode 才是真正的文件标识。
    s 参数为 HMAC-SHA256 路径签名（新格式），t 参数为明文 Token（旧格式，向后兼容）。
    """
    from fastapi.responses import RedirectResponse

    # 安全验证：优先路径签名（s），回退 token（t），向后兼容
    from app.services.strm_token import verify_request
    if not verify_request(pickcode, token=t, signature=s):
        raise HTTPException(status_code=403, detail="安全验证失败，请重新生成 STRM 文件")

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

    # 115 直链要求下载 UA 与获取 UA 一致（f=1 参数控制）。
    # 播放器（浏览器/Infuse 等）会用自己的 UA 直连 115 CDN，
    # 因此获取直链时必须使用播放器客户端的 UA，否则 115 拒绝 -> 播放器报 NoCompatibleStream。
    # 参考 emby2Alist fetchLastLink：携带客户端 UA 换取绑定该 UA 的直链。
    # P0-3: 使用 get_download_url_multiplay 替代 get_download_url_with_ua，
    #       检测多端播放场景并自动复制文件获取独立直链。
    import re as _re
    request_ua = request.headers.get("User-Agent", "")
    if request_ua and not _re.search(r"(?i)httpx|python", request_ua):
        # 仅当反代/服务器端跟随（httpx）时不覆盖；真实客户端 UA 才用于换直链
        url = Client115Service.get_download_url_multiplay(
            cookies, pickcode, request_ua, account.get("id", 0)
        )
    else:
        url = Client115Service.get_download_url(cookies, pickcode, account.get("id", 0))

    if not url:
        # P0-4: 失败回源降级 -- 不直接抛 502，返回 JSON 降级响应让前端/播放器决定
        # 记录失败原因到日志（包含 pickcode/account_id/ua 便于排查）
        logger.warning(
            f"[115] 获取直链失败 pickcode={pickcode} account_id={account.get('id', 0)} "
            f"ua={request_ua[:80]}"
        )
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={
                "code": 503,
                "message": "获取直链失败，正在回退到原始流",
                "fallback": True,
                "pickcode": pickcode,
                "account_id": account.get("id", 0),
            }
        )

    # 禁止缓存 302 响应，避免过期直链被缓存
    response = RedirectResponse(url=url, status_code=302)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

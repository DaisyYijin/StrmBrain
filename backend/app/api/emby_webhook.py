"""
Emby Webhook 接收端点
接收 Emby 的 Webhook 推送（入库、播放、删除等事件），触发通知或级联删除。
"""
import json
import re
import time
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from app.core.logbuffer import get_logger
from app.core.db_helper import read_setting

logger = get_logger("app.api.emby_webhook")

router = APIRouter(prefix="/api/emby", tags=["emby-webhook"])

# 只处理这些事件（入库相关）
_ADD_EVENTS = frozenset({"item.add", "media.added", "ItemAdded", "new"})


def _extract_item(payload: dict) -> dict | None:
    """
    从 Emby webhook payload 中提取媒体信息。
    兼容多种 Emby webhook 插件格式。
    """
    # 格式1：Emby Webhooks 插件（Plex 风格）
    metadata = payload.get("Metadata") or payload.get("metadata") or {}
    item = payload.get("Item") or payload.get("item") or {}

    name = (
        metadata.get("Title")
        or metadata.get("Name")
        or item.get("Name")
        or item.get("Title")
        or payload.get("Title")
        or payload.get("Name")
        or "未知"
    )
    item_type = (
        metadata.get("Type")
        or item.get("Type")
        or payload.get("Type")
        or ""
    )
    year = metadata.get("Year") or item.get("Year") or payload.get("Year") or ""
    path = metadata.get("Path") or item.get("Path") or payload.get("Path") or ""
    overview = (
        metadata.get("Overview")
        or item.get("Overview")
        or payload.get("Overview")
        or ""
    )

    # 提取图片
    thumb = ""
    if metadata.get("ImageTags"):
        thumb = metadata.get("ImageTags", {}).get("Primary", "") or ""
    if not thumb and item.get("ImageTags"):
        thumb = item.get("ImageTags", {}).get("Primary", "") or ""

    item_id = (
        metadata.get("Id")
        or item.get("Id")
        or payload.get("ItemId")
        or payload.get("Id")
        or ""
    )

    return {
        "name": name,
        "type": item_type,
        "year": year,
        "path": path,
        "overview": overview[:200] if overview else "",
        "item_id": str(item_id) if item_id else "",
    }


def _extract_delete_paths(payload: dict) -> list:
    """
    从 Emby webhook payload 中提取被删媒体的路径列表（去重）。
    兼容：
    - payload.Path / Metadata.Path / Item.Path（单路径）
    - ItemIds（部分插件以 ItemIds 列表给出路径）
    - deep.delete 的 Description 中含多条路径（按行/逗号拆出）
    """
    paths: list = []

    def _add(p) -> None:
        p = (p or "").strip()
        if p and p not in paths:
            paths.append(p)

    # 单路径字段
    _add(payload.get("Path"))
    metadata = payload.get("Metadata") or payload.get("metadata") or {}
    if isinstance(metadata, dict):
        _add(metadata.get("Path"))
    item = payload.get("Item") or payload.get("item") or {}
    if isinstance(item, dict):
        _add(item.get("Path"))

    # ItemIds 列表
    item_ids = payload.get("ItemIds")
    if isinstance(item_ids, list):
        for p in item_ids:
            _add(p)

    # deep.delete Description：按行/逗号拆出多条路径
    description = payload.get("Description") or ""
    if isinstance(description, str) and description.strip():
        for seg in re.split(r"[\r\n,]+", description):
            seg = (seg or "").strip()
            if not seg:
                continue
            # 去掉 "Item Path:" 前缀（注意 Windows 路径含冒号，需用指定分隔符拆分）
            if "Item Path:" in seg:
                seg = seg.split("Item Path:", 1)[1].strip()
            elif seg.startswith("Path:") and not seg[5:6].isalpha():
                seg = seg[5:].strip()
            if not seg:
                continue
            # 过滤非路径段（说明文字/链接等），防误删
            if seg.startswith(("http://", "https://")):
                continue
            looks_like_path = (
                seg.startswith(("/", "\\", "./", "../"))
                or (len(seg) > 2 and seg[1] == ":" and seg[2] in "/\\")
                or ("/" in seg and "://" not in seg)
                or ("\\" in seg)
            )
            if looks_like_path:
                _add(seg)

    return paths


@router.post("/webhook")
@router.get("/webhook")
async def emby_webhook(
    request: Request,
    token: str = Query(default=""),
):
    """
    接收 Emby Webhook 推送。
    Emby 配置 Webhook URL 为: http://<host>:<port>/api/emby/webhook?token=<your_token>
    请求内容类型: application/json
    """
    # 验证 token
    notify_cfg = read_setting("emby_notify") or {}
    expected_token = (notify_cfg.get("webhook_token") or "").strip()

    if expected_token:
        if token != expected_token:
            logger.warning(f"[emby-webhook] token 验证失败")
            return JSONResponse(
                status_code=403,
                content={"code": 403, "message": "Token 验证失败", "data": None},
            )
    else:
        # 安全保护：未配置 token 时拒绝可能触发级联删除的 deep 事件
        # 仅允许无害的通知类事件（如 play/stop），阻止 delete 类事件
        logger.warning("[emby-webhook] 未配置 webhook_token，安全保护：拒绝请求（请先配置 token）")
        return JSONResponse(
            status_code=403,
            content={"code": 403, "message": "未配置 Webhook Token，请先在通知设置中配置 webhook_token", "data": None},
        )

    # 解析请求体
    body_bytes = await request.body()
    if not body_bytes:
        return JSONResponse(
            content={"code": 0, "message": "空请求体", "data": None}
        )

    payload = None
    content_type = request.headers.get("content-type", "").lower()

    # 尝试 JSON 解析
    if "json" in content_type or not content_type:
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            payload = None

    # 尝试 form-data 解析（Emby 有时会用 form 编码，data 字段含 JSON）
    if payload is None:
        try:
            form = await request.form()
            data_str = form.get("data") or form.get("payload") or ""
            if data_str:
                payload = json.loads(data_str)
        except Exception:
            pass

    if payload is None:
        logger.warning("[emby-webhook] 无法解析请求体")
        return JSONResponse(
            content={"code": 0, "message": "无法解析请求体", "data": None}
        )

    # 提取事件类型
    event = (
        payload.get("Event")
        or payload.get("event")
        or payload.get("NotificationType")
        or ""
    )
    event_lower = str(event).lower()

    logger.info(f"[emby-webhook] 收到事件: {event}, payload keys: {list(payload.keys())}")

    # ===== 删除事件分支：级联删除 115 网盘对应文件（在入库通知流程之前处理） =====
    # 判断是否为删除事件（deep.delete / item.removed / item.delete 等）
    is_delete_event = (
        "deep.delete" in event_lower
        or "item.removed" in event_lower
        or "delete" in event_lower
        or "removed" in event_lower
    )

    if is_delete_event:
        # 级联删除开关检查：禁用时事件不入队，避免无意义消费
        from app.services.mediasyncdel_service import get_mediasync_del_service
        if not get_mediasync_del_service().get_enabled():
            logger.info(f"[emby-webhook] 级联删除已禁用，忽略删除事件: {event}")
            return JSONResponse(
                content={"code": 0, "message": "级联删除已禁用", "data": None}
            )

        # 提取被删媒体信息（复用入库提取逻辑）
        info = _extract_item(payload) or {}
        # 提取被删媒体路径列表（兼容 Path / Metadata.Path / Item.Path / ItemIds /
        # deep.delete Description 按行/逗号拆出的多条路径）
        paths = _extract_delete_paths(payload)

        snapshot = {
            "event": event,
            "name": info.get("name", ""),
            "type": info.get("type", ""),
            "path": info.get("path", ""),
            "paths": paths,
            "payload": payload,
            "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 入队由后台 worker 异步处理，不阻塞 webhook 线程
        queued = get_mediasync_del_service().enqueue_event(snapshot)

        logger.info(
            f"[emby-webhook] 删除事件{'已入队' if queued else '入队失败（队列已满）'}: "
            f"{event}, 提取到 {len(paths)} 个路径"
        )
        return JSONResponse(
            content={"code": 0, "message": "删除事件已入队处理", "data": None}
        )

    # 检查是否启用了入库通知（仅约束入库通知流程，不影响上面的删除分支）
    if not notify_cfg.get("notify_on_emby_add", True):
        return JSONResponse(
            content={"code": 0, "message": "入库通知已关闭，跳过", "data": None}
        )

    # 只处理入库相关事件
    is_add_event = (
        event_lower in _ADD_EVENTS
        or "add" in event_lower
        or "added" in event_lower
    )

    if not is_add_event:
        # 非入库事件，直接返回成功但不发通知
        return JSONResponse(
            content={"code": 0, "message": f"忽略事件: {event}", "data": None}
        )

    # 提取媒体信息
    info = _extract_item(payload)
    if not info:
        return JSONResponse(
            content={"code": 0, "message": "无法提取媒体信息", "data": None}
        )

    # O7: 新入库的 STRM 媒体触发 Emby 原生媒体信息提取，使编码/分辨率徽章立即显示。
    # 仅对视频类条目（Movie/Episode/Video）处理，且需开关启用（默认关闭，避免风控）。
    try:
        emby_notify_cfg = read_setting("emby_notify") or {}
        if emby_notify_cfg.get("sync_media_info", False):
            item_type = info.get("type", "")
            item_path = info.get("path", "")
            is_video = item_type in ("Movie", "Episode", "Video") or item_path.lower().endswith(".strm")
            is_strm = item_path.lower().endswith(".strm")
            if is_video and (is_strm or item_type in ("Movie", "Episode")):
                from app.services.emby import EmbyClient
                emby_cfg = read_setting("emby") or {}
                _host = (emby_cfg.get("host", "") or "").strip()
                _key = emby_cfg.get("api_key", "")
                if _host and _key:
                    client = EmbyClient(_host, _key)
                    item_id = info.get("item_id", "")
                    if not item_id and item_path:
                        item_id = await client.get_item_id_by_path(item_path)
                    if item_id or item_path:
                        await client.sync_media_info_native(item_id=item_id, path=item_path)
    except Exception as e:
        logger.warning(f"[emby-webhook] O7 SyncMediaInfo 触发失败: {e}")

    # 发送通知
    try:
        from app.services.notification_service import NotificationService

        type_label = {
            "Movie": "电影",
            "Series": "剧集",
            "Episode": "剧集",
            "MusicAlbum": "音乐",
            "Audio": "音乐",
            "MusicVideo": "音乐视频",
            "BoxSet": "合集",
            "Book": "图书",
        }.get(info.get("type", ""), info.get("type", ""))

        title_text = info.get("name", "未知")
        if info.get("year"):
            title_text += f" ({info['year']})"

        content = f"> **Emby 新入库通知**\n\n"
        content += f"> 标题: **{title_text}**\n"
        if type_label:
            content += f"> 类型: {type_label}\n"
        if info.get("path"):
            content += f"> 路径: `{info['path']}`\n"
        if info.get("overview"):
            content += f"> 简介: {info['overview']}\n"

        await NotificationService.notify("Emby 入库通知", content)
        logger.info(f"[emby-webhook] 入库通知已发送: {title_text}")
    except Exception as e:
        logger.warning(f"[emby-webhook] 发送通知失败: {e}")

    return JSONResponse(
        content={"code": 0, "message": "通知已发送", "data": None}
    )


@router.get("/webhook-url", response_model=None)
async def get_webhook_url(request: Request):
    """获取 Emby Webhook URL（供前端显示）"""
    base_url = str(request.base_url)
    notify_cfg = read_setting("emby_notify") or {}
    token = (notify_cfg.get("webhook_token") or "").strip()
    webhook_url = f"{base_url}api/emby/webhook"
    if token:
        webhook_url += f"?token={token}"
    return {"code": 0, "data": {"webhook_url": webhook_url}}

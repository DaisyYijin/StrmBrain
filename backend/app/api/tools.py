"""
API 路由 - 特色工具
1. 媒体库封面生成（Emby）
2. 清空 115 文件夹
3. 清空 115 回收站
4. 替换 STRM 字符串
5. Emby 影视缺集管理
"""
import os
import re
import json
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from app.core.json_storage import read_setting, get_first_valid_account
from app.schemas import ApiResponse
from app.config import DATA_DIR
from app.services.client_115 import Client115Service

router = APIRouter(prefix="/api/tools", tags=["tools"])


# ============ 辅助函数 ============

def _get_default_account() -> Optional[dict]:
    """获取第一个有效的 115 账号（status === 1）"""
    return get_first_valid_account()


def _get_setting(key: str) -> dict:
    return read_setting(key)


def _get_emby_client():
    """获取 Emby 客户端"""
    data = _get_setting("emby")
    host = data.get("host", "").rstrip("/")
    api_key = data.get("api_key", "")
    if not host or not api_key:
        return None
    from app.services.emby import EmbyClient
    return EmbyClient(host, api_key)


# ============ 工具1：媒体库封面生成 ============

class CoverGenRequest(BaseModel):
    lib_id: str = ""  # 空则扫描所有库
    item_type: str = "Movie"  # Movie / Series


@router.post("/cover-gen/scan", response_model=ApiResponse)
async def cover_gen_scan(payload: CoverGenRequest):
    """扫描缺少封面的媒体条目"""
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    try:
        items = await client.get_items_missing_images(
            lib_id=payload.lib_id, item_type=payload.item_type
        )
        return ApiResponse(data={"items": items, "count": len(items)})
    except Exception as e:
        return ApiResponse(code=500, message=f"扫描失败: {str(e)}")


@router.post("/cover-gen/run", response_model=ApiResponse)
async def cover_gen_run(payload: CoverGenRequest):
    """
    执行封面生成：
    1. 扫描缺少封面的条目
    2. 通过 TMDB 搜索并下载封面图片
    3. 上传到 Emby
    """
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    from app.services.tmdb_service import TmdbService
    import httpx

    try:
        items = await client.get_items_missing_images(
            lib_id=payload.lib_id, item_type=payload.item_type
        )
    except Exception as e:
        return ApiResponse(code=500, message=f"扫描失败: {str(e)}")

    if not items:
        return ApiResponse(data={"total": 0, "success": 0, "failed": 0, "details": []})

    # 获取 TMDB 配置
    tmdb_settings = _get_setting("tmdb")
    tmdb_api_key = tmdb_settings.get("api_key", "")
    tmdb_image_domain = tmdb_settings.get("image_domain", "https://image.tmdb.org").rstrip("/")

    if not tmdb_api_key:
        return ApiResponse(code=400, message="请先在核心配置中设置 TMDB API Key")

    details = []
    success_count = 0
    failed_count = 0

    for item in items:
        item_name = item["name"]
        item_type = payload.item_type
        media_type = "tv" if item_type == "Series" else "movie"

        try:
            # 搜索 TMDB
            search_result = await TmdbService.search_media(item_name, media_type)

            if not search_result or not search_result.get("id"):
                details.append({"name": item_name, "status": "failed", "reason": "TMDB 未找到匹配"})
                failed_count += 1
                continue

            # 获取封面图片路径
            poster_path = search_result.get("poster_path", "")

            if not poster_path:
                details.append({"name": item_name, "status": "failed", "reason": "TMDB 无封面图片"})
                failed_count += 1
                continue

            # 下载图片
            image_url = f"{tmdb_image_domain}/t/p/original{poster_path}"
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                img_resp = await http_client.get(image_url)
                if img_resp.status_code != 200:
                    details.append({"name": item_name, "status": "failed", "reason": f"下载图片失败 (HTTP {img_resp.status_code})"})
                    failed_count += 1
                    continue
                image_data = img_resp.content

            # 上传到 Emby
            ok = await client.upload_image(item["id"], image_data, "Primary")
            if ok:
                details.append({"name": item_name, "status": "success"})
                success_count += 1
            else:
                details.append({"name": item_name, "status": "failed", "reason": "上传到 Emby 失败"})
                failed_count += 1

        except Exception as e:
            details.append({"name": item_name, "status": "failed", "reason": str(e)})
            failed_count += 1

    return ApiResponse(data={
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "details": details,
    })


# ---- 库封面（拼图） ----

class LibraryCoverRequest(BaseModel):
    lib_id: str = ""
    item_type: str = "Movie"          # Movie / Series
    sort_by: str = "DateCreated"      # DateCreated / SortName / PremiereDate / CommunityRating / Random
    sort_order: str = "Descending"    # Descending / Ascending
    title: str = ""                   # 封面上叠加的标题文字
    layout: str = "3x3"              # 3x3 / 2x2 / 4x3 / 1x5


class GenerateAllCoverRequest(BaseModel):
    style: str = "anime"                 # anime / starfield / featured / random
    sort_option: str = "latest_added"    # latest_added / latest_released / title_az / rating_high / random
    cron: str = ""                       # 定时执行（cron 表达式，可选）


# 排序选项映射
SORT_OPTIONS_MAP = {
    "latest_added":    {"sort_by": "DateCreated",     "sort_order": "Descending"},
    "latest_released": {"sort_by": "PremiereDate",   "sort_order": "Descending"},
    "title_az":        {"sort_by": "SortName",       "sort_order": "Ascending"},
    "rating_high":     {"sort_by": "CommunityRating","sort_order": "Descending"},
    "random":          {"sort_by": "Random",         "sort_order": "Descending"},
}

# 样式需要的图片数量
STYLE_NEED_COUNT = {
    "anime":     6,
    "starfield": 5,
    "featured":  1,
    "random":    6,
}

# 可选样式列表
STYLE_OPTIONS = [
    {"value": "anime",     "label": "样式一", "desc": "红色动漫风"},
    {"value": "starfield", "label": "样式二", "desc": "深蓝星空风"},
    {"value": "featured",  "label": "样式三", "desc": "深蓝单图风"},
    {"value": "random",    "label": "随机",  "desc": "混合三种风格"},
]

# 兼容旧单库端点的布局（3x3/2x2/4x3/1x5）图片数量
LAYOUT_COUNTS = {
    "3x3": 9,
    "2x2": 4,
    "4x3": 12,
    "1x5": 5,
}


@router.get("/cover-gen/libraries", response_model=ApiResponse)
async def cover_gen_libraries():
    """获取 Emby 媒体库列表（用于库封面生成下拉选择）"""
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    try:
        libs = await client.libraries()
        result = []
        for lib in libs:
            result.append({
                "id": lib.get("ItemId", ""),
                "name": lib.get("Name", ""),
                "type": lib.get("CollectionType", ""),
            })
        return ApiResponse(data={"libraries": result})
    except Exception as e:
        return ApiResponse(code=500, message=f"获取媒体库列表失败: {str(e)}")


@router.post("/cover-gen/library/scan", response_model=ApiResponse)
async def cover_gen_library_scan(payload: LibraryCoverRequest):
    """预览：获取将用于生成封面的 9 个条目"""
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    if not payload.lib_id:
        return ApiResponse(code=400, message="请选择媒体库")

    try:
        item_count = LAYOUT_COUNTS.get(payload.layout, 9)
        items = await client.get_library_items_sorted(
            lib_id=payload.lib_id,
            item_type=payload.item_type,
            sort_by=payload.sort_by,
            sort_order=payload.sort_order,
            limit=item_count,
        )
        from app.services.cover_gen import get_sort_label
        sort_label = get_sort_label(payload.sort_by, payload.sort_order)

        return ApiResponse(data={
            "items": items,
            "count": len(items),
            "sort_label": sort_label,
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"扫描失败: {str(e)}")


@router.post("/cover-gen/library/generate")
async def cover_gen_library_generate(payload: LibraryCoverRequest):
    """生成精美拼图封面并上传到 Emby 媒体库"""
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    if not payload.lib_id:
        return ApiResponse(code=400, message="请选择媒体库")

    from app.services.cover_gen import generate_library_cover, get_sort_label

    try:
        item_count = LAYOUT_COUNTS.get(payload.layout, 9)
        items = await client.get_library_items_sorted(
            lib_id=payload.lib_id,
            item_type=payload.item_type,
            sort_by=payload.sort_by,
            sort_order=payload.sort_order,
            limit=item_count,
        )
    except Exception as e:
        return ApiResponse(code=500, message=f"获取条目失败: {str(e)}")

    if not items:
        return ApiResponse(code=400, message="该库中没有带有封面的条目")

    sort_label = get_sort_label(payload.sort_by, payload.sort_order)
    title = payload.title or ""

    try:
        image_data, used_items = await generate_library_cover(
            items, title=title, sort_label=sort_label, layout=payload.layout,
        )
    except Exception as e:
        return ApiResponse(code=500, message=f"封面合成失败: {str(e)}")

    if not image_data:
        return ApiResponse(code=500, message="封面合成失败：无法下载海报图片")

    # 上传到 Emby 媒体库（作为 Primary 图片）
    ok = await client.upload_image(payload.lib_id, image_data, "Primary")

    return ApiResponse(data={
        "success": ok,
        "items": [{"name": it["name"], "year": it.get("year", "")} for it in used_items],
        "count": len(used_items),
        "sort_label": sort_label,
    })


@router.post("/cover-gen/library/preview")
async def cover_gen_library_preview(payload: LibraryCoverRequest):
    """生成封面预览（不上传到 Emby），返回 JPEG 图片"""
    from fastapi.responses import Response

    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    if not payload.lib_id:
        return ApiResponse(code=400, message="请选择媒体库")

    from app.services.cover_gen import generate_library_cover, get_sort_label

    try:
        item_count = LAYOUT_COUNTS.get(payload.layout, 9)
        items = await client.get_library_items_sorted(
            lib_id=payload.lib_id,
            item_type=payload.item_type,
            sort_by=payload.sort_by,
            sort_order=payload.sort_order,
            limit=item_count,
        )
    except Exception as e:
        return ApiResponse(code=500, message=f"获取条目失败: {str(e)}")

    if not items:
        return ApiResponse(code=400, message="该库中没有带有封面的条目")

    sort_label = get_sort_label(payload.sort_by, payload.sort_order)

    try:
        image_data, _ = await generate_library_cover(
            items, title=payload.title or "", sort_label=sort_label, layout=payload.layout,
        )
    except Exception as e:
        return ApiResponse(code=500, message=f"封面合成失败: {str(e)}")

    if not image_data:
        return ApiResponse(code=500, message="封面合成失败：无法下载海报图片")

    # 直接返回 JPEG 图片
    return Response(content=image_data, media_type="image/jpeg")


# ---- 一键生成所有库封面 ----

@router.get("/cover-gen/library/styles", response_model=ApiResponse)
async def cover_gen_styles():
    """返回可选样式列表（前端 2x2 卡片用）"""
    return ApiResponse(data={"styles": STYLE_OPTIONS})


@router.get("/cover-gen/library/style-preview/{style}")
async def cover_gen_style_preview(style: str):
    """返回某个样式的预览缩略图（用于前端展示）"""
    from fastapi.responses import Response
    from app.services.cover_gen import render_style_preview
    if style not in ("anime", "starfield", "featured", "random"):
        return ApiResponse(code=400, message=f"未知样式: {style}")
    img = render_style_preview(style)
    return Response(content=img, media_type="image/jpeg")


@router.post("/cover-gen/library/generate-all", response_model=ApiResponse)
async def cover_gen_generate_all(payload: GenerateAllCoverRequest):
    """遍历所有 Emby 媒体库，按选定样式自动生成封面并上传"""
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    from app.services.cover_gen import generate_library_cover, get_sort_label, _zh_to_en_subtitle

    sort_cfg = SORT_OPTIONS_MAP.get(payload.sort_option, SORT_OPTIONS_MAP["latest_added"])
    sort_by = sort_cfg["sort_by"]
    sort_order = sort_cfg["sort_order"]
    item_count = STYLE_NEED_COUNT.get(payload.style, 6)
    # featured 模式只取 1 张，但为了字幕提示可取前 12
    fetch_count = max(item_count, 6)

    # 获取所有媒体库
    try:
        libs = await client.libraries()
    except Exception as e:
        return ApiResponse(code=500, message=f"获取媒体库列表失败: {str(e)}")

    if not libs:
        return ApiResponse(code=400, message="Emby 中没有媒体库")

    results = []
    success_count = 0
    failed_count = 0

    for lib in libs:
        lib_id = lib.get("ItemId", "")
        lib_name = lib.get("Name", "")
        collection_type = lib.get("CollectionType", "")

        if not lib_id or not lib_name:
            continue

        # 根据库类型自动判断 item_type
        if collection_type == "movies":
            item_type = "Movie"
        elif collection_type == "tvshows":
            item_type = "Series"
        else:
            # 跳过非影视库（音乐、照片等）
            results.append({
                "name": lib_name,
                "status": "skipped",
                "reason": f"不支持的库类型: {collection_type or '未知'}",
            })
            continue

        try:
            items = await client.get_library_items_sorted(
                lib_id=lib_id,
                item_type=item_type,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=fetch_count,
            )
        except Exception as e:
            results.append({"name": lib_name, "status": "failed", "reason": f"获取条目失败: {str(e)}"})
            failed_count += 1
            continue

        if not items:
            results.append({"name": lib_name, "status": "skipped", "reason": "库中没有带封面的条目"})
            continue

        # 副标题按库名生成（如"动漫电影" → "ANIME MOVIE"）
        sub = _zh_to_en_subtitle(lib_name)

        try:
            image_data, used_items = await generate_library_cover(
                items, title=lib_name, subtitle=sub, style=payload.style,
                sort_by=sort_by, sort_order=sort_order,
            )
        except Exception as e:
            results.append({"name": lib_name, "status": "failed", "reason": f"封面合成失败: {str(e)}"})
            failed_count += 1
            continue

        if not image_data:
            results.append({"name": lib_name, "status": "failed", "reason": "无法下载海报图片"})
            failed_count += 1
            continue

        # 上传到 Emby
        ok = await client.upload_image(lib_id, image_data, "Primary")
        if ok:
            results.append({
                "name": lib_name,
                "status": "success",
                "count": len(used_items),
            })
            success_count += 1
        else:
            results.append({"name": lib_name, "status": "failed", "reason": "上传到 Emby 失败"})
            failed_count += 1

    return ApiResponse(data={
        "total": len(results),
        "success": success_count,
        "failed": failed_count,
        "skipped": len([r for r in results if r["status"] == "skipped"]),
        "results": results,
    })


@router.post("/cover-gen/library/preview-first")
async def cover_gen_preview_first(payload: GenerateAllCoverRequest):
    """预览：取第一个有效媒体库生成预览图（按所选样式）"""
    from fastapi.responses import Response

    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    from app.services.cover_gen import generate_library_cover, _zh_to_en_subtitle

    sort_cfg = SORT_OPTIONS_MAP.get(payload.sort_option, SORT_OPTIONS_MAP["latest_added"])
    sort_by = sort_cfg["sort_by"]
    sort_order = sort_cfg["sort_order"]
    fetch_count = max(STYLE_NEED_COUNT.get(payload.style, 6), 6)

    try:
        libs = await client.libraries()
    except Exception as e:
        return ApiResponse(code=500, message=f"获取媒体库列表失败: {str(e)}")

    # 找到第一个影视库
    for lib in libs:
        collection_type = lib.get("CollectionType", "")
        if collection_type not in ("movies", "tvshows"):
            continue

        lib_id = lib.get("ItemId", "")
        lib_name = lib.get("Name", "")
        item_type = "Movie" if collection_type == "movies" else "Series"

        try:
            items = await client.get_library_items_sorted(
                lib_id=lib_id,
                item_type=item_type,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=fetch_count,
            )
        except Exception:
            continue

        if not items:
            continue

        sub = _zh_to_en_subtitle(lib_name)

        try:
            image_data, _ = await generate_library_cover(
                items, title=lib_name, subtitle=sub, style=payload.style,
                sort_by=sort_by, sort_order=sort_order,
            )
        except Exception as e:
            return ApiResponse(code=500, message=f"封面合成失败: {str(e)}")

        if not image_data:
            return ApiResponse(code=500, message="封面合成失败：无法下载海报图片")

        return Response(content=image_data, media_type="image/jpeg")

    return ApiResponse(code=400, message="没有找到可用的影视媒体库")


# ============ 工具2：清空 115 文件夹 ============

class ClearFolderRequest(BaseModel):
    cid: str
    recursive: bool = True


@router.post("/clear-folder/scan", response_model=ApiResponse)
async def clear_folder_scan(payload: ClearFolderRequest):
    """扫描文件夹内容，返回文件列表和数量"""
    account = _get_default_account()
    if not account:
        return ApiResponse(code=404, message="没有可用的 115 账号")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    if not payload.cid:
        return ApiResponse(code=400, message="请选择要清空的文件夹")

    try:
        cookies = account.get("cookies", "")
        items = Client115Service.list_all_items(cookies, payload.cid, recursive=False)
        return ApiResponse(data={
            "items": items,
            "count": len(items),
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"扫描失败: {str(e)}")


@router.post("/clear-folder/run", response_model=ApiResponse)
async def clear_folder_run(payload: ClearFolderRequest):
    """清空 115 文件夹（删除文件夹下所有内容，不删除文件夹本身）"""
    account = _get_default_account()
    if not account:
        return ApiResponse(code=404, message="没有可用的 115 账号")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    if not payload.cid:
        return ApiResponse(code=400, message="请选择要清空的文件夹")

    try:
        cookies = account.get("cookies", "")
        # 先列出所有直接子项
        items = Client115Service.list_all_items(cookies, payload.cid, recursive=False)
        if not items:
            return ApiResponse(data={"deleted": 0, "message": "文件夹为空"})

        # 删除所有子项（文件和子目录）
        file_ids = [it["id"] for it in items if it["id"]]
        if not file_ids:
            return ApiResponse(data={"deleted": 0, "message": "没有可删除的项目"})

        # 分批删除，每批最多 500 个
        batch_size = 500
        total_deleted = 0
        errors = []
        for i in range(0, len(file_ids), batch_size):
            batch = file_ids[i:i + batch_size]
            resp = Client115Service.delete_files(cookies, batch)
            if isinstance(resp, dict) and resp.get("error"):
                errors.append(resp["error"])
            else:
                total_deleted += len(batch)

        return ApiResponse(data={
            "deleted": total_deleted,
            "total": len(file_ids),
            "errors": errors,
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"清空失败: {str(e)}")


# ============ 工具3：清空 115 回收站 ============

@router.post("/clear-recycle/run", response_model=ApiResponse)
async def clear_recycle_run():
    """清空 115 回收站"""
    account = _get_default_account()
    if not account:
        return ApiResponse(code=404, message="没有可用的 115 账号")

    if account.get("status") == 0:
        return ApiResponse(code=401, message="账号 cookies 已失效，请重新登录")

    try:
        cookies = account.get("cookies", "")
        resp = Client115Service.clean_recycle_bin(cookies)
        if isinstance(resp, dict) and resp.get("error"):
            return ApiResponse(code=500, message=f"清空回收站失败: {resp['error']}")
        # 115 API 成功返回 state: true 或类似结构
        state = resp.get("state", resp.get("success", False)) if isinstance(resp, dict) else False
        return ApiResponse(data={
            "success": True,
            "response": resp,
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"清空回收站失败: {str(e)}")


# ============ 工具4：替换 STRM 字符串 ============

class StrmReplaceRequest(BaseModel):
    directory: str  # 本地 STRM 文件目录
    find: str       # 要查找的字符串
    replace: str = ""  # 替换为的字符串
    recursive: bool = True


@router.post("/strm-replace/scan", response_model=ApiResponse)
async def strm_replace_scan(payload: StrmReplaceRequest):
    """扫描目录下的 .strm 文件，返回包含目标字符串的文件列表"""
    if not payload.directory:
        return ApiResponse(code=400, message="请输入 STRM 文件目录")
    if not payload.find:
        return ApiResponse(code=400, message="请输入要查找的字符串")

    root = Path(payload.directory)
    if not root.exists() or not root.is_dir():
        return ApiResponse(code=400, message="目录不存在或不是有效目录")

    try:
        matched_files = []
        strm_files = list(root.rglob("*.strm")) if payload.recursive else list(root.glob("*.strm"))

        for strm_path in strm_files:
            try:
                content = strm_path.read_text(encoding="utf-8")
                if payload.find in content:
                    matched_files.append({
                        "path": str(strm_path.relative_to(root)),
                        "preview": content[:200],
                    })
            except Exception:
                continue

        return ApiResponse(data={
            "matched": matched_files,
            "count": len(matched_files),
            "total_strm": len(strm_files),
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"扫描失败: {str(e)}")


@router.post("/strm-replace/run", response_model=ApiResponse)
async def strm_replace_run(payload: StrmReplaceRequest):
    """执行 STRM 字符串替换"""
    if not payload.directory:
        return ApiResponse(code=400, message="请输入 STRM 文件目录")
    if not payload.find:
        return ApiResponse(code=400, message="请输入要查找的字符串")

    root = Path(payload.directory)
    if not root.exists() or not root.is_dir():
        return ApiResponse(code=400, message="目录不存在或不是有效目录")

    try:
        replaced_files = []
        skipped = 0
        strm_files = list(root.rglob("*.strm")) if payload.recursive else list(root.glob("*.strm"))

        for strm_path in strm_files:
            try:
                content = strm_path.read_text(encoding="utf-8")
                if payload.find in content:
                    new_content = content.replace(payload.find, payload.replace)
                    strm_path.write_text(new_content, encoding="utf-8")
                    replaced_files.append({
                        "path": str(strm_path.relative_to(root)),
                        "old": content[:200],
                        "new": new_content[:200],
                    })
                else:
                    skipped += 1
            except Exception as e:
                replaced_files.append({
                    "path": str(strm_path.relative_to(root)),
                    "error": str(e),
                })

        return ApiResponse(data={
            "replaced": len([f for f in replaced_files if "error" not in f]),
            "skipped": skipped,
            "total": len(strm_files),
            "details": replaced_files,
        })
    except Exception as e:
        return ApiResponse(code=500, message=f"替换失败: {str(e)}")


# ============ 工具5：Emby 影视缺集管理 ============

class MissingEpRequest(BaseModel):
    lib_id: str = ""  # 空则扫描所有电视剧库


@router.post("/missing-ep/scan", response_model=ApiResponse)
async def missing_ep_scan(payload: MissingEpRequest):
    """
    扫描 Emby 电视剧库，对比 TMDB 数据，找出缺集。
    返回: [{"name", "year", "tmdb_id", "seasons": [{"season", "expected", "actual", "missing": [int]}]}]
    """
    client = _get_emby_client()
    if not client:
        return ApiResponse(code=400, message="请先在核心配置中设置 Emby 地址和 API Key")

    from app.services.tmdb_service import TmdbService

    # 检查 TMDB API Key
    tmdb_settings = _get_setting("tmdb")
    if not tmdb_settings.get("api_key"):
        return ApiResponse(code=400, message="请先在核心配置中设置 TMDB API Key")

    try:
        # 获取 Emby 中所有电视剧
        tv_shows = await client.get_tv_shows(lib_id=payload.lib_id)
    except Exception as e:
        return ApiResponse(code=500, message=f"获取电视剧列表失败: {str(e)}")

    if not tv_shows:
        return ApiResponse(data={"shows": [], "count": 0, "total_missing": 0})

    results = []
    total_missing = 0

    for show in tv_shows:
        show_name = show["name"]
        show_year = str(show["year"]) if show["year"] else ""
        emby_tmdb_id = show.get("tmdb_id", "")

        try:
            # 获取 Emby 中已有的季和集
            emby_seasons = await client.get_season_episodes(show["id"])

            # 如果有 TMDB ID，直接用；否则搜索
            if emby_tmdb_id:
                tmdb_id = int(emby_tmdb_id)
            else:
                search_result = await TmdbService.search_media(show_name, "tv")
                if not search_result or not search_result.get("id"):
                    results.append({
                        "name": show_name,
                        "year": show_year,
                        "tmdb_id": "",
                        "status": "tmdb_not_found",
                        "seasons": [],
                        "missing_count": 0,
                    })
                    continue
                tmdb_id = search_result["id"]

            # 获取 TMDB 电视剧详情（含季列表）
            tv_detail = await TmdbService._get_tv_detail(tmdb_id)
            if not tv_detail:
                results.append({
                    "name": show_name,
                    "year": show_year,
                    "tmdb_id": str(tmdb_id),
                    "status": "tmdb_detail_failed",
                    "seasons": [],
                    "missing_count": 0,
                })
                continue

            tmdb_seasons = tv_detail.get("seasons", [])
            show_missing = []
            show_missing_count = 0

            for tmdb_season in tmdb_seasons:
                season_num = tmdb_season.get("season_number", 0) or 0
                if season_num <= 0:
                    continue  # 跳过特别篇（Season 0）

                expected_count = tmdb_season.get("episode_count", 0) or 0
                if expected_count == 0:
                    continue

                # 找到 Emby 中对应季的集
                emby_season = next(
                    (s for s in emby_seasons if s["season"] == season_num), None
                )
                if emby_season:
                    actual_eps = set(emby_season["episodes"])
                else:
                    actual_eps = set()

                # 找出缺失的集
                missing_eps = [
                    i for i in range(1, expected_count + 1)
                    if i not in actual_eps
                ]

                if missing_eps:
                    show_missing.append({
                        "season": season_num,
                        "expected": expected_count,
                        "actual": len(actual_eps),
                        "missing": missing_eps,
                    })
                    show_missing_count += len(missing_eps)

            results.append({
                "name": show_name,
                "year": show_year,
                "tmdb_id": str(tmdb_id),
                "status": "ok",
                "seasons": show_missing,
                "missing_count": show_missing_count,
            })
            total_missing += show_missing_count

        except Exception as e:
            results.append({
                "name": show_name,
                "year": show_year,
                "tmdb_id": emby_tmdb_id,
                "status": f"error: {str(e)}",
                "seasons": [],
                "missing_count": 0,
            })

    # 只返回有缺集的
    shows_with_missing = [r for r in results if r.get("missing_count", 0) > 0]

    return ApiResponse(data={
        "shows": shows_with_missing,
        "all_scanned": len(results),
        "count": len(shows_with_missing),
        "total_missing": total_missing,
    })

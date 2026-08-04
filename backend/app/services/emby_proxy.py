"""
Emby 反代服务 — 参考 qmediasync emby302 方案

在独立端口上提供 Emby 反向代理，统一接管 Emby 的播放请求：

1. 拦截 PlaybackInfo 请求 → 代理到真实 Emby，改写 MediaSources：
   - 设置 SupportsDirectPlay/SupportsDirectStream=true，SupportsTranscoding=false
   - DirectStreamUrl 指向反代自身的 stream 接口，强制客户端直连播放（防止转码）
2. 拦截 stream/universal 请求 → 通过 PlaybackInfo 获取媒体真实 Path：
   - Path 是 .strm 文件 → 读取 STRM 内容 → 307 重定向到直链
   - Path 是本地视频 → 回源 Emby original 接口处理
3. 其他请求 → 透明代理回源 Emby

使用方式：
- Emby 客户端（或 Emby 设置中的服务器地址）改为 http://反代地址:端口
- 反代端口默认 6086，可在设置页修改
"""
import re
import threading
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, save_setting

logger = get_logger("app.services.emby_proxy")

# ===== 路由匹配规则（参考 qmediasync constant.go）=====

# PlaybackInfo 接口: /Items/{id}/PlaybackInfo
_REG_PLAYBACK_INFO = re.compile(r"(?i)^/.*items/([^/]+)/playbackinfo\??")

# 资源播放接口: /Videos/{id}/stream|universal|original
_REG_RESOURCE_STREAM = re.compile(r"(?i)^/.*(?:videos|audio)/([^/]+)/(stream|universal|original)(?:\.\w+)?\??")

# 资源下载接口: /Items/{id}/Download
_REG_ITEM_DOWNLOAD = re.compile(r"(?i)^/.*items/([^/]+)/download($|\?)")

# 字幕接口（回源处理）
_REG_SUBTITLES = re.compile(r"(?i)subtitles")

# ===== 反代服务状态（全局单例）=====

_proxy_state = {
    "server": None,       # uvicorn.Server 实例
    "thread": None,       # 后台线程
    "port": 0,            # 当前监听端口
    "running": False,     # 是否运行中
    "lock": threading.RLock(),  # 可重入锁（start_proxy 内部嵌套调用 get_status 不会死锁）
}

# 反代 FastAPI 应用
proxy_app = FastAPI(title="STRMhub Emby Proxy", docs_url=None, redoc_url=None, openapi_url=None)


# ===== 配置读取 =====

def _get_config() -> dict:
    """读取反代配置（含 Emby 配置）"""
    data = read_setting("emby_proxy")
    emby_data = read_setting("emby")
    emby_host = (emby_data.get("host", "") or "").strip()
    # 自动补全缺失的 http:// 协议前缀（用户可能只填 IP:端口）
    if emby_host and not emby_host.lower().startswith(("http://", "https://")):
        emby_host = "http://" + emby_host
    return {
        # 反代为内置功能，始终启用（历史配置即使存了 false 也强制为 True）
        "enabled": True,
        "port": int(data.get("port", 6086) or 6086),
        "emby_host": emby_host.rstrip("/"),
        "emby_api_key": (emby_data.get("api_key", "") or "").strip(),
    }


def _extract_api_key(request: Request, cfg: dict) -> str:
    """从 query 或 header 提取 Emby API Key"""
    # 1. query 参数
    key = request.query_params.get("api_key", "")
    if key:
        return key
    # 2. X-Emby-Token header
    key = request.headers.get("X-Emby-Token", "")
    if key:
        return key
    # 3. Authorization: MediaBrowser Token="xxx"
    auth = request.headers.get("Authorization", "")
    m = re.search(r'Token="?([^"]+)"?', auth)
    if m:
        return m.group(1)
    # 4. 使用配置的 API Key
    return cfg.get("emby_api_key", "")


def _strip_hop_headers(headers: dict) -> dict:
    """移除 hop-by-hop 头，避免转发冲突"""
    blocked = {
        "host", "content-length", "transfer-encoding", "connection",
        "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "upgrade",
    }
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


# ===== STRM 文件读取 =====

def _read_strm_file(path: str) -> Optional[str]:
    """
    读取 STRM 文件内容。
    兼容 Windows 路径、Linux 路径、nfs:// 协议前缀。
    返回文件内容（去除首尾空白），读取失败返回 None。
    """
    candidates: list[str] = []
    if path.startswith("nfs:"):
        # nfs://192.168.1.10/media/xxx.strm → 去掉主机部分
        stripped = re.sub(r"^nfs://[^/]+", "", path)
        candidates.append(stripped)
        candidates.append(path)
    else:
        candidates.append(path)
    for c in candidates:
        try:
            with open(c, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def _resolve_strm_target(content: str) -> str:
    """处理 STRM 内容，返回最终重定向地址"""
    target = content.strip()
    if not target:
        return ""
    # 仅对 115 域名直链追加 force=1 强制刷新（参考 qmediasync getFinalRedirectLink）
    # 判断：host 为 115 相关域名且路径含 /url 或 /newurl，且不含 smartstrm
    host_match = re.search(r"https?://([^/]+)", target)
    if host_match:
        host = host_match.group(1).lower()
        is_115 = "115.com" in host or "115cdn.com" in host or host.startswith("115")
        if is_115 and ("/url" in target or "/newurl" in target) and "smartstrm" not in target:
            if "force" not in target:
                sep = "&" if "?" in target else "?"
                target += sep + "force=1"
    return target


def _is_self_strm_url(target: str) -> bool:
    """判断 STRM 内容是否指向本服务的 302 播放接口（/api/115/url/）
    参考 emby2Alist redirectStrmLastLinkRule：STRM 内部链接指向自身反代/接口时，
    由服务器端跟随跳转获取最终直链，避免客户端访问不到内部地址（如 172.17.0.1）。
    """
    return "/api/115/url/" in target


async def _follow_strm_url(target: str) -> Optional[str]:
    """服务器端跟随 STRM 内部链接（参考 emby2Alist fetchLastLink）。
    当 STRM 内容指向本服务 302 接口时，由反代在服务器端请求主应用获取 115 直链，
    再返回给客户端。客户端拿到的是 115 CDN 直链，不接触内部地址（172.17.0.1 等）。
    返回最终直链 URL，失败返回 None。
    """
    if not _is_self_strm_url(target):
        return None
    # 提取路径部分（含 query），host 无关紧要（可能是 172.17.0.1 / 局域网IP / 域名）
    m = re.match(r"https?://[^/]+(/api/115/url/.*)$", target)
    if not m:
        return None
    path_with_query = m.group(1)
    from app.config import PORT as MAIN_PORT
    upstream = f"http://127.0.0.1:{MAIN_PORT}{path_with_query}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=10, pool=5)) as client:
            resp = await client.get(upstream, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if loc:
                return loc
        logger.warning(f"[proxy] 跟随 STRM 链接未返回重定向: {target[:80]} (HTTP {resp.status_code})")
    except Exception as e:
        logger.warning(f"[proxy] 跟随 STRM 链接失败 {target[:80]}: {e}")
    return None


# ===== Emby API 调用 =====

async def _get_emby_path(cfg: dict, item_id: str, media_source_id: str, api_key: str) -> str:
    """
    通过 Emby PlaybackInfo 接口获取媒体在 Emby 中的真实路径。
    返回路径字符串，失败返回空字符串。
    """
    if not cfg.get("emby_host") or not api_key:
        return ""
    params = {
        "api_key": api_key,
        "reqformat": "json",
        "IsPlayback": "false",
        "AutoOpenLiveStream": "false",
    }
    if media_source_id:
        params["MediaSourceId"] = media_source_id
    url = f"{cfg['emby_host']}/Items/{item_id}/PlaybackInfo"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5)) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(f"[proxy] PlaybackInfo 请求失败: {resp.status_code}")
                return ""
        data = resp.json()
        sources = data.get("MediaSources", []) or []
        if not media_source_id:
            return sources[0].get("Path", "") if sources else ""
        for src in sources:
            if src.get("Id") == media_source_id:
                return src.get("Path", "")
        return sources[0].get("Path", "") if sources else ""
    except Exception as e:
        logger.warning(f"[proxy] 获取媒体路径失败 {item_id}: {e}")
        return ""


# ===== 回源代理 =====

async def proxy_origin(request: Request):
    """透明代理请求到真实 Emby 服务器"""
    cfg = _get_config()
    emby_host = cfg.get("emby_host", "")
    if not emby_host:
        return JSONResponse(status_code=502, content={"detail": "Emby 未配置"})
    url = emby_host + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    body = None
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        body = await request.body()

    headers = _strip_hop_headers(dict(request.headers))
    # 需要保持 api_key 相关的 query 或 header

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.request(
                request.method,
                url,
                headers=headers,
                content=body,
                follow_redirects=False,
            )
        resp_headers = _strip_hop_headers(dict(resp.headers))
        # 302/307 重定向响应原样返回（保留 Location）
        if resp.status_code in (301, 302, 303, 307, 308):
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers,
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        logger.warning(f"[proxy] 回源失败 {request.url.path}: {e}")
        return JSONResponse(status_code=502, content={"detail": f"回源失败: {e}"})


# ===== 播放请求处理 =====

async def handle_playback_info(request: Request):
    """
    拦截 PlaybackInfo 请求：
    1. 转发到真实 Emby
    2. 改写 MediaSources，强制客户端直连播放（DirectStreamUrl 指向反代）
    """
    cfg = _get_config()
    m = _REG_PLAYBACK_INFO.match(request.url.path)
    if not m:
        return await proxy_origin(request)
    item_id = m.group(1)
    api_key = _extract_api_key(request, cfg)
    if not api_key:
        return await proxy_origin(request)

    emby_host = cfg.get("emby_host", "")
    if not emby_host:
        return JSONResponse(status_code=502, content={"detail": "Emby 未配置"})

    # 构造请求参数（保留原始 query 参数）
    params = {"api_key": api_key}
    for k, v in request.query_params.items():
        if k.lower() != "api_key":
            params[k] = v

    body = None
    if request.method in ("POST", "PUT"):
        body = await request.body()

    headers = _strip_hop_headers(dict(request.headers))
    headers.pop("Content-Length", None)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5)) as client:
            resp = await client.request(
                request.method,
                f"{emby_host}/Items/{item_id}/PlaybackInfo",
                params=params,
                headers=headers,
                content=body,
                follow_redirects=False,
            )
        if resp.status_code != 200:
            logger.warning(f"[proxy] PlaybackInfo 源响应异常: {resp.status_code}")
            return await proxy_origin(request)
        data = resp.json()
    except Exception as e:
        logger.warning(f"[proxy] PlaybackInfo 转发失败: {e}")
        return await proxy_origin(request)

    # 改写 MediaSources
    sources = data.get("MediaSources", []) or []
    for src in sources:
        src_id = src.get("Id", "")
        src["SupportsDirectPlay"] = True
        src["SupportsDirectStream"] = True
        src["SupportsTranscoding"] = False
        src.pop("TranscodingUrl", None)
        src.pop("TranscodingSubProtocol", None)
        src.pop("TranscodingContainer", None)
        # DirectStreamUrl 指向反代自身的 stream 接口（相对路径，客户端基于反代地址拼接）
        src["DirectStreamUrl"] = (
            f"/Videos/{item_id}/stream?MediaSourceId={src_id}"
            f"&api_key={api_key}&Static=true"
        )

    logger.info(f"[proxy] PlaybackInfo 改写完成: item={item_id}, sources={len(sources)}")
    return JSONResponse(content=data)


async def handle_stream(request: Request):
    """
    拦截 stream/universal/original 请求：
    1. 获取媒体在 Emby 中的真实路径
    2. 如果是 .strm 文件 → 读取内容 → 307 重定向到直链
    3. 否则回源 Emby 处理
    """
    # 字幕请求直接回源
    if _REG_SUBTITLES.search(request.url.path):
        return await proxy_origin(request)

    cfg = _get_config()
    m = _REG_RESOURCE_STREAM.match(request.url.path)
    if not m:
        return await proxy_origin(request)
    item_id = m.group(1)
    stream_type = m.group(2).lower()
    api_key = _extract_api_key(request, cfg)

    # original 请求直接回源（本地媒体走 Emby 原样输出）
    if stream_type == "original":
        return await proxy_origin(request)

    media_source_id = request.query_params.get("MediaSourceId", "")
    emby_path = await _get_emby_path(cfg, item_id, media_source_id, api_key)

    if not emby_path:
        logger.warning(f"[proxy] 播放请求未获取到媒体路径，回源: item={item_id}")
        return await proxy_origin(request)

    # 提取文件名（路径最后一段），用于日志展示
    file_name = re.sub(r"[/\\]", "/", emby_path).rsplit("/", 1)[-1]
    # 客户端信息（简化 UA 名称，便于日志辨认播放设备）
    ua = request.headers.get("User-Agent", "") or ""
    if "Infuse" in ua:
        client = "Infuse"
    elif "Jellyfin" in ua:
        client = "Jellyfin"
    elif "Emby" in ua or "EmbyWeb" in ua:
        client = "Emby"
    elif "Kodi" in ua:
        client = "Kodi"
    else:
        client = ua[:20] or "未知客户端"
    # 客户端 IP（直接连接反代时取 socket 地址；经主应用转发时取 X-Forwarded-For 首个地址）
    client_ip = ""
    if request.client:
        client_ip = request.client.host or ""
    if not client_ip:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            client_ip = fwd.split(",")[0].strip()
    play_label = f"[proxy] 302 播放: {file_name} (客户端: {client}, IP: {client_ip or '未知'})"

    # 判断是否为 STRM 文件
    if emby_path.lower().endswith(".strm"):
        content = _read_strm_file(emby_path)
        if content:
            target = _resolve_strm_target(content)
            if target:
                # 服务器端跟随：STRM 内容指向本服务 302 接口时，由反代请求主应用
                # 获取 115 直链，再 302 给客户端（参考 emby2Alist fetchLastLink）。
                # 客户端拿到的是 115 CDN 直链，不接触内部地址（172.17.0.1 等）。
                if _is_self_strm_url(target):
                    final_url = await _follow_strm_url(target)
                    if final_url:
                        logger.info(f"{play_label} -> 服务器端跟随 -> {final_url[:120]}")
                        response = RedirectResponse(url=final_url, status_code=302)
                        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                        response.headers["Pragma"] = "no-cache"
                        response.headers["Expires"] = "0"
                        response.headers["Referrer-Policy"] = "no-referrer"
                        return response
                    # 跟随失败则回源，让 Emby 处理（避免把不可达的内部地址给客户端）
                    logger.warning(f"[proxy] 服务器端跟随失败，回源: {play_label}")
                    return await proxy_origin(request)
                # 普通直链（如 115 CDN 等客户端可访问的地址）直接 307 跳转
                logger.info(f"{play_label} -> {target[:120]}")
                response = RedirectResponse(url=target, status_code=307)
                # 禁止缓存，避免过期直链被缓存
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
                return response
            logger.warning(f"[proxy] STRM 内容为空: {emby_path}")
        else:
            logger.warning(f"[proxy] 读取 STRM 文件失败（回源处理）: {emby_path}")
    else:
        logger.info(f"[proxy] 本地媒体回源: {file_name}")

    return await proxy_origin(request)


# ===== 路由注册 =====

async def proxy_websocket(websocket: WebSocket, path: str):
    """WebSocket 双向代理（Emby 客户端连接服务器必需）。
    客户端连反代 6086 时，把 WebSocket 升级请求转发到真实 Emby。
    使用 websockets 库实现双向透传，避免客户端连不上。
    """
    import websockets
    from urllib.parse import urlencode as _urlencode
    cfg = _get_config()
    emby_host = cfg.get("emby_host", "")
    if not emby_host:
        await websocket.close(code=1011, reason="Emby 未配置")
        return
    await websocket.accept()
    # 构造上游 WebSocket 地址（http→ws, https→wss）
    upstream = emby_host.replace("http://", "ws://").replace("https://", "wss://")
    upstream += websocket.url.path
    if websocket.url.query:
        upstream += "?" + websocket.url.query
    try:
        # 传递客户端的子协议（Emby 客户端可能指定）
        subprotocols = websocket.headers.get_all("sec-websocket-protocol") or None
        async with websockets.connect(upstream, subprotocols=subprotocols) as upstream_ws:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await upstream_ws.send(msg)
                except (WebSocketDisconnect, Exception):
                    pass
            async def upstream_to_client():
                try:
                    while True:
                        msg = await upstream_ws.recv()
                        await websocket.send_text(msg)
                except (WebSocketDisconnect, Exception):
                    pass
            import asyncio as _asyncio
            await _asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as e:
        logger.warning(f"[proxy] WebSocket 代理失败 {path[:60]}: {e}")
        try:
            await websocket.close()
        except Exception:
            pass


@proxy_app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def proxy_catch_all(path: str, request: Request):
    """反代入口：按规则分发请求"""
    full_path = "/" + path if not path.startswith("/") else path

    # 健康检查（独立路径，不占用根路径；根路径回源 Emby 保持客户端兼容）
    if full_path == "/proxy/health":
        return Response(content="STRMhub Emby Proxy OK", status_code=200)

    # STRMhub 302 下载接口 → 转发到主应用（/api/115/url/...）
    # 场景：STRM 内容指向反代端口时，客户端经反代访问 302 接口
    if full_path.startswith("/api/115/url/"):
        return await proxy_to_main_app(request)

    # PlaybackInfo → 改写
    if _REG_PLAYBACK_INFO.match(full_path):
        return await handle_playback_info(request)

    # stream/universal/original → 读 STRM 重定向或回源
    if _REG_RESOURCE_STREAM.match(full_path):
        return await handle_stream(request)

    # 下载接口 → 与 stream 同样处理
    if _REG_ITEM_DOWNLOAD.match(full_path):
        m = _REG_ITEM_DOWNLOAD.match(full_path)
        item_id = m.group(1)
        # 下载请求直接回源（Emby 会按需处理，或返回原始文件）
        return await proxy_origin(request)

    # 其余请求回源 Emby
    return await proxy_origin(request)


async def proxy_to_main_app(request: Request):
    """
    将请求转发到 STRMhub 主应用（同进程，127.0.0.1:主端口）。
    用于处理 /api/115/url/ 302 下载接口：反代收到 STRM 内容指向自身的
    URL 时，转发到主应用获取 115 直链。
    """
    from app.config import PORT as MAIN_PORT
    target = f"http://127.0.0.1:{MAIN_PORT}{request.url.path}"
    if request.url.query:
        target += "?" + request.url.query

    body = None
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        body = await request.body()

    headers = _strip_hop_headers(dict(request.headers))
    headers.pop("Content-Length", None)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=10, pool=5)) as client:
            resp = await client.request(
                request.method,
                target,
                headers=headers,
                content=body,
                follow_redirects=False,
            )
        # 302 重定向响应原样返回（保留 Location 指向 115 直链）
        resp_headers = _strip_hop_headers(dict(resp.headers))
        if resp.status_code in (301, 302, 303, 307, 308):
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers,
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        logger.warning(f"[proxy] 转发主应用失败 {request.url.path}: {e}")
        return JSONResponse(status_code=502, content={"detail": f"转发主应用失败: {e}"})


# WebSocket 代理路由：Emby 客户端连接服务器必须建立 WebSocket，
# 反代把 /embywebsocket 和 /socket 的升级请求转发到真实 Emby
@proxy_app.websocket("/{path:path}")
async def proxy_ws_route(websocket: WebSocket, path: str):
    await proxy_websocket(websocket, path)


# ===== 反代服务生命周期管理 =====

def get_status() -> dict:
    """获取反代服务状态"""
    with _proxy_state["lock"]:
        cfg = _get_config()
        return {
            "enabled": cfg.get("enabled", True),
            "port": cfg.get("port", 6086),
            "running": _proxy_state["running"],
            "emby_configured": bool(cfg.get("emby_host")),
            "current_port": _proxy_state["port"],
        }


def save_config(enabled: bool, port: int) -> dict:
    """保存配置并应用（变更端口时自动重启）
    反代为内置功能，始终启用（忽略传入的 enabled，强制 True）
    """
    save_setting("emby_proxy", {"enabled": True, "port": int(port)})
    # 应用配置：如果运行中则重启，否则按需启动
    with _proxy_state["lock"]:
        was_running = _proxy_state["running"]
        if was_running:
            _stop_proxy_locked()
    if was_running:
        start_proxy()
    else:
        start_proxy()
    return get_status()


def start_proxy() -> dict:
    """
    启动反代服务（非阻塞模式）。
    后台线程运行 uvicorn，主线程立即返回，绝不阻塞主应用启动。
    log_config=None 防止子线程 uvicorn 重配全局日志系统（避免与主应用冲突）。
    注意：内部返回状态时使用 _state_snapshot() 避免锁内嵌套调用 get_status。
    """
    with _proxy_state["lock"]:
        cfg = _get_config()
        if not cfg.get("enabled"):
            return _state_snapshot()
        if not cfg.get("emby_host") or not cfg.get("emby_api_key"):
            logger.warning("[proxy] Emby 未配置完整，反代服务未启动")
            return _state_snapshot()
        if _proxy_state["running"]:
            return _state_snapshot()

        port = cfg.get("port", 6086)
        try:
            import uvicorn
            config = uvicorn.Config(
                proxy_app,
                host="0.0.0.0",
                port=port,
                log_level="warning",
                log_config=None,      # 关键：不重配全局日志，避免与主应用日志系统冲突
                access_log=False,     # 反代请求日志不输出，避免刷屏
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=_run_server_safe, args=(server,),
                daemon=True, name="emby-proxy",
            )
            thread.start()
            _proxy_state["server"] = server
            _proxy_state["thread"] = thread
            _proxy_state["port"] = port
            _proxy_state["running"] = True
            logger.info(f"[proxy] Emby 反代服务已启动，端口 {port}")
        except Exception as e:
            logger.warning(f"[proxy] 反代服务启动失败: {e}")
            _proxy_state["running"] = False
        return _state_snapshot()


def _state_snapshot() -> dict:
    """构造状态快照（不获取锁，仅供已持锁的调用方使用）"""
    cfg = _get_config()
    return {
        "enabled": cfg.get("enabled", True),
        "port": cfg.get("port", 6086),
        "running": _proxy_state["running"],
        "emby_configured": bool(cfg.get("emby_host")),
        "current_port": _proxy_state["port"],
    }


def _run_server_safe(server):
    """在子线程中运行 uvicorn Server，捕获所有异常防止静默崩溃"""
    try:
        server.run()
    except Exception as e:
        logger.warning(f"[proxy] 反代服务线程异常退出: {e}")
        # 线程退出后重置状态
        with _proxy_state["lock"]:
            if _proxy_state["server"] is server:
                _proxy_state["running"] = False
                _proxy_state["server"] = None
                _proxy_state["port"] = 0


def _stop_proxy_locked():
    """停止反代服务（调用方需持有锁）"""
    server = _proxy_state["server"]
    if server:
        server.should_exit = True
    _proxy_state["running"] = False
    _proxy_state["server"] = None
    _proxy_state["port"] = 0
    logger.info("[proxy] Emby 反代服务已停止")


def stop_proxy() -> dict:
    """停止反代服务"""
    with _proxy_state["lock"]:
        _stop_proxy_locked()
    return get_status()

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
import time as _time
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

# P3: Items 详情接口: /Items/{id}（纯详情结尾，用于 PlaybackInfo 缓存覆盖）
_REG_ITEMS = re.compile(r"(?i)^/.*items/([^/]+)$")

# P4: 播放会话接口: /Sessions/Playing/{Stopped|Progress}（播放进度辅助）
_REG_SESSIONS_PLAYING = re.compile(r"(?i)^/.*sessions/playing/(stopped|progress)$")

# ===== A2: 固定 DeviceProfile（防转码增强）=====
# 向 Emby 声明客户端支持所有常见容器/编码，从源头阻止 Emby 决定转码。
# 参考 embyExternalUrl 的 DirectPlayProfile 配置和 qmediasync 的 PlaybackInfo 拦截策略。
_FIXED_DEVICE_PROFILE = {
    "DirectPlayProfiles": [
        # 视频全容器 + 全编码直连
        {"Container": "mp4,m4v,mkv,avi,mov,wmv,flv,ts,m2ts,rmvb,iso,webm,3gp,mpg,mpeg,vob,wtv", "Type": "Video"},
    ],
    "TranscodingProfiles": [],  # 空 = 不允许转码
    "CodecProfiles": [],        # 无编码限制（不按分辨率/位深触发转码）
    "SubtitleProfiles": [
        {"Format": "srt", "Method": "External"},
        {"Format": "ass", "Method": "External"},
        {"Format": "ssa", "Method": "External"},
        {"Format": "vtt", "Method": "External"},
        {"Format": "sub", "Method": "External"},
        {"Format": "srt", "Method": "Embed"},
        {"Format": "ass", "Method": "Embed"},
        {"Format": "ssa", "Method": "Embed"},
        {"Format": "pgs", "Method": "Embed"},
        {"Format": "pgssub", "Method": "Embed"},
    ],
}

# ===== A3 + P3: PlaybackInfo 缓存空间 =====
# 缓存 Emby PlaybackInfo 完整响应（以 item_id 为键），降低 Emby 请求放大。
# P3 增强（参考 qmediasync useCacheSpacePlaybackInfo / LoadCacheItems）：
# - 无 MediaSourceId 请求：命中直接返回完整改写数据
# - 带 MediaSourceId 请求：从全量缓存筛出对应源并移到最前
# - Items 接口覆盖：用缓存空间的改写版本覆盖响应（防止转码源列表丢失）
_playback_info_cache: dict[str, dict] = {}  # item_id -> {"data": json, "ts": float}
_playback_info_lock = threading.Lock()
_PLAYBACK_INFO_TTL = 120  # 2 分钟（媒体库变更频率低，2 分钟足够）

# ===== A4: 声明式 Expired 缓存中间件 =====
# 通用响应缓存：按路径模式声明 TTL，命中缓存直接返回，减少回源请求。
# 示例：302 结果缓存 10min、字幕缓存 30 天、图片缓存 1 天
_response_cache: dict[str, dict] = {}  # cache_key -> {"content": bytes, "status": int, "headers": dict, "ts": float, "ttl": float}
_response_cache_lock = threading.Lock()

# 声明式缓存规则：路径正则 -> TTL（秒）
# 匹配到的请求缓存响应体，未匹配的不缓存
_CACHE_RULES: list[tuple[re.Pattern, float]] = [
    # 字幕请求缓存 30 天（2592000 秒）
    (re.compile(r"(?i)/Videos/[^/]+/[^/]*sub"), 2592000),
    # 图片请求缓存 1 天（86400 秒）— Images/Items/{id}/...
    (re.compile(r"(?i)/Items/[^/]+/Images/"), 86400),
    # 302 重定向结果缓存 10 分钟（600 秒）— stream/universal 请求
    # 注意：仅在 Range 代理和 302 回退都失败时才考虑缓存
]


def _get_cache_ttl(path: str) -> float:
    """检查路径是否匹配缓存规则，返回 TTL（秒），不匹配返回 0"""
    for pattern, ttl in _CACHE_RULES:
        if pattern.search(path):
            return ttl
    return 0


# ===== P5: 缓存键忽略参数表 =====
# 参考 qmediasync emby302/web/cache/cache.go CacheKeyIgnoreParams：
# 生成缓存键时忽略这些会话/鉴权相关参数（小写比较），
# 其余参数（如 MediaSourceId）保留并按参数名排序拼接，
# 保证同一资源的不同会话参数命中同一缓存。
_CACHE_KEY_IGNORE_PARAMS = {
    "starttimeticks", "x-playback-session-id", "playsessionid", "range",
    "x-emby-client", "x-emby-device-name", "x-emby-device-id",
    "x-emby-client-version", "x-mediabrowser-token", "x-ms-token",
    "deviceid", "userid", "itemid",
}


def _cache_key(request: Request) -> str:
    """生成缓存键（路径 + 查询参数）。
    P5: 跳过 _CACHE_KEY_IGNORE_PARAMS 黑名单中的参数（小写比较），
    其余参数按名排序后拼接，保证相同资源不同会话参数命中同一缓存。
    """
    path = request.url.path
    if not request.url.query:
        return path
    # 解析 query 为 (k, v) 列表，过滤黑名单参数（小写比较），排序后拼接
    pairs = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k.lower() not in _CACHE_KEY_IGNORE_PARAMS
    ]
    pairs.sort(key=lambda kv: (kv[0].lower(), kv[0], kv[1]))
    encoded = urlencode(pairs)
    return path + ("?" + encoded if encoded else "")


def _get_cached_response(key: str) -> Optional[dict]:
    """获取缓存的响应，过期返回 None"""
    with _response_cache_lock:
        entry = _response_cache.get(key)
        if not entry:
            return None
        if _time.time() - entry["ts"] > entry["ttl"]:
            del _response_cache[key]
            return None
        return entry
    return None


def _set_cached_response(key: str, content: bytes, status: int, headers: dict, ttl: float):
    """设置缓存响应"""
    with _response_cache_lock:
        # 限制缓存条目数量，防止内存泄漏
        if len(_response_cache) > 5000:
            # 清理过期条目
            now = _time.time()
            expired = [k for k, v in _response_cache.items() if now - v["ts"] > v["ttl"]]
            for k in expired:
                del _response_cache[k]
        _response_cache[key] = {
            "content": content, "status": status, "headers": headers,
            "ts": _time.time(), "ttl": ttl,
        }


# ===== P3: MediaSources 改写辅助函数 =====
# 供 handle_playback_info 与 handle_items 共用，保证改写逻辑一致。
# 参考 qmediasync useCacheSpacePlaybackInfo：带 MediaSourceId 时将选中源移到最前。

def _rewrite_playback_data(data: dict, item_id: str, api_key: str, media_source_id: str = "",
                           request_host: str = "") -> dict:
    """改写 PlaybackInfo/Items 数据中的 MediaSources：
    - 设置 DirectPlay/DirectStream=true, Transcoding=false
    - 删除 TranscodingUrl/Path（STRM 内部 URL 不外泄）
    - DirectStreamUrl 指向反代 stream 接口
    - 若指定 media_source_id，将选中源移到最前（参考 qmediasync useCacheSpacePlaybackInfo）
    - P1: 注入外部播放器 ExternalUrls（request_host 为反代自身 host，供客户端拼接绝对地址）
    - 注入固定 DeviceProfile（A2）
    """
    import copy as _copy
    data = _copy.deepcopy(data)
    sources = data.get("MediaSources", []) or []

    # 带 MediaSourceId 时：将选中源移到最前，保证客户端优先使用
    if media_source_id:
        for idx, src in enumerate(sources):
            if src.get("Id") == media_source_id:
                if idx > 0:
                    sources.insert(0, sources.pop(idx))
                break

    for src in sources:
        src_id = src.get("Id", "")
        src["SupportsDirectPlay"] = True
        src["SupportsDirectStream"] = True
        src["SupportsTranscoding"] = False
        src.pop("TranscodingUrl", None)
        src.pop("TranscodingSubProtocol", None)
        src.pop("TranscodingContainer", None)
        # 删除 Path 字段：STRM 内容是内部 URL（如 http://172.17.0.1:6060/...），
        # 客户端看到 HTTP 形式的 Path 会尝试 DirectPlay 直连该 URL。
        # 删除后客户端只能走 DirectStreamUrl → 反代 stream 接口 → 服务器端跟随获取 CDN 直链。
        path = src.get("Path", "")
        if path and (path.lower().endswith(".strm") or "pickcode=" in path or "account_id=" in path):
            src.pop("Path", None)
        # DirectStreamUrl 指向反代自身的 stream 接口（相对路径，客户端基于反代地址拼接）
        src["DirectStreamUrl"] = (
            f"/Videos/{item_id}/stream?MediaSourceId={src_id}"
            f"&api_key={api_key}&Static=true"
        )
        # P1: 注入外部播放器 ExternalUrls（仅 STRM 源）
        _inject_external_urls(src, item_id, api_key, request_host)

    data["MediaSources"] = sources
    # A2: 注入固定 DeviceProfile，双重保险防转码
    data["Profile"] = _FIXED_DEVICE_PROFILE
    return data


# ===== P1: 外部播放器 URL 注入 =====
# 参考 embyExternalUrl-main\embyAddExternalUrl\nginx\conf.d\externalUrl.js：
# 为每个 MediaSource 注入 ExternalUrls 数组（15 款播放器的调用协议），
# 播放地址经 /redirect2external 中转（base64url 编码），避免特殊字符破坏协议链接。
# 仅对 STRM/内部 URL 源注入（含 pickcode 或 Path 以 .strm 结尾）。
_EXTERNAL_PLAYER_URLS = {
    "PotPlayer": lambda u: f"potplayer://{u}",
    "VLC": lambda u: f"vlc://{u}",
    "IINA": lambda u: f"iina://weblink?url={u}",
    "Infuse": lambda u: f"infuse://x-callback-url/play?url={u}",
    "MX Player": lambda u: f"mxplayer://play?url={u}",
    "NPlayer": lambda u: f"nplayer-{u}",
    "MPV": lambda u: f"mpv://open?url={u}",
    "Fileball": lambda u: f"fileball://play?url={u}",
    "弹弹play": lambda u: f"dandanplay://play?url={u}",
    "网页播放": lambda u: u,
}


def _b64url_encode(data: str) -> str:
    """URL 安全的 base64 编码（无填充）"""
    import base64 as _b64
    return _b64.urlsafe_b64encode(data.encode("utf-8")).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> str:
    """URL 安全的 base64 解码（兼容无填充）"""
    import base64 as _b64
    padding = "=" * (-len(data) % 4)
    try:
        return _b64.urlsafe_b64decode(data + padding).decode("utf-8")
    except Exception:
        return ""


def _get_request_host(request: Request) -> str:
    """获取客户端访问反代时的绝对地址前缀（scheme://host[:port]），无尾部斜杠"""
    try:
        base = str(request.base_url).rstrip("/")
        return base
    except Exception:
        return ""


def _inject_external_urls(src: dict, item_id: str, api_key: str, request_host: str) -> None:
    """为单个 MediaSource 注入外部播放器 ExternalUrls（仅 STRM/内部 URL 源）"""
    # 判断是否为 STRM 源：Path 以 .strm 结尾，或 URL 含 pickcode/account_id
    path = src.get("Path", "") or ""
    is_strm = path.lower().endswith(".strm") or "pickcode=" in path or "account_id=" in path
    if not is_strm:
        return

    # 播放地址：优先 DirectStreamUrl（反代自身 stream 接口），否则用源 Path
    direct_url = src.get("DirectStreamUrl", "") or path
    if not direct_url:
        return

    # 拼上反代自身 host，形成客户端可访问的绝对地址
    play_url = direct_url
    if direct_url.startswith("/") and request_host:
        play_url = f"{request_host}{direct_url}"

    # 生成 ExternalUrls：经 /redirect2external 中转（base64url），再套各播放器协议
    external_urls: list[dict] = []
    for player_name, builder in _EXTERNAL_PLAYER_URLS.items():
        redirect_url = f"/redirect2external?url={_b64url_encode(play_url)}"
        external_urls.append({"Name": player_name, "Url": builder(redirect_url)})
    src["ExternalUrls"] = external_urls


# ===== A10 + P6: UA 兼容矩阵 =====
# 特定客户端需要特殊处理才能正常播放：
# - Infuse: 不跟随 302 重定向 → 优先使用 Range 代理
# - dandanplay: Range 请求格式特殊 → 需要修正 Range 头
# - VidHub/SenPlayer: seek 存在偏移 bug → 标记 need_seek_fix（简化：不做实际修正）
# - Infuse-Download: 下载场景 → 标记 block_download（简化：仅记录日志）
# - 其他客户端: 标准处理
# P6 参考 embyExternalUrl UA.txt 与 config/constant-common.js strHead.xUAs 扩充分类
_UA_COMPAT_MATRIX = {
    # Infuse-Download（下载场景，需放在 infuse 之前匹配）
    "infuse_download": {
        "keywords": ["infuse-download"],
        "force_range_proxy": False,
        "fix_range_header": False,
        "block_download": True,       # 下载场景标记
    },
    # Infuse 主客户端
    "infuse": {
        "keywords": ["infuse"],
        "force_range_proxy": True,    # 强制使用 Range 代理（不跟随 302）
        "fix_range_header": False,
    },
    # 弹弹play：Range 请求格式特殊
    "dandanplay": {
        "keywords": ["dandanplay", "dandan"],
        "force_range_proxy": False,
        "fix_range_header": True,     # 修正 dandanplay 的 Range 请求格式
    },
    # VLC
    "vlc": {
        "keywords": ["vlc", "videolan"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # Kodi
    "kodi": {
        "keywords": ["kodi"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # Fileball（iOS 第三方客户端，标准处理）
    "fileball": {
        "keywords": ["fileball"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # VidHub（iOS，seek 偏移 bug → 标记 need_seek_fix）
    "vidhub": {
        "keywords": ["vidhub"],
        "force_range_proxy": False,
        "fix_range_header": False,
        "need_seek_fix": True,
    },
    # SenPlayer（同 VidHub 处理）
    "senplayer": {
        "keywords": ["senplayer"],
        "force_range_proxy": False,
        "fix_range_header": False,
        "need_seek_fix": True,
    },
    # MX Player
    "mxplayer": {
        "keywords": ["mxplayer", "mx player"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # PotPlayer
    "potplayer": {
        "keywords": ["potplayer"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # Jellyfin（官方客户端，标准处理；放在 emby 之前防止 UA 交叉匹配）
    "jellyfin": {
        "keywords": ["jellyfin"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # Emby 官方客户端（含 EmbyTheater/EmbyWeb/Emby for iOS 等）
    "emby": {
        "keywords": ["emby"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
    # 浏览器 / AppleCoreMedia / QQ 内置播放
    "browser": {
        "keywords": ["applecoremedia", "mqqbrowser", "qq/", "chrome", "firefox", "safari"],
        "force_range_proxy": False,
        "fix_range_header": False,
    },
}


def _detect_client_type(ua: str) -> str:
    """从 User-Agent 检测客户端类型，返回匹配的客户端名或 'default'"""
    ua_lower = ua.lower()
    for client_name, config in _UA_COMPAT_MATRIX.items():
        for kw in config["keywords"]:
            if kw in ua_lower:
                return client_name
    return "default"


# 客户端兼容配置默认值（P6: 新增 need_seek_fix / block_download 字段）
_DEFAULT_CLIENT_COMPAT = {
    "force_range_proxy": False,
    "fix_range_header": False,
    "need_seek_fix": False,
    "block_download": False,
}


def _get_client_compat(ua: str) -> dict:
    """获取客户端兼容配置（合并默认值，始终包含 block_download/need_seek_fix 字段）"""
    client_type = _detect_client_type(ua)
    config = _UA_COMPAT_MATRIX.get(client_type) if client_type != "default" else None
    merged = {**_DEFAULT_CLIENT_COMPAT}
    if config:
        merged.update(config)
    merged["client_type"] = client_type
    return merged


def _fix_range_header(range_header: str) -> str:
    """修正 dandanplay 等客户端的 Range 请求格式
    dandanplay 有时发送 'Range: bytes=0-' 不带结束值，某些 CDN 需要完整格式。
    """
    if not range_header:
        return range_header
    # dandanplay 的 Range 格式通常正确，但有时缺少 bytes= 前缀
    if not range_header.lower().startswith("bytes=") and range_header.startswith("0-"):
        return f"bytes={range_header}"
    return range_header


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
    # 反代端口优先级：环境变量 PROXY_PORT > 配置文件 > 默认 6086
    # Docker 部署时可通过环境变量直接指定，无需在页面配置
    import os as _os
    _env_port = (_os.getenv("PROXY_PORT", "") or "").strip()
    try:
        port = int(_env_port) if _env_port else int(data.get("port", 6086) or 6086)
    except (TypeError, ValueError):
        port = 6086
    return {
        # 反代为内置功能，始终启用（历史配置即使存了 false 也强制为 True）
        "enabled": True,
        "port": port,
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


# 播放日志去重：同一文件的播放请求在去重窗口内只记录一次。
# 播放器（AfuseKt/Infuse 等）启动时会并发/连续发起多个 stream 请求
# （预探测、分段拉取、seek），全部打印会刷屏。
_play_log_cache: dict[str, float] = {}
_play_log_lock = threading.Lock()
_PLAY_LOG_DEDUP_SECONDS = 10.0  # 10 秒内相同播放请求只记录一次


def _should_log_play(play_label: str) -> bool:
    """判断是否应该记录该播放请求日志（去重）"""
    with _play_log_lock:
        now = _time.time()
        last = _play_log_cache.get(play_label, 0)
        return (now - last) >= _PLAY_LOG_DEDUP_SECONDS


def _mark_play_logged(play_label: str) -> None:
    """标记播放请求已记录"""
    with _play_log_lock:
        _play_log_cache[play_label] = _time.time()
        # 清理过期条目，防止内存泄漏
        if len(_play_log_cache) > 2000:
            cutoff = _time.time() - _PLAY_LOG_DEDUP_SECONDS
            expired = [k for k, v in _play_log_cache.items() if v < cutoff]
            for k in expired:
                _play_log_cache.pop(k, None)


# A1: Range 分片流代理 — 每账号并发连接限流
# 防止同一账号的播放请求过多导致 115 风控
_account_proxy_slots: dict[int, int] = {}  # account_id -> current concurrent count
_account_proxy_lock = threading.Lock()
_MAX_CONCURRENT_PER_ACCOUNT = 3  # 每账号最多 3 个并发代理流

# CDN 直链失效缓存：pickcode -> expiry timestamp
# 避免短时间内反复用已失效的直链重试
_link_expiry_cache: dict[str, float] = {}
_link_expiry_lock = threading.Lock()
_LINK_EXPIRY_SECONDS = 60  # 标记失效后 60 秒内不重试该链接


def _acquire_proxy_slot(account_id: int) -> bool:
    """尝试获取代理并发槽位，成功返回 True，超过上限返回 False"""
    with _account_proxy_lock:
        current = _account_proxy_slots.get(account_id, 0)
        if current >= _MAX_CONCURRENT_PER_ACCOUNT:
            return False
        _account_proxy_slots[account_id] = current + 1
        return True


def _release_proxy_slot(account_id: int):
    """释放代理并发槽位"""
    with _account_proxy_lock:
        current = _account_proxy_slots.get(account_id, 0)
        if current > 0:
            _account_proxy_slots[account_id] = current - 1


async def _follow_strm_url(target: str, client_ua: str = "") -> Optional[str]:
    """服务器端跟随 STRM 内部链接（参考 emby2Alist fetchLastLink）。
    当 STRM 内容指向本服务 302 接口时，由反代在服务器端请求主应用获取 115 直链，
    再返回给客户端。客户端拿到的是 115 CDN 直链，不接触内部地址（172.17.0.1 等）。
    携带客户端 UA 换取直链：115 要求下载 UA 与获取 UA 一致（f=1），否则
    播放器直连 115 会被拒绝 → NoCompatibleStream。
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
        headers = {}
        if client_ua:
            headers["User-Agent"] = client_ua
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=60, write=10, pool=5)) as client:
            resp = await client.get(upstream, headers=headers, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if loc:
                return loc
        logger.warning(f"[proxy] 跟随 STRM 链接未返回重定向: {target[:80]} (HTTP {resp.status_code})")
    except Exception as e:
        logger.warning(f"[proxy] 跟随 STRM 链接失败 {target[:80]}: {e}")
    return None


async def handle_range_proxy(
    request: Request,
    strm_target: str,
    client_ua: str,
    account_id: int = 0,
) -> Optional[Response]:
    """
    A1: Range 分片流代理 — 代理 115 CDN 内容流，支持 Range 请求和失效自愈。

    当客户端（如 Infuse）不跟随 302 重定向时，由反代服务器直接代理 CDN 内容流。
    支持 HTTP Range 请求实现视频拖动/seek。
    CDN 链接失效（403/过期）时自动重新获取直链并重试。

    strm_target: STRM 内容中的本服务 302 接口 URL（/api/115/url/...）
    client_ua: 客户端 User-Agent（用于 115 直链 UA 一致性）
    account_id: 115 账号 ID（用于每账号并发限流）
    返回 Response 或 None（失败时回退到 302 方式）
    """
    import asyncio

    # 检查并发槽位
    if not _acquire_proxy_slot(account_id):
        logger.info(f"[proxy] 账号 {account_id} 并发代理达上限，回退 302")
        return None
    try:
        # 获取 115 CDN 直链（通过本服务 302 接口）
        cdn_url = await _follow_strm_url(strm_target, client_ua)
        if not cdn_url:
            logger.warning(f"[proxy] Range 代理：获取 CDN 直链失败，回退 302")
            return None

        # 解析客户端 Range 请求
        range_header = request.headers.get("range", "")

        # 最多重试 2 次（链接失效自愈）
        for attempt in range(2):
            try:
                headers = {}
                if client_ua:
                    headers["User-Agent"] = client_ua
                if range_header:
                    headers["Range"] = range_header

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
                    follow_redirects=True,
                ) as client:
                    req = client.build_request("GET", cdn_url, headers=headers)
                    resp = await client.send(req, stream=True)

                    if resp.status_code == 403:
                        # CDN 链接已失效，重新获取
                        await resp.aclose()
                        logger.info(f"[proxy] Range 代理：CDN 链接 403，尝试重新获取（第 {attempt+1} 次）")
                        # 标记旧链接失效
                        with _link_expiry_lock:
                            _link_expiry_cache[strm_target] = _time.time() + _LINK_EXPIRY_SECONDS
                        # 重新获取直链
                        cdn_url = await _follow_strm_url(strm_target, client_ua)
                        if cdn_url:
                            continue
                        return None

                    if resp.status_code not in (200, 206):
                        await resp.aclose()
                        logger.warning(f"[proxy] Range 代理：CDN 返回 {resp.status_code}，回退 302")
                        return None

                    # 流式转发响应
                    # 收集响应头（移除 hop-by-hop 头和 content-encoding）
                    resp_headers = _strip_hop_headers(dict(resp.headers))
                    resp_headers.pop("content-encoding", None)
                    resp_headers.pop("Content-Encoding", None)
                    # 保持连接不缓存
                    resp_headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

                    # 使用 StreamingResponse 流式转发
                    from starlette.responses import StreamingResponse

                    async def stream_generator():
                        try:
                            async for chunk in resp.aiter_raw():
                                yield chunk
                        finally:
                            await resp.aclose()

                    return StreamingResponse(
                        stream_generator(),
                        status_code=resp.status_code,
                        headers=resp_headers,
                        media_type=resp.headers.get("content-type", "video/mp4"),
                    )

            except httpx.ConnectError as e:
                logger.warning(f"[proxy] Range 代理连接失败（第 {attempt+1} 次）: {e}")
                if attempt == 0:
                    # 重新获取直链重试
                    cdn_url = await _follow_strm_url(strm_target, client_ua)
                    if cdn_url:
                        continue
                return None
            except Exception as e:
                logger.warning(f"[proxy] Range 代理异常: {e}")
                return None

        return None
    finally:
        _release_proxy_slot(account_id)


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

    # A4: 声明式缓存中间件 — 检查缓存规则
    cache_ttl = _get_cache_ttl(request.url.path)
    if cache_ttl > 0 and request.method == "GET":
        ck = _cache_key(request)
        cached = _get_cached_response(ck)
        if cached:
            logger.info(f"[proxy] 缓存命中 (TTL={cache_ttl}s): {request.url.path[:60]}")
            resp_headers = dict(cached["headers"])
            resp_headers["X-Cache"] = "HIT"
            return Response(
                content=cached["content"],
                status_code=cached["status"],
                headers=resp_headers,
                media_type=resp_headers.get("content-type"),
            )

    body = None
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        body = await request.body()

    headers = _strip_hop_headers(dict(request.headers))
    # 不让上游返回压缩内容：httpx 会解压响应但保留 content-encoding 头，
    # 转发给浏览器会导致 ERR_CONTENT_DECODING_FAILED。移除 Accept-Encoding
    # 让上游返回明文，彻底避免压缩/解压不一致。
    headers.pop("accept-encoding", None)
    headers.pop("Accept-Encoding", None)

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
        # httpx 已自动解压响应体，content-encoding 头不再有效，必须移除
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("Content-Encoding", None)

        # A4: 声明式缓存中间件 — 写入缓存（仅缓存成功的 GET 响应）
        if cache_ttl > 0 and request.method == "GET" and resp.status_code == 200:
            ck = _cache_key(request)
            _set_cached_response(ck, resp.content, resp.status_code, resp_headers, cache_ttl)
            resp_headers["X-Cache"] = "MISS"

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
        raw_body = await request.body()
        # A2: 注入固定 DeviceProfile，从源头阻止 Emby 决定转码。
        # 解析请求体 JSON，替换 DeviceProfile 为全兼容配置，再序列化转发。
        # 若解析失败（非 JSON 或格式异常），回退使用原始 body。
        if raw_body:
            try:
                import json as _json
                body_json = _json.loads(raw_body)
                body_json["DeviceProfile"] = _FIXED_DEVICE_PROFILE
                body = _json.dumps(body_json, ensure_ascii=False).encode("utf-8")
            except Exception:
                body = raw_body
        else:
            body = raw_body

    headers = _strip_hop_headers(dict(request.headers))
    headers.pop("Content-Length", None)

    # P3: 缓存空间 — 以 item_id 为键检查缓存命中
    # 无 MediaSourceId 请求命中 → 返回完整改写数据
    # 带 MediaSourceId 请求命中 → 从全量缓存筛出对应源并移到最前
    media_source_id = request.query_params.get("MediaSourceId", "")
    with _playback_info_lock:
        cached = _playback_info_cache.get(item_id)
        if cached and (_time.time() - cached["ts"] < _PLAYBACK_INFO_TTL):
            logger.info(f"[proxy] PlaybackInfo 缓存命中: item={item_id}, media_source={media_source_id or 'all'}")
            rewritten = _rewrite_playback_data(cached["data"], item_id, api_key, media_source_id,
                                               _get_request_host(request))
            return JSONResponse(content=rewritten)

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
        # P3: 以 item_id 为键缓存全量数据（供 PlaybackInfo 复用和 Items 覆盖）
        with _playback_info_lock:
            _playback_info_cache[item_id] = {"data": data, "ts": _time.time()}
            # 清理过期条目
            if len(_playback_info_cache) > 1000:
                cutoff = _time.time() - _PLAYBACK_INFO_TTL
                expired = [k for k, v in _playback_info_cache.items() if v["ts"] < cutoff]
                for k in expired:
                    del _playback_info_cache[k]
    except Exception as e:
        logger.warning(f"[proxy] PlaybackInfo 转发失败: {e}")
        return await proxy_origin(request)

    # 改写 MediaSources（复用 P3 辅助函数，保证与缓存命中逻辑一致）
    rewritten = _rewrite_playback_data(data, item_id, api_key, media_source_id,
                                       _get_request_host(request))
    sources = rewritten.get("MediaSources", [])

    logger.info(f"[proxy] PlaybackInfo 改写完成: item={item_id}, sources={len(sources)}")

    return JSONResponse(content=rewritten)


# ===== P3: Items 接口覆盖 =====
# 客户端请求 /Items/{id} 时，用 PlaybackInfo 缓存空间中的改写版本覆盖 MediaSources，
# 防止转码源列表只在首次 PlaybackInfo 出现后丢失（参考 qmediasync LoadCacheItems）。
# UA 含 infuse 跳过（Infuse 有自己的播放处理逻辑，避免干扰）。

async def handle_items(request: Request):
    """拦截 /Items/{id} 详情请求，用 PlaybackInfo 缓存覆盖 MediaSources"""
    # 仅处理 GET 详情请求
    if request.method != "GET":
        return await proxy_origin(request)

    cfg = _get_config()
    m = _REG_ITEMS.match(request.url.path)
    if not m:
        return await proxy_origin(request)
    item_id = m.group(1)

    # UA 含 infuse 跳过（参考 qmediasync LoadCacheItems）
    ua = request.headers.get("User-Agent", "") or ""
    if "infuse" in ua.lower():
        return await proxy_origin(request)

    # 检查是否有 PlaybackInfo 缓存
    with _playback_info_lock:
        cached = _playback_info_cache.get(item_id)
        if not cached or (_time.time() - cached["ts"] >= _PLAYBACK_INFO_TTL):
            return await proxy_origin(request)

    api_key = _extract_api_key(request, cfg)
    emby_host = cfg.get("emby_host", "")
    if not api_key or not emby_host:
        return await proxy_origin(request)

    # 转发请求到 Emby 获取原始 Items 响应
    url = emby_host + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    headers = _strip_hop_headers(dict(request.headers))
    headers.pop("accept-encoding", None)
    headers.pop("Accept-Encoding", None)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5)) as client:
            resp = await client.request("GET", url, headers=headers, follow_redirects=False)
        if resp.status_code != 200:
            return await proxy_origin(request)
        data = resp.json()
    except Exception as e:
        logger.warning(f"[proxy] Items 转发失败: {e}")
        return await proxy_origin(request)

    # 用缓存空间的 MediaSources 覆盖响应（防转码源丢失）
    cached_sources = (cached["data"].get("MediaSources", []) or [])
    if cached_sources:
        media_source_id = request.query_params.get("MediaSourceId", "")
        rewritten = _rewrite_playback_data(cached["data"], item_id, api_key, media_source_id,
                                           _get_request_host(request))
        # 仅覆盖 MediaSources 与 Profile，保留 Items 的其他字段（名称/简介/图片等）
        data["MediaSources"] = rewritten.get("MediaSources", cached_sources)
        data["Profile"] = rewritten.get("Profile", _FIXED_DEVICE_PROFILE)
        logger.info(f"[proxy] Items 缓存覆盖: item={item_id}, sources={len(cached_sources)}")

    resp_headers = _strip_hop_headers(dict(resp.headers))
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("Content-Encoding", None)
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
    # A10: UA 兼容矩阵 — 使用统一检测函数
    ua = request.headers.get("User-Agent", "") or ""
    client_compat = _get_client_compat(ua)
    client = client_compat["client_type"]
    # P6: 打印检测到的客户端类型到日志（最小侵入，不做额外分支）
    logger.info(f"[proxy] UA 检测: client_type={client_compat['client_type']}, ua={ua[:80]}")
    # P6: Infuse-Download 等下载场景（block_download=True）— 简化实现：仅记录日志
    if client_compat.get("block_download"):
        logger.info(f"[proxy] 检测到下载场景客户端 (block_download=True)，按常规流程处理: {file_name}")
    if client == "default":
        # 回退到原有日志友好名称
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

    # 日志去重：播放器启动时会对同一文件发起多个请求（预探测/分段拉取/seek），
    # 短时间内相同文件只打印第一条日志，避免刷屏。
    # 参考 emby2Alist：仅记录"首次"播放请求，后续请求静默处理。
    if _should_log_play(play_label):
        _mark_play_logged(play_label)

    # 判断是否为 STRM 文件。
    # 兼容两种情况：
    # 1) emby_path 以 .strm 结尾（常规 STRM 文件路径）
    # 2) emby_path 本身是 URL 形式（如 ...mkv?pickcode=xxx，STRM 内容被 Emby 存成 Path）
    is_strm = emby_path.lower().endswith(".strm") or "pickcode=" in emby_path or "account_id=" in emby_path
    if is_strm:
        if emby_path.lower().endswith(".strm"):
            # 情况 1：读取 STRM 文件内容
            content = _read_strm_file(emby_path)
            if content:
                target = _resolve_strm_target(content)
            else:
                logger.warning(f"[proxy] 读取 STRM 文件失败（回源处理）: {emby_path}")
                return await proxy_origin(request)
        else:
            # 情况 2：emby_path 本身就是 STRM 内容 URL，直接用
            target = _resolve_strm_target(emby_path)
        if target:
            # 服务器端跟随：STRM 内容指向本服务 302 接口时，由反代请求主应用
            # 获取 115 直链，再 302 给客户端（参考 emby2Alist fetchLastLink）。
            # 客户端拿到的是 115 CDN 直链，不接触内部地址（172.17.0.1 等）。
            if _is_self_strm_url(target):
                # A1 + A10: 根据 UA 兼容矩阵决定策略
                # Infuse 强制使用 Range 代理（不跟随 302）；其他客户端优先 Range，失败回退 302
                force_range = client_compat.get("force_range_proxy", False)
                range_response = await handle_range_proxy(request, target, ua, account_id=0)
                if range_response:
                    if _should_log_play(play_label):
                        logger.info(f"{play_label} -> Range 分片流代理 (client={client})")
                    return range_response

                # Infuse 等 force_range 客户端 Range 代理失败时不再回退 302（因为不跟随）
                # 而是回源让 Emby 处理
                if force_range:
                    logger.warning(f"[proxy] Range 代理失败且客户端不支持 302，回源: {play_label}")
                    return await proxy_origin(request)

                # 回退：302 重定向方式
                final_url = await _follow_strm_url(target, ua)
                if final_url:
                    if _should_log_play(play_label):
                        logger.info(f"{play_label} -> 302 重定向 -> {final_url[:120]}")
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
            if _should_log_play(play_label):
                logger.info(f"{play_label} -> {target[:120]}")
            response = RedirectResponse(url=target, status_code=307)
            # 禁止缓存，避免过期直链被缓存
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response
        logger.warning(f"[proxy] STRM 内容为空: {emby_path}")
    else:
        logger.info(f"[proxy] 本地媒体回源: {file_name}")

    return await proxy_origin(request)


# ===== P4: 播放进度辅助 =====
# 参考 qmediasync emby302/service/emby/playing.go PlayingStoppedHelper / PlayingProgressHelper：
# - Stopped: PositionTicks >= 5 分钟视为真实播放停止 → 透传回源（原样代理）并记录日志
# - Progress: PositionTicks <= 1 秒视为无效进度 → 直接 204 不转发 Emby
# 1 秒 = 10_000_000 ticks（Emby 时间单位）
_PLAYING_STOP_MIN_POSITION_TICKS = 5 * 60 * 10_000_000  # 5 分钟（ticks）
_PLAYING_PROGRESS_INVALID_MAX_TICKS = 10_000_000        # 1 秒（ticks）


async def handle_playing_event(request: Request):
    """拦截 /Sessions/Playing/{Stopped|Progress} 会话接口（播放进度辅助）。
    解析请求体 JSON：
    - Stopped: PositionTicks >= 5 分钟 → 回源转发并记录"外部播放器停止事件已转发"；
      缺失或 < 5 分钟视为无效，仍回源（保持兼容）。
    - Progress: PositionTicks 存在且 <= 1 秒视为无效进度 → 直接返回 204，不转发；
      否则正常回源。
    JSON 解析失败一律回源（proxy_origin 会再次读取 body，Content-Length 由它处理）。
    """
    import json as _json
    try:
        raw_body = await request.body()
        data = _json.loads(raw_body) if raw_body else {}
    except Exception:
        # 请求体解析失败：无法判断，回源保持兼容
        return await proxy_origin(request)
    if not isinstance(data, dict):
        return await proxy_origin(request)

    m = _REG_SESSIONS_PLAYING.match(request.url.path)
    event_type = m.group(1).lower() if m else ""
    try:
        position_ticks = int(data.get("PositionTicks", 0))
    except (TypeError, ValueError):
        position_ticks = None

    if event_type == "stopped":
        # 真实播放停止：PositionTicks >= 5 分钟 → 透传回源（原样代理，不额外处理）
        if position_ticks is not None and position_ticks >= _PLAYING_STOP_MIN_POSITION_TICKS:
            logger.info(f"[proxy] 外部播放器停止事件已转发 (PositionTicks={position_ticks})")
        else:
            # PositionTicks 缺失或 < 5 分钟：视为无效，仍回源（保持兼容）
            logger.info("[proxy] 外部播放器停止事件已转发 (PositionTicks 缺失或不足 5 分钟，仍回源)")
        return await proxy_origin(request)

    if event_type == "progress":
        # 无效进度：PositionTicks 存在且 <= 1 秒 → 直接 204，不转发 Emby
        if position_ticks is not None and position_ticks <= _PLAYING_PROGRESS_INVALID_MAX_TICKS:
            return Response(status_code=204)
        return await proxy_origin(request)

    return await proxy_origin(request)


# ===== 路由注册 =====

async def handle_redirect2external(request: Request):
    """
    P1: 外部播放器中转端点。
    URL: /redirect2external?url=<base64url 编码的播放地址>
    解码后 302 重定向到真实地址（经 base64 中转避免特殊字符破坏各播放器协议链接）。
    参考 embyExternalUrl /redirect2external 设计。
    """
    raw = request.query_params.get("url", "")
    if not raw:
        return JSONResponse(status_code=400, content={"detail": "缺少 url 参数"})
    target = _b64url_decode(raw)
    if not target:
        return JSONResponse(status_code=400, content={"detail": "url 解码失败"})
    # 仅允许站内相对路径或本服务地址，防止被利用为任意重定向
    if not (target.startswith("/") or target.startswith("http://") or target.startswith("https://")):
        return JSONResponse(status_code=400, content={"detail": "url 不合法"})
    # 站内相对路径补全为当前 host
    if target.startswith("/"):
        base = _get_request_host(request)
        if base:
            target = f"{base}{target}"
    response = RedirectResponse(url=target, status_code=302)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


async def handle_m3u8_route(request: Request):
    """
    P2: /api/playback/m3u8 — 拉取远端 m3u8 并重写为本地代理地址。
    参数: url（远端 m3u8 地址）
    返回: 重写后的 m3u8 文本（Content-Type: application/vnd.apple.mpegurl）
    """
    url = request.query_params.get("url", "")
    if not url:
        return JSONResponse(status_code=400, content={"detail": "缺少 url 参数"})

    from app.services.m3u8_proxy import handle_m3u8
    client_ua = request.headers.get("User-Agent", "") or ""
    result = await handle_m3u8(url, client_ua)
    if not result:
        return JSONResponse(status_code=502, content={"detail": "m3u8 拉取或解析失败"})

    return Response(
        content=result["text"],
        status_code=200,
        headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


async def handle_proxy_ts_route(request: Request):
    """
    P2: /api/playback/proxy_ts — 分段 302 重定向到真实 ts 地址。
    参数: t（token）、idx（分段序号）
    缓存失效时返回 503 提示重新拉取播放列表。
    """
    token = request.query_params.get("t", "")
    idx_raw = request.query_params.get("idx", "")
    if not token or not idx_raw:
        return JSONResponse(status_code=400, content={"detail": "缺少 t 或 idx 参数"})
    try:
        idx = int(idx_raw)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "idx 参数无效"})

    from app.services.m3u8_proxy import resolve_segment
    seg_url = resolve_segment(token, idx)
    if not seg_url:
        return JSONResponse(status_code=503, content={"detail": "播放列表已失效，请重新拉取"})

    response = RedirectResponse(url=seg_url, status_code=302)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


async def proxy_websocket(websocket: WebSocket, path: str):
    """WebSocket 双向代理（Emby 客户端连接服务器必需）。
    客户端连反代 6086 时，把 WebSocket 升级请求转发到真实 Emby。
    使用 websockets 库实现双向透传，兼容文本与二进制帧。
    """
    import websockets
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
        # 传递客户端的子协议（Emby 客户端可能指定）；starlette Headers 用 getlist
        subprotocols = websocket.headers.getlist("sec-websocket-protocol") or None
        async with websockets.connect(upstream, subprotocols=subprotocols) as upstream_ws:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        msg_type = msg.get("type")
                        if msg_type == "websocket.disconnect":
                            break
                        if msg_type == "websocket.receive":
                            if "text" in msg and msg["text"] is not None:
                                await upstream_ws.send(msg["text"])
                            elif "bytes" in msg and msg["bytes"] is not None:
                                await upstream_ws.send(msg["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass
            async def upstream_to_client():
                try:
                    while True:
                        msg = await upstream_ws.recv()
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        elif isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
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

    # P4: 播放会话接口（Sessions/Playing/Stopped|Progress）— 播放进度辅助
    # 放在现有路由判断之前；正则精确匹配这两类接口，不影响其他路由
    if request.method == "POST" and _REG_SESSIONS_PLAYING.match(full_path):
        return await handle_playing_event(request)

    # P1: 外部播放器中转 → base64url 解码后 302 到真实播放地址
    if full_path.startswith("/redirect2external"):
        return await handle_redirect2external(request)

    # P2: m3u8 播放列表拉取+重写
    if full_path == "/api/playback/m3u8":
        return await handle_m3u8_route(request)

    # P2: m3u8 分段 302 重定向
    if full_path == "/api/playback/proxy_ts":
        return await handle_proxy_ts_route(request)

    # STRMhub 302 下载接口 → 转发到主应用（/api/115/url/...）
    # 场景：STRM 内容指向反代端口时，客户端经反代访问 302 接口
    if full_path.startswith("/api/115/url/"):
        return await proxy_to_main_app(request)

    # PlaybackInfo → 改写
    if _REG_PLAYBACK_INFO.match(full_path):
        return await handle_playback_info(request)

    # P3: Items 详情 → 用 PlaybackInfo 缓存覆盖 MediaSources（防转码源丢失）
    if _REG_ITEMS.match(full_path):
        return await handle_items(request)

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
    # 同 proxy_origin：移除 Accept-Encoding，避免 httpx 解压与头不一致
    headers.pop("accept-encoding", None)
    headers.pop("Accept-Encoding", None)

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
        # httpx 已解压，移除无效的 content-encoding 头
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("Content-Encoding", None)
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

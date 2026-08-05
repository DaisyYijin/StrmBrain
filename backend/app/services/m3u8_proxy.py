"""
m3u8 播放列表管理 — 参考 qmediasync emby302/service/m3u8 方案。

能力：
1. parse_m3u8：解析远端 m3u8 文本为结构化对象（头/分段/尾）
2. rewrite_to_proxy：将分段 URI 重写为本地 proxy_ts 代理地址
3. 内存播放列表缓存（LRU + TTL）：token -> 播放列表
4. fetch_remote_m3u8：httpx 拉取远端 m3u8
5. 路由处理：/api/playback/m3u8（拉取+重写）与 /api/playback/proxy_ts（分段 302）

适用场景：115 云转码 / 预告片等 m3u8 内容，客户端无法直连远端 m3u8 时的中转。
"""
import re
import time as _time
import secrets
import threading
from typing import Optional
from collections import OrderedDict

import httpx

from app.core.logbuffer import get_logger

logger = get_logger("app.services.m3u8_proxy")

# ===== 播放列表缓存（LRU + TTL）=====
# token -> {"playlist": dict, "base_url": str, "ts": float}
_playlist_cache: OrderedDict[str, dict] = OrderedDict()
_playlist_lock = threading.Lock()
_PLAYLIST_CACHE_MAX = 20      # LRU 上限
_PLAYLIST_TTL = 600           # 10 分钟

# 远端 m3u8 拉取超时
_M3U8_FETCH_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)


def _now() -> float:
    return _time.time()


# ===== m3u8 解析 =====

def parse_m3u8(content: str) -> Optional[dict]:
    """解析 m3u8 文本为结构化对象。

    返回: {"header": [行...], "segments": [{"uri": str, "duration": float|None}], "footer": [行...]}
    - header: #EXTM3U 开始的标签行（含 #EXT-X-STREAM-INF 变体行、#EXT-X-MEDIA 等）
    - segments: 分段（#EXTINF 之后的 uri 行）；变体 m3u8 中 uri 为子播放列表地址
    - footer: 尾部非分段行
    解析失败（非 m3u8）返回 None。
    """
    if not content or not content.strip():
        return None
    lines = [line.strip() for line in content.splitlines()]
    if not lines or "#EXTM3U" not in lines[0]:
        return None

    header: list[str] = []
    segments: list[dict] = []
    footer: list[str] = []
    cur_duration: Optional[float] = None
    in_segments = False

    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF"):
            # 解析时长: #EXTINF:10.0,title
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            cur_duration = float(m.group(1)) if m else None
            header.append(line)
            in_segments = True
            continue
        if line.startswith("#"):
            # 普通标签行
            header.append(line)
            continue
        # 非 # 开头的行 = uri（分段或子播放列表）
        if line.startswith(("http://", "https://", "/")) or "/" in line or "." in line:
            segments.append({"uri": line, "duration": cur_duration})
            cur_duration = None
            in_segments = True
        elif in_segments:
            footer.append(line)

    return {"header": header, "segments": segments, "footer": footer}


def rewrite_to_proxy(playlist: dict, token: str) -> str:
    """将播放列表分段 URI 重写为本地代理地址。

    返回重写后的 m3u8 文本（Content-Type: application/vnd.apple.mpegurl）。
    分段地址统一改为 /api/playback/proxy_ts?t={token}&idx={index}。
    """
    lines: list[str] = []
    for line in playlist.get("header", []):
        if line.startswith("#EXTINF"):
            lines.append(line)
        else:
            lines.append(line)
    for idx, seg in enumerate(playlist.get("segments", [])):
        # 保留 EXTINF 行（若在 header 中已包含则跳过重复添加）
        dur = seg.get("duration")
        if dur is not None:
            lines.append(f"#EXTINF:{dur},")
        lines.append(f"/api/playback/proxy_ts?t={token}&idx={idx}")
    lines.extend(playlist.get("footer", []))
    return "\n".join(lines) + "\n"


# ===== 远端拉取 =====

async def fetch_remote_m3u8(url: str, headers: Optional[dict] = None) -> Optional[str]:
    """httpx 拉取远端 m3u8 文本。失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=_M3U8_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers or {})
            if resp.status_code != 200:
                logger.warning(f"[m3u8] 拉取失败 {url[:120]}: HTTP {resp.status_code}")
                return None
            return resp.text
    except Exception as e:
        logger.warning(f"[m3u8] 拉取异常 {url[:120]}: {e}")
        return None


# ===== 缓存管理 =====

def _cache_put(token: str, entry: dict):
    with _playlist_lock:
        _playlist_cache[token] = entry
        _playlist_cache.move_to_end(token)
        # 淘汰过期 + 超上限
        now = _now()
        expired = [k for k, v in _playlist_cache.items() if now - v["ts"] > _PLAYLIST_TTL]
        for k in expired:
            del _playlist_cache[k]
        while len(_playlist_cache) > _PLAYLIST_CACHE_MAX:
            _playlist_cache.popitem(last=False)


def _cache_get(token: str) -> Optional[dict]:
    with _playlist_lock:
        entry = _playlist_cache.get(token)
        if not entry:
            return None
        if _now() - entry["ts"] > _PLAYLIST_TTL:
            del _playlist_cache[token]
            return None
        _playlist_cache.move_to_end(token)
        return entry


def generate_token() -> str:
    """生成播放列表访问 token"""
    return secrets.token_urlsafe(16)


# ===== 路由处理 =====

async def handle_m3u8(url: str, client_ua: str = "") -> dict:
    """处理 /api/playback/m3u8 请求：拉取远端 m3u8 → 生成 token → 缓存 → 返回重写文本。

    返回 {"text": str, "token": str}；失败返回 None。
    """
    headers = {"User-Agent": client_ua} if client_ua else None
    content = await fetch_remote_m3u8(url, headers)
    if not content:
        return None
    playlist = parse_m3u8(content)
    if not playlist or not playlist.get("segments"):
        logger.warning(f"[m3u8] 解析失败或无分段: {url[:120]}")
        return None

    token = generate_token()
    # 记录远端 base_url（用于分段地址拼接）
    base_url = url.rsplit("/", 1)[0] if "/" in url else ""
    _cache_put(token, {"playlist": playlist, "base_url": base_url, "ts": _now()})

    rewritten = rewrite_to_proxy(playlist, token)
    logger.info(f"[m3u8] 已缓存播放列表: token={token[:8]}..., segments={len(playlist['segments'])}")
    return {"text": rewritten, "token": token}


def resolve_segment(token: str, idx: int) -> Optional[str]:
    """处理 /api/playback/proxy_ts 请求：从缓存取播放列表，返回分段真实地址。

    缓存失效返回 None（调用方回 503 提示重新拉取）。
    """
    entry = _cache_get(token)
    if not entry:
        return None
    segments = entry.get("playlist", {}).get("segments", [])
    if idx < 0 or idx >= len(segments):
        return None
    uri = segments[idx].get("uri", "")
    if uri.startswith(("http://", "https://")):
        return uri
    # 相对路径：拼上远端 base_url
    base = entry.get("base_url", "")
    if base and uri.startswith("/"):
        # 根相对：取远端域名
        scheme_host = base.split("/", 3)[:3]
        return f"{'/'.join(scheme_host)}{uri}"
    if base:
        return f"{base}/{uri}"
    return uri

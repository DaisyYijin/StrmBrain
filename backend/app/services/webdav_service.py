"""
WebDAV 只读访问服务（G5 生态扩展）
====================================

将 STRM 输出目录（本地媒体目录）以只读 WebDAV 形式暴露，
让其他播放器/工具（如 Kodi、nPlayer、Infuse 的 WebDAV 挂载）可直接挂载浏览。

设计取舍：
- 只读：仅实现 OPTIONS / PROPFIND / GET / HEAD，不支持写入（PUT/DELETE/MKCOL 返回 403）。
  STRM 目录由同步流程管理，WebDAV 写入会与同步冲突，故禁止写入。
- 轻量：不引入 wsgidav 等新依赖，直接用 Starlette 路由 + 手写 PROPFIND XML。
- 安全：默认关闭；启用后要求 HTTP Basic 认证（复用本地管理账号）。
  未启用时所有 /dav 请求返回 404，不暴露任何目录信息。
- 路径安全：所有请求路径限制在配置的根目录内，防止路径穿越（../）。

配置（settings.json 的 webdav 键）：
- enabled: bool 是否启用（默认 False）
- root: str  暴露的根目录（留空则取同步计划的 local_media_dir）
"""
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse, FileResponse

from app.core.json_storage import read_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.webdav")

SETTINGS_KEY = "webdav"


def is_enabled() -> bool:
    """WebDAV 是否启用（默认关闭）"""
    cfg = read_setting(SETTINGS_KEY) or {}
    return bool(cfg.get("enabled", False))


def get_root() -> Optional[Path]:
    """获取 WebDAV 暴露的根目录。

    优先取 webdav.root 配置，留空则回退同步计划的 local_media_dir。
    返回存在的目录 Path，否则 None。
    """
    cfg = read_setting(SETTINGS_KEY) or {}
    root = (cfg.get("root") or "").strip()
    if not root:
        try:
            from app.services.sync_service import SyncService
            root = SyncService.load_schedule().get("local_media_dir", "") or ""
        except Exception:
            root = ""
    if not root:
        return None
    p = Path(root)
    return p if p.exists() and p.is_dir() else None


def _check_basic_auth(request: Request) -> bool:
    """校验 HTTP Basic 认证（复用本地管理账号）。

    AUTH_ENABLED=false 时跳过校验（开发模式）。
    """
    from app.config import AUTH_ENABLED
    if not AUTH_ENABLED:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    from app.core.auth import authenticate_user
    return authenticate_user(username, password) is not None


def _auth_challenge() -> Response:
    """返回 401 并要求 Basic 认证"""
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="STRMhub WebDAV"'},
    )


def _resolve_path(root: Path, url_path: str) -> Optional[Path]:
    """将 WebDAV URL 路径解析为根目录内的真实路径，防止路径穿越。

    返回解析后的绝对路径；越界或非法时返回 None。
    """
    # 去掉 /dav 前缀，URL 解码
    rel = unquote(url_path)
    if rel.startswith("/dav"):
        rel = rel[4:]
    rel = rel.lstrip("/")
    try:
        target = (root / rel).resolve()
        root_resolved = root.resolve()
        # 限制在根目录内（含根目录本身）
        if target == root_resolved or root_resolved in target.parents:
            return target
    except Exception:
        pass
    return None


def _iso_time(ts: float) -> str:
    """转 RFC1123 时间格式（WebDAV getlastmodified）"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _href(base_prefix: str, root: Path, path: Path, is_dir: bool) -> str:
    """构造资源的 href（相对 URL，含 /dav 前缀）"""
    try:
        rel = path.resolve().relative_to(root.resolve())
        rel_str = str(rel).replace("\\", "/")
    except Exception:
        rel_str = ""
    href = base_prefix.rstrip("/")
    if rel_str and rel_str != ".":
        href += "/" + quote(rel_str)
    if is_dir and not href.endswith("/"):
        href += "/"
    return href or "/"


def _propfind_response_xml(base_prefix: str, root: Path, path: Path) -> str:
    """为单个资源生成 PROPFIND <response> XML 片段"""
    is_dir = path.is_dir()
    try:
        st = path.stat()
        size = st.st_size
        mtime = _iso_time(st.st_mtime)
    except Exception:
        size = 0
        mtime = _iso_time(0)
    href = _href(base_prefix, root, path, is_dir)
    name = escape(path.name)
    if is_dir:
        resourcetype = "<D:collection/>"
        prop_extra = ""
    else:
        resourcetype = ""
        # 猜测 content-type（简单按扩展名）
        ctype = _guess_content_type(path.name)
        prop_extra = (
            f"<D:getcontentlength>{size}</D:getcontentlength>"
            f"<D:getcontenttype>{escape(ctype)}</D:getcontenttype>"
        )
    return (
        "<D:response>"
        f"<D:href>{escape(href)}</D:href>"
        "<D:propstat>"
        "<D:prop>"
        f"<D:displayname>{name}</D:displayname>"
        f"<D:resourcetype>{resourcetype}</D:resourcetype>"
        f"<D:getlastmodified>{mtime}</D:getlastmodified>"
        f"{prop_extra}"
        "</D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status>"
        "</D:propstat>"
        "</D:response>"
    )


def _guess_content_type(name: str) -> str:
    """按扩展名猜测 Content-Type（覆盖 STRM 场景常见类型）"""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mapping = {
        "strm": "text/plain", "nfo": "text/xml", "srt": "application/x-subrip",
        "ass": "text/plain", "ssa": "text/plain", "vtt": "text/vtt",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "mkv": "video/x-matroska", "mp4": "video/mp4",
        "txt": "text/plain", "json": "application/json",
    }
    return mapping.get(ext, "application/octet-stream")


async def handle_webdav(request: Request) -> Response:
    """WebDAV 请求总入口（挂载到 /dav 及 /dav/{path:path}）。

    未启用时返回 404；启用后按方法分发 OPTIONS/PROPFIND/GET/HEAD，
    写类方法（PUT/DELETE/MKCOL/MOVE/COPY/PROPPATCH）返回 403（只读）。
    """
    if not is_enabled():
        return PlainTextResponse("Not Found", status_code=404)

    method = request.method.upper()

    # OPTIONS 无需认证，广告 DAV 能力（部分客户端先探测）
    if method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "DAV": "1",
                "Allow": "OPTIONS, GET, HEAD, PROPFIND",
                "MS-Author-Via": "DAV",
            },
        )

    # 其余方法需 Basic 认证
    if not _check_basic_auth(request):
        return _auth_challenge()

    root = get_root()
    if root is None:
        return PlainTextResponse("WebDAV root not configured", status_code=500)

    # 只读：拒绝写类方法
    if method in ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"):
        return PlainTextResponse("Read-only WebDAV", status_code=403)

    target = _resolve_path(root, request.url.path)
    if target is None:
        return PlainTextResponse("Forbidden (path traversal)", status_code=403)

    if method == "PROPFIND":
        return _handle_propfind(request, root, target)
    if method in ("GET", "HEAD"):
        return _handle_get(request, target, head_only=(method == "HEAD"))

    return PlainTextResponse("Method Not Allowed", status_code=405)


def _handle_propfind(request: Request, root: Path, target: Path) -> Response:
    """处理 PROPFIND：返回目录/文件的属性 XML。

    Depth: 0 只返回资源本身；Depth: 1 返回资源 + 直接子项（目录列举）。
    """
    if not target.exists():
        return PlainTextResponse("Not Found", status_code=404)

    depth = request.headers.get("Depth", "1")
    base_prefix = "/dav"

    parts = [_propfind_response_xml(base_prefix, root, target)]
    if depth != "0" and target.is_dir():
        try:
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                parts.append(_propfind_response_xml(base_prefix, root, child))
        except Exception as e:
            logger.warning(f"[webdav] 列举目录失败 {target}: {e}")

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">'
        + "".join(parts)
        + "</D:multistatus>"
    )
    return Response(
        content=xml,
        status_code=207,
        media_type="application/xml; charset=utf-8",
    )


def _handle_get(request: Request, target: Path, head_only: bool = False) -> Response:
    """处理 GET/HEAD：返回文件内容（支持 Range 由 FileResponse 处理）。

    目录 GET 返回简单 HTML 列表，方便浏览器直接访问。
    """
    if not target.exists():
        return PlainTextResponse("Not Found", status_code=404)

    if target.is_dir():
        # 目录：返回简单 HTML 列表
        rows = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                name = escape(child.name) + ("/" if child.is_dir() else "")
                link = quote(child.name) + ("/" if child.is_dir() else "")
                rows.append(f'<li><a href="{link}">{name}</a></li>')
        except Exception:
            pass
        html = f"<html><body><h3>{escape(target.name or '/')}</h3><ul>{''.join(rows)}</ul></body></html>"
        if head_only:
            return Response(status_code=200, media_type="text/html; charset=utf-8")
        return Response(content=html, media_type="text/html; charset=utf-8")

    if head_only:
        try:
            size = target.stat().st_size
        except Exception:
            size = 0
        return Response(
            status_code=200,
            headers={
                "Content-Length": str(size),
                "Content-Type": _guess_content_type(target.name),
                "Accept-Ranges": "bytes",
            },
        )
    # FileResponse 自动处理 Range 请求
    return FileResponse(
        target,
        media_type=_guess_content_type(target.name),
        filename=target.name,
    )

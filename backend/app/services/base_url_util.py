"""
base_url 自动推导 + 批量替换工具（A6）

功能：
1. 从请求头自动推导当前访问的 base_url（支持反代场景的 X-Forwarded-* 头）
2. 从已有 STRM 文件中提取当前使用的 base_url
3. 批量替换所有 STRM 文件中的旧 base_url 为新 base_url

使用场景：
- 用户更换服务器地址/端口后，STRM 文件仍指向旧地址 → 一键批量替换
- 反代部署时自动推导外部访问地址 → 推荐给用户作为新 base_url
"""
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from app.core.logbuffer import get_logger

logger = get_logger("app.services.base_url_util")


def detect_base_url(request) -> str:
    """
    从 FastAPI Request 对象自动推导 base_url。

    优先级：
    1. X-Forwarded-Proto + X-Forwarded-Host（标准反代头）
    2. Forwarded 头（RFC 7239）
    3. Host 头 + request.url.scheme（直连场景）

    返回格式: http(s)://host(:port)，无尾部斜杠。
    """
    # 1. X-Forwarded-* 头（Nginx/Caddy/Traefik 等反代会设置）
    proto = request.headers.get("x-forwarded-proto", "").strip()
    host = request.headers.get("x-forwarded-host", "").strip()

    # 2. Forwarded 头（RFC 7239 格式: Forwarded: proto=https;host=example.com）
    if not proto or not host:
        forwarded = request.headers.get("forwarded", "").strip()
        if forwarded:
            for part in forwarded.split(";"):
                part = part.strip()
                if part.lower().startswith("proto=") and not proto:
                    proto = part[6:].strip('"')
                elif part.lower().startswith("host=") and not host:
                    host = part[5:].strip('"')

    # 3. 回退到 request 自身的 scheme + Host 头
    if not proto:
        proto = request.url.scheme
    if not host:
        host = request.headers.get("host", "")

    if not host:
        return ""

    return f"{proto}://{host}".rstrip("/")


def extract_strm_base_url(local_dir: str, sample_size: int = 10) -> Optional[str]:
    """
    从已有 STRM 文件中提取当前使用的 base_url。

    读取前 N 个 .strm 文件，解析其 URL 的 scheme://host:port 部分。
    如果多数文件使用相同的 base_url，返回该值；否则返回 None。

    local_dir: 本地 STRM 文件根目录
    sample_size: 采样文件数量（默认 10 个）
    """
    root = Path(local_dir)
    if not root.exists() or not root.is_dir():
        return None

    strm_files = list(root.rglob("*.strm"))[:sample_size]
    if not strm_files:
        return None

    base_urls: dict[str, int] = {}
    for strm_path in strm_files:
        try:
            content = strm_path.read_text(encoding="utf-8").strip()
            if not content or not content.startswith("http"):
                continue
            parsed = urlparse(content)
            base = f"{parsed.scheme}://{parsed.netloc}"
            base_urls[base] = base_urls.get(base, 0) + 1
        except Exception:
            continue

    if not base_urls:
        return None

    # 返回出现次数最多的 base_url
    most_common = max(base_urls.items(), key=lambda x: x[1])
    return most_common[0]


def batch_replace_strm_base_url(local_dir: str, old_base: str, new_base: str) -> dict:
    """
    批量替换 STRM 文件中的 base_url。

    遍历 local_dir 下所有 .strm 文件，将内容中的 old_base 替换为 new_base。

    返回统计信息:
    {
        "total": 总 STRM 文件数,
        "replaced": 替换的文件数,
        "skipped": 未包含 old_base 的文件数,
        "errors": 错误文件数,
        "details": [{"path": ..., "old": ..., "new": ...}, ...]
    }
    """
    root = Path(local_dir)
    if not root.exists() or not root.is_dir():
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": []}

    if not old_base or not new_base:
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": [],
                "message": "old_base 和 new_base 不能为空"}

    old_base = old_base.rstrip("/")
    new_base = new_base.rstrip("/")

    if old_base == new_base:
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": [],
                "message": "新旧地址相同，无需替换"}

    strm_files = list(root.rglob("*.strm"))
    total = len(strm_files)
    replaced = 0
    skipped = 0
    errors = 0
    details = []

    for strm_path in strm_files:
        try:
            content = strm_path.read_text(encoding="utf-8")
            if old_base in content:
                new_content = content.replace(old_base, new_base)
                strm_path.write_text(new_content, encoding="utf-8")
                replaced += 1
                if len(details) < 50:  # 限制详情数量
                    details.append({
                        "path": str(strm_path.relative_to(root)),
                        "old": content[:150],
                        "new": new_content[:150],
                    })
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            if len(details) < 50:
                details.append({
                    "path": str(strm_path.relative_to(root)),
                    "error": str(e),
                })

    logger.info(f"[base_url] 批量替换完成: 总 {total}, 替换 {replaced}, 跳过 {skipped}, 错误 {errors}")
    return {
        "total": total,
        "replaced": replaced,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }


def extract_strm_account_id(local_dir: str) -> Optional[int]:
    """Q3: 从已有 STRM 文件中提取当前使用的 account_id（参考 LitePan account_repair.go 采样思路）。

    读取前 10 个 .strm 文件，从 URL 解析 account_id=N 参数，
    返回出现次数最多的值；无有效 STRM 或解析不到时返回 None。
    """
    root = Path(local_dir)
    if not root.exists() or not root.is_dir():
        return None

    strm_files = list(root.rglob("*.strm"))[:10]
    if not strm_files:
        return None

    counts: dict[int, int] = {}
    for strm_path in strm_files:
        try:
            content = strm_path.read_text(encoding="utf-8").strip()
            if not content.startswith("http"):
                continue
            # account_id 是查询参数，用 parse_qs 解析（兼容带其他参数的 URL）
            query = urlparse(content).query
            values = parse_qs(query).get("account_id")
            if not values:
                continue
            try:
                aid = int(values[0])
            except (TypeError, ValueError):
                continue
            counts[aid] = counts.get(aid, 0) + 1
        except Exception:
            continue

    if not counts:
        return None

    # 返回出现次数最多的 account_id
    most_common = max(counts.items(), key=lambda x: x[1])
    return most_common[0]


def batch_replace_strm_account(local_dir: str, old_account_id, new_account_id) -> dict:
    """Q3: 批量替换 STRM 文件中的 account_id 参数（账号更换后重写引用）。

    遍历 local_dir 下所有 .strm 文件，把内容中的 account_id={old} 替换为 account_id={new}。
    返回与 batch_replace_strm_base_url 相同结构的统计 dict:
    {
        "total": 总 STRM 文件数,
        "replaced": 替换的文件数,
        "skipped": 未包含旧 account_id 的文件数,
        "errors": 错误文件数,
        "details": [{"path": ..., "old": ..., "new": ...}, ...]
    }
    """
    root = Path(local_dir)
    if not root.exists() or not root.is_dir():
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": []}

    try:
        old_account_id = int(old_account_id)
        new_account_id = int(new_account_id)
    except (TypeError, ValueError):
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": [],
                "message": "old_account_id 和 new_account_id 必须为整数"}

    if old_account_id == new_account_id:
        return {"total": 0, "replaced": 0, "skipped": 0, "errors": 0, "details": [],
                "message": "新旧账号 ID 相同，无需替换"}

    old_token = f"account_id={old_account_id}"
    new_token = f"account_id={new_account_id}"

    strm_files = list(root.rglob("*.strm"))
    total = len(strm_files)
    replaced = 0
    skipped = 0
    errors = 0
    details = []

    for strm_path in strm_files:
        try:
            content = strm_path.read_text(encoding="utf-8")
            if old_token in content:
                new_content = content.replace(old_token, new_token)
                strm_path.write_text(new_content, encoding="utf-8")
                replaced += 1
                if len(details) < 50:  # 限制详情数量
                    details.append({
                        "path": str(strm_path.relative_to(root)),
                        "old": content[:150],
                        "new": new_content[:150],
                    })
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            if len(details) < 50:
                details.append({
                    "path": str(strm_path.relative_to(root)),
                    "error": str(e),
                })

    logger.info(f"[account] 批量替换 account_id 完成: 总 {total}, 替换 {replaced}, 跳过 {skipped}, 错误 {errors}")
    return {
        "total": total,
        "replaced": replaced,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }

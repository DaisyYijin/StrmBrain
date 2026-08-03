"""
版本检查服务 — 通过 GitHub Releases API 检查是否有新版本
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import VERSION, GITHUB_REPO
from app.core.logbuffer import get_logger

logger = get_logger("app.services.version_service")

# GitHub Releases API（匿名访问，每小时 60 次限额）
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_LIST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# 缓存的检查结果（内存中）
_latest_result: dict = {
    "current_version": VERSION,
    "latest_version": None,
    "has_update": False,
    "release_url": None,
    "release_notes": None,
    "checked_at": None,
    "error": None,
}


def _compare_versions(v1: str, v2: str) -> int:
    """
    比较两个语义化版本号。
    返回:  1 表示 v1 > v2
          -1 表示 v1 < v2
           0 表示相等
    """
    def _parse(v: str):
        parts = []
        for p in v.lstrip("vV").split("."):
            num = ""
            for ch in p:
                if ch.isdigit():
                    num += ch
                else:
                    break
            parts.append(int(num) if num else 0)
        return parts

    a, b = _parse(v1), _parse(v2)
    # 补齐长度
    while len(a) < len(b):
        a.append(0)
    while len(b) < len(a):
        b.append(0)
    for x, y in zip(a, b):
        if x > y:
            return 1
        if x < y:
            return -1
    return 0


async def _fetch_github(url: str) -> httpx.Response:
    """请求 GitHub API，证书验证失败时降级为不验证"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(
                url,
                headers={"Accept": "application/vnd.github+json"},
            )
    except Exception:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            return await client.get(
                url,
                headers={"Accept": "application/vnd.github+json"},
            )


def _parse_release(data: dict) -> tuple[str, str, str]:
    """从 Release JSON 中提取 (tag_name, html_url, body)"""
    return (
        data.get("tag_name", ""),
        data.get("html_url", ""),
        data.get("body", ""),
    )


def _update_result(latest: str, html_url: str, notes: str):
    """更新缓存结果"""
    global _latest_result
    has_update = _compare_versions(latest, VERSION) > 0

    _latest_result.update({
        "current_version": VERSION,
        "latest_version": latest,
        "has_update": has_update,
        "release_url": html_url or f"https://github.com/{GITHUB_REPO}/releases",
        "release_notes": notes if has_update else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    })

    if has_update:
        logger.info(f"版本检查：发现新版本 {latest}（当前 {VERSION}）")
    else:
        logger.info(f"版本检查：当前已是最新版本 {VERSION}")


async def check_latest_version() -> dict:
    """
    请求 GitHub Releases API，检查是否有新版本。
    优先使用 /releases/latest，404 时回退到 /releases 列表取第一条。
    结果写入内存缓存并返回。
    """
    global _latest_result
    try:
        # 优先请求 /releases/latest
        resp = await _fetch_github(_RELEASES_LATEST_URL)

        if resp.status_code == 200:
            latest, html_url, notes = _parse_release(resp.json())
            if latest:
                _update_result(latest, html_url, notes)
                return _latest_result.copy()

        # /releases/latest 返回 404 或无有效数据时，回退到 /releases 列表
        if resp.status_code == 404 or resp.status_code == 200:
            logger.info("版本检查：/releases/latest 无数据，回退到 /releases 列表")
            resp2 = await _fetch_github(_RELEASES_LIST_URL)
            if resp2.status_code == 200:
                releases = resp2.json()
                # 过滤掉草稿和预发布，取第一条正式 Release
                for r in releases:
                    if not r.get("draft", False) and not r.get("prerelease", False):
                        latest, html_url, notes = _parse_release(r)
                        if latest:
                            _update_result(latest, html_url, notes)
                            return _latest_result.copy()

            # 两个端点都没有 Release
            _latest_result.update({
                "latest_version": None,
                "has_update": False,
                "release_url": f"https://github.com/{GITHUB_REPO}/releases",
                "release_notes": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": "暂无已发布的 Release",
            })
            logger.info("版本检查：GitHub 上暂无已发布的 Release")
            return _latest_result.copy()

        resp.raise_for_status()

    except Exception as e:
        _latest_result.update({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
        })
        logger.warning(f"版本检查失败: {e}")

    return _latest_result.copy()


def get_version_info() -> dict:
    """获取缓存的版本信息（不发起网络请求）"""
    return _latest_result.copy()


async def start_version_check_loop():
    """
    启动后台版本检查循环：启动时检查一次，之后每小时检查一次。
    应在 lifespan 中以 asyncio.create_task 方式调用。
    """
    # 首次检查
    await check_latest_version()
    # 每小时检查一次
    while True:
        await asyncio.sleep(3600)
        await check_latest_version()

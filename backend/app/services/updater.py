"""
内置自动更新服务 - #30

STRMhub 采用 Docker 部署，实际更新方式为 `docker compose pull && docker compose up -d`。
本服务复用 version_service 的版本检查逻辑，提供：
- 检查最新版本（GitHub Release API）
- 展示 Release Notes
- 提供更新命令指引

不实际下载替换文件（那是 Docker 的职责）。
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import VERSION, GITHUB_REPO
from app.core.logbuffer import get_logger

logger = get_logger("app.services.updater")

# GitHub Releases API（匿名访问，每小时 60 次限额）
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Docker 更新命令指引
_DOCKER_UPDATE_COMMANDS = (
    "docker compose pull\n"
    "docker compose up -d"
)


class Updater:
    """内置自动更新服务（单例）

    复用 version_service 的版本检查能力，避免重复轮询 GitHub。
    通过 check_latest() 发起一次即时检查，通过 get_status() 获取缓存状态。
    """

    _instance: Optional["Updater"] = None

    def __new__(cls) -> "Updater":
        """单例模式：全局只有一个 Updater 实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        # 缓存的最新版本信息
        self._latest_info: dict = {
            "tag": "",
            "name": "",
            "notes": "",
            "assets": [],
            "html_url": "",
            "checked_at": None,
            "error": None,
        }

    async def check_latest(self) -> dict:
        """查询 GitHub Release API，获取最新版本信息。

        返回: {
            "tag": str,         # 版本标签（如 v1.2.0）
            "name": str,        # Release 名称
            "notes": str,       # Release Notes（Markdown）
            "assets": [str],    # 附件下载 URL 列表
            "html_url": str,    # Release 页面 URL
            "checked_at": str,  # 检查时间（ISO 8601 UTC）
            "error": str|None,  # 错误信息（无错误时为 None）
        }
        """
        try:
            resp = await self._fetch_github(_RELEASES_LATEST_URL)
            if resp.status_code != 200:
                self._latest_info.update({
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"GitHub API 返回 {resp.status_code}",
                })
                logger.warning(f"[updater] 检查最新版本失败: HTTP {resp.status_code}")
                return self._latest_info.copy()

            data = resp.json()
            assets = []
            for asset in (data.get("assets") or []):
                url = asset.get("browser_download_url", "")
                if url:
                    assets.append(url)

            self._latest_info.update({
                "tag": data.get("tag_name", ""),
                "name": data.get("name", ""),
                "notes": data.get("body", ""),
                "assets": assets,
                "html_url": data.get("html_url", ""),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            })

            logger.info(
                f"[updater] 检查最新版本完成: "
                f"latest={self._latest_info['tag']}, current={VERSION}"
            )
            # 同步更新 version_service 的缓存，保持一致性
            self._sync_version_service()
            return self._latest_info.copy()

        except Exception as e:
            self._latest_info.update({
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            })
            logger.warning(f"[updater] 检查最新版本异常: {e}")
            return self._latest_info.copy()

    def download_and_update(self, version_tag: str = "") -> dict:
        """提供 Docker 更新命令指引。

        STRMhub 是 Docker 部署的，实际更新方式是 `docker compose pull && docker compose up -d`。
        本方法不实际下载替换文件，而是返回更新命令和说明。

        version_tag: 指定要更新到的版本标签（留空=最新版本）
        返回: {
            "version_tag": str,     # 目标版本标签
            "current_version": str, # 当前版本
            "update_commands": str, # Docker 更新命令
            "instructions": str,    # 更新说明
            "html_url": str,        # Release 页面 URL
        }
        """
        tag = version_tag or self._latest_info.get("tag", "")
        logger.info(f"[updater] 请求更新到版本: {tag}（当前 {VERSION}）")

        instructions = (
            "STRMhub 使用 Docker 部署，请在服务器上执行以下命令完成更新：\n"
            "1. 拉取最新镜像\n"
            "2. 重新启动容器\n"
            "更新完成后，新版本会自动生效。"
        )

        return {
            "version_tag": tag,
            "current_version": VERSION,
            "update_commands": _DOCKER_UPDATE_COMMANDS,
            "instructions": instructions,
            "html_url": self._latest_info.get("html_url", ""),
            "notes": self._latest_info.get("notes", ""),
        }

    def get_status(self) -> dict:
        """返回当前版本和最新版本信息。

        优先使用 version_service 的缓存（后台轮询维护），若 version_service
        有更新数据则合并使用，避免两个服务数据不一致。

        返回: {
            "current_version": str,      # 当前版本
            "latest_version": str,       # 最新版本标签
            "has_update": bool,          # 是否有可用更新
            "release_notes": str|None,   # Release Notes
            "release_url": str|None,     # Release 页面 URL
            "checked_at": str|None,      # 最后检查时间
            "error": str|None,           # 错误信息
            "update_commands": str,      # 更新命令指引
        }
        """
        # 复用 version_service 的缓存数据（后台轮询已维护）
        from app.services.version_service import get_version_info
        vs_info = get_version_info()

        # 合并 version_service 和自身缓存的数据
        latest_tag = vs_info.get("latest_version") or self._latest_info.get("tag", "")
        has_update = vs_info.get("has_update", False)
        notes = vs_info.get("release_notes") or self._latest_info.get("notes", "")
        html_url = vs_info.get("release_url") or self._latest_info.get("html_url", "")
        checked_at = vs_info.get("checked_at") or self._latest_info.get("checked_at")
        error = vs_info.get("error") or self._latest_info.get("error")

        return {
            "current_version": VERSION,
            "latest_version": latest_tag,
            "has_update": has_update,
            "release_notes": notes,
            "release_url": html_url,
            "checked_at": checked_at,
            "error": error,
            "update_commands": _DOCKER_UPDATE_COMMANDS,
        }

    async def _fetch_github(self, url: str) -> httpx.Response:
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

    def _sync_version_service(self):
        """将本服务检查到的最新版本信息同步到 version_service 的缓存中。

        避免两个服务分别检查 GitHub 导致重复请求和数据不一致。
        version_service 的 _latest_result 是模块级全局变量，直接更新即可。
        """
        try:
            from app.services import version_service as vs
            tag = self._latest_info.get("tag", "")
            if not tag:
                return
            has_update = vs._compare_versions(tag, VERSION) > 0
            vs._latest_result.update({
                "current_version": VERSION,
                "latest_version": tag,
                "has_update": has_update,
                "release_url": self._latest_info.get("html_url", "")
                               or f"https://github.com/{GITHUB_REPO}/releases",
                "release_notes": self._latest_info.get("notes", "") if has_update else None,
                "checked_at": self._latest_info.get("checked_at"),
                "error": None,
            })
        except Exception as e:
            logger.debug(f"[updater] 同步 version_service 缓存失败: {e}")


def get_updater() -> Updater:
    """获取 Updater 单例实例"""
    return Updater()

"""
外部播放器脚本服务 - #26

生成外部播放器脚本，让用户可以通过生成的脚本直接在本地播放器中
打开 STRM 对应的 115 网盘视频。

支持播放器：
    - PotPlayer（Windows）：生成 .bat 批处理脚本
    - VLC（跨平台）：生成 shell 脚本
    - mpv（跨平台）：生成 shell 脚本
    - IINA（macOS）：生成 shell 脚本

支持自定义模板，持久化到 settings.json 的 "external_player" 键。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.external_player")


# ===== 常量 =====

SETTING_KEY: str = "external_player"
"""外部播放器配置在 settings.json 中的存储键。"""

# 内置播放器模板
# 模板中使用 {player_path} 和 {url} 占位符，生成时替换为实际值
BUILTIN_PLAYERS: dict[str, dict[str, Any]] = {
    "potplayer": {
        "name": "PotPlayer",
        "platform": "windows",
        "player_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        "template": '@echo off\n"{player_path}" "{url}"\n',
        "extension": ".bat",
        "mime_type": "application/x-bat",
    },
    "vlc": {
        "name": "VLC",
        "platform": "cross",
        "player_path": "vlc",
        "template": '"{player_path}" "{url}"\n',
        "extension": ".sh",
        "mime_type": "application/x-sh",
    },
    "mpv": {
        "name": "mpv",
        "platform": "cross",
        "player_path": "mpv",
        "template": '"{player_path}" --force-window "{url}"\n',
        "extension": ".sh",
        "mime_type": "application/x-sh",
    },
    "iina": {
        "name": "IINA",
        "platform": "macos",
        "player_path": "",
        "template": 'open -a IINA "{url}"\n',
        "extension": ".sh",
        "mime_type": "application/x-sh",
    },
}


class ExternalPlayerService:
    """
    外部播放器脚本服务（单例）

    管理播放器模板，生成可直接在本地执行的播放脚本。
    内置 PotPlayer / VLC / mpv / IINA 四种播放器模板，
    同时支持自定义模板（持久化到 settings.json）。

    配置结构::

        {
            "default_player": "potplayer",
            "custom_templates": {
                "my_player": {
                    "name": "My Player",
                    "platform": "windows",
                    "player_path": "C:\\\\path\\\\to\\\\player.exe",
                    "template": "@echo off\\n\\"{player_path}\\" \\"{url}\\"\\n",
                    "extension": ".bat",
                    "mime_type": "application/x-bat"
                }
            }
        }

    典型用法::

        from app.services.external_player import ExternalPlayerService

        service = ExternalPlayerService()
        result = service.generate_script("potplayer", "https://example.com/video.m3u8", "我的视频")
        # result = {"filename": "我的视频.bat", "content": "...", "mime_type": "application/x-bat"}
    """

    _instance: Optional["ExternalPlayerService"] = None

    def __new__(cls) -> "ExternalPlayerService":
        """单例模式：全局只有一个 ExternalPlayerService 实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

    # ===== 模板查询 =====

    @staticmethod
    def _get_player_template(player: str) -> Optional[dict[str, Any]]:
        """
        获取播放器模板（优先自定义模板，再查内置模板）。

        自定义模板可覆盖同名内置模板。

        Args:
            player: 播放器标识（如 potplayer / vlc / mpv / iina）。

        Returns:
            播放器模板字典，不存在时返回 None。
        """
        # 优先查找自定义模板（可覆盖内置模板）
        config: dict = read_setting(SETTING_KEY)
        custom_templates: dict = config.get("custom_templates", {})
        if player in custom_templates:
            return custom_templates[player]
        # 查找内置模板
        return BUILTIN_PLAYERS.get(player)

    def get_supported_players(self) -> list[dict[str, Any]]:
        """
        返回支持的播放器列表（内置 + 自定义）。

        Returns:
            播放器信息列表，每项包含::

                {
                    "id": str,          # 播放器标识
                    "name": str,        # 显示名称
                    "platform": str,    # 目标平台（windows / cross / macos）
                    "extension": str,   # 脚本扩展名
                    "is_custom": bool   # 是否为自定义模板
                }
        """
        players: list[dict[str, Any]] = []

        # 内置播放器
        for key, info in BUILTIN_PLAYERS.items():
            players.append({
                "id": key,
                "name": info.get("name", key),
                "platform": info.get("platform", "cross"),
                "extension": info.get("extension", ".sh"),
                "is_custom": False,
            })

        # 自定义播放器
        for key, info in self.get_custom_templates().items():
            players.append({
                "id": key,
                "name": info.get("name", key),
                "platform": info.get("platform", "cross"),
                "extension": info.get("extension", ".sh"),
                "is_custom": True,
            })

        return players

    # ===== 脚本生成 =====

    def generate_script(
        self, player: str, url: str, title: str = ""
    ) -> dict[str, str]:
        """
        生成播放脚本。

        根据播放器模板渲染脚本内容，用于在本地播放器中打开指定 URL。
        模板中的 ``{player_path}`` 和 ``{url}`` 占位符会被替换为实际值。

        Args:
            player: 播放器标识（如 potplayer / vlc / mpv / iina）。
            url: 视频 URL（STRM 对应的播放地址）。
            title: 视频标题，用于生成脚本文件名（可选，为空时使用默认名）。

        Returns:
            脚本信息字典::

                {
                    "filename": str,   # 脚本文件名（含扩展名）
                    "content": str,    # 脚本内容
                    "mime_type": str   # MIME 类型
                }

        Raises:
            ValueError: 播放器不支持或 URL 为空时抛出。
        """
        if not url:
            raise ValueError("URL 不能为空")

        template_info: Optional[dict[str, Any]] = self._get_player_template(player)
        if template_info is None:
            raise ValueError(f"不支持的播放器: {player}")

        player_path: str = template_info.get("player_path", "")
        template_str: str = template_info.get("template", "")
        extension: str = template_info.get("extension", ".sh")
        mime_type: str = template_info.get("mime_type", "application/octet-stream")

        # 渲染模板：替换 {player_path} 和 {url} 占位符
        # 使用 replace 而非 str.format，避免模板中其他花括号导致格式化异常
        content: str = (
            template_str
            .replace("{player_path}", player_path)
            .replace("{url}", url)
        )

        # 生成文件名：标题清理 + 扩展名
        safe_title: str = self._sanitize_filename(title) if title else "play_video"
        filename: str = f"{safe_title}{extension}"

        logger.debug(f"生成播放脚本: player={player}, filename={filename}")
        return {
            "filename": filename,
            "content": content,
            "mime_type": mime_type,
        }

    def generate_batch_scripts(
        self, player: str, items: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """
        批量生成播放脚本。

        逐条调用 :meth:`generate_script` 生成脚本。URL 为空的条目会被跳过，
        播放器不支持时返回空列表。

        Args:
            player: 播放器标识。
            items: 脚本条目列表，每项为 ``{"url": str, "title": str}``。

        Returns:
            脚本信息列表（同 :meth:`generate_script` 返回结构）。
        """
        # 提前校验播放器，避免逐条生成时重复报错
        if self._get_player_template(player) is None:
            logger.warning(f"批量生成脚本失败：不支持的播放器: {player}")
            return []

        results: list[dict[str, str]] = []
        for item in items:
            url: str = item.get("url", "")
            title: str = item.get("title", "")
            if not url:
                logger.debug("批量生成：URL 为空，跳过该条目")
                continue
            try:
                result: dict[str, str] = self.generate_script(player, url, title)
                results.append(result)
            except ValueError as e:
                logger.warning(f"批量生成脚本条目失败: {e}")

        logger.info(f"批量生成播放脚本完成: player={player}, 共 {len(results)} 个")
        return results

    # ===== 自定义模板管理 =====

    def get_custom_templates(self) -> dict[str, Any]:
        """
        从 settings.json 读取自定义模板。

        Returns:
            自定义模板字典，键为播放器标识，值为模板配置。
            无配置时返回空字典。
        """
        config: dict = read_setting(SETTING_KEY)
        return config.get("custom_templates", {})

    def save_custom_templates(self, templates: dict[str, Any]) -> bool:
        """
        保存自定义模板到 settings.json。

        保留已有的 default_player 配置，仅更新 custom_templates 字段。

        Args:
            templates: 自定义模板字典，键为播放器标识，值为模板配置。
                每个模板配置应包含 name / platform / player_path /
                template / extension / mime_type 字段。

        Returns:
            是否保存成功。
        """
        config: dict = read_setting(SETTING_KEY)
        config["custom_templates"] = templates
        ok: bool = save_setting(SETTING_KEY, config)
        if ok:
            logger.info(f"自定义播放器模板已保存（{len(templates)} 个）")
        else:
            logger.warning("自定义播放器模板保存失败")
        return ok

    # ===== 默认播放器 =====

    def get_default_player(self) -> str:
        """
        获取默认播放器标识。

        Returns:
            默认播放器标识，未配置时返回 "potplayer"。
        """
        config: dict = read_setting(SETTING_KEY)
        return config.get("default_player", "potplayer")

    def set_default_player(self, player: str) -> bool:
        """
        设置默认播放器。

        Args:
            player: 播放器标识。

        Returns:
            是否保存成功。
        """
        config: dict = read_setting(SETTING_KEY)
        config["default_player"] = player
        ok: bool = save_setting(SETTING_KEY, config)
        if ok:
            logger.info(f"默认播放器已设置为: {player}")
        else:
            logger.warning("默认播放器设置失败")
        return ok

    # ===== 工具方法 =====

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """
        清理文件名中的非法字符。

        替换 Windows / Linux 文件名中的非法字符为下划线，
        压缩连续空白，去除首尾空格和点，限制长度。

        Args:
            name: 原始文件名（不含扩展名）。

        Returns:
            清理后的安全文件名。
        """
        if not name or not name.strip():
            return "play_video"
        # 替换 Windows / Linux 文件名非法字符
        illegal_chars = '<>:"/\\|?*'
        result: str = name
        for ch in illegal_chars:
            result = result.replace(ch, "_")
        # 压缩连续空白为单个下划线
        result = re.sub(r"\s+", "_", result)
        # 去除首尾空格和点
        result = result.strip(". ")
        # 限制长度（避免文件名过长）
        if len(result) > 100:
            result = result[:100]
        return result if result else "play_video"

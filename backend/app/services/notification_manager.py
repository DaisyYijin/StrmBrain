"""
多渠道通知管理器
================

参考 qmediasync 的设计，实现增强的多渠道通知系统。

支持渠道：
    - Telegram（支持代理 URL、图片发送）
    - Bark（iOS 推送，支持自定义服务器 URL、声音、图标）
    - Server 酱（微信推送）
    - MeoW（第三方推送）
    - 自定义 Webhook（GET/POST、JSON/Form/Text 模板、Bearer/Basic/Header/Query 鉴权）
    - 企业微信（兼容现有 WeChatAppService）

事件规则分发：
    每种事件类型（sync_complete / scrape_complete / system_alert / error）
    可映射到多个渠道，按规则将通知分发到对应渠道。
    渠道配置存储在 settings.json 的 "notification_channels" 键，
    规则配置存储在 settings.json 的 "notification_rules" 键。

每个渠道发送有 15 秒超时，失败记录错误但不中断其他渠道。
使用 httpx 异步发送。
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from typing import Any, Optional

import httpx

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, save_setting

logger = get_logger("app.services.notification_manager")


# ===== 常量 =====

NOTIFY_TIMEOUT: float = 15.0
"""每个渠道发送的超时时间（秒）。"""

CHANNELS_KEY: str = "notification_channels"
"""渠道配置在 settings.json 中的存储键。"""

RULES_KEY: str = "notification_rules"
"""规则配置在 settings.json 中的存储键。"""

EVENT_TYPES: list[str] = ["sync_complete", "scrape_complete", "system_alert", "error"]
"""支持的事件类型。"""

CHANNEL_TYPES: list[str] = [
    "telegram",
    "bark",
    "serverchan",
    "meow",
    "webhook",
    "wechat_work",
]
"""支持的渠道类型。"""


# ===== 模板渲染工具 =====


def render_template(text: str, ctx: dict[str, Any]) -> str:
    """
    简单变量替换：将 ``{{key}}`` 替换为 ``ctx`` 中对应的值。

    Args:
        text: 包含 ``{{key}}`` 占位符的模板字符串。
        ctx: 变量上下文。

    Returns:
        替换后的字符串。
    """
    if not text:
        return ""
    result = text
    for key, value in ctx.items():
        result = result.replace("{{" + key + "}}", str(value) if value is not None else "")
    return result


def render_json_template(template_str: str, ctx: dict[str, Any]) -> Any:
    """
    渲染 JSON 模板：先解析为 JSON 对象，再递归替换字符串值中的 ``{{key}}`` 变量。

    这样可以安全处理包含特殊字符（如引号、换行）的变量值，避免破坏 JSON 结构。
    若模板不是合法 JSON，退回到纯文本替换。

    Args:
        template_str: JSON 格式的模板字符串。
        ctx: 变量上下文。

    Returns:
        渲染后的 Python 对象（dict / list / str）。
    """
    try:
        data = json.loads(template_str)
    except (json.JSONDecodeError, TypeError):
        return render_template(template_str, ctx)

    def _substitute(obj: Any) -> Any:
        if isinstance(obj, str):
            return render_template(obj, ctx)
        if isinstance(obj, dict):
            return {k: _substitute(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_substitute(v) for v in obj]
        return obj

    return _substitute(data)


def clean_empty_values(obj: Any) -> Any:
    """
    递归清理空值（参考 qmediasync 的 cleanEmptyValues）。

    移除值为 ``None``、空字符串、空字典、空列表的字段，
    确保发送的 JSON 数据不包含无意义的空值。

    Args:
        obj: 待清理的对象。

    Returns:
        清理后的对象。
    """
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            cleaned_v = clean_empty_values(v)
            if cleaned_v is None:
                continue
            if isinstance(cleaned_v, str) and cleaned_v == "":
                continue
            if isinstance(cleaned_v, (dict, list)) and len(cleaned_v) == 0:
                continue
            cleaned[k] = cleaned_v
        return cleaned
    if isinstance(obj, list):
        return [
            clean_empty_values(v)
            for v in obj
            if v is not None and v != ""
        ]
    return obj


def _create_async_client(
    proxy_url: str = "",
    timeout: float = NOTIFY_TIMEOUT,
) -> httpx.AsyncClient:
    """
    创建 httpx 异步客户端，兼容不同版本的代理参数写法。

    Args:
        proxy_url: 代理 URL（可选）。
        timeout: 请求超时时间（秒）。

    Returns:
        httpx.AsyncClient 实例。
    """
    if not proxy_url:
        return httpx.AsyncClient(timeout=timeout)
    try:
        # httpx >= 0.26 使用 proxy 参数
        return httpx.AsyncClient(timeout=timeout, proxy=proxy_url)
    except TypeError:
        # 旧版 httpx（< 0.26）使用 proxies 参数
        return httpx.AsyncClient(timeout=timeout, proxies=proxy_url)


# ===== 渠道处理器基类 =====


class BaseChannelHandler:
    """
    通知渠道处理器基类。

    所有具体渠道处理器需继承此类并实现 :meth:`send` 方法。
    """

    channel_type: str = ""

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Args:
            config: 渠道配置字典。
        """
        self.config: dict[str, Any] = config or {}

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        发送通知。

        Args:
            title: 通知标题。
            content: 通知内容。
            image: 图片 URL（可选）。
            metadata: 附加元数据（可选）。

        Returns:
            是否发送成功。

        Raises:
            NotImplementedError: 子类必须实现此方法。
        """
        raise NotImplementedError


# ===== 具体渠道处理器 =====


class TelegramHandler(BaseChannelHandler):
    """Telegram Bot 通知渠道处理器（支持代理 URL、图片发送）。"""

    channel_type = "telegram"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        bot_token: str = self.config.get("bot_token", "").strip()
        chat_id: str = self.config.get("chat_id", "").strip()
        proxy_url: str = self.config.get("proxy_url", "").strip()

        if not bot_token or not chat_id:
            logger.warning("Telegram 渠道配置不完整（缺少 bot_token 或 chat_id）")
            return False

        html_content: str = self._markdown_to_html(content)
        text: str = f"<b>{title}</b>\n{html_content}"
        base_url: str = f"https://api.telegram.org/bot{bot_token}"

        try:
            async with _create_async_client(proxy_url) as client:
                # 若提供了图片 URL，优先以图片消息发送（caption 携带文本）
                if image:
                    try:
                        resp = await client.post(
                            f"{base_url}/sendPhoto",
                            json={
                                "chat_id": chat_id,
                                "photo": image,
                                "caption": text,
                                "parse_mode": "HTML",
                            },
                        )
                        if resp.status_code == 200 and resp.json().get("ok"):
                            logger.info("Telegram 图片通知发送成功")
                            return True
                        logger.warning(
                            "Telegram 图片发送失败，回退到文本消息: "
                            f"{resp.json().get('description', 'HTTP ' + str(resp.status_code))}"
                        )
                    except Exception as e:
                        logger.warning(f"Telegram 图片发送异常，回退到文本消息: {e}")

                # 文本消息
                resp = await client.post(
                    f"{base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        logger.info("Telegram 通知发送成功")
                        return True
                    logger.warning(
                        f"Telegram 通知发送失败: {data.get('description', '未知错误')}"
                    )
                    return False
                logger.warning(f"Telegram 通知发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Telegram 通知发送异常: {e}")
            return False

    @staticmethod
    def _markdown_to_html(content: str) -> str:
        """将简单 Markdown 转换为 Telegram 支持的 HTML 子集。"""
        # 先转义 HTML 特殊字符
        text = html.escape(content)
        # **bold** -> <b>bold</b>
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        # `code` -> <code>code</code>
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        # 移除引用符号 > （已转义为 &gt; ）
        text = text.replace("&gt; ", "")
        return text


class BarkHandler(BaseChannelHandler):
    """Bark（iOS 推送）通知渠道处理器，支持自定义服务器 URL、声音、图标。"""

    channel_type = "bark"

    DEFAULT_SERVER: str = "https://api.day.app"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        device_key: str = self.config.get("device_key", "").strip()
        server_url: str = (
            self.config.get("server_url", "").strip() or self.DEFAULT_SERVER
        )
        sound: str = self.config.get("sound", "").strip()
        icon: str = self.config.get("icon", "").strip()
        group: str = self.config.get("group", "").strip()

        server_url = server_url.rstrip("/")

        if not device_key:
            logger.warning("Bark 渠道配置不完整（缺少 device_key）")
            return False

        payload: dict[str, Any] = {
            "title": title,
            "body": content,
        }
        if sound:
            payload["sound"] = sound
        if icon:
            payload["icon"] = icon
        if image:
            payload["image"] = image
        if group:
            payload["group"] = group

        url: str = f"{server_url}/{device_key}"

        try:
            async with _create_async_client() as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 200:
                        logger.info("Bark 通知发送成功")
                        return True
                    logger.warning(
                        f"Bark 通知发送失败: {data.get('message', '未知错误')}"
                    )
                    return False
                logger.warning(f"Bark 通知发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Bark 通知发送异常: {e}")
            return False


class ServerChanHandler(BaseChannelHandler):
    """Server 酱（微信推送）通知渠道处理器。"""

    channel_type = "serverchan"

    DEFAULT_SERVER: str = "https://sctapi.ftqq.com"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        sendkey: str = self.config.get("sendkey", "").strip()
        server_url: str = (
            self.config.get("server_url", "").strip() or self.DEFAULT_SERVER
        )
        server_url = server_url.rstrip("/")

        if not sendkey:
            logger.warning("Server酱 渠道配置不完整（缺少 sendkey）")
            return False

        url: str = f"{server_url}/{sendkey}.send"
        payload: dict[str, str] = {"title": title, "desp": content}

        try:
            async with _create_async_client() as client:
                resp = await client.post(url, data=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0 or data.get("errno") == 0:
                        logger.info("Server酱 通知发送成功")
                        return True
                    logger.warning(
                        f"Server酱 通知发送失败: {data.get('message', '未知错误')}"
                    )
                    return False
                logger.warning(f"Server酱 通知发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Server酱 通知发送异常: {e}")
            return False


class MeoWHandler(BaseChannelHandler):
    """MeoW（第三方推送）通知渠道处理器。"""

    channel_type = "meow"

    DEFAULT_SERVER: str = "https://meow.diao.liana.com"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        sendkey: str = self.config.get("sendkey", "").strip()
        server_url: str = (
            self.config.get("server_url", "").strip() or self.DEFAULT_SERVER
        )
        server_url = server_url.rstrip("/")

        if not sendkey:
            logger.warning("MeoW 渠道配置不完整（缺少 sendkey）")
            return False

        url: str = f"{server_url}/{sendkey}.send"
        payload: dict[str, str] = {"title": title, "desp": content}

        try:
            async with _create_async_client() as client:
                resp = await client.post(url, data=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0 or data.get("success"):
                        logger.info("MeoW 通知发送成功")
                        return True
                    logger.warning(
                        f"MeoW 通知发送失败: {data.get('message', '未知错误')}"
                    )
                    return False
                logger.warning(f"MeoW 通知发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"MeoW 通知发送异常: {e}")
            return False


class WebhookHandler(BaseChannelHandler):
    """
    自定义 Webhook 通知渠道处理器。

    支持：
        - 请求方法：GET / POST
        - 请求格式：JSON / Form / Text
        - 鉴权方式：Bearer / Basic / Header / Query
        - 模板变量替换：{{title}}、{{content}}、{{timestamp}}、{{image}}

    Webhook 配置结构::

        {
            "url": "https://example.com/webhook",
            "method": "POST",
            "format": "json",        # json / form / text
            "body_template": "{\"title\": \"{{title}}\", \"content\": \"{{content}}\"}",
            "headers": {"X-Custom": "value"},
            "auth": {
                "type": "bearer",     # bearer / basic / header / query / none
                "token": "...",       # bearer
                "username": "...",    # basic
                "password": "...",    # basic
                "key": "...",         # header / query
                "value": "..."        # header / query
            }
        }
    """

    channel_type = "webhook"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        config = self.config
        url: str = config.get("url", "").strip()
        method: str = config.get("method", "POST").upper()
        fmt: str = config.get("format", "json").lower()
        body_template: str = config.get("body_template", "")
        extra_headers: dict[str, str] = config.get("headers", {})
        auth: dict[str, str] = config.get("auth", {})

        if not url:
            logger.warning("Webhook 渠道配置不完整（缺少 url）")
            return False

        # 构建变量上下文
        ctx: dict[str, Any] = {
            "title": title,
            "content": content,
            "timestamp": str(int(time.time())),
            "image": image or "",
        }

        # URL 中的变量替换
        url = render_template(url, ctx)

        # 构建请求头
        headers: dict[str, str] = dict(extra_headers)

        # 构建查询参数
        params: dict[str, str] = {}

        # 应用鉴权
        auth_type: str = auth.get("type", "none").lower()
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {auth.get('token', '')}"
        elif auth_type == "basic":
            credentials: str = base64.b64encode(
                f"{auth.get('username', '')}:{auth.get('password', '')}".encode("utf-8")
            ).decode("utf-8")
            headers["Authorization"] = f"Basic {credentials}"
        elif auth_type == "header":
            key: str = auth.get("key", "Authorization")
            headers[key] = auth.get("value", "")
        elif auth_type == "query":
            params[auth.get("key", "token")] = auth.get("value", "")

        # 构建请求参数
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "params": params,
            "timeout": NOTIFY_TIMEOUT,
        }

        if method != "GET" and body_template:
            if fmt == "json":
                body = render_json_template(body_template, ctx)
                if isinstance(body, dict):
                    body = clean_empty_values(body)
                    request_kwargs["json"] = body
                else:
                    # 模板不是合法 JSON，作为纯文本发送
                    rendered = render_template(body_template, ctx)
                    request_kwargs["content"] = rendered.encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
            elif fmt == "form":
                body = render_json_template(body_template, ctx)
                if isinstance(body, dict):
                    body = clean_empty_values(body)
                    request_kwargs["data"] = body
                else:
                    request_kwargs["content"] = render_template(
                        body_template, ctx
                    ).encode("utf-8")
            else:  # text
                rendered = render_template(body_template, ctx)
                request_kwargs["content"] = rendered.encode("utf-8")
                headers.setdefault("Content-Type", "text/plain; charset=utf-8")

        try:
            async with _create_async_client() as client:
                resp = await client.request(method, url, **request_kwargs)
                if resp.status_code < 400:
                    logger.info(
                        f"Webhook 通知发送成功: {url} (HTTP {resp.status_code})"
                    )
                    return True
                logger.warning(
                    f"Webhook 通知发送失败: HTTP {resp.status_code}, "
                    f"响应: {resp.text[:200]}"
                )
                return False
        except Exception as e:
            logger.error(f"Webhook 通知发送异常: {e}")
            return False


class WeChatWorkHandler(BaseChannelHandler):
    """企业微信通知渠道处理器（兼容现有 WeChatAppService）。"""

    channel_type = "wechat_work"

    async def send(
        self,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        try:
            from app.services.wechat_app import WeChatAppService

            if not WeChatAppService.is_configured():
                logger.warning("企业微信应用未配置或未启用")
                return False
            markdown: str = f"### {title}\n{content}"
            return await WeChatAppService.send_markdown(markdown)
        except Exception as e:
            logger.error(f"企业微信通知发送异常: {e}")
            return False


# ===== 处理器工厂 =====


_CHANNEL_HANDLER_CLASSES: dict[str, type[BaseChannelHandler]] = {
    "telegram": TelegramHandler,
    "bark": BarkHandler,
    "serverchan": ServerChanHandler,
    "meow": MeoWHandler,
    "webhook": WebhookHandler,
    "wechat_work": WeChatWorkHandler,
}


def create_channel_handler(channel: dict[str, Any]) -> Optional[BaseChannelHandler]:
    """
    根据渠道配置创建对应的渠道处理器实例。

    Args:
        channel: 渠道配置字典，需包含 ``type`` 和 ``config`` 字段。

    Returns:
        渠道处理器实例，不支持的类型返回 None。
    """
    channel_type: str = channel.get("type", "")
    config: dict[str, Any] = channel.get("config", {})
    handler_cls: Optional[type[BaseChannelHandler]] = _CHANNEL_HANDLER_CLASSES.get(
        channel_type
    )
    if handler_cls is None:
        logger.warning(f"不支持的渠道类型: {channel_type}")
        return None
    return handler_cls(config)


# ===== 通知管理器 =====


class NotificationManager:
    """
    多渠道通知管理器。

    管理通知渠道配置和事件分发规则，按规则将通知分发到对应渠道。
    渠道配置存储在 settings.json 的 ``notification_channels`` 键，
    规则配置存储在 settings.json 的 ``notification_rules`` 键。

    典型用法::

        from app.services.notification_manager import get_notification_manager

        manager = get_notification_manager()
        await manager.send_notification("sync_complete", "同步完成", "共 100 个文件")
    """

    def __init__(self) -> None:
        self._channels: dict[int, dict[str, Any]] = {}
        self._rules: list[dict[str, Any]] = []
        self._handlers: dict[int, BaseChannelHandler] = {}
        self._loaded: bool = False
        self.load_channels()

    # ===== 加载 =====

    def load_channels(self) -> None:
        """从存储加载渠道配置和规则到内存缓存。"""
        channels_data: dict = read_setting(CHANNELS_KEY)
        rules_data: dict = read_setting(RULES_KEY)

        channels_list: list[dict] = channels_data.get("channels", [])
        self._channels = {ch["id"]: ch for ch in channels_list if "id" in ch}
        self._rules = rules_data.get("rules", [])

        # 清理旧的处理器缓存，下次发送时重新创建
        self._handlers.clear()

        self._loaded = True
        logger.info(
            f"通知配置已加载: 渠道 {len(self._channels)} 个, 规则 {len(self._rules)} 条"
        )

    def reload_channel(self, channel_id: int) -> bool:
        """
        重新加载单个渠道的配置。

        从存储重新读取该渠道的最新配置并更新内存缓存，
        同时清除该渠道的处理器缓存（下次发送时重新创建）。

        Args:
            channel_id: 渠道 ID。

        Returns:
            是否成功重新加载（渠道存在则返回 True）。
        """
        channels_data: dict = read_setting(CHANNELS_KEY)
        channels_list: list[dict] = channels_data.get("channels", [])

        for ch in channels_list:
            if ch.get("id") == channel_id:
                self._channels[channel_id] = ch
                # 清除该渠道的处理器缓存，下次发送时重新创建
                self._handlers.pop(channel_id, None)
                logger.info(f"渠道 {channel_id} 配置已重新加载")
                return True

        # 渠道不存在，从缓存中移除
        self._channels.pop(channel_id, None)
        self._handlers.pop(channel_id, None)
        logger.warning(f"渠道 {channel_id} 不存在，已从缓存移除")
        return False

    # ===== 渠道管理 =====

    def get_channels(self) -> list[dict[str, Any]]:
        """获取所有渠道配置列表。"""
        if not self._loaded:
            self.load_channels()
        return list(self._channels.values())

    def get_channel(self, channel_id: int) -> Optional[dict[str, Any]]:
        """
        获取单个渠道配置。

        Args:
            channel_id: 渠道 ID。

        Returns:
            渠道配置字典，不存在时返回 None。
        """
        if not self._loaded:
            self.load_channels()
        return self._channels.get(channel_id)

    def add_channel(self, channel_data: dict[str, Any]) -> dict[str, Any]:
        """
        添加通知渠道。

        自动分配 ID 和 created_at 时间戳。

        Args:
            channel_data: 渠道配置数据（不含 id 和 created_at）。

        Returns:
            添加后的完整渠道配置（含 id 和 created_at）。
        """
        channels_data: dict = read_setting(CHANNELS_KEY)
        channels_list: list[dict] = channels_data.get("channels", [])
        next_id: int = channels_data.get("next_id", 1)

        channel_id: int = next_id
        now: int = int(time.time())
        new_channel: dict[str, Any] = {
            "id": channel_id,
            "name": channel_data.get("name", ""),
            "type": channel_data.get("type", ""),
            "config": channel_data.get("config", {}),
            "is_enabled": channel_data.get("is_enabled", True),
            "created_at": now,
        }

        channels_list.append(new_channel)
        channels_data["channels"] = channels_list
        channels_data["next_id"] = next_id + 1
        save_setting(CHANNELS_KEY, channels_data)

        # 更新内存缓存
        self._channels[channel_id] = new_channel
        logger.info(
            f"通知渠道已添加: id={channel_id}, "
            f"name={new_channel['name']}, type={new_channel['type']}"
        )
        return new_channel

    def update_channel(
        self, channel_id: int, channel_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """
        更新通知渠道配置。

        保留 id 和 created_at，更新其他字段。

        Args:
            channel_id: 渠道 ID。
            channel_data: 新的渠道配置数据（不含 id）。

        Returns:
            更新后的完整渠道配置，渠道不存在时返回 None。
        """
        channels_data: dict = read_setting(CHANNELS_KEY)
        channels_list: list[dict] = channels_data.get("channels", [])

        for ch in channels_list:
            if ch.get("id") == channel_id:
                # 保留 id 和 created_at，更新其他字段
                ch["name"] = channel_data.get("name", ch.get("name", ""))
                ch["type"] = channel_data.get("type", ch.get("type", ""))
                ch["config"] = channel_data.get("config", ch.get("config", {}))
                ch["is_enabled"] = channel_data.get(
                    "is_enabled", ch.get("is_enabled", True)
                )

                channels_data["channels"] = channels_list
                save_setting(CHANNELS_KEY, channels_data)

                # 更新内存缓存
                self._channels[channel_id] = ch
                # 清除处理器缓存
                self._handlers.pop(channel_id, None)
                logger.info(f"通知渠道已更新: id={channel_id}")
                return ch

        logger.warning(f"通知渠道不存在: id={channel_id}")
        return None

    def delete_channel(self, channel_id: int) -> bool:
        """
        删除通知渠道。

        同时会从所有规则中移除该渠道 ID。

        Args:
            channel_id: 渠道 ID。

        Returns:
            是否删除成功。
        """
        channels_data: dict = read_setting(CHANNELS_KEY)
        channels_list: list[dict] = channels_data.get("channels", [])

        new_list: list[dict] = [
            ch for ch in channels_list if ch.get("id") != channel_id
        ]
        if len(new_list) == len(channels_list):
            logger.warning(f"通知渠道不存在: id={channel_id}")
            return False

        channels_data["channels"] = new_list
        save_setting(CHANNELS_KEY, channels_data)

        # 更新内存缓存
        self._channels.pop(channel_id, None)
        self._handlers.pop(channel_id, None)

        # 从所有规则中移除该渠道 ID
        self._remove_channel_from_rules(channel_id)

        logger.info(f"通知渠道已删除: id={channel_id}")
        return True

    # ===== 规则管理 =====

    def get_rules(self) -> list[dict[str, Any]]:
        """获取所有分发规则。"""
        if not self._loaded:
            self.load_channels()
        return list(self._rules)

    def get_rules_for_event(self, event_type: str) -> list[dict[str, Any]]:
        """
        获取指定事件类型的所有启用规则。

        Args:
            event_type: 事件类型。

        Returns:
            匹配的规则列表。
        """
        if not self._loaded:
            self.load_channels()
        return [
            rule
            for rule in self._rules
            if rule.get("event_type") == event_type
            and rule.get("is_enabled", True)
        ]

    def add_rule(self, rule_data: dict[str, Any]) -> dict[str, Any]:
        """
        添加分发规则。

        Args:
            rule_data: 规则配置数据（不含 id）。

        Returns:
            添加后的完整规则配置（含 id）。
        """
        rules_data: dict = read_setting(RULES_KEY)
        rules_list: list[dict] = rules_data.get("rules", [])
        next_id: int = rules_data.get("next_id", 1)

        rule_id: int = next_id
        new_rule: dict[str, Any] = {
            "id": rule_id,
            "event_type": rule_data.get("event_type", ""),
            "channel_ids": rule_data.get("channel_ids", []),
            "is_enabled": rule_data.get("is_enabled", True),
        }

        rules_list.append(new_rule)
        rules_data["rules"] = rules_list
        rules_data["next_id"] = next_id + 1
        save_setting(RULES_KEY, rules_data)

        self._rules.append(new_rule)
        logger.info(
            f"通知规则已添加: id={rule_id}, event_type={new_rule['event_type']}"
        )
        return new_rule

    def update_rule(
        self, rule_id: int, rule_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """
        更新分发规则。

        Args:
            rule_id: 规则 ID。
            rule_data: 新的规则配置数据（不含 id）。

        Returns:
            更新后的完整规则配置，规则不存在时返回 None。
        """
        rules_data: dict = read_setting(RULES_KEY)
        rules_list: list[dict] = rules_data.get("rules", [])

        for rule in rules_list:
            if rule.get("id") == rule_id:
                rule["event_type"] = rule_data.get(
                    "event_type", rule.get("event_type", "")
                )
                rule["channel_ids"] = rule_data.get(
                    "channel_ids", rule.get("channel_ids", [])
                )
                rule["is_enabled"] = rule_data.get(
                    "is_enabled", rule.get("is_enabled", True)
                )

                rules_data["rules"] = rules_list
                save_setting(RULES_KEY, rules_data)

                # 更新内存缓存
                for i, r in enumerate(self._rules):
                    if r.get("id") == rule_id:
                        self._rules[i] = rule
                        break

                logger.info(f"通知规则已更新: id={rule_id}")
                return rule

        logger.warning(f"通知规则不存在: id={rule_id}")
        return None

    def delete_rule(self, rule_id: int) -> bool:
        """
        删除分发规则。

        Args:
            rule_id: 规则 ID。

        Returns:
            是否删除成功。
        """
        rules_data: dict = read_setting(RULES_KEY)
        rules_list: list[dict] = rules_data.get("rules", [])

        new_list: list[dict] = [r for r in rules_list if r.get("id") != rule_id]
        if len(new_list) == len(rules_list):
            logger.warning(f"通知规则不存在: id={rule_id}")
            return False

        rules_data["rules"] = new_list
        save_setting(RULES_KEY, rules_data)

        self._rules = [r for r in self._rules if r.get("id") != rule_id]
        logger.info(f"通知规则已删除: id={rule_id}")
        return True

    def _remove_channel_from_rules(self, channel_id: int) -> None:
        """从所有规则中移除指定渠道 ID。"""
        changed: bool = False
        for rule in self._rules:
            if channel_id in rule.get("channel_ids", []):
                rule["channel_ids"] = [
                    cid for cid in rule["channel_ids"] if cid != channel_id
                ]
                changed = True

        if changed:
            rules_data: dict = read_setting(RULES_KEY)
            rules_data["rules"] = self._rules
            save_setting(RULES_KEY, rules_data)
            logger.info(f"已从规则中移除渠道 {channel_id}")

    # ===== 通知发送 =====

    def _get_channel_ids_for_event(self, event_type: str) -> list[int]:
        """获取事件类型对应的所有启用渠道 ID（去重，保持顺序）。"""
        rules: list[dict[str, Any]] = self.get_rules_for_event(event_type)
        channel_ids: list[int] = []
        seen: set[int] = set()
        for rule in rules:
            for cid in rule.get("channel_ids", []):
                if cid not in seen:
                    seen.add(cid)
                    channel_ids.append(cid)
        return channel_ids

    def _get_or_create_handler(
        self, channel: dict[str, Any]
    ) -> Optional[BaseChannelHandler]:
        """获取或创建渠道处理器（带缓存）。"""
        channel_id: int = channel.get("id", 0)
        if channel_id in self._handlers:
            return self._handlers[channel_id]
        handler: Optional[BaseChannelHandler] = create_channel_handler(channel)
        if handler is not None:
            self._handlers[channel_id] = handler
        return handler

    async def _send_to_channel(
        self,
        channel: dict[str, Any],
        title: str,
        content: str,
        image: str,
        metadata: Optional[dict[str, Any]],
    ) -> bool:
        """
        向单个渠道发送通知（带超时和异常隔离）。

        任何异常（包括超时）都不会向外抛出，确保不影响其他渠道的发送。

        Args:
            channel: 渠道配置。
            title: 通知标题。
            content: 通知内容。
            image: 图片 URL。
            metadata: 附加元数据。

        Returns:
            是否发送成功。
        """
        channel_name: str = channel.get("name", str(channel.get("id", "")))
        handler: Optional[BaseChannelHandler] = self._get_or_create_handler(channel)
        if handler is None:
            logger.warning(f"渠道 '{channel_name}' 无可用处理器，跳过")
            return False
        try:
            return await asyncio.wait_for(
                handler.send(title, content, image=image, metadata=metadata),
                timeout=NOTIFY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"渠道 '{channel_name}' 发送超时（{NOTIFY_TIMEOUT}s）")
            return False
        except Exception as e:
            logger.error(f"渠道 '{channel_name}' 发送失败: {e}")
            return False

    async def send_notification(
        self,
        event_type: str,
        title: str,
        content: str,
        image: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        按规则分发通知到对应渠道。

        根据事件类型查找启用的规则，收集对应的渠道 ID，
        并发向所有目标渠道发送通知。每个渠道有 15 秒超时，
        失败记录错误但不中断其他渠道。

        Args:
            event_type: 事件类型
                （sync_complete / scrape_complete / system_alert / error）。
            title: 通知标题。
            content: 通知内容。
            image: 图片 URL（可选）。
            metadata: 附加元数据（可选）。

        Returns:
            分发结果字典::

                {
                    "event_type": "sync_complete",
                    "sent": 2,       # 成功数
                    "total": 3,      # 总数
                    "results": [     # 各渠道结果
                        {
                            "channel_id": 1,
                            "channel_name": "我的Telegram",
                            "success": True
                        },
                        ...
                    ]
                }
        """
        if not self._loaded:
            self.load_channels()

        if event_type not in EVENT_TYPES:
            logger.warning(f"未知的事件类型: {event_type}")
            return {
                "event_type": event_type,
                "sent": 0,
                "total": 0,
                "results": [],
                "error": "未知事件类型",
            }

        channel_ids: list[int] = self._get_channel_ids_for_event(event_type)
        if not channel_ids:
            logger.debug(f"事件 '{event_type}' 无匹配的启用规则，跳过通知")
            return {"event_type": event_type, "sent": 0, "total": 0, "results": []}

        # 收集目标渠道（存在且启用）
        target_channels: list[dict[str, Any]] = []
        for channel_id in channel_ids:
            channel: Optional[dict[str, Any]] = self._channels.get(channel_id)
            if channel is None:
                logger.warning(f"渠道 {channel_id} 不存在，跳过")
                continue
            if not channel.get("is_enabled", True):
                logger.debug(f"渠道 {channel_id} 已禁用，跳过")
                continue
            target_channels.append(channel)

        if not target_channels:
            logger.info(f"事件 '{event_type}' 无可用渠道，跳过通知")
            return {"event_type": event_type, "sent": 0, "total": 0, "results": []}

        # 并发发送到所有目标渠道
        send_tasks: list[asyncio.Task] = [
            asyncio.ensure_future(
                self._send_to_channel(channel, title, content, image, metadata)
            )
            for channel in target_channels
        ]

        gathered: list[Any] = await asyncio.gather(
            *send_tasks, return_exceptions=True
        )

        results: list[dict[str, Any]] = []
        for channel, result in zip(target_channels, gathered):
            channel_id: int = channel.get("id", 0)
            channel_name: str = channel.get("name", "")
            if isinstance(result, Exception):
                logger.error(f"渠道 '{channel_name}' 发送异常: {result}")
                results.append(
                    {
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "success": False,
                        "error": str(result),
                    }
                )
            else:
                results.append(
                    {
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "success": result,
                    }
                )

        sent: int = sum(1 for r in results if r.get("success"))
        logger.info(
            f"通知分发完成: 事件={event_type}, 标题='{title}', "
            f"渠道数={len(results)}, 成功={sent}"
        )
        return {
            "event_type": event_type,
            "sent": sent,
            "total": len(results),
            "results": results,
        }


# ===== 全局单例 =====

global_notification_manager: Optional[NotificationManager] = None


def get_notification_manager() -> NotificationManager:
    """
    获取全局通知管理器单例。

    首次调用时自动创建并加载配置。

    Returns:
        NotificationManager 实例。
    """
    global global_notification_manager
    if global_notification_manager is None:
        global_notification_manager = NotificationManager()
    return global_notification_manager


def init_notification_manager() -> NotificationManager:
    """
    初始化全局通知管理器单例。

    若已存在则重新加载配置，确保与存储同步。

    Returns:
        NotificationManager 实例。
    """
    global global_notification_manager
    if global_notification_manager is None:
        global_notification_manager = NotificationManager()
    else:
        global_notification_manager.load_channels()
    return global_notification_manager

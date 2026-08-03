"""
通知服务 - 支持企业微信 / Telegram / QQ 机器人推送
"""
import httpx
from typing import Optional
from app.core.logbuffer import get_logger

logger = get_logger("app.services.notification")


class NotificationService:
    """通知服务"""

    @staticmethod
    def _get_settings() -> dict:
        """从数据库读取通知设置"""
        try:
            from app.core.db_helper import read_setting
            return read_setting("notification")
        except Exception as e:
            logger.debug(f"读取通知设置失败: {e}")
            return {}

    # ===== 企业微信（自建应用模式）=====

    @staticmethod
    async def send_wechat_work_message(title: str, content: str) -> bool:
        """发送企业微信应用消息（Markdown 格式）"""
        from app.services.wechat_app import WeChatAppService
        if not WeChatAppService.is_configured():
            return False
        return await WeChatAppService.send_markdown(
            f"### {title}\n{content}"
        )

    # ===== Telegram =====

    @staticmethod
    async def send_telegram_message(bot_token: str, chat_id: str, title: str, content: str) -> bool:
        """
        发送 Telegram Bot 消息（HTML 格式）
        bot_token: Telegram Bot 的 API Token
        chat_id: 目标聊天 ID（个人 / 群组 / 频道）
        """
        if not bot_token or not chat_id:
            return False

        # 将 Markdown 粗体/引用转为 HTML
        html_content = content
        html_content = html_content.replace("**", "<b>").replace("**", "</b>")
        # 企业微信引用符号 > 转为换行
        html_content = html_content.replace("> ", "")
        # 转义 HTML 特殊字符
        import html as html_mod
        html_content = html_mod.escape(html_content)
        # 恢复 <b> 标签
        html_content = html_content.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")

        text = f"<b>{title}</b>\n{html_content}"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=10.0)
                if response.status_code == 200:
                    resp_data = response.json()
                    if resp_data.get("ok"):
                        logger.info("Telegram 通知发送成功")
                        return True
                    else:
                        logger.warning(f"Telegram 通知发送失败: {resp_data.get('description', '未知错误')}")
                        return False
                else:
                    logger.warning(f"Telegram 通知发送失败: HTTP {response.status_code}")
                    return False
        except Exception as e:
            logger.warning(f"Telegram 通知发送异常: {e}")
            return False

    # ===== QQ 机器人（基于 go-cqhttp / NapCat HTTP API）=====

    @staticmethod
    async def send_qq_message(api_url: str, access_token: str, user_id: str, group_id: str, title: str, content: str) -> bool:
        """
        发送 QQ 机器人消息（通过 go-cqhttp / NapCat 兼容的 HTTP API）
        api_url: go-cqhttp/NapCat 的 HTTP 上报地址（如 http://127.0.0.1:5700）
        access_token: access_token（可为空）
        user_id: 私信目标 QQ 号（与 group_id 二选一）
        group_id: 群号（与 user_id 二选一）
        """
        if not api_url:
            return False
        if not user_id and not group_id:
            return False

        # 简化为纯文本消息
        plain = content.replace("**", "").replace("> ", "").replace("`", "")
        message = f"【{title}】\n{plain}"

        url = f"{api_url.rstrip('/')}/send_msg"
        params = {"access_token": access_token} if access_token else {}
        payload = {"message": message}
        if group_id:
            payload["group_id"] = int(group_id)
        elif user_id:
            payload["user_id"] = int(user_id)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, params=params, timeout=10.0)
                if response.status_code == 200:
                    resp_data = response.json()
                    if resp_data.get("status") == "ok" or resp_data.get("retcode") == 0:
                        logger.info("QQ 机器人通知发送成功")
                        return True
                    else:
                        logger.warning(f"QQ 机器人通知发送失败: {resp_data.get('msg', resp_data.get('word', '未知错误'))}")
                        return False
                else:
                    logger.warning(f"QQ 机器人通知发送失败: HTTP {response.status_code}")
                    return False
        except Exception as e:
            logger.warning(f"QQ 机器人通知发送异常: {e}")
            return False

    # ===== 统一发送（多渠道）=====

    @classmethod
    async def notify(cls, title: str, content: str):
        """
        根据已保存的通知配置，向所有已配置的渠道发送通知。
        任一渠道未配置则静默跳过。
        """
        settings = cls._get_settings()

        # 企业微信（自建应用）
        from app.services.wechat_app import WeChatAppService
        if WeChatAppService.is_configured():
            await WeChatAppService.send_markdown(f"### {title}\n{content}")

        # Telegram
        if settings.get("tg_enabled", False):
            tg_bot_token = settings.get("tg_bot_token", "").strip()
            tg_chat_id = settings.get("tg_chat_id", "").strip()
            if tg_bot_token and tg_chat_id:
                await cls.send_telegram_message(tg_bot_token, tg_chat_id, title, content)

        # QQ 机器人
        if settings.get("qq_enabled", False):
            qq_api_url = settings.get("qq_api_url", "").strip()
            qq_access_token = settings.get("qq_access_token", "").strip()
            qq_user_id = settings.get("qq_user_id", "").strip()
            qq_group_id = settings.get("qq_group_id", "").strip()
            if qq_api_url and (qq_user_id or qq_group_id):
                await cls.send_qq_message(qq_api_url, qq_access_token, qq_user_id, qq_group_id, title, content)

    @classmethod
    async def notify_organize_complete(cls, result: dict):
        """整理完成通知"""
        total = result.get("total", 0)
        success = len(result.get("organized", []))
        failed = len(result.get("errors", []))
        redundant = len(result.get("redundant", []))
        unrecognized = len(result.get("unrecognized", []))

        content = (
            f"> **整理完成**\n\n"
            f"> 总计: **{total}** 个文件\n"
            f"> 成功: **{success}** | 冗余: **{redundant}** | 无法识别: **{unrecognized}** | 失败: **{failed}**\n"
        )

        # 电视剧整理汇总
        tv_summary = result.get("tv_summary", [])
        if tv_summary:
            content += "\n> ---\n> **电视剧整理汇总**\n"
            for s in tv_summary[:20]:
                path = s.get("path", "")
                ep_range = s.get("episode_range", "")
                ep_count = s.get("episode_count", 0)
                renamed = s.get("renamed_count", 0)
                content += f"> {path} {ep_range}（{ep_count}集，重命名{renamed}个）\n"
                sample_orig = s.get("sample_original", "")
                sample_new = s.get("sample_renamed", "")
                if sample_new and sample_orig:
                    content += f">   {sample_orig} → {sample_new}\n"
            if len(tv_summary) > 20:
                content += f"> ... 还有 {len(tv_summary) - 20} 部剧\n"

        details = result.get("organized", [])
        if details:
            content += "\n> ---\n> **整理明细**\n"
            for d in details[:10]:
                name = d.get("name", "未知")
                to = d.get("to", "")
                renamed = d.get("renamed_to", "")
                content += f"> {name}"
                if renamed:
                    content += f" → {renamed}"
                if to:
                    content += f" [{to}]"
                content += "\n"
            if len(details) > 10:
                content += f"> ... 还有 {len(details) - 10} 个文件\n"

        await cls.notify("影视整理完成通知", content)

    @classmethod
    async def notify_sync_complete(cls, sync_type: str, result: dict, local_dir: str = ""):
        """同步完成通知"""
        total = result.get("total", 0)
        synced = result.get("synced", [])
        uploaded = result.get("uploaded", [])
        skipped = result.get("skipped", 0)
        errors = result.get("errors", [])

        if sync_type == "upload":
            type_label = "上传同步"
            action_label = "上传"
            items = uploaded
        elif sync_type == "full":
            type_label = "全量同步"
            action_label = "新增同步"
            items = synced
        else:
            type_label = "增量同步"
            action_label = "新增同步"
            items = synced

        content = (
            f"> **{type_label}完成**\n\n"
            f"> 总计: **{total}** 个文件\n"
            f"> {action_label}: **{len(items)}** | 跳过: **{skipped}**"
            + (f" | 失败: **{len(errors)}**" if errors else "")
            + "\n"
        )

        if local_dir:
            content += f"> 本地目录: `{local_dir}`\n"

        if items:
            content += "\n> ---\n"
            for s in items[:10]:
                name = s.get("filename", s.get("name", "未知"))
                content += f"> {name}\n"
            if len(items) > 10:
                content += f"> ... 还有 {len(items) - 10} 个文件\n"

        await cls.notify(f"STRM {type_label}完成通知", content)

    @classmethod
    async def notify_error(cls, task_name: str, error: str):
        """任务异常通知"""
        content = (
            f"> **任务执行异常**\n\n"
            f"> 任务: {task_name}\n"
            f"> 错误: {error}\n"
        )
        await cls.notify("STRMhub 异常通知", content)

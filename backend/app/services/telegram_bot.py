"""
Telegram Bot 双向控制服务
==========================

Telegram Bot 既是通知渠道又是控制入口，支持通过命令触发系统操作。

工作原理：
- 后台线程轮询 Telegram Bot API 的 /getUpdates 接口获取消息
- 解析命令并在主事件循环上调度对应的异步操作
- ChatID 白名单鉴权：仅处理 allowed_chat_ids 中配置的会话
- 使用 httpx 直接调用 Telegram Bot API（不依赖 python-telegram-bot 库）

支持的命令：
- /status   返回系统状态（账号数/同步状态/缓存状态）
- /sync_inc 触发增量同步
- /sync_full 触发全量同步
- /scrape   触发刮削
- /checkin  触发 115 签到
- /help     显示帮助

配置（存于 settings.json 的 telegram_bot 键）：
- enabled: bool 是否启用（默认 False）
- token: str Bot Token（从 @BotFather 获取）
- allowed_chat_ids: [int] 允许控制的 Chat ID 列表
"""
import asyncio
import threading
import time
from typing import Optional

import httpx

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, save_setting

logger = get_logger("app.services.telegram_bot")

# ===== 常量 =====

SETTINGS_KEY = "telegram_bot"
"""配置在 settings.json 中的存储键。"""

TELEGRAM_API_BASE = "https://api.telegram.org"
"""Telegram Bot API 基础 URL。"""

POLL_TIMEOUT = 30
"""长轮询超时时间（秒）。"""

POLL_ERROR_DELAY = 5
"""轮询出错后的等待时间（秒）。"""

# 支持的命令列表
SUPPORTED_COMMANDS = ["/status", "/sync_inc", "/sync_full", "/scrape", "/checkin", "/help"]


class TelegramBotService:
    """
    Telegram Bot 双向控制服务（单例）。

    通过后台线程轮询 Telegram Bot API 实现命令接收，
    在主事件循环上调度对应的异步操作。

    典型用法::

        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        bot.start()                    # 启动 Bot
        status = bot.get_status()      # 获取状态
        await bot.stop()               # 停止 Bot
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running: bool = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot_username: str = ""
        self._last_update_id: int = 0

    # ===== 配置读取 =====

    @staticmethod
    def get_config() -> dict:
        """读取 Telegram Bot 配置，返回合并默认值后的完整配置。"""
        cfg = read_setting(SETTINGS_KEY) or {}
        allowed = cfg.get("allowed_chat_ids", [])
        # 确保 allowed_chat_ids 为 int 列表
        if isinstance(allowed, list):
            allowed_ids = []
            for cid in allowed:
                try:
                    allowed_ids.append(int(cid))
                except (TypeError, ValueError):
                    pass
        else:
            allowed_ids = []
        return {
            "enabled": cfg.get("enabled", False),
            "token": cfg.get("token", ""),
            "allowed_chat_ids": allowed_ids,
        }

    @staticmethod
    def save_config(enabled: bool, token: str, allowed_chat_ids: list) -> dict:
        """保存 Telegram Bot 配置到 settings.json。"""
        # 确保 allowed_chat_ids 为 int 列表
        allowed_ids = []
        for cid in (allowed_chat_ids or []):
            try:
                allowed_ids.append(int(cid))
            except (TypeError, ValueError):
                pass
        cfg = {
            "enabled": bool(enabled),
            "token": (token or "").strip(),
            "allowed_chat_ids": allowed_ids,
        }
        save_setting(SETTINGS_KEY, cfg)
        return cfg

    def is_enabled(self) -> bool:
        """是否已启用。"""
        return bool(self.get_config().get("enabled", False))

    # ===== 生命周期 =====

    def start(self) -> bool:
        """启动 Bot 后台轮询线程（幂等）。需在事件循环中调用。

        Returns:
            是否成功启动。
        """
        if self._running:
            return True

        cfg = self.get_config()
        if not cfg.get("enabled", False):
            logger.info("[tg-bot] Telegram Bot 未启用（enabled=false），不启动")
            return False

        token = cfg.get("token", "")
        if not token:
            logger.warning("[tg-bot] Bot Token 未配置，无法启动")
            return False

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[tg-bot] 启动失败：不在事件循环中")
            return False

        # 验证 Token 并获取 Bot 信息
        bot_info = self._get_me(token)
        if not bot_info:
            logger.warning("[tg-bot] Token 验证失败，无法启动 Bot")
            return False

        self._bot_username = bot_info.get("username", "")
        logger.info(f"[tg-bot] Bot 已连接: @{self._bot_username}")

        self._stop_event.clear()
        self._last_update_id = 0
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="telegram-bot-poller",
        )
        self._thread.start()
        self._running = True
        logger.info("[tg-bot] Telegram Bot 轮询已启动")
        return True

    async def stop(self) -> None:
        """停止 Bot 后台轮询线程（幂等）。"""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=POLL_TIMEOUT + 5)
        self._thread = None
        self._bot_username = ""
        logger.info("[tg-bot] Telegram Bot 轮询已停止")

    def restart(self) -> bool:
        """重启 Bot（先停止再启动）。需在事件循环中调用。"""
        if self._running:
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=POLL_TIMEOUT + 5)
            self._running = False
            self._stop_event.clear()
        return self.start()

    # ===== Telegram Bot API 调用 =====

    def _get_me(self, token: str) -> Optional[dict]:
        """调用 /getMe 获取 Bot 信息，验证 Token 有效性。"""
        url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return data.get("result", {})
            logger.warning(f"[tg-bot] getMe 失败: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.warning(f"[tg-bot] getMe 异常: {e}")
            return None

    def get_updates(self, token: str, offset: int = 0) -> list[dict]:
        """轮询 /getUpdates 获取最新消息（长轮询）。

        Args:
            token: Bot Token
            offset: 上次处理的 update_id + 1

        Returns:
            更新列表，每项为 Telegram Update 对象。
        """
        url = f"{TELEGRAM_API_BASE}/bot{token}/getUpdates"
        params = {"timeout": POLL_TIMEOUT}
        if offset > 0:
            params["offset"] = offset
        try:
            with httpx.Client(timeout=POLL_TIMEOUT + 10) as client:
                resp = client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return data.get("result", [])
                else:
                    logger.warning(f"[tg-bot] getUpdates 失败: HTTP {resp.status_code}")
        except httpx.ReadTimeout:
            # 长轮询超时是正常的，返回空列表
            pass
        except Exception as e:
            logger.warning(f"[tg-bot] getUpdates 异常: {e}")
        return []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """发送消息到指定会话。

        Args:
            chat_id: 目标会话 ID
            text: 消息文本（支持 HTML 格式）
            reply_markup: 可选的回复键盘/内联键盘

        Returns:
            是否发送成功。
        """
        cfg = self.get_config()
        token = cfg.get("token", "")
        if not token:
            return False

        url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return True
                    logger.warning(f"[tg-bot] sendMessage 失败: {data.get('description', '')}")
                else:
                    logger.warning(f"[tg-bot] sendMessage 失败: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"[tg-bot] sendMessage 异常: {e}")
        return False

    async def send_message_async(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """异步发送消息（供主事件循环中使用）。"""
        cfg = self.get_config()
        token = cfg.get("token", "")
        if not token:
            return False

        url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return True
                    logger.warning(f"[tg-bot] sendMessage 失败: {data.get('description', '')}")
                else:
                    logger.warning(f"[tg-bot] sendMessage 失败: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"[tg-bot] sendMessage 异常: {e}")
        return False

    # ===== 后台轮询主循环 =====

    def _poll_loop(self) -> None:
        """后台轮询主循环（在线程中运行）。"""
        logger.info("[tg-bot] 轮询线程已启动")
        cfg = self.get_config()
        token = cfg.get("token", "")

        while not self._stop_event.is_set():
            try:
                updates = self.get_updates(token, self._last_update_id + 1)
                for update in updates:
                    try:
                        self._last_update_id = update.get("update_id", self._last_update_id)
                        self._handle_update(update)
                    except Exception as e:
                        logger.warning(f"[tg-bot] 处理更新异常: {e}")
            except Exception as e:
                logger.warning(f"[tg-bot] 轮询异常: {e}")
                # 出错后等待，避免高频重试
                self._stop_event.wait(POLL_ERROR_DELAY)

        logger.info("[tg-bot] 轮询线程已退出")

    # ===== 消息处理 =====

    def _handle_update(self, update: dict) -> None:
        """处理单个 Telegram Update。

        解析消息文本，鉴权 ChatID，分发到对应的命令处理器。
        """
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return

        # ChatID 白名单鉴权
        cfg = self.get_config()
        allowed_ids = cfg.get("allowed_chat_ids", [])
        if allowed_ids and chat_id not in allowed_ids:
            logger.warning(f"[tg-bot] 未授权的 ChatID: {chat_id}，忽略消息")
            return

        # 解析命令（支持 /cmd 和 /cmd@botname 格式）
        parts = text.split()
        command = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        logger.info(f"[tg-bot] 收到命令: {command} (chat_id={chat_id})")

        # 分发到对应的命令处理器
        handler = self._COMMAND_MAP.get(command)
        if handler:
            # 在主事件循环上调度异步命令处理
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(handler, chat_id, args),
                    self._loop,
                )
            else:
                logger.warning("[tg-bot] 事件循环未运行，无法处理命令")
        else:
            # 未知命令
            self.send_message(chat_id, f"未知命令: {command}\n发送 /help 查看支持的命令")

    async def _dispatch(self, handler, chat_id: int, args: list) -> None:
        """在主事件循环上执行命令处理器并发送结果。"""
        try:
            reply = await handler(self, chat_id, args)
            if reply:
                await self.send_message_async(chat_id, reply)
        except Exception as e:
            logger.warning(f"[tg-bot] 命令执行异常: {e}")
            await self.send_message_async(chat_id, f"命令执行失败: {str(e)}")

    # ===== 命令处理器 =====

    async def _cmd_status(self, chat_id: int, args: list) -> str:
        """/status - 返回系统状态。"""
        from app.core.json_storage import read_accounts, get_first_valid_account
        from app.services.sync_service import SyncService

        accounts = read_accounts()
        valid_accounts = [a for a in accounts if a.get("status") == 1]
        sync_config = SyncService.load_schedule()

        # 缓存预热状态
        cache_status = ""
        try:
            from app.services.cache_warmer import get_cache_warmer
            cw_status = get_cache_warmer().get_status()
            cache_status = (
                f"\n缓存预热: {'运行中' if cw_status.get('running') else '已停止'}, "
                f"累计预热 {cw_status.get('total_warmed', 0)} 个"
            )
        except Exception:
            pass

        # 生活事件监控状态
        life_status = ""
        try:
            from app.services.life_event_monitor import get_life_event_monitor
            lem = get_life_event_monitor().get_status()
            life_status = (
                f"\n事件监控: {'运行中' if lem.get('running') else '已停止'}"
            )
        except Exception:
            pass

        lines = [
            "<b>STRMhub 系统状态</b>",
            "",
            f"账号总数: {len(accounts)}（有效 {len(valid_accounts)}）",
            f"同步目录: {sync_config.get('source_cid', '未配置')}",
            f"本地目录: {sync_config.get('local_media_dir', '未配置')}",
            f"定时同步: {sync_config.get('cron', '未配置') or '未配置'}",
        ]
        if cache_status:
            lines.append(cache_status)
        if life_status:
            lines.append(life_status)

        return "\n".join(lines)

    async def _cmd_sync_inc(self, chat_id: int, args: list) -> str:
        """/sync_inc - 触发增量同步。"""
        from app.core.json_storage import get_first_valid_account
        from app.services.sync_service import SyncService

        account = get_first_valid_account()
        if not account or account.get("status") == 0:
            return "未找到有效账号，请先登录 115"

        cookies = account.get("cookies", "")
        config = SyncService.load_schedule()
        source_cid = config.get("source_cid", "")
        local_media_dir = config.get("local_media_dir", "")

        if not source_cid or not local_media_dir:
            return "同步配置不完整，请先执行全量同步以保存目录配置"

        # 先发送"开始"消息
        await self.send_message_async(chat_id, "正在执行增量同步...")

        try:
            video_exts = _parse_exts(config.get("video_exts_str", ""))
            image_exts = _parse_exts(config.get("image_exts_str", ""))
            data_exts = _parse_exts(config.get("data_exts_str", ""))
            min_video_size_mb = config.get("min_video_size_mb", 0)

            result = await asyncio.to_thread(
                SyncService.incremental_sync,
                cookies=cookies,
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                video_exts=video_exts,
                image_exts=image_exts,
                data_exts=data_exts,
                min_video_size_mb=min_video_size_mb,
                account_id=account.get("id", 0),
                loop=self._loop,
            )

            synced = len(result.get("synced", []))
            skipped = result.get("skipped", 0)
            errors = len(result.get("errors", []))

            return (
                f"<b>增量同步完成</b>\n"
                f"新增同步: {synced} 个\n"
                f"跳过: {skipped} 个\n"
                f"错误: {errors} 个"
            )
        except Exception as e:
            return f"增量同步失败: {str(e)}"

    async def _cmd_sync_full(self, chat_id: int, args: list) -> str:
        """/sync_full - 触发全量同步。"""
        from app.core.json_storage import get_first_valid_account
        from app.services.sync_service import SyncService

        account = get_first_valid_account()
        if not account or account.get("status") == 0:
            return "未找到有效账号，请先登录 115"

        cookies = account.get("cookies", "")
        config = SyncService.load_schedule()
        source_cid = config.get("source_cid", "")
        local_media_dir = config.get("local_media_dir", "")

        if not source_cid or not local_media_dir:
            return "同步配置不完整，请先在全量同步页面配置目录"

        # 先发送"开始"消息
        await self.send_message_async(chat_id, "正在执行全量同步（可能需要较长时间）...")

        try:
            video_exts = _parse_exts(config.get("video_exts_str", ""))
            image_exts = _parse_exts(config.get("image_exts_str", ""))
            data_exts = _parse_exts(config.get("data_exts_str", ""))
            min_video_size_mb = config.get("min_video_size_mb", 0)

            result = await asyncio.to_thread(
                SyncService.full_sync,
                cookies=cookies,
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                video_exts=video_exts,
                image_exts=image_exts,
                data_exts=data_exts,
                min_video_size_mb=min_video_size_mb,
                account_id=account.get("id", 0),
                loop=self._loop,
            )

            synced = len(result.get("synced", []))
            skipped = result.get("skipped", 0)
            errors = len(result.get("errors", []))

            return (
                f"<b>全量同步完成</b>\n"
                f"新增同步: {synced} 个\n"
                f"跳过: {skipped} 个\n"
                f"错误: {errors} 个"
            )
        except Exception as e:
            return f"全量同步失败: {str(e)}"

    async def _cmd_scrape(self, chat_id: int, args: list) -> str:
        """/scrape - 触发刮削。"""
        from app.core.json_storage import get_first_valid_account
        from app.services.sync_service import SyncService
        from app.services.tmdb_service import TmdbService

        account = get_first_valid_account()
        if not account or account.get("status") == 0:
            return "未找到有效账号，请先登录 115"

        # 检查 TMDB 配置
        tmdb_api_key = TmdbService._get_api_key()
        if not tmdb_api_key:
            return "TMDB API Key 未配置，无法刮削"

        cookies = account.get("cookies", "")
        config = SyncService.load_schedule()
        source_cid = config.get("source_cid", "")

        if not source_cid:
            return "同步目录未配置，无法刮削"

        await self.send_message_async(chat_id, "正在执行刮削...")

        try:
            from app.services.strmscrape_service import StrmScrapeService

            # 扫描分组
            groups = await asyncio.to_thread(
                StrmScrapeService.scan_for_scrape,
                cookies=cookies,
                source_cid=source_cid,
            )

            if not groups:
                return "未找到可刮削的视频文件"

            # 逐组刮削
            success = 0
            miss = 0
            for g in groups:
                result = await asyncio.to_thread(
                    StrmScrapeService.scrape_group,
                    cookies=cookies,
                    tmdb_api_key=tmdb_api_key,
                    group=g.get("group", ""),
                    files=g.get("files", []),
                )
                if result.get("status") != "miss":
                    success += 1
                else:
                    miss += 1

            return (
                f"<b>刮削完成</b>\n"
                f"匹配成功: {success} 组\n"
                f"未匹配: {miss} 组\n"
                f"总分组: {len(groups)}"
            )
        except Exception as e:
            return f"刮削失败: {str(e)}"

    async def _cmd_checkin(self, chat_id: int, args: list) -> str:
        """/checkin - 触发 115 签到。"""
        from app.core.json_storage import read_accounts
        from app.services.client_115 import Client115Service

        accounts = read_accounts()
        valid = [a for a in accounts if a.get("status") == 1]
        if not valid:
            return "无有效账号，无法签到"

        ok = 0
        fail = 0
        for acc in valid:
            cookies = acc.get("cookies", "")
            if not cookies:
                fail += 1
                continue
            try:
                result = await asyncio.to_thread(
                    Client115Service.daily_checkin,
                    cookies=cookies,
                )
                if isinstance(result, dict) and not result.get("error") and result.get("state") is not False:
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1

        return f"<b>115 签到完成</b>\n成功: {ok} 个账号\n失败: {fail} 个账号"

    async def _cmd_help(self, chat_id: int, args: list) -> str:
        """/help - 显示帮助。"""
        lines = [
            "<b>STRMhub Bot 命令列表</b>",
            "",
            "/status - 查看系统状态",
            "/sync_inc - 触发增量同步",
            "/sync_full - 触发全量同步",
            "/scrape - 触发 TMDB 刮削",
            "/checkin - 触发 115 每日签到",
            "/help - 显示此帮助信息",
        ]
        return "\n".join(lines)

    # ===== 命令映射表 =====

    _COMMAND_MAP = {
        "/status": _cmd_status,
        "/sync_inc": _cmd_sync_inc,
        "/sync_full": _cmd_sync_full,
        "/scrape": _cmd_scrape,
        "/checkin": _cmd_checkin,
        "/help": _cmd_help,
    }

    # ===== 状态查询 =====

    def get_status(self) -> dict:
        """获取 Bot 状态（供 API 使用）。

        Returns:
            状态字典::

                {
                    "running": bool,         # 是否运行中
                    "bot_username": str,     # Bot 用户名
                    "commands": [str],       # 支持的命令列表
                    "enabled": bool,         # 是否已启用
                }
        """
        return {
            "running": self._running,
            "bot_username": self._bot_username,
            "commands": list(SUPPORTED_COMMANDS),
            "enabled": self.is_enabled(),
        }


# ===== 全局单例 =====

_global_telegram_bot: Optional[TelegramBotService] = None


def get_telegram_bot() -> TelegramBotService:
    """获取全局 Telegram Bot 服务单例。"""
    global _global_telegram_bot
    if _global_telegram_bot is None:
        _global_telegram_bot = TelegramBotService()
    return _global_telegram_bot


# ===== 工具函数 =====

def _parse_exts(exts_str: str) -> set:
    """解析后缀字符串为集合（与 scheduler._parse_exts 一致）。"""
    if not exts_str or not exts_str.strip():
        return set()
    result = set()
    for e in exts_str.split(","):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        result.add(e)
    return result

"""
企业微信自建应用服务
- access_token 管理（带缓存）
- 应用消息发送（文本 / Markdown）
- 用户命令处理（双向互动）
"""
import json
import time
import asyncio
import httpx
from typing import Optional

from app.core.logbuffer import get_logger
from app.core.db_helper import read_setting
from app.core.json_storage import read_accounts, get_first_valid_account
from app.config import CONFIG_DIR

logger = get_logger("app.services.wechat_app")


class WeChatAppService:
    """企业微信自建应用服务"""

    # access_token 缓存: { token, expires_at }
    _token_cache: dict = {}

    @classmethod
    def _get_settings(cls) -> dict:
        """读取企业微信应用配置"""
        return read_setting("notification")

    @classmethod
    def _get_config(cls) -> dict:
        """获取企业微信应用配置项"""
        s = cls._get_settings()
        return {
            "corp_id": s.get("wechat_corp_id", "").strip(),
            "agent_secret": s.get("wechat_agent_secret", "").strip(),
            "agent_id": s.get("wechat_agent_id", "").strip(),
            "api_base": s.get("wechat_api_base", "https://qyapi.weixin.qq.com").strip().rstrip("/"),
            "callback_token": s.get("wechat_callback_token", "").strip(),
            "callback_aes_key": s.get("wechat_callback_aes_key", "").strip(),
            "default_user": s.get("wechat_default_user", "@all").strip() or "@all",
        }

    @classmethod
    def is_configured(cls) -> bool:
        """检查企业微信应用是否已配置且已启用"""
        s = cls._get_settings()
        if not s.get("wechat_enabled", False):
            return False
        c = cls._get_config()
        return bool(c["corp_id"] and c["agent_secret"] and c["agent_id"])

    @classmethod
    def is_callback_configured(cls) -> bool:
        """检查回调是否已配置（Token + AESKey）"""
        c = cls._get_config()
        return bool(c["corp_id"] and c["callback_token"] and c["callback_aes_key"])

    @classmethod
    async def get_access_token(cls) -> Optional[str]:
        """获取 access_token（带缓存，提前 5 分钟刷新）"""
        c = cls._get_config()
        if not c["corp_id"] or not c["agent_secret"]:
            return None

        cache = cls._token_cache
        if cache.get("token") and cache.get("expires_at", 0) > time.time() + 300:
            return cache["token"]

        url = f"{c['api_base']}/cgi-bin/gettoken"
        params = {
            "corpid": c["corp_id"],
            "corpsecret": c["agent_secret"],
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=10.0)
                data = resp.json()
                if data.get("errcode") == 0:
                    token = data["access_token"]
                    expires = data.get("expires_in", 7200)
                    cls._token_cache = {
                        "token": token,
                        "expires_at": time.time() + expires,
                    }
                    logger.info("企业微信 access_token 获取成功")
                    return token
                else:
                    logger.warning(f"企业微信获取 access_token 失败: {data.get('errmsg')}")
                    return None
        except Exception as e:
            logger.error(f"企业微信获取 access_token 异常: {e}")
            return None

    @classmethod
    async def send_text(cls, content: str, to_user: str = "") -> bool:
        """发送文本消息"""
        c = cls._get_config()
        if not cls.is_configured():
            return False

        token = await cls.get_access_token()
        if not token:
            return False

        url = f"{c['api_base']}/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": to_user or c["default_user"],
            "msgtype": "text",
            "agentid": int(c["agent_id"]),
            "text": {"content": content},
        }
        return await cls._send(url, payload)

    @classmethod
    async def send_markdown(cls, content: str, to_user: str = "") -> bool:
        """发送 Markdown 消息"""
        c = cls._get_config()
        if not cls.is_configured():
            return False

        token = await cls.get_access_token()
        if not token:
            return False

        url = f"{c['api_base']}/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": to_user or c["default_user"],
            "msgtype": "markdown",
            "agentid": int(c["agent_id"]),
            "markdown": {"content": content},
        }
        return await cls._send(url, payload)

    @classmethod
    async def _send(cls, url: str, payload: dict) -> bool:
        """实际发送请求"""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=10.0)
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info("企业微信应用消息发送成功")
                    return True
                else:
                    logger.warning(f"企业微信应用消息发送失败: {data.get('errmsg')}")
                    # access_token 过期则清除缓存
                    if data.get("errcode") in (40014, 42001):
                        cls._token_cache = {}
                    return False
        except Exception as e:
            logger.error(f"企业微信应用消息发送异常: {e}")
            return False

    # ===== 命令处理（双向互动） =====

    @classmethod
    async def handle_command(cls, content: str, from_user: str) -> str:
        """
        处理用户发来的消息，返回回复文本。
        支持的命令:
          帮助 / help     - 显示可用命令
          状态 / status   - 查看系统状态
          整理 / organize - 触发影视整理
          全量同步 / fullsync - 触发全量同步
          增量同步 / incremental - 触发增量同步
        """
        cmd = content.strip().lower()

        # 帮助
        if cmd in ("帮助", "help", "?", "？", "h"):
            return cls._help_text()

        # 系统状态
        if cmd in ("状态", "status", "st"):
            return await cls._cmd_status()

        # 触发整理
        if cmd in ("整理", "organize", "org"):
            return await cls._cmd_organize(from_user)

        # 全量同步
        if cmd in ("全量同步", "fullsync", "full"):
            return await cls._cmd_full_sync(from_user)

        # 增量同步
        if cmd in ("增量同步", "incremental", "inc"):
            return await cls._cmd_incremental_sync(from_user)

        # 未识别命令
        return (
            f"收到消息: {content}\n"
            f"输入「帮助」查看可用命令。"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "STRMhub 指令列表\n"
            "━━━━━━━━━━━━\n"
            "状态  - 查看系统状态\n"
            "整理  - 触发影视整理\n"
            "全量同步 - 触发全量 STRM 生成\n"
            "增量同步 - 触发增量同步\n"
            "帮助  - 显示此帮助\n"
            "━━━━━━━━━━━━\n"
            "直接发送对应关键词即可执行。"
        )

    @classmethod
    async def _cmd_status(cls) -> str:
        """查询系统状态"""
        try:
            accounts = read_accounts()
            valid_accounts = [a for a in accounts if a.get("status") == 1]

            lines = ["STRMhub 系统状态", "━━━━━━━━━━━━"]

            if not valid_accounts:
                lines.append("⚠ 暂无有效 115 账号")
            else:
                lines.append(f"115 账号: {len(valid_accounts)} 个有效")
                for acc in valid_accounts[:3]:
                    name = acc.get("username") or acc.get("name") or f"ID:{acc.get('id')}"
                    lines.append(f"  · {name}")

            # 同步配置
            sync_file = CONFIG_DIR / "sync_schedule.json"
            if sync_file.exists():
                sync_cfg = json.loads(sync_file.read_text(encoding="utf-8"))
                source = sync_cfg.get("source_path", "")
                local = sync_cfg.get("local_media_dir", "")
                lines.append(f"同步源目录: {source or '未配置'}")
                lines.append(f"本地目录: {local or '未配置'}")
            else:
                lines.append("同步配置: 未配置")

            # 整理配置
            dirs_file = CONFIG_DIR / "organize_dirs.json"
            if dirs_file.exists():
                dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
                source = dirs_cfg.get("source_path", "")
                lines.append(f"整理源目录: {source or '未配置'}")
            else:
                lines.append("整理配置: 未配置")

            return "\n".join(lines)
        except Exception as e:
            return f"查询状态失败: {e}"

    @classmethod
    async def _cmd_organize(cls, from_user: str) -> str:
        """触发整理"""
        try:
            # 读取整理配置
            dirs_file = CONFIG_DIR / "organize_dirs.json"
            if not dirs_file.exists():
                return "❌ 未找到整理配置，请先在网页端配置整理目录。"

            dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
            source_cid = dirs_cfg.get("source_cid", "")
            if not source_cid:
                return "❌ 整理源目录未配置，请先在网页端选择需要整理的目录。"

            # 读取同步配置获取 target_cid
            sync_file = CONFIG_DIR / "sync_schedule.json"
            if not sync_file.exists():
                return "❌ 未找到同步配置，无法确定整理目标目录。"

            sync_cfg = json.loads(sync_file.read_text(encoding="utf-8"))
            target_cid = sync_cfg.get("source_cid", "")
            if not target_cid:
                return "❌ 同步源目录未配置，请先在网页端配置全量同步目录。"

            # 异步触发整理
            asyncio.create_task(cls._run_organize_async(dirs_cfg, sync_cfg, from_user))
            return "⏳ 整理任务已启动，完成后会发送通知。"
        except Exception as e:
            return f"❌ 启动整理失败: {e}"

    @classmethod
    async def _run_organize_async(cls, dirs_cfg: dict, sync_cfg: dict, from_user: str):
        """异步执行整理任务"""
        try:
            from app.services.organize_service import OrganizeService
            from app.services.emby import trigger_emby_refresh
            from app.services.notification_service import NotificationService
            from app.core.progress import progress_manager

            account = get_first_valid_account()

            if not account:
                await cls.send_text("❌ 整理失败: 未找到有效 115 账号", from_user)
                return

            # 读取分类和洗版配置
            classify_config = ""
            category_roots = None
            classify_file = CONFIG_DIR / "classify_config.json"
            if classify_file.exists():
                classify_data = json.loads(classify_file.read_text(encoding="utf-8"))
                classify_config = classify_data.get("classify_config", "")
                category_roots = classify_data.get("category_roots")

            wash_config = {}
            wash_file = CONFIG_DIR / "wash_config.json"
            if wash_file.exists():
                wash_config = json.loads(wash_file.read_text(encoding="utf-8"))

            rename_rules = {}
            rename_file = CONFIG_DIR / "rename_rules.json"
            if rename_file.exists():
                rename_rules = json.loads(rename_file.read_text(encoding="utf-8"))

            if not await progress_manager.start_task("organize", 0, "影视整理（企业微信触发）"):
                await cls.send_text("⚠️ 已有任务正在运行，请等待完成后再试", from_user)
                return

            async def _prog(current, total, filename):
                await progress_manager.update_progress(current, total, filename)

            result = await OrganizeService.scan_and_organize(
                cookies=account.get("cookies", ""),
                source_cid=dirs_cfg.get("source_cid", ""),
                target_cid=sync_cfg.get("source_cid", ""),
                existing_cid=dirs_cfg.get("existing_cid", ""),
                redundant_cid=dirs_cfg.get("redundant_cid", ""),
                unrecognized_cid=dirs_cfg.get("unrecognized_cid", ""),
                classify_config=classify_config,
                category_roots=category_roots,
                rename_rules=rename_rules,
                wash_config=wash_config,
                use_ffprobe=dirs_cfg.get("use_ffprobe", False),
                skip_no_info=dirs_cfg.get("skip_no_info", False),
                prefer_filename=dirs_cfg.get("prefer_filename", False),
                min_organize_size_mb=dirs_cfg.get("min_organize_size_mb", 0),
                organize_blacklist=dirs_cfg.get("organize_blacklist", ""),
                dry_run=False,
                progress_callback=_prog,
            )

            await progress_manager.complete_task(
                f"整理完成: 成功 {len(result.get('organized', []))}，"
                f"冗余 {len(result.get('redundant', []))}"
            )

            # Emby 刷新（根据配置）
            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True):
                await trigger_emby_refresh()
            # 通知（根据配置）
            notify_cfg = read_setting("emby_notify") or {}
            if notify_cfg.get("notify_on_organize", True):
                await NotificationService.notify_organize_complete(result)
        except Exception as e:
            logger.error(f"企业微信触发整理失败: {e}")
            await cls.send_text(f"❌ 整理失败: {e}", from_user)

    @classmethod
    async def _cmd_full_sync(cls, from_user: str) -> str:
        """触发全量同步"""
        try:
            sync_file = CONFIG_DIR / "sync_schedule.json"
            if not sync_file.exists():
                return "❌ 未找到同步配置，请先在网页端配置全量同步。"

            sync_cfg = json.loads(sync_file.read_text(encoding="utf-8"))
            source_cid = sync_cfg.get("source_cid", "")
            local_dir = sync_cfg.get("local_media_dir", "")
            if not source_cid:
                return "❌ 同步源目录未配置。"
            if not local_dir:
                return "❌ 本地媒体目录未配置。"

            asyncio.create_task(cls._run_full_sync_async(sync_cfg, from_user))
            return "⏳ 全量同步任务已启动，完成后会发送通知。"
        except Exception as e:
            return f"❌ 启动全量同步失败: {e}"

    @classmethod
    async def _run_full_sync_async(cls, sync_cfg: dict, from_user: str):
        """异步执行全量同步"""
        try:
            from app.services.sync_service import SyncService
            from app.services.emby import trigger_emby_refresh
            from app.services.notification_service import NotificationService
            from app.core.progress import progress_manager

            account = get_first_valid_account()

            if not account:
                await cls.send_text("❌ 全量同步失败: 未找到有效 115 账号", from_user)
                return

            source_cid = sync_cfg.get("source_cid", "")
            local_dir = sync_cfg.get("local_media_dir", "")

            video_exts_str = sync_cfg.get("video_exts_str", ".mp4,.mkv,.avi,.mov,.wmv,.flv,.m4v,.ts,.m2ts,.rmvb,.iso")
            image_exts_str = sync_cfg.get("image_exts_str", "")
            data_exts_str = sync_cfg.get("data_exts_str", "")
            min_video_size_mb = sync_cfg.get("min_video_size_mb", 0)

            video_exts = set(e.strip().lower() for e in video_exts_str.split(",") if e.strip())
            image_exts = set(e.strip().lower() for e in image_exts_str.split(",") if e.strip())
            data_exts = set(e.strip().lower() for e in data_exts_str.split(",") if e.strip())

            if not await progress_manager.start_task("full_sync", 0, "全量同步（企业微信触发）"):
                await cls.send_text("⚠️ 已有任务正在运行，请等待完成后再试", from_user)
                return

            _loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(
                SyncService.full_sync,
                cookies=account.get("cookies", ""),
                source_cid=source_cid,
                local_media_dir=local_dir,
                video_exts=video_exts,
                image_exts=image_exts,
                data_exts=data_exts,
                min_video_size_mb=min_video_size_mb,
                loop=_loop,
            )

            await progress_manager.complete_task(
                f"全量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
            )

            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True) and result.get("synced"):
                await trigger_emby_refresh(local_dir)

                # Emby 刮削后自动上传 nfo/图片到网盘
                await SyncService.trigger_auto_upload(
                    cookies=account.get("cookies", ""),
                    source_cid=source_cid,
                    local_media_dir=local_dir,
                    account_id=account.get("id", 0),
                    loop=_loop,
                )
            notify_cfg = read_setting("emby_notify") or {}
            if notify_cfg.get("notify_on_sync", True):
                await NotificationService.notify_sync_complete("full", result, local_dir)
        except Exception as e:
            logger.error(f"企业微信触发全量同步失败: {e}")
            await cls.send_text(f"❌ 全量同步失败: {e}", from_user)

    @classmethod
    async def _cmd_incremental_sync(cls, from_user: str) -> str:
        """触发增量同步"""
        try:
            sync_file = CONFIG_DIR / "sync_schedule.json"
            if not sync_file.exists():
                return "❌ 未找到同步配置，请先在网页端配置全量同步。"

            asyncio.create_task(cls._run_incremental_async(from_user))
            return "⏳ 增量同步任务已启动，完成后会发送通知。"
        except Exception as e:
            return f"❌ 启动增量同步失败: {e}"

    @classmethod
    async def _run_incremental_async(cls, from_user: str):
        """异步执行增量同步"""
        try:
            from app.services.sync_service import SyncService
            from app.services.emby import trigger_emby_refresh
            from app.services.notification_service import NotificationService
            from app.core.progress import progress_manager

            account = get_first_valid_account()

            if not account:
                await cls.send_text("❌ 增量同步失败: 未找到有效 115 账号", from_user)
                return

            # 从已保存的配置中读取同步参数
            schedule = SyncService.load_schedule()
            source_cid = schedule.get("source_cid", "")
            local_media_dir = schedule.get("local_media_dir", "")

            video_exts = _parse_exts_str(schedule.get("video_exts_str", ""))
            image_exts = _parse_exts_str(schedule.get("image_exts_str", ""))
            data_exts = _parse_exts_str(schedule.get("data_exts_str", ""))
            min_video_size_mb = schedule.get("min_video_size_mb", 0)

            if not await progress_manager.start_task("incremental_sync", 0, "增量同步（企业微信触发）"):
                await cls.send_text("⚠️ 已有任务正在运行，请等待完成后再试", from_user)
                return

            _loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(
                SyncService.incremental_sync,
                cookies=account.get("cookies", ""),
                source_cid=source_cid,
                local_media_dir=local_media_dir,
                video_exts=video_exts,
                image_exts=image_exts,
                data_exts=data_exts,
                min_video_size_mb=min_video_size_mb,
                loop=_loop,
            )

            await progress_manager.complete_task(
                f"增量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
            )

            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True) and result.get("synced"):
                await trigger_emby_refresh(local_media_dir)

                # Emby 刮削后自动上传 nfo/图片到网盘
                await SyncService.trigger_auto_upload(
                    cookies=account.get("cookies", ""),
                    source_cid=source_cid,
                    local_media_dir=local_media_dir,
                    account_id=account.get("id", 0),
                    loop=_loop,
                )
            notify_cfg = read_setting("emby_notify") or {}
            if notify_cfg.get("notify_on_sync", True):
                await NotificationService.notify_sync_complete("incremental", result, local_media_dir)
        except Exception as e:
            logger.error(f"企业微信触发增量同步失败: {e}")
            await cls.send_text(f"❌ 增量同步失败: {e}", from_user)


def _parse_exts_str(exts_str: str) -> set:
    """解析后缀字符串为集合"""
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

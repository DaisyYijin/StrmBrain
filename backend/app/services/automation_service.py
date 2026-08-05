"""
自动化规则引擎服务（G7）

规则 = 触发器 + 动作序列：
- 触发器：cron（cron 表达式定时触发）| webhook（外部 POST 触发）
- 动作：strm_sync（增量同步）| scrape（STRM 刮削）| organize（整理）| emby_refresh（刷新 Emby 媒体库）

规则持久化到 DATA_DIR/automation_rules.json（原子写入，参考 app.core.json_storage.write_json）。
设计思路参考 LitePan 的 internal/automation（仅借鉴概念，不复制 Go 代码）。
"""
import asyncio
import json
import threading
import time
from typing import Optional

from app.config import DATA_DIR, CONFIG_DIR
from app.core.json_storage import find_account, get_first_valid_account, read_json, write_json
from app.core.logbuffer import get_logger

logger = get_logger("app.services.automation_service")

# 规则持久化文件名（存放于 DATA_DIR/）
RULES_FILE = "automation_rules.json"

# 支持的触发器 / 动作类型
TRIGGER_TYPES = ("cron", "webhook")
ACTION_TYPES = ("strm_sync", "scrape", "organize", "emby_refresh")

# 规则文件读写锁（防止并发增删改）
_rules_lock = threading.RLock()

# 全局单例
_singleton: Optional["AutomationService"] = None


def get_automation_service() -> "AutomationService":
    """获取自动化服务全局单例"""
    global _singleton
    if _singleton is None:
        _singleton = AutomationService()
    return _singleton


def _parse_exts(exts_str: str) -> set:
    """解析后缀字符串为集合（兼容逗号分隔、可带或不带点）"""
    if not exts_str or not str(exts_str).strip():
        return set()
    result = set()
    for e in str(exts_str).split(","):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        result.add(e)
    return result


def _resolve_account(account_id=None) -> Optional[dict]:
    """解析 115 账号：优先指定 ID（且有效），否则取第一个有效账号"""
    if account_id:
        acc = find_account(account_id)
        if acc and acc.get("status") == 1:
            return acc
    return get_first_valid_account()


class AutomationService:
    """自动化规则引擎服务

    负责规则的增删改查、持久化、按动作序列执行，以及 webhook 触发。
    """

    def __init__(self):
        # 正在执行中的规则 id 集合（防同规则重入）
        self._running: set = set()

    # ==================== 持久化 ====================

    def list_rules(self) -> list:
        """读取全部规则（文件不存在时返回空列表）"""
        data = read_json(RULES_FILE, [])
        return data if isinstance(data, list) else []

    def save_rules(self, rules: list) -> bool:
        """原子写入全部规则（tmp + rename，并同步备份）"""
        return write_json(RULES_FILE, rules)

    def get_rule(self, rule_id: int) -> Optional[dict]:
        """按 id 查找规则"""
        for rule in self.list_rules():
            if int(rule.get("id", 0)) == int(rule_id):
                return rule
        return None

    def add_rule(self, rule: dict) -> dict:
        """新增规则，id 自增，返回完整规则"""
        with _rules_lock:
            rules = self.list_rules()
            new_id = max((int(r.get("id", 0)) for r in rules), default=0) + 1
            normalized = self._normalize_rule(rule)
            normalized["id"] = new_id
            rules.append(normalized)
            if not self.save_rules(rules):
                logger.warning(f"保存自动化规则失败（新增规则 {new_id}）")
            return normalized

    def update_rule(self, rule_id: int, patch: dict) -> Optional[dict]:
        """更新规则（patch 为部分字段），返回更新后的规则；不存在返回 None"""
        with _rules_lock:
            rules = self.list_rules()
            for i, rule in enumerate(rules):
                if int(rule.get("id", 0)) != int(rule_id):
                    continue
                merged = self._normalize_rule({**rule, **patch})
                merged["id"] = int(rule.get("id", 0))
                rules[i] = merged
                if not self.save_rules(rules):
                    logger.warning(f"保存自动化规则失败（更新规则 {rule_id}）")
                return merged
        return None

    def delete_rule(self, rule_id: int) -> bool:
        """删除规则，成功返回 True；规则不存在返回 False"""
        with _rules_lock:
            rules = self.list_rules()
            new_rules = [r for r in rules if int(r.get("id", 0)) != int(rule_id)]
            if len(new_rules) == len(rules):
                return False
            if not self.save_rules(new_rules):
                logger.warning(f"保存自动化规则失败（删除规则 {rule_id}）")
            return True

    # ==================== 规则归一化 ====================

    @staticmethod
    def _normalize_rule(rule: dict) -> dict:
        """规则结构归一化：保证字段齐全、类型正确"""
        trigger_type = rule.get("trigger_type", "cron")
        if trigger_type not in TRIGGER_TYPES:
            trigger_type = "cron"

        actions = rule.get("actions") or []
        normalized_actions = []
        if isinstance(actions, list):
            for a in actions:
                if not isinstance(a, dict):
                    continue
                action_type = a.get("type", "")
                if action_type not in ACTION_TYPES:
                    continue
                normalized_actions.append({
                    "type": action_type,
                    "enabled": bool(a.get("enabled", True)),
                    "config": a.get("config") if isinstance(a.get("config"), dict) else {},
                })

        return {
            "id": int(rule.get("id", 0)),
            "name": str(rule.get("name", "") or "").strip() or "未命名规则",
            "trigger_type": trigger_type,
            "cron": str(rule.get("cron", "") or ""),
            "webhook_token": str(rule.get("webhook_token", "") or ""),
            "actions": normalized_actions,
            "is_enabled": bool(rule.get("is_enabled", True)),
        }

    # ==================== 执行 ====================

    async def run_rule(self, rule_id: int, context: str = "") -> dict:
        """按规则动作序列执行，返回执行结果 dict

        每个动作独立 try/except，记录成功/失败；跳过（disabled）视为成功。
        """
        rule = self.get_rule(rule_id)
        if not rule:
            return {"rule_id": int(rule_id), "success": False, "message": "规则不存在", "results": []}
        if not rule.get("is_enabled", True):
            return {
                "rule_id": int(rule_id),
                "name": rule.get("name", ""),
                "success": False,
                "message": "规则已禁用，跳过执行",
                "results": [],
            }

        # 防同规则重入（check + add 之间无 await，事件循环单线程下安全）
        if rule_id in self._running:
            return {
                "rule_id": int(rule_id),
                "name": rule.get("name", ""),
                "success": False,
                "message": "规则正在执行中，已忽略本次触发",
                "results": [],
            }
        self._running.add(rule_id)

        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        results = []
        try:
            for action in rule.get("actions", []) or []:
                action_type = action.get("type", "")
                if not action.get("enabled", True):
                    results.append({
                        "type": action_type,
                        "status": "skipped",
                        "success": True,
                        "message": "动作已禁用，跳过",
                    })
                    continue
                try:
                    step = await self._execute_action(action_type, action.get("config", {}) or {})
                except Exception as e:
                    logger.warning(f"自动化规则 {rule_id} 动作 {action_type} 执行异常: {e}")
                    step = {"status": "failed", "success": False, "message": f"动作执行异常: {e}"}
                results.append({"type": action_type, **step})
        finally:
            self._running.discard(rule_id)

        success = all(r.get("success", False) for r in results)
        return {
            "rule_id": int(rule_id),
            "name": rule.get("name", ""),
            "context": context,
            "started_at": started_at,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "message": "执行完成" if success else "存在失败动作",
            "results": results,
        }

    async def _execute_action(self, action_type: str, config: dict) -> dict:
        """按动作类型分发执行，返回 {"status", "success", "message", "data"}"""
        if action_type == "strm_sync":
            return await self._run_strm_sync(config)
        if action_type == "scrape":
            return await self._run_scrape(config)
        if action_type == "organize":
            return await self._run_organize(config)
        if action_type == "emby_refresh":
            return await self._run_emby_refresh(config)
        return {"status": "failed", "success": False, "message": f"不支持的动作类型: {action_type}"}

    # ---------- 动作 1：增量同步 ----------

    async def _run_strm_sync(self, config: dict) -> dict:
        """增量同步：读 SyncService.load_schedule() 取 source_cid/local_media_dir/account_id，
        config 中的字段优先，缺省用同步计划。"""
        from app.services.sync_service import SyncService

        schedule = SyncService.load_schedule() or {}
        source_cid = (config.get("source_cid") or "").strip() or schedule.get("source_cid", "")
        local_media_dir = (config.get("local_media_dir") or "").strip() or schedule.get("local_media_dir", "")
        if not source_cid or not local_media_dir:
            return {
                "status": "failed", "success": False,
                "message": "未配置同步计划（source_cid / local_media_dir 为空）",
            }

        account = _resolve_account(config.get("account_id") or schedule.get("account_id"))
        if not account:
            return {"status": "failed", "success": False, "message": "未找到有效 115 账号"}
        if account.get("status") == 0:
            return {"status": "failed", "success": False, "message": f"账号 {account.get('id')} cookies 已失效"}

        cookies = account.get("cookies", "")
        video_exts = _parse_exts(config.get("video_exts_str") or schedule.get("video_exts_str", ""))
        image_exts = _parse_exts(config.get("image_exts_str") or schedule.get("image_exts_str", ""))
        data_exts = _parse_exts(config.get("data_exts_str") or schedule.get("data_exts_str", ""))
        try:
            min_video_size_mb = int(config.get("min_video_size_mb") or schedule.get("min_video_size_mb", 0) or 0)
        except (TypeError, ValueError):
            min_video_size_mb = 0

        loop = asyncio.get_running_loop()
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
            loop=loop,
        )
        synced_count = len(result.get("synced", [])) if isinstance(result, dict) else 0
        skipped = result.get("skipped", 0) if isinstance(result, dict) else 0
        return {
            "status": "success",
            "success": True,
            "message": f"增量同步完成: 新增 {synced_count} 个, 跳过 {skipped} 个",
            "data": {"total": result.get("total"), "synced": synced_count, "skipped": skipped},
        }

    # ---------- 动作 2：STRM 刮削 ----------

    async def _run_scrape(self, config: dict) -> dict:
        """STRM 刮削：scan_for_scrape 扫描分组 → scrape_group TMDB 刮削 → write_nfo 写 NFO/海报。
        config 含 tmdb_api_key/local_dir/source_cid，缺省用同步计划；tmdb_api_key 缺省取全局设置。"""
        from app.core.db_helper import read_setting
        from app.services.strmscrape_service import StrmScrapeService
        from app.services.sync_service import SyncService

        schedule = SyncService.load_schedule() or {}
        source_cid = (config.get("source_cid") or "").strip() or schedule.get("source_cid", "")
        if not source_cid:
            return {"status": "failed", "success": False, "message": "未配置刮削源目录（source_cid 为空）"}

        local_dir = (config.get("local_dir") or "").strip() or schedule.get("local_media_dir", "")
        tmdb_api_key = (config.get("tmdb_api_key") or "").strip()
        if not tmdb_api_key:
            tmdb_api_key = (read_setting("tmdb") or {}).get("api_key", "") or ""

        account = _resolve_account(config.get("account_id") or schedule.get("account_id"))
        if not account:
            return {"status": "failed", "success": False, "message": "未找到有效 115 账号"}

        cookies = account.get("cookies", "")
        groups = await asyncio.to_thread(StrmScrapeService.scan_for_scrape, cookies, source_cid)
        if not groups:
            return {"status": "success", "success": True, "message": "未扫描到视频文件", "data": {"groups": 0}}

        hit, miss, nfo_paths = 0, 0, []
        for group in groups:
            try:
                scraped = await asyncio.to_thread(
                    StrmScrapeService.scrape_group,
                    cookies, tmdb_api_key,
                    group.get("group", ""), group.get("files", []),
                )
            except Exception as e:
                logger.warning(f"刮削分组失败: {e}")
                miss += 1
                continue
            if not scraped or not scraped.get("tmdb_id"):
                miss += 1
                continue
            hit += 1
            if local_dir:
                poster_path = scraped.get("poster_path") or ""
                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
                nfo_path = await asyncio.to_thread(
                    StrmScrapeService.write_nfo,
                    local_dir, scraped.get("title", ""), scraped.get("year"),
                    scraped.get("tmdb_id"), poster_url,
                )
                if nfo_path:
                    nfo_paths.append(nfo_path)

        return {
            "status": "success",
            "success": True,
            "message": f"刮削完成: 命中 {hit} 组, 未命中 {miss} 组",
            "data": {"groups": len(groups), "hit": hit, "miss": miss, "nfo_count": len(nfo_paths)},
        }

    # ---------- 动作 3：整理 ----------

    async def _run_organize(self, config: dict) -> dict:
        """整理：参考 api/organize.py 的 /run 实现（OrganizeService.scan_and_organize 为异步）。
        config 含 target_cid 等，缺省用同步计划 source_cid；整理目录/分类/洗版/重命名缺省读取 config/ 下配置文件。"""
        from app.services.organize_service import OrganizeService
        from app.services.sync_service import SyncService

        schedule = SyncService.load_schedule() or {}

        # 整理目录默认配置（organize_dirs.json）
        dirs_cfg = {}
        dirs_file = CONFIG_DIR / "organize_dirs.json"
        if dirs_file.exists():
            try:
                dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"读取整理目录配置失败: {e}")

        source_cid = (config.get("source_cid") or "").strip() or dirs_cfg.get("source_cid", "")
        target_cid = (config.get("target_cid") or "").strip() or schedule.get("source_cid", "")
        if not source_cid:
            return {"status": "failed", "success": False, "message": "未配置整理源目录（source_cid 为空）"}
        if not target_cid:
            return {"status": "failed", "success": False, "message": "未配置全量同步目录（target_cid 为空）"}

        account = _resolve_account(config.get("account_id") or schedule.get("account_id"))
        if not account:
            return {"status": "failed", "success": False, "message": "未找到有效 115 账号"}

        # 二级分类配置（classify_config.json）
        classify_config = config.get("classify_config", "") or ""
        category_roots = config.get("category_roots")
        classify_file = CONFIG_DIR / "classify_config.json"
        if classify_file.exists():
            try:
                classify_data = json.loads(classify_file.read_text(encoding="utf-8"))
                if not classify_config:
                    classify_config = classify_data.get("classify_config", "") or ""
                if category_roots is None:
                    category_roots = classify_data.get("category_roots")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"读取二级分类配置失败: {e}")

        # 洗版策略（wash_config.json）
        wash_config = config.get("wash_config")
        wash_file = CONFIG_DIR / "wash_config.json"
        if wash_file.exists() and wash_config is None:
            try:
                wash_config = json.loads(wash_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                wash_config = None

        # 重命名规则（rename_rules.json）
        rename_rules = config.get("rename_rules")
        rename_file = CONFIG_DIR / "rename_rules.json"
        if rename_file.exists() and rename_rules is None:
            try:
                rename_rules = json.loads(rename_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                rename_rules = None

        def _cfg_int(key, default):
            try:
                v = config.get(key, dirs_cfg.get(key, default))
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        result = await OrganizeService.scan_and_organize(
            cookies=account.get("cookies", ""),
            source_cid=source_cid,
            target_cid=target_cid,
            existing_cid=(config.get("existing_cid") or "").strip() or dirs_cfg.get("existing_cid", ""),
            redundant_cid=(config.get("redundant_cid") or "").strip() or dirs_cfg.get("redundant_cid", ""),
            unrecognized_cid=(config.get("unrecognized_cid") or "").strip() or dirs_cfg.get("unrecognized_cid", ""),
            classify_config=classify_config,
            category_roots=category_roots,
            rename_rules=rename_rules,
            wash_config=wash_config,
            use_ffprobe=bool(config.get("use_ffprobe", dirs_cfg.get("use_ffprobe", False))),
            skip_no_info=bool(config.get("skip_no_info", dirs_cfg.get("skip_no_info", False))),
            prefer_filename=bool(config.get("prefer_filename", dirs_cfg.get("prefer_filename", False))),
            min_organize_size_mb=_cfg_int("min_organize_size_mb", 0),
            organize_blacklist=(config.get("organize_blacklist") or "").strip() or dirs_cfg.get("organize_blacklist", ""),
            ai_mode=(config.get("ai_mode") or "").strip() or dirs_cfg.get("ai_mode", "off"),
            dry_run=False,
        )

        organized = len(result.get("organized", []))
        redundant = len(result.get("redundant", []))
        unrecognized = len(result.get("unrecognized", []))
        errors = len(result.get("errors", []))
        return {
            "status": "success",
            "success": True,
            "message": f"整理完成: 成功 {organized}, 冗余 {redundant}, 无法识别 {unrecognized}, 失败 {errors}",
            "data": {"organized": organized, "redundant": redundant, "unrecognized": unrecognized, "errors": errors},
        }

    # ---------- 动作 4：Emby 刷新 ----------

    async def _run_emby_refresh(self, config: dict) -> dict:
        """刷新 Emby 媒体库：config 含 library_id 时按库刷新，缺省刷新全部。
        参考 services/emby.py 的 trigger_emby_refresh / EmbyClient.refresh_library。"""
        import httpx

        from app.core.db_helper import read_setting
        from app.services.emby import EmbyClient, trigger_emby_refresh

        settings = read_setting("emby") or {}
        host = (settings.get("host") or "").strip()
        api_key = (settings.get("api_key") or "").strip()
        if not host or not api_key:
            return {"status": "failed", "success": False, "message": "未配置 Emby（host / api_key 为空）"}

        library_id = (config.get("library_id") or "").strip()
        if library_id:
            client = EmbyClient(host, api_key)
            url = f"{client.host}/Items/{library_id}/Refresh"
            try:
                async with httpx.AsyncClient(timeout=30.0) as http:
                    resp = await http.post(url, params={"api_key": client.api_key})
                ok = resp.status_code in (200, 204)
            except Exception as e:
                logger.warning(f"按媒体库刷新 Emby 失败: {e}")
                return {"status": "failed", "success": False, "message": f"按媒体库刷新失败: {e}"}
            if ok:
                return {
                    "status": "success", "success": True,
                    "message": f"已通知 Emby 刷新媒体库 {library_id}",
                    "data": {"library_id": library_id, "mode": "library"},
                }
            return {"status": "failed", "success": False, "message": f"按媒体库刷新失败（HTTP {resp.status_code}）"}

        ok = await trigger_emby_refresh()
        return {
            "status": "success" if ok else "failed",
            "success": ok,
            "message": "已通知 Emby 全库刷新" if ok else "Emby 全库刷新失败",
            "data": {"mode": "all"},
        }

    # ==================== 后台执行 / Webhook 触发 ====================

    def _run_rule_async(self, rule_id: int, context: str = ""):
        """asyncio.create_task 后台执行规则（避免阻塞 API 请求）"""
        try:
            task = asyncio.create_task(self.run_rule(rule_id, context))
        except RuntimeError:
            logger.warning("无运行中的事件循环，无法后台执行自动化规则")
            return None
        task.add_done_callback(self._on_run_done)
        return task

    @staticmethod
    def _on_run_done(task):
        try:
            task.result()
        except Exception as e:
            logger.warning(f"自动化规则后台执行异常: {e}")

    def handle_webhook(self, token: str) -> Optional[str]:
        """按 webhook_token 匹配规则：匹配则后台执行并返回规则名；未匹配返回 None"""
        token = (token or "").strip()
        if not token:
            return None
        for rule in self.list_rules():
            if not rule.get("is_enabled", True):
                continue
            if rule.get("trigger_type") != "webhook":
                continue
            if (rule.get("webhook_token") or "").strip() != token:
                continue
            self._run_rule_async(rule.get("id"), context="webhook")
            return rule.get("name", "")
        return None

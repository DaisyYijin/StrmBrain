"""
媒体整理规划器
==============

整理前先生成操作计划（预览），用户确认后批量执行。

工作流程：
1. scan_and_plan：扫描源目录 -> TMDB 匹配 -> 生成操作计划（预览，不实际移动文件）
2. 用户在前端确认计划
3. execute_plan：批量执行确认后的计划（实际移动/重命名文件）

设计原则：
- 复用 organize_service 的重命名规则逻辑（不重复实现重命名算法）
- scan_and_plan 调用 OrganizeService.scan_and_organize(dry_run=True) 生成预览
- execute_plan 调用 OrganizeService.scan_and_organize(dry_run=False) 实际执行
- 冲突检测：目标路径已存在文件时标记 action="skip"
- 计划中携带执行上下文（_context），execute_plan 据此重新执行整理
"""
import json
from typing import Any, Optional

from app.core.logbuffer import get_logger
from app.config import CONFIG_DIR

logger = get_logger("app.services.organize_planner")


class OrganizePlanner:
    """
    媒体整理规划器。

    通过两阶段操作（预览 -> 确认 -> 执行）实现安全的批量整理：
    - scan_and_plan：生成操作计划，不修改任何文件
    - execute_plan：执行确认后的计划

    典型用法::

        from app.services.organize_planner import get_organize_planner
        planner = get_organize_planner()
        plan = await planner.scan_and_plan(source_cid, cookies, rename_rules)
        # 用户确认后...
        result = await planner.execute_plan(plan, cookies)
    """

    # ===== 配置读取 =====

    @staticmethod
    def _load_organize_context(source_cid: str, rename_rules: Optional[dict] = None) -> dict:
        """从已保存的整理配置文件中读取完整执行上下文。

        读取 organize_dirs.json / classify_config.json / wash_config.json /
        rename_rules.json / sync_schedule.json，组装为 scan_and_organize 的参数。

        Args:
            source_cid: 源目录 cid（覆盖配置中的 source_cid）
            rename_rules: 可选的重命名规则（覆盖配置中的 rename_rules）

        Returns:
            执行上下文字典，包含 scan_and_organize 所需的全部参数。
        """
        context: dict[str, Any] = {
            "source_cid": source_cid,
        }

        # 读取整理目录配置
        dirs_file = CONFIG_DIR / "organize_dirs.json"
        if dirs_file.exists():
            try:
                dirs_cfg = json.loads(dirs_file.read_text(encoding="utf-8"))
                context["existing_cid"] = dirs_cfg.get("existing_cid", "")
                context["redundant_cid"] = dirs_cfg.get("redundant_cid", "")
                context["unrecognized_cid"] = dirs_cfg.get("unrecognized_cid", "")
                context["use_ffprobe"] = dirs_cfg.get("use_ffprobe", False)
                context["skip_no_info"] = dirs_cfg.get("skip_no_info", False)
                context["prefer_filename"] = dirs_cfg.get("prefer_filename", False)
                context["min_organize_size_mb"] = dirs_cfg.get("min_organize_size_mb", 0)
                context["organize_blacklist"] = dirs_cfg.get("organize_blacklist", "")
                context["ai_mode"] = dirs_cfg.get("ai_mode", "off")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[planner] 读取整理目录配置失败: {e}")

        # 读取全量同步目录作为整理目标（target_cid）
        sync_schedule_file = CONFIG_DIR / "sync_schedule.json"
        if sync_schedule_file.exists():
            try:
                schedule = json.loads(sync_schedule_file.read_text(encoding="utf-8"))
                context["target_cid"] = schedule.get("source_cid", "")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[planner] 读取同步配置失败: {e}")

        # 读取二级分类配置
        classify_file = CONFIG_DIR / "classify_config.json"
        if classify_file.exists():
            try:
                classify_data = json.loads(classify_file.read_text(encoding="utf-8"))
                context["classify_config"] = classify_data.get("classify_config", "")
                context["category_roots"] = classify_data.get("category_roots")
            except (json.JSONDecodeError, OSError):
                pass

        # 读取洗版策略
        wash_file = CONFIG_DIR / "wash_config.json"
        if wash_file.exists():
            try:
                context["wash_config"] = json.loads(wash_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # 读取重命名规则（参数优先，否则从配置文件读取）
        if rename_rules:
            context["rename_rules"] = rename_rules
        else:
            rename_file = CONFIG_DIR / "rename_rules.json"
            if rename_file.exists():
                try:
                    context["rename_rules"] = json.loads(rename_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass

        return context

    # ===== 预览计划 =====

    async def scan_and_plan(
        self,
        source_path: str,
        cookies: str,
        rename_rules: Optional[dict] = None,
    ) -> dict:
        """扫描目录 + TMDB 匹配 + 生成操作计划（预览，不实际移动文件）。

        复用 OrganizeService.scan_and_organize 的重命名规则逻辑，
        以 dry_run=True 模式执行，生成预览计划。

        Args:
            source_path: 源目录 cid（115 网盘目录 ID）
            cookies: 115 账号 cookies
            rename_rules: 可选的重命名规则字典（覆盖已保存的配置）

        Returns:
            操作计划字典::

                {
                    "plan": [
                        {
                            "file_name": str,       # 原始文件名
                            "current_path": str,    # 当前所在路径
                            "target_path": str,     # 目标路径
                            "new_name": str,        # 新文件名（未重命名则为空）
                            "action": "rename"|"move"|"skip",  # 操作类型
                            "reason": str,          # 原因说明
                        },
                        ...
                    ],
                    "total": int,        # 计划总数
                    "skip_count": int,   # 跳过数量
                    "_context": dict,    # 执行上下文（内部使用，execute_plan 据此执行）
                }
        """
        from app.services.organize_service import OrganizeService

        # 读取执行上下文
        ctx = self._load_organize_context(source_path, rename_rules)

        target_cid = ctx.get("target_cid", "")
        if not target_cid:
            return {
                "plan": [],
                "total": 0,
                "skip_count": 0,
                "_context": ctx,
                "error": "未配置全量同步目录，请先在全量同步页面设置源目录",
            }

        if not source_path:
            return {
                "plan": [],
                "total": 0,
                "skip_count": 0,
                "_context": ctx,
                "error": "请选择需要整理的源目录",
            }

        logger.info(f"[planner] 生成预览计划: source_cid={source_path}, target_cid={target_cid}")

        # 调用现有整理服务（dry_run=True 仅预览）
        try:
            organize_result = await OrganizeService.scan_and_organize(
                cookies=cookies,
                source_cid=ctx.get("source_cid", source_path),
                target_cid=target_cid,
                existing_cid=ctx.get("existing_cid", ""),
                redundant_cid=ctx.get("redundant_cid", ""),
                unrecognized_cid=ctx.get("unrecognized_cid", ""),
                classify_config=ctx.get("classify_config", ""),
                category_roots=ctx.get("category_roots"),
                rename_rules=ctx.get("rename_rules"),
                wash_config=ctx.get("wash_config"),
                use_ffprobe=ctx.get("use_ffprobe", False),
                skip_no_info=ctx.get("skip_no_info", False),
                prefer_filename=ctx.get("prefer_filename", False),
                min_organize_size_mb=ctx.get("min_organize_size_mb", 0),
                organize_blacklist=ctx.get("organize_blacklist", ""),
                ai_mode=ctx.get("ai_mode", "off"),
                dry_run=True,
            )
        except Exception as e:
            logger.warning(f"[planner] 生成预览计划失败: {e}", exc_info=True)
            return {
                "plan": [],
                "total": 0,
                "skip_count": 0,
                "_context": ctx,
                "error": f"生成预览计划失败: {str(e)}",
            }

        # 将整理结果转换为计划格式
        plan_items: list[dict] = []
        skip_count = 0

        # 已整理的文件 -> action="move" 或 "rename"
        for item in organize_result.get("organized", []):
            renamed_to = item.get("renamed_to", "")
            action = "rename" if renamed_to else "move"
            plan_items.append({
                "file_name": item.get("name", ""),
                "current_path": item.get("from", ""),
                "target_path": item.get("to", ""),
                "new_name": renamed_to,
                "action": action,
                "reason": "",
            })

        # 冗余文件（目标已存在等）-> action="skip"
        for item in organize_result.get("redundant", []):
            reason = item.get("reason", "目标已存在")
            plan_items.append({
                "file_name": item.get("name", ""),
                "current_path": "",
                "target_path": "",
                "new_name": "",
                "action": "skip",
                "reason": reason,
            })
            skip_count += 1

        # 无法识别的文件 -> action="skip"
        for item in organize_result.get("unrecognized", []):
            reason = item.get("reason", "无法识别")
            plan_items.append({
                "file_name": item.get("name", ""),
                "current_path": "",
                "target_path": "",
                "new_name": "",
                "action": "skip",
                "reason": reason,
            })
            skip_count += 1

        # 出错的文件 -> action="skip"
        for item in organize_result.get("errors", []):
            reason = item.get("error", "处理出错")
            plan_items.append({
                "file_name": item.get("name", ""),
                "current_path": "",
                "target_path": "",
                "new_name": "",
                "action": "skip",
                "reason": reason,
            })
            skip_count += 1

        logger.info(
            f"[planner] 预览计划生成完成: 共 {len(plan_items)} 项, "
            f"跳过 {skip_count} 项"
        )

        return {
            "plan": plan_items,
            "total": len(plan_items),
            "skip_count": skip_count,
            "_context": ctx,
        }

    # ===== 执行计划 =====

    async def execute_plan(self, plan: dict, cookies: str) -> dict:
        """批量执行确认后的计划。

        从计划中提取执行上下文，调用 OrganizeService.scan_and_organize(dry_run=False)
        实际执行整理。只执行 action != "skip" 的条目（由整理服务内部处理）。

        Args:
            plan: scan_and_plan 返回的计划字典（含 _context 执行上下文）
            cookies: 115 账号 cookies

        Returns:
            执行结果字典::

                {
                    "success": int,       # 成功数
                    "failed": int,        # 失败数
                    "skipped": int,       # 跳过数
                    "errors": [str],      # 错误信息列表
                }
        """
        from app.services.organize_service import OrganizeService

        result = {"success": 0, "failed": 0, "skipped": 0, "errors": []}

        ctx = plan.get("_context", {})
        source_cid = ctx.get("source_cid", "")
        target_cid = ctx.get("target_cid", "")

        if not source_cid:
            result["errors"].append("计划中缺少源目录信息（source_cid）")
            return result

        if not target_cid:
            result["errors"].append("计划中缺少目标目录信息（target_cid）")
            return result

        # 统计计划中的 skip 条目数
        plan_items = plan.get("plan", [])
        skip_count = sum(1 for item in plan_items if item.get("action") == "skip")
        result["skipped"] = skip_count

        logger.info(
            f"[planner] 执行计划: source_cid={source_cid}, target_cid={target_cid}, "
            f"计划 {len(plan_items)} 项（其中跳过 {skip_count} 项）"
        )

        # 调用现有整理服务（dry_run=False 实际执行）
        try:
            organize_result = await OrganizeService.scan_and_organize(
                cookies=cookies,
                source_cid=source_cid,
                target_cid=target_cid,
                existing_cid=ctx.get("existing_cid", ""),
                redundant_cid=ctx.get("redundant_cid", ""),
                unrecognized_cid=ctx.get("unrecognized_cid", ""),
                classify_config=ctx.get("classify_config", ""),
                category_roots=ctx.get("category_roots"),
                rename_rules=ctx.get("rename_rules"),
                wash_config=ctx.get("wash_config"),
                use_ffprobe=ctx.get("use_ffprobe", False),
                skip_no_info=ctx.get("skip_no_info", False),
                prefer_filename=ctx.get("prefer_filename", False),
                min_organize_size_mb=ctx.get("min_organize_size_mb", 0),
                organize_blacklist=ctx.get("organize_blacklist", ""),
                ai_mode=ctx.get("ai_mode", "off"),
                dry_run=False,
            )

            # 统计执行结果
            success = len(organize_result.get("organized", []))
            failed = len(organize_result.get("errors", []))
            result["success"] = success
            result["failed"] = failed
            result["skipped"] += len(organize_result.get("redundant", []))
            result["skipped"] += len(organize_result.get("unrecognized", []))

            # 收集错误信息
            for err in organize_result.get("errors", []):
                err_msg = err.get("error", "") or err.get("name", "")
                if err_msg:
                    result["errors"].append(err_msg)

            logger.info(
                f"[planner] 计划执行完成: 成功 {success}, 失败 {failed}, "
                f"跳过 {result['skipped']}"
            )

            # 执行完成后根据配置触发 Emby 刷新和通知
            await self._post_execute_hooks(organize_result, success)

        except Exception as e:
            logger.warning(f"[planner] 执行计划失败: {e}", exc_info=True)
            result["failed"] += 1
            result["errors"].append(str(e))

        return result

    async def _post_execute_hooks(self, organize_result: dict, success_count: int) -> None:
        """执行完成后的钩子：Emby 刷新 + 通知（与 organize API 保持一致）。"""
        try:
            if success_count <= 0:
                return

            from app.core.db_helper import read_setting
            sync_cfg = read_setting("emby_sync") or {}
            if sync_cfg.get("auto_refresh", True):
                from app.services.emby import trigger_emby_refresh
                await trigger_emby_refresh()

            notify_cfg = read_setting("emby_notify") or {}
            if notify_cfg.get("notify_on_organize", True):
                from app.services.notification_service import NotificationService
                await NotificationService.notify_organize_complete(organize_result)
        except Exception as e:
            logger.warning(f"[planner] 执行后钩子异常: {e}")


# ===== 全局单例 =====

_global_organize_planner: Optional[OrganizePlanner] = None


def get_organize_planner() -> OrganizePlanner:
    """获取全局媒体整理规划器单例。"""
    global _global_organize_planner
    if _global_organize_planner is None:
        _global_organize_planner = OrganizePlanner()
    return _global_organize_planner

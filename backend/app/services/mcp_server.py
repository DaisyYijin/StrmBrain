"""
MCP Server - Model Context Protocol 服务端

为 AI 助手提供 JSON-RPC 接口，支持通过 MCP 协议控制 STRMhub：
- SSE 端点（GET /api/mcp/sse）：服务端 -> 客户端的消息推送
- POST 端点（POST /api/mcp/messages）：客户端 -> 服务端的 JSON-RPC 请求

注册的工具：
- get_status：获取系统状态
- trigger_sync_inc：触发增量同步
- trigger_sync_full：触发全量同步
- trigger_organize：触发影视整理
- list_accounts：列出 115 账号
- add_share_transfer：添加分享链接转存任务
- trigger_checkin：触发 115 每日签到
- get_rate_stats：获取 API 速率统计

JSON-RPC 方法：
- initialize：返回服务器能力
- tools/list：返回工具列表
- tools/call：执行指定工具
"""
import asyncio
import uuid
from typing import Any, Optional

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting, read_accounts, get_first_valid_account, find_account

logger = get_logger("app.services.mcp_server")


class MCPSession:
    """MCP 会话：管理一个 SSE 连接的消息队列"""

    def __init__(self, session_id: str, loop: asyncio.AbstractEventLoop):
        self.session_id = session_id
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def send(self, message: dict):
        """向会话队列投递消息（跨线程安全）"""
        if self.closed:
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, message)
        except RuntimeError:
            pass

    async def wait_message(self, timeout: float = 30.0) -> Optional[dict]:
        """等待下一条消息，超时返回 None"""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self):
        """关闭会话"""
        self.closed = True


class MCPServer:
    """
    MCP 服务端：管理 SSE 会话和 JSON-RPC 消息分发。

    单例模式，全局共享。工具注册在初始化时完成。
    """

    _instance: Optional["MCPServer"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._sessions: dict[str, MCPSession] = {}
            cls._instance._tools: dict[str, dict] = {}
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._register_tools()
            self._initialized = True

    # ===== 会话管理 =====

    def create_session(self, loop: asyncio.AbstractEventLoop) -> str:
        """创建新的 MCP 会话，返回会话 ID"""
        session_id = uuid.uuid4().hex[:16]
        session = MCPSession(session_id, loop)
        self._sessions[session_id] = session
        logger.info(f"[mcp] 新建会话: {session_id}")
        return session_id

    def close_session(self, session_id: str):
        """关闭并移除会话"""
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            logger.info(f"[mcp] 会话已关闭: {session_id}")

    def get_session(self, session_id: str) -> Optional[MCPSession]:
        """获取会话"""
        return self._sessions.get(session_id)

    def wait_message(self, session_id: str, timeout: float = 30.0) -> Any:
        """等待指定会话的下一条消息"""
        session = self._sessions.get(session_id)
        if not session:
            return asyncio.sleep(0)  # 会话不存在时立即返回
        return session.wait_message(timeout)

    # ===== 工具注册 =====

    def _register_tools(self):
        """注册全部 MCP 工具"""
        self._tools = {
            "get_status": {
                "description": "获取 STRMhub 系统状态，包括版本、账号数量、任务运行状态等",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "handler": self._tool_get_status,
            },
            "trigger_sync_inc": {
                "description": "触发增量同步（仅同步新增/变更的文件），使用已保存的同步配置",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "integer", "description": "115 账号 ID，0=自动选择第一个有效账号", "default": 0},
                    },
                    "required": [],
                },
                "handler": self._tool_trigger_sync_inc,
            },
            "trigger_sync_full": {
                "description": "触发全量同步（重新扫描并生成所有 STRM 文件），使用已保存的同步配置",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "integer", "description": "115 账号 ID，0=自动选择第一个有效账号", "default": 0},
                    },
                    "required": [],
                },
                "handler": self._tool_trigger_sync_full,
            },
            "trigger_organize": {
                "description": "触发影视整理（扫描源目录，通过 TMDB 识别并分类移动文件）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "integer", "description": "115 账号 ID，0=自动选择第一个有效账号", "default": 0},
                        "source_cid": {"type": "string", "description": "源目录 cid（留空则使用整理配置中保存的目录）", "default": ""},
                        "dry_run": {"type": "boolean", "description": "是否仅预览不实际移动", "default": False},
                    },
                    "required": [],
                },
                "handler": self._tool_trigger_organize,
            },
            "list_accounts": {
                "description": "列出所有已配置的 115 网盘账号（不返回 cookies 等敏感信息）",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "handler": self._tool_list_accounts,
            },
            "add_share_transfer": {
                "description": "添加 115 分享链接转存任务（将分享链接中的文件转存到自己的网盘）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "share_url": {"type": "string", "description": "115 分享链接"},
                        "target_cid": {"type": "string", "description": "转存到目标目录 cid，0=根目录", "default": "0"},
                        "account_id": {"type": "integer", "description": "115 账号 ID，0=自动选择", "default": 0},
                    },
                    "required": ["share_url"],
                },
                "handler": self._tool_add_share_transfer,
            },
            "trigger_checkin": {
                "description": "触发 115 每日签到（遍历所有有效账号逐个签到）",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "handler": self._tool_trigger_checkin,
            },
            "get_rate_stats": {
                "description": "获取 115 API 速率限制和请求统计（QPS/QPM/延迟/缓存命中率/限流次数）",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "handler": self._tool_get_rate_stats,
            },
        }

    # ===== JSON-RPC 消息处理 =====

    async def handle_message(self, message: dict) -> dict:
        """
        处理 JSON-RPC 2.0 请求。

        支持 initialize / tools/list / tools/call 方法。
        返回 JSON-RPC 响应 dict。
        """
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params", {}) or {}

        try:
            if method == "initialize":
                return self._handle_initialize(msg_id, params)
            elif method == "notifications/initialized":
                # 客户端初始化完成通知，无需响应
                return {}
            elif method == "tools/list":
                return self._handle_tools_list(msg_id, params)
            elif method == "tools/call":
                return await self._handle_tools_call(msg_id, params)
            elif method == "ping":
                return {"jsonrpc": "2.0", "result": {}, "id": msg_id}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": msg_id,
                }
        except Exception as e:
            logger.warning(f"[mcp] 处理消息异常: {method} - {e}")
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {e}"},
                "id": msg_id,
            }

    def _handle_initialize(self, msg_id, params: dict) -> dict:
        """处理 initialize 请求，返回服务器能力和协议版本"""
        return {
            "jsonrpc": "2.0",
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "STRMhub MCP Server",
                    "version": "1.0.0",
                },
            },
            "id": msg_id,
        }

    def _handle_tools_list(self, msg_id, params: dict) -> dict:
        """处理 tools/list 请求，返回工具列表"""
        tools = []
        for name, tool in self._tools.items():
            tools.append({
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            })
        return {"jsonrpc": "2.0", "result": {"tools": tools}, "id": msg_id}

    async def _handle_tools_call(self, msg_id, params: dict) -> dict:
        """处理 tools/call 请求，执行指定工具"""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}

        tool = self._tools.get(tool_name)
        if not tool:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
                "id": msg_id,
            }

        try:
            result = await tool["handler"](arguments)
            return {
                "jsonrpc": "2.0",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": _to_json_text(result),
                        }
                    ],
                },
                "id": msg_id,
            }
        except Exception as e:
            logger.warning(f"[mcp] 工具执行异常: {tool_name} - {e}")
            return {
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": f"工具执行失败: {e}"}],
                    "isError": True,
                },
                "id": msg_id,
            }

    # ===== 工具实现 =====

    async def _tool_get_status(self, args: dict) -> dict:
        """获取系统状态"""
        from app.config import VERSION
        from app.core.progress import progress_manager
        from app.core.event_bus import get_event_bus

        accounts = read_accounts()
        valid_accounts = [a for a in accounts if a.get("status") == 1]

        # Emby 配置状态
        emby_cfg = read_setting("emby")
        emby_configured = bool(emby_cfg.get("host") and emby_cfg.get("api_key"))

        # TMDB 配置状态
        tmdb_cfg = read_setting("tmdb")
        tmdb_configured = bool(tmdb_cfg.get("api_key"))

        # 同步计划
        from app.services.sync_service import SyncService
        schedule = SyncService.load_schedule()

        return {
            "version": VERSION,
            "accounts_total": len(accounts),
            "accounts_valid": len(valid_accounts),
            "emby_configured": emby_configured,
            "tmdb_configured": tmdb_configured,
            "task_running": progress_manager.is_running(),
            "sync_configured": bool(schedule.get("source_cid")),
            "sync_local_dir": schedule.get("local_media_dir", ""),
            "event_subscribers": get_event_bus().subscriber_count(),
        }

    async def _tool_trigger_sync_inc(self, args: dict) -> dict:
        """触发增量同步"""
        from app.services.sync_service import SyncService
        from app.core.progress import progress_manager

        account_id = args.get("account_id", 0)
        account = find_account(account_id) if account_id else get_first_valid_account()
        if not account:
            return {"success": False, "error": "未找到有效 115 账号"}

        if account.get("status") == 0:
            return {"success": False, "error": "账号 cookies 已失效"}

        schedule = SyncService.load_schedule()
        source_cid = schedule.get("source_cid", "")
        local_media_dir = schedule.get("local_media_dir", "")
        if not source_cid or not local_media_dir:
            return {"success": False, "error": "未配置同步目录，请先执行一次全量同步"}

        # 检查是否有任务正在运行
        if progress_manager.is_running():
            return {"success": False, "error": "已有任务正在运行，请等待完成"}

        # 后台执行增量同步
        loop = asyncio.get_running_loop()
        if not await progress_manager.start_task("incremental_sync", 0, "增量同步(MCP)"):
            return {"success": False, "error": "无法启动任务（已有任务运行中）"}

        async def _run():
            try:
                video_exts = _parse_exts_str(schedule.get("video_exts_str", ""))
                image_exts = _parse_exts_str(schedule.get("image_exts_str", ""))
                data_exts = _parse_exts_str(schedule.get("data_exts_str", ""))
                result = await asyncio.to_thread(
                    SyncService.incremental_sync,
                    cookies=account.get("cookies", ""),
                    source_cid=source_cid,
                    local_media_dir=local_media_dir,
                    video_exts=video_exts,
                    image_exts=image_exts,
                    data_exts=data_exts,
                    min_video_size_mb=schedule.get("min_video_size_mb", 0),
                    account_id=account.get("id", 0),
                    loop=loop,
                )
                await progress_manager.complete_task(
                    f"增量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
                )
            except Exception as e:
                await progress_manager.error_task(f"增量同步失败: {e}")
                logger.warning(f"[mcp] 增量同步失败: {e}")

        asyncio.create_task(_run())
        return {"success": True, "message": "增量同步已启动，可通过事件总线或进度通道查看状态"}

    async def _tool_trigger_sync_full(self, args: dict) -> dict:
        """触发全量同步"""
        from app.services.sync_service import SyncService
        from app.core.progress import progress_manager

        account_id = args.get("account_id", 0)
        account = find_account(account_id) if account_id else get_first_valid_account()
        if not account:
            return {"success": False, "error": "未找到有效 115 账号"}

        if account.get("status") == 0:
            return {"success": False, "error": "账号 cookies 已失效"}

        schedule = SyncService.load_schedule()
        source_cid = schedule.get("source_cid", "")
        local_media_dir = schedule.get("local_media_dir", "")
        if not source_cid or not local_media_dir:
            return {"success": False, "error": "未配置同步目录，请先在 Web 界面设置全量同步目录"}

        if progress_manager.is_running():
            return {"success": False, "error": "已有任务正在运行，请等待完成"}

        loop = asyncio.get_running_loop()
        if not await progress_manager.start_task("full_sync", 0, "全量同步(MCP)"):
            return {"success": False, "error": "无法启动任务（已有任务运行中）"}

        async def _run():
            try:
                video_exts = _parse_exts_str(schedule.get("video_exts_str", ""))
                image_exts = _parse_exts_str(schedule.get("image_exts_str", ""))
                data_exts = _parse_exts_str(schedule.get("data_exts_str", ""))
                result = await asyncio.to_thread(
                    SyncService.full_sync,
                    cookies=account.get("cookies", ""),
                    source_cid=source_cid,
                    local_media_dir=local_media_dir,
                    video_exts=video_exts,
                    image_exts=image_exts,
                    data_exts=data_exts,
                    min_video_size_mb=schedule.get("min_video_size_mb", 0),
                    account_id=account.get("id", 0),
                    loop=loop,
                )
                await progress_manager.complete_task(
                    f"全量同步完成: 新增 {len(result.get('synced', []))}，跳过 {result.get('skipped', 0)}"
                )
            except Exception as e:
                await progress_manager.error_task(f"全量同步失败: {e}")
                logger.warning(f"[mcp] 全量同步失败: {e}")

        asyncio.create_task(_run())
        return {"success": True, "message": "全量同步已启动，可通过事件总线或进度通道查看状态"}

    async def _tool_trigger_organize(self, args: dict) -> dict:
        """触发影视整理"""
        from app.services.organize_service import OrganizeService
        from app.core.progress import progress_manager
        from app.config import CONFIG_DIR
        import json as _json

        account_id = args.get("account_id", 0)
        source_cid = args.get("source_cid", "")
        dry_run = args.get("dry_run", False)

        account = find_account(account_id) if account_id else get_first_valid_account()
        if not account:
            return {"success": False, "error": "未找到有效 115 账号"}

        # source_cid 为空时从整理目录配置中读取
        if not source_cid:
            dirs_file = CONFIG_DIR / "organize_dirs.json"
            if dirs_file.exists():
                try:
                    dirs = _json.loads(dirs_file.read_text(encoding="utf-8"))
                    source_cid = dirs.get("source_cid", "")
                except Exception:
                    pass
            if not source_cid:
                return {"success": False, "error": "未指定整理源目录 cid"}

        # 读取全量同步目录作为整理目标
        from app.services.sync_service import SyncService
        schedule = SyncService.load_schedule()
        target_cid = schedule.get("source_cid", "")
        if not target_cid:
            return {"success": False, "error": "未配置全量同步目录（整理目标）"}

        # 读取整理配置
        rename_rules_file = CONFIG_DIR / "rename_rules.json"
        rename_rules = {}
        if rename_rules_file.exists():
            try:
                rename_rules = _json.loads(rename_rules_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        classify_config = ""
        classify_file = CONFIG_DIR / "classify_config.json"
        if classify_file.exists():
            try:
                classify_data = _json.loads(classify_file.read_text(encoding="utf-8"))
                classify_config = classify_data.get("yaml", "") or classify_data.get("config", "")
            except Exception:
                pass

        wash_config = {}
        wash_file = CONFIG_DIR / "wash_config.json"
        if wash_file.exists():
            try:
                wash_config = _json.loads(wash_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 读取整理目录配置（已存在/冗余/识别不准目录）
        dirs = {}
        dirs_file = CONFIG_DIR / "organize_dirs.json"
        if dirs_file.exists():
            try:
                dirs = _json.loads(dirs_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        if progress_manager.is_running():
            return {"success": False, "error": "已有任务正在运行，请等待完成"}

        if not await progress_manager.start_task("organize", 0, "影视整理(MCP)"):
            return {"success": False, "error": "无法启动任务（已有任务运行中）"}

        async def _run():
            try:
                async def _progress_cb(current, total, filename):
                    await progress_manager.update_progress(current, total, filename)

                result = await OrganizeService.scan_and_organize(
                    cookies=account.get("cookies", ""),
                    source_cid=source_cid,
                    target_cid=target_cid,
                    existing_cid=dirs.get("existing_cid", ""),
                    redundant_cid=dirs.get("redundant_cid", ""),
                    unrecognized_cid=dirs.get("unrecognized_cid", ""),
                    classify_config=classify_config,
                    rename_rules=rename_rules,
                    wash_config=wash_config,
                    dry_run=dry_run,
                    progress_callback=_progress_cb,
                )
                await progress_manager.complete_task(
                    f"整理完成: 成功 {len(result.get('organized', []))}，"
                    f"冗余 {len(result.get('redundant', []))}，"
                    f"无法识别 {len(result.get('unrecognized', []))}"
                )
            except Exception as e:
                await progress_manager.error_task(f"整理失败: {e}")
                logger.warning(f"[mcp] 整理失败: {e}")

        asyncio.create_task(_run())
        return {"success": True, "message": f"影视整理已启动（{'预览' if dry_run else '实际执行'}），可通过进度通道查看状态"}

    async def _tool_list_accounts(self, args: dict) -> dict:
        """列出 115 账号（不含敏感信息）"""
        accounts = read_accounts()
        result = []
        for acc in sorted(accounts, key=lambda a: a.get("id", 0)):
            result.append({
                "id": acc.get("id", 0),
                "name": acc.get("name", ""),
                "username": acc.get("username", ""),
                "user_id": acc.get("user_id", ""),
                "status": acc.get("status", 0),
                "status_text": "正常" if acc.get("status") == 1 else "失效",
                "vip_level": acc.get("vip_level", 0),
            })
        return {"accounts": result, "count": len(result)}

    async def _tool_add_share_transfer(self, args: dict) -> dict:
        """添加分享链接转存任务"""
        from app.services.client_115 import Client115Service

        share_url = args.get("share_url", "")
        target_cid = args.get("target_cid", "0")
        account_id = args.get("account_id", 0)

        if not share_url:
            return {"success": False, "error": "share_url 不能为空"}

        account = find_account(account_id) if account_id else get_first_valid_account()
        if not account:
            return {"success": False, "error": "未找到有效 115 账号"}

        cookies = account.get("cookies", "")

        # 先获取分享文件列表
        snap_result = Client115Service.share_snap(cookies, share_url)
        if snap_result.get("error"):
            return {"success": False, "error": f"获取分享文件列表失败: {snap_result['error']}"}

        # 提取文件 ID 列表
        file_ids = []
        data = snap_result.get("data", snap_result)
        if isinstance(data, dict):
            items = data.get("list", [])
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for item in items:
            fid = item.get("file_id") or item.get("id") or item.get("fid")
            if fid:
                file_ids.append(str(fid))

        if not file_ids:
            return {"success": False, "error": "分享链接中没有可转存的文件"}

        # 执行转存
        receive_result = Client115Service.share_receive(cookies, share_url, file_ids, target_cid)
        if receive_result.get("error"):
            return {"success": False, "error": f"转存失败: {receive_result['error']}"}

        return {
            "success": True,
            "message": f"已转存 {len(file_ids)} 个文件到目录 cid={target_cid}",
            "file_count": len(file_ids),
        }

    async def _tool_trigger_checkin(self, args: dict) -> dict:
        """触发 115 每日签到"""
        from app.services.client_115 import Client115Service

        accounts = read_accounts()
        valid = [a for a in accounts if a.get("status") == 1]
        if not valid:
            return {"success": False, "error": "无有效 115 账号，无法签到"}

        results = []
        for acc in valid:
            cookies = acc.get("cookies", "")
            name = acc.get("name") or acc.get("username", f"ID:{acc.get('id')}")
            result = Client115Service.daily_checkin(cookies)
            if result.get("error"):
                results.append({"account": name, "success": False, "error": result["error"]})
            else:
                results.append({"account": name, "success": True, "data": result})

        success_count = sum(1 for r in results if r["success"])
        return {
            "success": True,
            "message": f"签到完成: 成功 {success_count}/{len(results)}",
            "details": results,
        }

    async def _tool_get_rate_stats(self, args: dict) -> dict:
        """获取 API 速率统计"""
        from app.services.client_115 import Client115Service
        return Client115Service.get_rate_limit_stats()


# ===== 辅助函数 =====

def _parse_exts_str(exts_str: str) -> set:
    """解析后缀字符串为集合（如 "mp4,mkv" -> {".mp4", ".mkv"}）"""
    result = set()
    if not exts_str:
        return result
    for ext in exts_str.split(","):
        ext = ext.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        result.add(ext)
    return result


def _to_json_text(data: Any) -> str:
    """将数据转为 JSON 文本（MCP 工具返回格式）"""
    import json
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(data)


def get_mcp_server() -> MCPServer:
    """获取 MCP Server 全局单例"""
    return MCPServer()

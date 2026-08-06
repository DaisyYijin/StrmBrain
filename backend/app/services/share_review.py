"""
分享审核队列服务（#27）

管理 115 网盘分享链接的审核队列。用户提交分享链接后，需先审核再决定是否转存。
数据持久化到 data/share_review_queue.json，启动时自动加载，每次修改后自动保存。
线程安全（threading.RLock 可重入锁，允许 _save 在持锁方法内嵌套调用）。
"""
import uuid
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR
from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.share_review")

# 持久化文件路径
QUEUE_FILE = DATA_DIR / "share_review_queue.json"

# settings.json 中存放分享审核配置的 key
_SETTINGS_KEY = "share_review"

# 合法的来源
_VALID_SOURCES = ("manual", "webhook", "telegram")

# 合法的状态
_VALID_STATUS = ("pending", "approved", "rejected")


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串"""
    return datetime.now(timezone.utc).isoformat()


class ShareReviewQueue:
    """
    分享审核队列（单例）

    管理待审核的 115 分享链接：添加、审核（通过/拒绝）、删除、列表查询。
    每个队列项结构：
        {
            "id": str,           # UUID
            "link": str,         # 115分享链接
            "title": str,        # 用户填写的标题/备注
            "status": str,       # pending/approved/rejected
            "created_at": str,   # ISO时间戳
            "reviewed_at": str,  # 审核时间（空=未审核）
            "review_note": str,  # 审核备注
            "file_count": int,   # 分享内文件数量（可选，添加时尝试获取）
            "source": str,       # 来源: manual/webhook/telegram
        }
    """

    _instance: Optional["ShareReviewQueue"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._items: list[dict] = []
        self._lock = threading.RLock()
        self._load()

    @classmethod
    def get_instance(cls) -> "ShareReviewQueue":
        """获取单例（双重检查锁，保证只初始化一次）"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ============ 持久化 ============

    def _load(self):
        """启动时从 JSON 文件加载队列"""
        if not QUEUE_FILE.exists():
            return
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            with self._lock:
                self._items = data if isinstance(data, list) else []
            logger.info(f"[share-review] 加载 {len(self._items)} 条审核队列记录")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[share-review] 加载队列文件失败: {e}")
            with self._lock:
                self._items = []

    def _save(self):
        """保存队列到 JSON 文件（原子写入：先写临时文件再重命名，防止写坏）"""
        try:
            # 持锁快照数据，文件 I/O 在锁外执行以缩短持锁时间
            with self._lock:
                content = json.dumps(self._items, ensure_ascii=False, indent=2)
            tmp_file = QUEUE_FILE.with_suffix(".tmp")
            tmp_file.write_text(content, encoding="utf-8")
            tmp_file.replace(QUEUE_FILE)
        except OSError as e:
            logger.warning(f"[share-review] 保存队列文件失败: {e}")

    # ============ 队列操作 ============

    def add_item(self, link: str, title: str = "", source: str = "manual") -> dict:
        """
        添加待审核分享链接
        link: 115 分享链接
        title: 用户填写的标题/备注
        source: 来源（manual/webhook/telegram，非法值回退为 manual）
        返回新建的队列项（失败返回空 dict）

        说明：file_count 为可选项，添加时会尝试调用 115 接口获取，
        获取失败（无有效账号 / 接口异常）则记为 0，不影响入队。
        如需自动通过，调用方可在此后判断 is_auto_approve_enabled() 并调用 approve_item()。
        """
        link = (link or "").strip()
        if not link:
            return {}
        if source not in _VALID_SOURCES:
            source = "manual"

        item = {
            "id": uuid.uuid4().hex,
            "link": link,
            "title": (title or "").strip(),
            "status": "pending",
            "created_at": _now_iso(),
            "reviewed_at": "",
            "review_note": "",
            "file_count": self._fetch_file_count(link),
            "source": source,
        }

        with self._lock:
            self._items.append(item)
        self._save()
        logger.info(f"[share-review] 新增待审核分享: {link}（来源 {source}）")
        return dict(item)

    def approve_item(self, item_id: str, note: str = "") -> bool:
        """审核通过，仅对 pending 状态生效"""
        return self._review(item_id, "approved", note)

    def reject_item(self, item_id: str, note: str = "") -> bool:
        """审核拒绝，仅对 pending 状态生效"""
        return self._review(item_id, "rejected", note)

    def _review(self, item_id: str, status: str, note: str = "") -> bool:
        """执行审核操作（通过/拒绝），成功返回 True；项不存在或非 pending 返回 False"""
        # 仅允许审核为 approved / rejected，pending 不可作为审核目标
        if status not in _VALID_STATUS or status == "pending":
            return False
        with self._lock:
            for item in self._items:
                if item.get("id") != item_id:
                    continue
                if item.get("status") != "pending":
                    logger.warning(
                        f"[share-review] 项 {item_id} 当前状态为 "
                        f"{item.get('status')}，无法审核为 {status}"
                    )
                    return False
                item["status"] = status
                item["reviewed_at"] = _now_iso()
                item["review_note"] = (note or "").strip()
                self._save()
                logger.info(f"[share-review] 项 {item_id} 已审核为 {status}")
                return True
        return False

    def delete_item(self, item_id: str) -> bool:
        """删除队列项，成功返回 True"""
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.get("id") != item_id]
            deleted = before - len(self._items)
        if deleted > 0:
            self._save()
            logger.info(f"[share-review] 删除项 {item_id}")
            return True
        return False

    def list_items(self, status: str = "") -> list[dict]:
        """
        列表查询
        status 为空时返回全部；否则按状态过滤（pending/approved/rejected）
        返回副本，按创建时间倒序（新的在前）
        """
        with self._lock:
            items = [dict(it) for it in self._items]
        if status:
            items = [it for it in items if it.get("status") == status]
        items.sort(key=lambda it: it.get("created_at", ""), reverse=True)
        return items

    def get_item(self, item_id: str) -> dict:
        """获取单条，不存在返回空 dict（返回副本）"""
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    return dict(item)
        return {}

    def get_pending_count(self) -> int:
        """待审核数量"""
        with self._lock:
            return sum(1 for it in self._items if it.get("status") == "pending")

    def clear_reviewed(self) -> int:
        """清除已审核项（approved/rejected），仅保留 pending，返回清除数量"""
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.get("status") == "pending"]
            cleared = before - len(self._items)
        if cleared > 0:
            self._save()
            logger.info(f"[share-review] 清除 {cleared} 条已审核记录")
        return cleared

    # ============ 自动通过配置 ============

    def is_auto_approve_enabled(self) -> bool:
        """从 settings.json 读取是否自动通过（默认关闭）"""
        cfg = read_setting(_SETTINGS_KEY) or {}
        return bool(cfg.get("auto_approve", False))

    def set_auto_approve(self, enabled: bool) -> bool:
        """设置自动通过开关，返回是否保存成功"""
        cfg = read_setting(_SETTINGS_KEY) or {}
        cfg["auto_approve"] = bool(enabled)
        ok = save_setting(_SETTINGS_KEY, cfg)
        logger.info(f"[share-review] 自动通过已设置为 {bool(enabled)}")
        return ok

    # ============ 辅助 ============

    @staticmethod
    def _fetch_file_count(link: str) -> int:
        """
        尝试获取分享内文件数量（可选，失败返回 0）。
        通过 Client115Service.share_snap 解析分享链接，惰性导入避免循环依赖。
        """
        try:
            from app.core.json_storage import get_first_valid_account
            from app.services.client_115 import Client115Service

            account = get_first_valid_account()
            if not account:
                return 0
            cookies = account.get("cookies", "")
            if not cookies:
                return 0
            result = Client115Service.share_snap(cookies, link)
            if not isinstance(result, dict) or result.get("error"):
                return 0
            # 兼容 115 返回结构：data.list 或直接为 list
            data = result.get("data", result)
            if isinstance(data, dict):
                items = data.get("list", [])
            elif isinstance(data, list):
                items = data
            else:
                items = []
            return len(items)
        except Exception as e:
            logger.debug(f"[share-review] 获取分享文件数量失败: {e}")
            return 0


# ============ 便捷接口 ============

def get_share_review_queue() -> ShareReviewQueue:
    """获取分享审核队列单例"""
    return ShareReviewQueue.get_instance()

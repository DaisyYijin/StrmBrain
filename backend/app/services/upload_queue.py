"""
持久化上传队列服务

将 Emby 刮削产生的元数据文件（nfo、图片、字幕等）异步上传到 115 网盘。
采用 JSON 持久化 + 后台线程消费的模式，支持失败重试。

核心设计：
- 任务持久化到 data/upload_queue.json，应用重启后自动恢复
- 后台 worker 线程从队列取任务执行上传
- 失败任务自动重试（最多 MAX_RETRIES 次）
- 上传去重：通过 local_path + dest_cid + filename 判断是否已有待上传任务
"""
import json
import time
import threading
import uuid
from pathlib import Path
from typing import Optional
from enum import Enum

from app.config import DATA_DIR
from app.core.logbuffer import get_logger
from app.services.client_115 import Client115Service

logger = get_logger("app.services.upload_queue")

# 持久化文件路径
QUEUE_FILE = DATA_DIR / "upload_queue.json"

# 最大重试次数
MAX_RETRIES = 3

# 上传间隔（秒），避免 115 API 限流
UPLOAD_INTERVAL = 0.5

# 任务状态
class TaskStatus(str, Enum):
    PENDING = "pending"        # 待上传
    UPLOADING = "uploading"    # 上传中
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"          # 失败（重试次数耗尽）
    SKIPPED = "skipped"        # 跳过（远程已存在）


class UploadTask:
    """单个上传任务"""
    __slots__ = (
        "id", "local_path", "filename", "parent_path",
        "dest_cid", "cookies", "account_id",
        "status", "retries", "error", "created_at", "updated_at", "completed_at",
        "file_size", "overwrite",
    )

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4().hex[:12])
        self.local_path = kwargs["local_path"]
        self.filename = kwargs["filename"]
        self.parent_path = kwargs.get("parent_path", "")
        self.dest_cid = kwargs["dest_cid"]
        self.cookies = kwargs.get("cookies", "")
        self.account_id = kwargs.get("account_id", 0)
        self.status = kwargs.get("status", TaskStatus.PENDING)
        self.retries = kwargs.get("retries", 0)
        self.error = kwargs.get("error", "")
        self.created_at = kwargs.get("created_at", time.time())
        self.updated_at = kwargs.get("updated_at", time.time())
        self.completed_at = kwargs.get("completed_at", 0)
        self.file_size = kwargs.get("file_size", 0)
        self.overwrite = kwargs.get("overwrite", False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "local_path": self.local_path,
            "filename": self.filename,
            "parent_path": self.parent_path,
            "dest_cid": self.dest_cid,
            "cookies": self.cookies,
            "account_id": self.account_id,
            "status": self.status.value if isinstance(self.status, TaskStatus) else self.status,
            "retries": self.retries,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "file_size": self.file_size,
            "overwrite": self.overwrite,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UploadTask":
        status_val = d.get("status", "pending")
        if isinstance(status_val, str) and status_val not in [s.value for s in TaskStatus]:
            status_val = "pending"
        return cls(
            id=d.get("id", uuid.uuid4().hex[:12]),
            local_path=d["local_path"],
            filename=d["filename"],
            parent_path=d.get("parent_path", ""),
            dest_cid=d["dest_cid"],
            cookies=d.get("cookies", ""),
            account_id=d.get("account_id", 0),
            status=TaskStatus(status_val) if isinstance(status_val, str) else status_val,
            retries=d.get("retries", 0),
            error=d.get("error", ""),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            completed_at=d.get("completed_at", 0),
            file_size=d.get("file_size", 0),
            overwrite=d.get("overwrite", False),
        )


class UploadQueue:
    """
    持久化上传队列

    线程安全设计：
    - _lock 保护内存队列操作
    - _file_lock 保护 JSON 持久化
    - worker 线程通过 _event 驱动，避免轮询
    """

    _instance: Optional["UploadQueue"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tasks: list[UploadTask] = []
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._event = threading.Event()
        self._running = False
        self._load()

    @classmethod
    def get_instance(cls) -> "UploadQueue":
        """单例获取"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ============ 持久化 ============

    def _load(self):
        """从 JSON 文件加载任务"""
        if not QUEUE_FILE.exists():
            return
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks_data = data.get("tasks", [])
            with self._lock:
                self._tasks = [UploadTask.from_dict(t) for t in tasks_data]
            # 将 uploading 状态的任务重置为 pending（应用重启后恢复）
            reset_count = 0
            for t in self._tasks:
                if t.status == TaskStatus.UPLOADING:
                    t.status = TaskStatus.PENDING
                    reset_count += 1
            if reset_count > 0:
                logger.info(f"[upload-queue] 恢复 {reset_count} 个中断的上传任务为待上传状态")
            logger.info(f"[upload-queue] 加载 {len(self._tasks)} 个上传任务")
        except Exception as e:
            logger.warning(f"[upload-queue] 加载队列文件失败: {e}")
            self._tasks = []

    def _save(self):
        """保存任务到 JSON 文件"""
        with self._file_lock:
            try:
                with self._lock:
                    tasks_data = [t.to_dict() for t in self._tasks]
                data = {
                    "version": 1,
                    "updated_at": time.time(),
                    "tasks": tasks_data,
                }
                tmp_file = QUEUE_FILE.with_suffix(".tmp")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp_file.replace(QUEUE_FILE)
            except Exception as e:
                logger.warning(f"[upload-queue] 保存队列文件失败: {e}")

    # ============ 队列操作 ============

    def add_task(self, task: UploadTask) -> bool:
        """
        添加上传任务，自动去重。
        如果已存在相同 local_path + dest_cid + filename 的 pending/uploading 任务，则跳过。
        """
        with self._lock:
            for existing in self._tasks:
                if (existing.local_path == task.local_path
                        and existing.dest_cid == task.dest_cid
                        and existing.filename == task.filename
                        and existing.status in (TaskStatus.PENDING, TaskStatus.UPLOADING)):
                    return False
            self._tasks.append(task)
        self._save()
        self._event.set()
        return True

    def add_tasks(self, tasks: list[UploadTask]) -> int:
        """批量添加任务，返回实际添加数量"""
        added = 0
        with self._lock:
            existing_keys = {
                (t.local_path, t.dest_cid, t.filename)
                for t in self._tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.UPLOADING)
            }
            for task in tasks:
                key = (task.local_path, task.dest_cid, task.filename)
                if key not in existing_keys:
                    self._tasks.append(task)
                    existing_keys.add(key)
                    added += 1
        if added > 0:
            self._save()
            self._event.set()
        return added

    def get_pending_task(self) -> Optional[UploadTask]:
        """获取下一个待上传任务"""
        with self._lock:
            for task in self._tasks:
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.UPLOADING
                    task.updated_at = time.time()
                    return task
        return None

    def complete_task(self, task_id: str, skipped: bool = False):
        """标记任务完成"""
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.status = TaskStatus.SKIPPED if skipped else TaskStatus.COMPLETED
                    t.completed_at = time.time()
                    t.updated_at = time.time()
                    t.error = ""
                    break
        self._save()
        self._cleanup_completed()

    def fail_task(self, task_id: str, error: str):
        """标记任务失败，若未超过重试次数则重置为 pending"""
        failed = False
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.retries += 1
                    t.error = error[:500]
                    t.updated_at = time.time()
                    if t.retries >= MAX_RETRIES:
                        t.status = TaskStatus.FAILED
                        t.completed_at = time.time()
                        failed = True
                        logger.warning(
                            f"[upload-queue] 任务失败（重试耗尽）: {t.filename} - {error}"
                        )
                    else:
                        t.status = TaskStatus.PENDING
                        logger.info(
                            f"[upload-queue] 任务失败，将重试 ({t.retries}/{MAX_RETRIES}): "
                            f"{t.filename} - {error}"
                        )
                    break
        self._save()
        if failed:
            self._cleanup_completed()

    def _cleanup_completed(self):
        """清理已完成/已跳过/已失败的任务（保留最近 100 条用于状态查询）"""
        with self._lock:
            finished = [t for t in self._tasks
                        if t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.FAILED)]
            if len(finished) > 100:
                # 按完成时间排序，保留最新的 100 条
                finished.sort(key=lambda t: t.completed_at or t.updated_at)
                to_remove = set(id(t) for t in finished[:-100])
                self._tasks = [t for t in self._tasks if id(t) not in to_remove]

    def get_status(self) -> dict:
        """获取队列状态统计"""
        with self._lock:
            pending = sum(1 for t in self._tasks if t.status == TaskStatus.PENDING)
            uploading = sum(1 for t in self._tasks if t.status == TaskStatus.UPLOADING)
            completed = sum(1 for t in self._tasks if t.status == TaskStatus.COMPLETED)
            skipped = sum(1 for t in self._tasks if t.status == TaskStatus.SKIPPED)
            failed = sum(1 for t in self._tasks if t.status == TaskStatus.FAILED)
            total = len(self._tasks)
        return {
            "total": total,
            "pending": pending,
            "uploading": uploading,
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "running": self._running,
        }

    def get_recent_tasks(self, limit: int = 20) -> list[dict]:
        """获取最近的任务列表（用于状态展示）"""
        with self._lock:
            sorted_tasks = sorted(
                self._tasks,
                key=lambda t: t.updated_at,
                reverse=True
            )
            return [
                {
                    "id": t.id,
                    "filename": t.filename,
                    "parent_path": t.parent_path,
                    "status": t.status.value if isinstance(t.status, TaskStatus) else str(t.status),
                    "retries": t.retries,
                    "error": t.error,
                    "file_size": t.file_size,
                    "created_at": t.created_at,
                    "completed_at": t.completed_at,
                }
                for t in sorted_tasks[:limit]
            ]

    def clear_finished(self) -> int:
        """清除所有已完成/已跳过/已失败的任务"""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [
                t for t in self._tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.UPLOADING)
            ]
            cleared = before - len(self._tasks)
        if cleared > 0:
            self._save()
        return cleared

    def retry_failed(self) -> int:
        """将所有失败任务重置为待上传"""
        count = 0
        with self._lock:
            for t in self._tasks:
                if t.status == TaskStatus.FAILED:
                    t.status = TaskStatus.PENDING
                    t.retries = 0
                    t.error = ""
                    t.updated_at = time.time()
                    count += 1
        if count > 0:
            self._save()
            self._event.set()
        return count

    # ============ Worker ============

    def start(self):
        """启动后台 worker 线程"""
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="upload-queue-worker")
        self._worker.start()
        logger.info("[upload-queue] 上传队列 worker 已启动")

    def stop(self):
        """停止 worker 线程"""
        self._running = False
        self._event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        logger.info("[upload-queue] 上传队列 worker 已停止")

    def _worker_loop(self):
        """worker 主循环：从队列取任务执行上传"""
        logger.info("[upload-queue] worker 循环开始")
        while self._running:
            task = self.get_pending_task()
            if task is None:
                # 没有任务，等待新任务到来
                self._event.wait(timeout=10)
                self._event.clear()
                continue

            self._execute_task(task)

            # 上传间隔，避免 115 API 限流
            if self._running:
                time.sleep(UPLOAD_INTERVAL)

        logger.info("[upload-queue] worker 循环结束")

    def _execute_task(self, task: UploadTask):
        """执行单个上传任务"""
        try:
            # 检查本地文件是否存在
            local_file = Path(task.local_path)
            if not local_file.exists():
                self.fail_task(task.id, f"本地文件不存在: {task.local_path}")
                return

            # 覆盖模式：先删除旧文件
            if task.overwrite:
                try:
                    # 通过文件名查找远程已有文件并删除
                    remote_files = Client115Service.list_all_files_with_meta(
                        task.cookies, task.dest_cid, set(), min_size=0, recursive=False
                    )
                    for f in remote_files:
                        if f.get("name") == task.filename:
                            file_id = f.get("file_id", "")
                            if file_id:
                                Client115Service.delete_files(task.cookies, [file_id])
                                logger.info(f"[upload-queue] 已删除旧文件: {task.filename}")
                            break
                except Exception as e:
                    logger.warning(f"[upload-queue] 删除旧文件失败（继续上传）: {task.filename} - {e}")

            # 执行上传
            ok = Client115Service.upload_file(
                task.cookies,
                str(local_file),
                task.filename,
                task.dest_cid,
            )
            if ok:
                self.complete_task(task.id)
                logger.info(f"[upload-queue] 上传成功: {task.filename}")
            else:
                self.fail_task(task.id, "115 upload_file 返回失败")

        except Exception as e:
            self.fail_task(task.id, str(e))


# ============ 便捷接口 ============

def init_upload_queue():
    """初始化并启动上传队列（应用启动时调用）"""
    queue = UploadQueue.get_instance()
    queue.start()


def shutdown_upload_queue():
    """停止上传队列（应用关闭时调用）"""
    queue = UploadQueue.get_instance()
    queue.stop()


def get_upload_queue() -> UploadQueue:
    """获取上传队列实例"""
    return UploadQueue.get_instance()

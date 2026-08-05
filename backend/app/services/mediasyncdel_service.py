"""
媒体库同步删除服务
==================

Emby Webhook 删除事件（deep.delete / item.removed / item.delete 等）级联删除
115 网盘对应文件。

设计（仅借鉴 p115strmhelper 的 mediasyncdel + webhook_queue 思路，
不复制其 MoviePilot 专属代码）：

- webhook 线程只负责将删除事件快照深拷贝后放入内存队列（容量 500），
  不阻塞、立即返回 200；
- 后台 daemon worker 线程从队列取事件，解析被删媒体路径列表，
  调用 115 API 在网盘对应目录下查找文件并移入回收站；
- 防误删：仅当路径非空、且能在网盘匹配到文件时才删除；
- 删除历史 append 到 DATA_DIR/mediasyncdel_history.json。
"""
import copy
import json
import queue
import re
import threading
import time
from typing import Optional

from app.config import DATA_DIR
from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger
from app.services.client_115 import (
    Client115Service,
    get_account_lock,
    _check_circuit_breaker,
)
from app.services.sync_service import SyncService

logger = get_logger("app.services.mediasyncdel_service")

# 删除事件队列容量（超出后丢弃事件，避免 webhook 线程阻塞）
_QUEUE_MAXSIZE = 500

# list_all_files_with_meta 的扩展名过滤集合：
# 覆盖视频 + 元数据（nfo/字幕/图片），保证可能被删除的媒体文件都能被列出匹配
_MEDIA_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts",
    ".rmvb", ".iso",
    ".nfo", ".srt", ".ass", ".ssa", ".sub", ".idx",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
}

# 删除历史文件（append 数组）
_HISTORY_FILE = DATA_DIR / "mediasyncdel_history.json"
_HISTORY_MAX = 1000  # 历史最多保留条数，防止无限增长


class MediasyncDelService:
    """Emby 删除事件 → 115 网盘级联删除服务。"""

    _SENTINEL = object()  # 停止哨兵

    def __init__(self) -> None:
        self._queue: Optional[queue.Queue] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()          # 队列/线程状态锁
        self._config_lock = threading.Lock()   # 启用开关配置读写锁
        self._history_lock = threading.Lock()  # 历史文件写锁

    # ===== 生命周期 =====

    def start(self) -> bool:
        """启动后台 worker（幂等）。"""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return True
            if self._queue is None:
                self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
            self._worker_thread = threading.Thread(
                target=self._worker, name="mediasyncdel-worker", daemon=True
            )
            self._worker_thread.start()
        logger.info("[mediasyncdel] 删除级联 worker 已启动")
        return True

    def stop(self) -> None:
        """停止 worker：发送哨兵并 join（幂等）。"""
        with self._lock:
            q = self._queue
            th = self._worker_thread
            if q is None or th is None:
                return
            if not th.is_alive():
                self._queue = None
                self._worker_thread = None
                return
        try:
            # 队列满时等待空位（worker 持续消费），否则哨兵无法入队导致 join 超时
            q.put(self._SENTINEL, timeout=5)
            th.join(timeout=15)
            if th.is_alive():
                logger.warning("[mediasyncdel] worker 未在 15 秒内退出")
        except queue.Full:
            logger.warning("[mediasyncdel] 停止 worker：队列已满，无法发送哨兵，等待进程退出时回收")
        except Exception as e:
            logger.warning(f"[mediasyncdel] 停止 worker 异常: {e}")
        finally:
            with self._lock:
                self._worker_thread = None
                self._queue = None

    def _ensure_started(self) -> None:
        """懒启动（首次入队时自动启动 worker）。"""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            if self._queue is None:
                self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
            self._worker_thread = threading.Thread(
                target=self._worker, name="mediasyncdel-worker", daemon=True
            )
            self._worker_thread.start()

    # ===== 队列 =====

    def enqueue_event(self, event_snapshot: dict) -> bool:
        """
        将删除事件快照深拷贝后加入内存队列，返回是否入队成功。
        不阻塞 webhook 线程；队列满（500）时丢弃事件并返回 False。
        """
        try:
            snapshot = copy.deepcopy(event_snapshot)
        except Exception as e:
            logger.warning(f"[mediasyncdel] 事件快照深拷贝失败: {e}")
            return False

        self._ensure_started()
        with self._lock:
            q = self._queue
        if q is None:
            logger.warning("[mediasyncdel] 队列未就绪，跳过入队")
            return False
        try:
            q.put_nowait(snapshot)
            return True
        except queue.Full:
            logger.warning("[mediasyncdel] 删除事件队列已满（500），丢弃事件")
            return False
        except Exception as e:
            logger.warning(f"[mediasyncdel] 入队失败: {e}")
            return False

    # ===== 后台 worker =====

    def _worker(self) -> None:
        """从队列取事件并处理；队列空时等待 1s。"""
        q = self._queue
        if q is None:
            return
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                # 队列空时等效 sleep 1s 后继续轮询
                continue
            except Exception as e:
                logger.warning(f"[mediasyncdel] worker 取任务异常: {e}")
                continue
            if item is self._SENTINEL:
                q.task_done()
                break
            # 级联删除开关：禁用时事件出队但不处理（防止队列堆积）
            if not self.get_enabled():
                logger.info("[mediasyncdel] 级联删除已禁用，跳过删除事件")
                q.task_done()
                continue
            try:
                self._process_delete_event(item)
            except Exception as e:
                logger.warning(f"[mediasyncdel] 处理删除事件失败: {e}", exc_info=True)
            finally:
                q.task_done()

    # ===== 配置开关 =====

    def get_enabled(self) -> bool:
        """读取级联删除启用开关（settings.json 的 mediasyncdel.enabled，默认 True）。"""
        with self._config_lock:
            try:
                cfg = read_setting("mediasyncdel") or {}
                return bool(cfg.get("enabled", True))
            except Exception as e:
                logger.warning(f"[mediasyncdel] 读取级联删除开关异常: {e}")
                return True

    def set_enabled(self, enabled: bool) -> bool:
        """写入级联删除启用开关（settings.json 的 mediasyncdel.enabled）。"""
        enabled = bool(enabled)
        with self._config_lock:
            try:
                cfg = read_setting("mediasyncdel") or {}
                cfg["enabled"] = enabled
                ok = save_setting("mediasyncdel", cfg)
                if ok:
                    logger.info(f"[mediasyncdel] 级联删除已{'启用' if enabled else '禁用'}")
                else:
                    logger.warning("[mediasyncdel] 保存级联删除开关失败")
                return ok
            except Exception as e:
                logger.warning(f"[mediasyncdel] 设置级联删除开关异常: {e}")
                return False

    # ===== 队列状态 =====

    def get_queue_size(self) -> int:
        """获取删除事件队列当前待处理数量。"""
        with self._lock:
            q = self._queue
        if q is None:
            return 0
        try:
            return q.qsize()
        except Exception:
            return 0

    # ===== 事件解析 =====

    def _process_delete_event(self, event: dict) -> None:
        """解析删除事件，提取被删媒体 Path 列表并级联删除。"""
        if not isinstance(event, dict):
            return
        paths = self._extract_paths(event)
        if not paths:
            logger.info(
                f"[mediasyncdel] 删除事件未提取到有效路径，跳过: "
                f"{event.get('event') or event.get('name') or '未知'}"
            )
            return
        logger.info(f"[mediasyncdel] 删除事件提取到 {len(paths)} 个路径: {paths}")
        self._delete_from_pan(paths)

    def _extract_paths(self, event: dict) -> list:
        """
        从事件快照中提取被删媒体路径列表（去重）。
        兼容 webhook 侧已解析的 paths 列表，以及原始 payload 中的
        Path / Metadata.Path / Item.Path / ItemIds / Description。
        """
        paths: list = []

        def _add(p) -> None:
            p = (p or "").strip()
            if p and p not in paths:
                paths.append(p)

        # 1) 快照中已解析好的路径列表
        raw = event.get("paths")
        if isinstance(raw, list):
            for p in raw:
                _add(p)

        # 2) 主路径字段
        _add(event.get("path"))

        # 3) 原始 payload 兜底解析
        payload = event.get("payload")
        if isinstance(payload, dict):
            _add(payload.get("Path"))
            metadata = payload.get("Metadata") or payload.get("metadata") or {}
            if isinstance(metadata, dict):
                _add(metadata.get("Path"))
            item = payload.get("Item") or payload.get("item") or {}
            if isinstance(item, dict):
                _add(item.get("Path"))
            # ItemIds 列表（部分插件以 ItemIds 列表给出路径）
            item_ids = payload.get("ItemIds")
            if isinstance(item_ids, list):
                for p in item_ids:
                    _add(p)
            # deep.delete Description：按行/逗号拆出多条路径
            description = payload.get("Description") or ""
            if isinstance(description, str) and description.strip():
                for seg in re.split(r"[\r\n,]+", description):
                    _add(self._clean_path_line(seg))

        return paths

    @staticmethod
    def _clean_path_line(line: str) -> str:
        """
        清洗 Description 中拆分出的一段，提取路径部分：
        - 去掉 "Item Path:" / "Path:" 之类前缀；
        - 仅保留看起来像路径的段（防误解析普通说明文字）。
        """
        line = (line or "").strip()
        if not line:
            return ""
        # 去掉 "Item Path:" 前缀（注意 Windows 路径含冒号，需用指定分隔符拆分）
        if "Item Path:" in line:
            line = line.split("Item Path:", 1)[1].strip()
        elif line.startswith("Path:") and not line[5:6].isalpha():
            line = line[5:].strip()

        if not line:
            return ""
        if line.startswith(("http://", "https://")):
            return ""
        # 路径特征判断（参考 mediasyncdel 的 parse_item_paths_from_description）
        looks_like_path = (
            line.startswith(("/", "\\", "./", "../"))
            or (len(line) > 2 and line[1] == ":" and line[2] in "/\\")
            or ("/" in line and "://" not in line)
            or ("\\" in line)
        )
        return line if looks_like_path else ""

    # ===== 网盘删除 =====

    def _delete_from_pan(self, paths: list) -> None:
        """
        对每个路径，从同步计划读取 source_cid 与账号 cookies，
        在 115 网盘递归列出的文件中匹配并移入回收站。
        防误删：仅当路径非空、且匹配到网盘文件时才删除。
        """
        # 读取同步计划配置（source_cid 与 account_id）
        schedule = SyncService.load_schedule()
        if not schedule:
            logger.warning("[mediasyncdel] 未找到同步计划配置，跳过删除")
            return
        source_cid = str(schedule.get("source_cid") or "").strip()
        account_id = 0
        try:
            account_id = int(schedule.get("account_id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        if not source_cid:
            logger.warning("[mediasyncdel] 同步计划未配置 source_cid，跳过删除")
            return

        # 获取账号 cookies（优先同步计划指定账号，缺失/失效时回退第一个有效账号）
        cookies = self._get_account_cookies(account_id)
        if not cookies:
            logger.warning("[mediasyncdel] 未获取到有效账号 cookies，跳过删除")
            return

        logger.info(
            f"[mediasyncdel] 开始级联删除 {len(paths)} 个路径 "
            f"(source_cid={source_cid}, account_id={account_id})"
        )

        # 同账号任务互斥：与同步/整理等 115 任务串行，避免并发触发风控
        account_lock = get_account_lock(account_id)
        with account_lock:
            # 熔断检查 + 递归列出 source_cid 下所有媒体文件
            _check_circuit_breaker()
            try:
                files = Client115Service.list_all_files_with_meta(
                    cookies, source_cid, exts=_MEDIA_EXTS, min_size=0, recursive=True
                )
            except Exception as e:
                logger.warning(f"[mediasyncdel] 列出网盘文件失败: {e}")
                return
            if not files:
                logger.info(f"[mediasyncdel] source_cid={source_cid} 下未列出文件，跳过删除")
                return

            # 建立索引：完整相对路径 → 文件；文件名（末段）→ 文件列表
            full_map: dict = {}
            name_map: dict = {}
            for f in files:
                name = f.get("name", "")
                parent = (f.get("parent_path") or "").strip("/")
                full = f"{parent}/{name}" if parent else name
                full_map[full] = f
                if name:
                    name_map.setdefault(name, []).append(f)

            # 逐路径删除
            for path in paths:
                path = (path or "").strip()
                if not path:
                    continue
                matched = self._match_file(path, full_map, name_map)
                if not matched:
                    logger.warning(f"[mediasyncdel] 网盘中未匹配到 {path}，跳过（防误删）")
                    self._save_history({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "path": path,
                        "result": "not_found",
                        "message": "网盘中未匹配到该路径，已跳过",
                    })
                    continue

                # 删除前熔断检查（list_all_files_with_meta 内部已查，双保险）
                try:
                    _check_circuit_breaker()
                except Exception as e:
                    logger.warning(f"[mediasyncdel] {e}，跳过删除 {path}")
                    self._save_history({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "path": path,
                        "result": "failed",
                        "message": str(e),
                    })
                    continue

                try:
                    resp = Client115Service.delete_files(cookies, [matched["file_id"]])
                except Exception as e:
                    logger.warning(f"[mediasyncdel] 删除 {path} 异常: {e}")
                    self._save_history({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "path": path,
                        "result": "failed",
                        "message": str(e)[:500],
                    })
                    continue

                ok = self._is_delete_ok(resp)
                if ok:
                    logger.info(
                        f"[mediasyncdel] 已删除网盘文件: {path} "
                        f"(file_id={matched['file_id']})，已移入回收站"
                    )
                else:
                    logger.warning(f"[mediasyncdel] 删除失败: {path} -> {resp}")
                self._save_history({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "path": path,
                    "result": "success" if ok else "failed",
                    "file_id": matched["file_id"],
                    "message": "" if ok else str(resp)[:500],
                })

    @staticmethod
    def _match_file(path: str, full_map: dict, name_map: dict) -> Optional[dict]:
        """
        路径 → 网盘文件匹配：
        1. 完整相对路径（parent_path/name）精确匹配；
        2. 删除路径以网盘相对路径结尾（Emby 根路径前缀不一致时兜底）；
        3. 兼容 .strm 后缀（本地 STRM 路径对应网盘原文件）；
        4. 文件名的末尾路径段匹配（多个同名时选父路径最接近的）。
        返回匹配的文件信息 dict 或 None。
        """
        path_norm = path.replace("\\", "/").strip("/")
        if not path_norm:
            return None

        # 1) 完整相对路径匹配
        hit = full_map.get(path_norm)
        if hit:
            return hit

        # 2) 删除路径以网盘相对路径结尾（根目录前缀不同但子路径一致时命中）
        if "/" in path_norm:
            for full, f in full_map.items():
                if path_norm.endswith("/" + full):
                    return f

        # 3) 兼容 .strm 后缀（先剥掉 .strm 再匹配）
        is_strm = path_norm.endswith(".strm")
        if is_strm:
            hit = full_map.get(path_norm[:-5])
            if hit:
                return hit

        # 4) 文件名的末尾路径段匹配（.strm 时先去除后缀再匹配）
        last_seg = path_norm.rsplit("/", 1)[-1]
        candidates = name_map.get(last_seg) or []
        if not candidates and is_strm:
            candidates = name_map.get(last_seg[:-5]) or []
        if not candidates:
            return None
        # 去重（同一 file_id 只保留一个）
        uniq: list = []
        seen: set = set()
        for c in candidates:
            fid = c.get("file_id")
            if fid in seen:
                continue
            seen.add(fid)
            uniq.append(c)
        if len(uniq) == 1:
            return uniq[0]
        # 多个同名文件：选择父路径与删除路径父路径公共前缀最长的
        parent = path_norm.rsplit("/", 1)[0] if "/" in path_norm else ""
        parent_parts = [p for p in parent.split("/") if p]

        def _score(c: dict) -> int:
            c_parent = (c.get("parent_path") or "").strip("/")
            c_parts = [p for p in c_parent.split("/") if p]
            common = 0
            for a, b in zip(parent_parts, c_parts):
                if a == b:
                    common += 1
                else:
                    break
            return common

        return max(uniq, key=_score)

    @staticmethod
    def _get_account_cookies(account_id: int) -> str:
        """获取账号 cookies：优先同步计划指定账号，缺失/失效时回退第一个有效账号。"""
        from app.core.json_storage import find_account, get_first_valid_account
        account = None
        if account_id:
            account = find_account(account_id)
        if not account or account.get("status") != 1:
            account = get_first_valid_account()
        if not account:
            return ""
        return account.get("cookies") or ""

    @staticmethod
    def _is_delete_ok(resp) -> bool:
        """判断 115 fs_delete 响应是否成功（成功通常返回 state=True）。"""
        if resp is None:
            return False
        if isinstance(resp, dict):
            if resp.get("error") and resp.get("state") is not True:
                return False
            if "state" in resp:
                return resp.get("state") is not False
            return True
        return bool(resp)

    # ===== 历史记录 =====

    def _save_history(self, record: dict) -> None:
        """追加删除历史到 DATA_DIR/mediasyncdel_history.json（保留最近 1000 条）。"""
        try:
            with self._history_lock:
                history: list = []
                if _HISTORY_FILE.exists():
                    try:
                        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            history = data
                    except Exception:
                        history = []
                history.append(record)
                if len(history) > _HISTORY_MAX:
                    history = history[-_HISTORY_MAX:]
                _HISTORY_FILE.write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.warning(f"[mediasyncdel] 保存删除历史失败: {e}")

    def get_history(self, limit: int = 50) -> list:
        """读取删除历史，返回最近 limit 条（倒序，最新在前）。文件不存在时返回空列表。"""
        try:
            if not _HISTORY_FILE.exists():
                return []
            with self._history_lock:
                data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            try:
                limit = max(int(limit), 0)
            except (TypeError, ValueError):
                limit = 50
            if limit <= 0:
                return []
            return list(reversed(data[-limit:]))
        except Exception as e:
            logger.warning(f"[mediasyncdel] 读取删除历史失败: {e}")
            return []

    def get_history_count(self) -> int:
        """获取删除历史总条数。"""
        try:
            if not _HISTORY_FILE.exists():
                return 0
            with self._history_lock:
                data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            return len(data) if isinstance(data, list) else 0
        except Exception as e:
            logger.warning(f"[mediasyncdel] 读取删除历史条数失败: {e}")
            return 0

    def clear_history(self) -> bool:
        """清空删除历史（写空数组）。"""
        try:
            with self._history_lock:
                _HISTORY_FILE.write_text("[]", encoding="utf-8")
            logger.info("[mediasyncdel] 删除历史已清空")
            return True
        except Exception as e:
            logger.warning(f"[mediasyncdel] 清空删除历史失败: {e}")
            return False


# ===== 全局单例 =====

global_mediasync_del_service: Optional[MediasyncDelService] = None


def get_mediasync_del_service() -> MediasyncDelService:
    """获取全局删除级联服务单例。"""
    global global_mediasync_del_service
    if global_mediasync_del_service is None:
        global_mediasync_del_service = MediasyncDelService()
    return global_mediasync_del_service

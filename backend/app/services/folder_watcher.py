"""
文件系统实时监控服务 - 基于 watchdog 实现递归目录监控。
参考 qmediasync helpers/fsnotify.go 设计。

提供：
  - FolderWatcher：单个目录的递归监控（事件去重 / 扩展名过滤 / 忽略目录）
  - FolderWatcherManager：多目录监控的全局管理器（单例 global_watcher_manager）
"""
import os
import threading
import time
from typing import Callable, Dict, List, Optional

from app.core.logbuffer import get_logger

logger = get_logger("app.services.folder_watcher")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WATCHDOG_AVAILABLE = False
    Observer = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore
    logger.warning("watchdog 库未安装，文件监控功能不可用，请执行 `pip install watchdog`")


# 默认监控的视频扩展名
DEFAULT_VIDEO_EXTENSIONS: List[str] = [
    ".mp4", ".mkv", ".avi", ".rmvb", ".ts", ".flv",
    ".wmv", ".mov", ".m4v", ".iso",
]

# 默认忽略的目录名
DEFAULT_IGNORE_DIRS: List[str] = [
    ".git", ".vscode", "node_modules", "__pycache__",
    ".tmp", "@eaDir", "#recycle",
]

# 事件缓存过期时间（毫秒）：超过 1 分钟的事件缓存被清理
_CACHE_TTL_MS = 60_000


class _EventHandler(FileSystemEventHandler):
    """watchdog 事件处理器，将原始事件转发给 FolderWatcher 做去重与分发。"""

    def __init__(self, watcher: "FolderWatcher"):
        super().__init__()
        self._watcher = watcher

    def on_created(self, event):
        if event.is_directory:
            return
        self._watcher._dispatch("created", event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._watcher._dispatch("modified", event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        self._watcher._dispatch("deleted", event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        self._watcher._dispatch("moved", event.src_path, dest)


class FolderWatcher:
    """单个目录的递归文件监控器。

    Args:
        watch_path: 要监控的目录路径
        extensions: 允许的文件扩展名列表（含点，如 ".mp4"），None 用默认视频扩展名
        ignore_dirs: 忽略的目录名列表，None 用默认忽略列表
        debounce_ms: 事件去重时间窗（毫秒），相同路径的同类事件在此窗口内被忽略
    """

    def __init__(
        self,
        watch_path: str,
        extensions: Optional[List[str]] = None,
        ignore_dirs: Optional[List[str]] = None,
        debounce_ms: int = 100,
    ):
        self.watch_path = os.path.abspath(str(watch_path))

        # 规范化扩展名：小写、带前导点
        exts = list(extensions) if extensions is not None else list(DEFAULT_VIDEO_EXTENSIONS)
        self.extensions = [
            (e if e.startswith(".") else "." + e).lower() for e in exts
        ]

        self.ignore_dirs = (
            list(ignore_dirs) if ignore_dirs is not None else list(DEFAULT_IGNORE_DIRS)
        )
        self.debounce_ms = max(0, int(debounce_ms))

        # 事件回调：event_type -> list[callback]
        self._callbacks: Dict[str, List[Callable]] = {
            "created": [],
            "modified": [],
            "deleted": [],
            "moved": [],
        }
        # 去重缓存：(event_type, path) -> 最近事件时间（毫秒，monotonic）
        self._event_cache: Dict[tuple, float] = {}
        self._lock = threading.Lock()

        self._observer = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._cleanup_interval = 5  # 秒，定期清理周期

    # ------------------------------------------------------------------ 回调注册
    def on_created(self, callback: Callable) -> None:
        """注册文件创建回调。"""
        with self._lock:
            self._callbacks["created"].append(callback)

    def on_modified(self, callback: Callable) -> None:
        """注册文件修改回调。"""
        with self._lock:
            self._callbacks["modified"].append(callback)

    def on_deleted(self, callback: Callable) -> None:
        """注册文件删除回调。"""
        with self._lock:
            self._callbacks["deleted"].append(callback)

    def on_moved(self, callback: Callable) -> None:
        """注册文件移动/重命名回调。"""
        with self._lock:
            self._callbacks["moved"].append(callback)

    # ------------------------------------------------------------------ 过滤与去重
    def _path_in_ignore_dir(self, path: str) -> bool:
        """路径任一层目录名命中忽略列表则返回 True。"""
        try:
            norm = path.replace("\\", "/")
            for part in norm.split("/"):
                if part and part in self.ignore_dirs:
                    return True
        except Exception:
            return False
        return False

    def _should_ignore(self, path: str) -> bool:
        """判断路径是否应被忽略（忽略目录或扩展名不匹配）。"""
        if not path:
            return True
        if self._path_in_ignore_dir(path):
            return True
        ext = os.path.splitext(path)[1].lower()
        if ext not in self.extensions:
            return True
        return False

    def _is_duplicated(self, key: tuple) -> bool:
        """事件去重：相同 (event_type, path) 在 debounce 窗口内视为重复。

        Returns:
            True 表示为重复事件（应忽略），False 表示首次（应分发）
        """
        now_ms = time.monotonic() * 1000.0
        with self._lock:
            last = self._event_cache.get(key)
            if last is not None and (now_ms - last) < self.debounce_ms:
                # 刷新时间戳，实现连续重复事件的持续去重
                self._event_cache[key] = now_ms
                return True
            self._event_cache[key] = now_ms
            return False

    def _cleanup_cache(self) -> None:
        """清理超过 TTL（1 分钟）的过期事件缓存。"""
        now_ms = time.monotonic() * 1000.0
        with self._lock:
            expired = [k for k, t in self._event_cache.items()
                       if (now_ms - t) > _CACHE_TTL_MS]
            for k in expired:
                self._event_cache.pop(k, None)
        if expired:
            logger.debug(f"清理过期事件缓存 {len(expired)} 条: {self.watch_path}")

    # ------------------------------------------------------------------ 事件分发
    def _dispatch(self, event_type: str, src_path: str,
                  dest_path: Optional[str] = None) -> None:
        """过滤、去重后向已注册回调分发事件。"""
        # moved 事件：src 与 dest 任一为关注文件即分发
        if event_type == "moved" and dest_path:
            src_ignored = self._should_ignore(src_path)
            dest_ignored = self._should_ignore(dest_path)
            if src_ignored and dest_ignored:
                return
            key_path = dest_path if src_ignored else src_path
        else:
            if self._should_ignore(src_path):
                return
            key_path = src_path

        if self._is_duplicated((event_type, key_path)):
            return

        payload = {
            "event_type": event_type,
            "src_path": src_path,
            "dest_path": dest_path,
            "is_directory": False,
            "timestamp": time.time(),
            "watch_path": self.watch_path,
        }

        with self._lock:
            callbacks = list(self._callbacks.get(event_type, []))
        for cb in callbacks:
            try:
                cb(payload)
            except Exception as e:
                logger.warning(
                    f"文件监控回调执行异常 ({event_type} {src_path}): {e}",
                    exc_info=True,
                )

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """开始监控（在后台线程中运行 watchdog Observer）。

        Returns:
            True 启动成功，False 启动失败（依赖缺失或路径无效）
        """
        if not _WATCHDOG_AVAILABLE:
            logger.warning("watchdog 未安装，无法启动文件监控")
            return False
        if self._running:
            logger.warning(f"监控已在运行: {self.watch_path}")
            return True
        if not os.path.isdir(self.watch_path):
            logger.warning(f"监控目录不存在: {self.watch_path}")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"FolderWatcher-{os.path.basename(self.watch_path)}",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self) -> None:
        """后台线程主循环：启动 Observer 并定期清理缓存。"""
        observer = None
        try:
            observer = Observer()
            handler = _EventHandler(self)
            observer.schedule(handler, self.watch_path, recursive=True)
            observer.start()
            self._observer = observer
            logger.info(
                f"开始监控目录: {self.watch_path} "
                f"(扩展名: {self.extensions}, 忽略目录: {self.ignore_dirs})"
            )
            while self._running:
                time.sleep(self._cleanup_interval)
                self._cleanup_cache()
        except Exception as e:
            logger.warning(f"文件监控线程异常 ({self.watch_path}): {e}", exc_info=True)
        finally:
            if observer is not None:
                try:
                    observer.stop()
                    observer.join(timeout=5)
                except Exception:
                    pass
            self._observer = None
            self._running = False
            logger.info(f"监控线程已退出: {self.watch_path}")

    def stop(self) -> None:
        """停止监控。"""
        if not self._running and self._observer is None and self._thread is None:
            return
        self._running = False
        # 通知 Observer 停止（_run 的 finally 会完成 join）
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        with self._lock:
            self._event_cache.clear()
        logger.info(f"已停止监控: {self.watch_path}")

    @property
    def is_running(self) -> bool:
        return self._running


class FolderWatcherManager:
    """多目录文件监控的全局管理器。"""

    def __init__(self):
        self._watchers: Dict[str, FolderWatcher] = {}
        self._lock = threading.Lock()

    def start_watching(
        self,
        path: str,
        extensions: Optional[List[str]] = None,
        callback: Optional[Callable] = None,
    ) -> Optional[FolderWatcher]:
        """开始监控指定路径。

        Args:
            path: 监控目录路径
            extensions: 允许的扩展名，None 用默认视频扩展名
            callback: 通用回调，会被注册到 created/modified/deleted/moved 四类事件

        Returns:
            成功返回 FolderWatcher 实例，失败返回 None
        """
        abs_path = os.path.abspath(str(path))
        with self._lock:
            existing = self._watchers.get(abs_path)
            if existing is not None:
                logger.warning(f"路径已在监控中，复用现有 watcher: {abs_path}")
                if callback is not None:
                    self._register_all(existing, callback)
                return existing

        watcher = FolderWatcher(abs_path, extensions=extensions)
        if callback is not None:
            self._register_all(watcher, callback)
        if not watcher.start():
            logger.warning(f"启动监控失败: {abs_path}")
            return None

        with self._lock:
            self._watchers[abs_path] = watcher
        logger.info(f"已加入监控管理: {abs_path}")
        return watcher

    @staticmethod
    def _register_all(watcher: FolderWatcher, callback: Callable) -> None:
        """将同一个回调注册到四类事件。"""
        watcher.on_created(callback)
        watcher.on_modified(callback)
        watcher.on_deleted(callback)
        watcher.on_moved(callback)

    def stop_watching(self, path: str) -> bool:
        """停止监控指定路径。

        Returns:
            True 表示存在并已停止，False 表示该路径未被监控
        """
        abs_path = os.path.abspath(str(path))
        with self._lock:
            watcher = self._watchers.pop(abs_path, None)
        if watcher is None:
            logger.warning(f"路径未被监控，无需停止: {abs_path}")
            return False
        watcher.stop()
        return True

    def stop_all(self) -> None:
        """停止所有监控。"""
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            try:
                watcher.stop()
            except Exception as e:
                logger.warning(f"停止监控异常 ({watcher.watch_path}): {e}")
        logger.info("已停止所有文件监控")

    def get_watched_paths(self) -> List[str]:
        """获取所有正在监控的路径。"""
        with self._lock:
            return list(self._watchers.keys())


# 全局单例
global_watcher_manager = FolderWatcherManager()

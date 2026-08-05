"""
日志缓冲 - 内存环形缓冲，供实时日志查看

每条日志额外标注 source 来源分类，便于前端区分。
uvicorn.access（HTTP 访问日志）不进入缓冲区，避免日志面板自身轮询
产生的请求日志反复刷屏；这些日志仍在终端控制台可见。

从 2026-08-05 起：应用日志同时输出到 stdout（容器 docker logs 可见），
uvicorn 日志排除在 stdout 之外（避免与 uvicorn 自带 handler 重复打印）。
"""
import logging
import sys
from collections import deque
from datetime import datetime


# logger.name → 来源标签的映射规则
# 按优先级匹配，越具体的规则越靠前
_SOURCE_RULES = [
    # uvicorn（access 不进缓冲，error 保留启动/关闭信息）
    ("uvicorn.error", "system"),
    ("uvicorn", "system"),
    # 第三方库日志
    ("apscheduler", "scheduler"),
    # 应用各服务模块
    ("app.services.sync_service", "sync"),
    ("app.services.organize_service", "organize"),
    ("app.services.client_115", "115"),
    ("app.services.tmdb_service", "tmdb"),
    ("app.services.cover_gen", "cover"),
    ("app.services.media_probe", "media"),
    ("app.services.emby", "emby"),
    ("app.services.emby_proxy", "proxy"),
    ("app.core.scheduler", "scheduler"),
    ("app", "system"),
    ("strmhub", "system"),
]

# 前端展示用的来源中文名
SOURCE_LABELS = {
    "system": "系统",
    "sync": "同步",
    "organize": "整理",
    "115": "115网盘",
    "tmdb": "TMDB",
    "emby": "Emby",
    "proxy": "反代",
    "cover": "封面生成",
    "media": "媒体探测",
    "scheduler": "定时任务",
    "app": "应用",
}

# 不进入缓冲区的 logger（纯噪音，终端控制台仍可见）
# uvicorn.access: HTTP 请求访问日志，日志面板轮询会自我循环
# httpx: 第三方 HTTP 客户端请求日志（Emby/TMDB 等外部 API 调用），量大且无用
_EXCLUDED_LOGGERS = {"uvicorn.access", "httpx"}


def _resolve_source(record: logging.LogRecord) -> str:
    """根据 logger.name 推断日志来源分类"""
    name = record.name or ""
    # 精确匹配优先
    for prefix, source in _SOURCE_RULES:
        if name == prefix:
            return source
    # 前缀匹配（子模块）
    for prefix, source in _SOURCE_RULES:
        if name.startswith(prefix + "."):
            return source
    return "app"


class RingBufferHandler(logging.Handler):
    """将日志记录到内存环形缓冲，附带来源分类"""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.buffer: deque[dict] = deque(maxlen=capacity)
        self._seq: int = 0  # 单调递增序列号，用于增量获取

    def emit(self, record: logging.LogRecord):
        # 过滤掉 HTTP 访问日志，避免日志面板轮询产生的日志反复刷屏
        if record.name in _EXCLUDED_LOGGERS:
            return
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = self.format(record)
            level = record.levelname
            source = _resolve_source(record)
            self._seq += 1
            self.buffer.append({
                "seq": self._seq,
                "time": ts,
                "level": level,
                "source": source,
                "message": msg,
                "line": f"[{ts}] {level} {msg}",
            })
        except Exception:
            pass

    def get_lines(self) -> list[str]:
        return [item["line"] for item in self.buffer]

    def get_entries(self, since: int = 0) -> list[dict]:
        """返回结构化日志条目。since > 0 时仅返回序列号大于 since 的条目（增量）。"""
        if since <= 0:
            return list(self.buffer)
        return [e for e in self.buffer if e["seq"] > since]

    @property
    def latest_seq(self) -> int:
        """当前最新序列号"""
        return self._seq

    def clear(self):
        """清空日志缓冲"""
        self.buffer.clear()


class _ExcludeUvicornFilter(logging.Filter):
    """stdout 过滤器：排除 uvicorn 日志，避免与 uvicorn 自带 handler 重复打印"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("uvicorn")


# 全局单例
ring_handler = RingBufferHandler(capacity=500)


def setup_logging():
    """配置日志：应用日志写入环形缓冲（Web 实时日志面板）+ 输出到 stdout（docker logs）

    - 环形缓冲：仅内存，供前端日志面板轮询读取
    - stdout handler：让 docker logs / 终端能看到应用日志；
      通过 _ExcludeUvicornFilter 排除 uvicorn 日志（uvicorn 自带 handler 已输出到 stdout，
      避免重复打印；uvicorn 日志仍会进入环形缓冲）
    """
    # 消息本身已带业务前缀（如 [sync]、[115]），formatter 不再拼模块全名，减少冗余
    ring_handler.setFormatter(logging.Formatter("%(message)s"))
    ring_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 只在 root logger 上挂一份 handler，uvicorn 等子 logger 的日志
    # 会通过 propagate 自然向上传播到 root，避免重复记录
    if ring_handler not in root.handlers:
        root.addHandler(ring_handler)

    # stdout handler：应用日志输出到容器 stdout（docker logs 可见）
    if not any(getattr(h, "_is_stdout_handler", False) for h in root.handlers):
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        stdout_handler.setLevel(logging.INFO)
        stdout_handler._is_stdout_handler = True
        stdout_handler.addFilter(_ExcludeUvicornFilter())
        root.addHandler(stdout_handler)


def get_logger(name: str = "strmhub") -> logging.Logger:
    return logging.getLogger(name)

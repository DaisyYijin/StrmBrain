"""
数据库辅助工具 - 为同步服务层提供设置读取。

同步服务（sync_service、emby、tmdb）运行在 asyncio.to_thread 线程中，
直接从 JSON 文件读取配置，无需数据库连接。
"""
import sqlite3
from app.config import DATA_DIR
from app.core.json_storage import read_setting as _read_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.core.db_helper")


def enable_wal_mode() -> None:
    """启用 SQLite WAL 模式，提升并发读写性能。

    对清单数据库（manifests.db）执行以下 PRAGMA：
    - journal_mode=WAL：写入-ahead 日志，允许读写并发
    - synchronous=NORMAL：在性能与安全间取得平衡（WAL 模式下 NORMAL 足够安全）
    - busy_timeout=5000：并发锁等待 5 秒，防止立即抛出 "database is locked"

    WAL 模式是数据库级持久设置（首次设置后持久化在 db 文件中），重复执行无副作用。
    失败不影响应用启动。
    """
    # 清单数据库路径（与 sync_service.MANIFEST_DB 保持一致）
    db_path = DATA_DIR / "manifests" / "manifests.db"
    if not db_path.exists():
        logger.debug(f"WAL 模式：数据库不存在，跳过: {db_path}")
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            logger.info(f"已启用 SQLite WAL 模式: {db_path}")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"启用 WAL 模式失败（不影响启动）: {e}")


def read_setting(key: str) -> dict:
    """
    读取 settings.json 中的指定 key，返回解析后的 dict。
    失败或不存在时返回空 dict。
    """
    return _read_setting(key)


def get_api_intervals() -> dict:
    """
    读取 API 请求间隔配置，返回间隔值（秒）。
    用于同步服务等需要控制 115 API 请求频率的场景。

    返回字段:
    - file_list_interval: 文件列表分页间隔
    - sync_file_interval: 同步文件间处理间隔
    - download_url_interval: 直链获取/写操作间隔（默认 3 秒，降低 115 风控概率）
    - retry_cooldown: 限流/错误重试的冷却等待时间（秒）
    - qpm_limit: 每分钟最大请求数（默认 0=不限，P0-2 三级限流）
    - qph_limit: 每小时最大请求数（默认 0=不限，P0-2 三级限流）
    """
    data = read_setting("api_interval")
    return {
        "file_list_interval": float(data.get("interval", 3.0)),
        "sync_file_interval": float(data.get("interval", 3.0)),
        "download_url_interval": float(data.get("interval", 3.0)),
        "retry_cooldown": float(data.get("retry_cooldown", 30.0)),
        "qpm_limit": int(data.get("qpm_limit", 0)),
        "qph_limit": int(data.get("qph_limit", 0)),
    }

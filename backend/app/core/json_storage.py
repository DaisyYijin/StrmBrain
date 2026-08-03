"""
JSON 文件存储 — 替代数据库的轻量级持久化方案

所有数据存储在 data/ 目录下的 JSON 文件中：
- accounts.json    — 115 账号列表
- settings.json    — 全局设置（Emby/TMDB/STRM/通知/API间隔）
- local_account.json — 本地管理账号
"""
import json
import time
import threading
from pathlib import Path
from typing import Any, Optional

from app.config import DATA_DIR, CONFIG_DIR
from app.core.logbuffer import get_logger

logger = get_logger("app.core.json_storage")

# 可重入锁，防止 save_setting → write_json 嵌套调用时死锁
_file_locks: dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()

# 存放在 config/ 目录下的配置类文件
_CONFIG_FILES = {"settings.json", "local_account.json"}


def _get_lock(filename: str) -> threading.RLock:
    """获取文件级可重入锁"""
    with _locks_lock:
        if filename not in _file_locks:
            _file_locks[filename] = threading.RLock()
        return _file_locks[filename]


def _filepath(filename: str) -> Path:
    """获取文件路径：配置类文件存放在 config/，数据类文件存放在 data/"""
    if filename in _CONFIG_FILES:
        return CONFIG_DIR / filename
    return DATA_DIR / filename


def read_json(filename: str, default: Any = None) -> Any:
    """
    读取 JSON 文件，返回解析后的数据。
    文件不存在或解析失败时返回 default。
    解析失败时会尝试从备份恢复。
    """
    path = _filepath(filename)
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取 {filename} 失败: {e}")
        # 尝试从备份恢复
        backup = path.with_suffix(".json.bak")
        if backup.exists():
            try:
                logger.info(f"尝试从备份恢复 {filename}")
                return json.loads(backup.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"从备份恢复 {filename} 也失败: {e}")
        return default if default is not None else {}


def write_json(filename: str, data: Any) -> bool:
    """
    写入 JSON 文件（原子写入：先写临时文件再重命名）。
    写入成功后同步备份。
    """
    lock = _get_lock(filename)
    with lock:
        path = _filepath(filename)
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            # 同步备份，防止写入后文件损坏
            backup = path.with_suffix(".json.bak")
            backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"写入 {filename} 失败: {e}")
            return False


# ===== 设置存储 =====

_SETTINGS_FILE = "settings.json"


def read_setting(key: str) -> dict:
    """读取指定设置项"""
    data = read_json(_SETTINGS_FILE, {})
    return data.get(key, {})


def save_setting(key: str, value: dict) -> bool:
    """保存指定设置项（合并写入）"""
    lock = _get_lock(_SETTINGS_FILE)
    with lock:
        data = read_json(_SETTINGS_FILE, {})
        data[key] = value
        return write_json(_SETTINGS_FILE, data)


def read_all_settings() -> dict:
    """读取全部设置"""
    return read_json(_SETTINGS_FILE, {})


# ===== 账号存储 =====

_ACCOUNTS_FILE = "accounts.json"


def read_accounts() -> list[dict]:
    """读取全部账号"""
    data = read_json(_ACCOUNTS_FILE, [])
    return data if isinstance(data, list) else []


def write_accounts(accounts: list[dict]) -> bool:
    """写入全部账号"""
    return write_json(_ACCOUNTS_FILE, accounts)


def find_account(account_id: int) -> Optional[dict]:
    """按 ID 查找账号"""
    for acc in read_accounts():
        if acc.get("id") == account_id:
            return acc
    return None


def find_account_by_user_id(user_id: str) -> Optional[dict]:
    """按 115 user_id 查找账号"""
    for acc in read_accounts():
        if acc.get("user_id") == user_id:
            return acc
    return None


def upsert_account(account: dict) -> dict:
    """
    新增或更新账号。
    如果 account 含 id 且存在则更新，否则新增（自动分配 id）。
    返回写入后的完整 account（含 id）。
    """
    lock = _get_lock(_ACCOUNTS_FILE)
    with lock:
        accounts = read_accounts()
        now = int(time.time())

        # 已有 user_id 的账号则更新
        existing_idx = None
        uid = account.get("user_id", "")
        aid = account.get("id")

        for i, acc in enumerate(accounts):
            if aid is not None and acc.get("id") == aid:
                existing_idx = i
                break
            if uid and acc.get("user_id") == uid:
                existing_idx = i
                break

        if existing_idx is not None:
            # 更新已有账号
            accounts[existing_idx].update(account)
            accounts[existing_idx]["updated_at"] = now
            result = accounts[existing_idx]
        else:
            # 新增账号
            if not aid:
                # 自动分配 ID（取最大 ID + 1）
                max_id = max((a.get("id", 0) for a in accounts), default=0)
                account["id"] = max_id + 1
            account["created_at"] = now
            account["updated_at"] = now
            accounts.append(account)
            result = account

        write_accounts(accounts)
        return result


def delete_account(account_id: int) -> bool:
    """删除账号"""
    lock = _get_lock(_ACCOUNTS_FILE)
    with lock:
        accounts = read_accounts()
        new_list = [a for a in accounts if a.get("id") != account_id]
        if len(new_list) == len(accounts):
            return False
        write_accounts(new_list)
        return True


def get_first_valid_account() -> Optional[dict]:
    """获取第一个有效账号（status == 1），按 ID 升序"""
    for acc in sorted(read_accounts(), key=lambda a: a.get("id", 0)):
        if acc.get("status") == 1:
            return acc
    return None


# ===== 本地账号存储 =====

_LOCAL_ACCOUNT_FILE = "local_account.json"


def read_local_account() -> dict:
    """读取本地管理账号"""
    return read_json(_LOCAL_ACCOUNT_FILE, {})


def save_local_account(username: str, password_hash: str) -> bool:
    """保存本地管理账号"""
    return write_json(_LOCAL_ACCOUNT_FILE, {
        "username": username,
        "password_hash": password_hash,
    })

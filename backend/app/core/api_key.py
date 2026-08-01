"""
API Key 管理模块

参考 qmediasync 的 api_key 控制器设计。

功能：
1. 创建 API Key（格式：sk- 前缀 + 32位随机字符）
2. 使用 bcrypt 哈希存储密钥（不存明文）
3. 验证 API Key（更新最后使用时间）
4. 启用/禁用/删除 API Key
5. 列出所有 API Key（不返回完整密钥）

存储位置：data/api_keys.json
[
    {
        "id": 1,
        "name": "脚本调用",
        "key_hash": "bcrypt_hash",
        "key_prefix": "sk-abcd****",
        "is_active": true,
        "last_used_at": 0,
        "created_at": 1234567890
    }
]

用法：
    from app.core.api_key import create_api_key, validate_api_key

    # 创建（仅此一次返回完整密钥）
    result = create_api_key("脚本调用")
    # result = {"key": "sk-xxxxxxxx...", "id": 1, "name": "脚本调用"}

    # 验证
    is_valid = validate_api_key("sk-xxxxxxxx...")
"""
import time
import secrets
import string
from typing import Optional

import bcrypt

from app.core.json_storage import read_json, write_json
from app.core.logbuffer import get_logger

logger = get_logger("app.core.api_key")

# API Key 存储文件名（位于 data/ 目录下）
_API_KEYS_FILE = "api_keys.json"

# API Key 前缀
_KEY_PREFIX = "sk-"

# 随机字符长度（前缀之后的部分）
_KEY_RANDOM_LENGTH = 32

# key_prefix 展示时保留的前缀长度（不含 sk-）
_KEY_DISPLAY_PREFIX_LEN = 8


def _read_keys() -> list[dict]:
    """读取所有 API Key 记录"""
    data = read_json(_API_KEYS_FILE, [])
    return data if isinstance(data, list) else []


def _write_keys(keys: list[dict]) -> bool:
    """写入所有 API Key 记录"""
    return write_json(_API_KEYS_FILE, keys)


def _generate_api_key() -> str:
    """
    生成 API Key：sk- 前缀 + 32位随机字符。

    随机字符包含大小写字母和数字。

    Returns:
        完整的 API Key 字符串（如 sk-aB3xK9mN...）
    """
    alphabet = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(_KEY_RANDOM_LENGTH))
    return f"{_KEY_PREFIX}{random_part}"


def _hash_key(raw_key: str) -> str:
    """
    使用 bcrypt 对 API Key 进行哈希。

    bcrypt 限制密码最大长度为 72 字节，截断处理。

    Args:
        raw_key: 原始 API Key

    Returns:
        bcrypt 哈希字符串
    """
    key_bytes = raw_key.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(key_bytes, salt).decode("utf-8")


def _verify_key(raw_key: str, key_hash: str) -> bool:
    """
    验证 API Key 是否匹配 bcrypt 哈希。

    Args:
        raw_key: 原始 API Key
        key_hash: bcrypt 哈希字符串

    Returns:
        是否匹配
    """
    try:
        key_bytes = raw_key.encode("utf-8")[:72]
        return bcrypt.checkpw(key_bytes, key_hash.encode("utf-8"))
    except Exception as e:
        logger.debug(f"API Key 验证异常: {e}")
        return False


def _make_key_prefix(raw_key: str) -> str:
    """
    生成用于展示的 key_prefix（前8位 + 星号）。

    例如：sk-abcd1234****

    Args:
        raw_key: 完整 API Key

    Returns:
        脱敏的 key_prefix 字符串
    """
    # 去掉 sk- 前缀后取前8位
    if raw_key.startswith(_KEY_PREFIX):
        body = raw_key[len(_KEY_PREFIX):]
    else:
        body = raw_key

    display = body[:_KEY_DISPLAY_PREFIX_LEN]
    return f"{_KEY_PREFIX}{display}{'*' * 4}"


def _get_next_id(keys: list[dict]) -> int:
    """获取下一个可用 ID（当前最大 ID + 1）"""
    max_id = max((k.get("id", 0) for k in keys), default=0)
    return max_id + 1


def create_api_key(name: str) -> Optional[dict]:
    """
    创建 API Key。

    生成格式为 sk- + 32位随机字符的 API Key，使用 bcrypt 哈希后存储。
    完整密钥仅在此方法返回时可见，后续无法再次获取。

    Args:
        name: API Key 名称（如"脚本调用"、"定时任务"等）

    Returns:
        包含完整密钥的 dict：
        {"key": "sk-xxxx...", "id": 1, "name": "脚本调用"}
        失败返回 None。
    """
    if not name or not name.strip():
        logger.warning("创建 API Key 失败：name 不能为空")
        return None

    name = name.strip()
    keys = _read_keys()
    new_id = _get_next_id(keys)

    # 生成完整密钥
    raw_key = _generate_api_key()

    # 哈希存储
    key_hash = _hash_key(raw_key)

    # 生成展示前缀
    key_prefix = _make_key_prefix(raw_key)

    now = int(time.time())
    record = {
        "id": new_id,
        "name": name,
        "key_hash": key_hash,
        "key_prefix": key_prefix,
        "is_active": True,
        "last_used_at": 0,
        "created_at": now,
    }
    keys.append(record)

    if _write_keys(keys):
        logger.info(f"API Key 创建成功: id={new_id}, name={name}, prefix={key_prefix}")
        return {
            "key": raw_key,
            "id": new_id,
            "name": name,
            "key_prefix": key_prefix,
            "created_at": now,
            "is_active": True,
        }
    else:
        logger.error(f"API Key 创建失败：写入存储失败, id={new_id}, name={name}")
        return None


def list_api_keys() -> list[dict]:
    """
    列出所有 API Key（不返回完整密钥和哈希）。

    Returns:
        API Key 列表，每条记录包含 id, name, key_prefix, is_active,
        last_used_at, created_at（不含 key_hash）。
    """
    keys = _read_keys()
    # 返回时去除 key_hash 字段（敏感信息）
    result = []
    for k in keys:
        result.append({
            "id": k.get("id", 0),
            "name": k.get("name", ""),
            "key_prefix": k.get("key_prefix", ""),
            "is_active": k.get("is_active", True),
            "last_used_at": k.get("last_used_at", 0),
            "created_at": k.get("created_at", 0),
        })
    # 按 ID 升序排列
    result.sort(key=lambda x: x.get("id", 0))
    return result


def delete_api_key(key_id: int) -> bool:
    """
    删除指定 API Key。

    Args:
        key_id: API Key ID

    Returns:
        是否删除成功
    """
    keys = _read_keys()
    new_keys = [k for k in keys if k.get("id") != key_id]

    if len(new_keys) == len(keys):
        logger.warning(f"删除 API Key 失败：id={key_id} 不存在")
        return False

    _write_keys(new_keys)
    logger.info(f"API Key 已删除: id={key_id}")
    return True


def update_api_key_status(key_id: int, is_active: bool) -> bool:
    """
    启用或禁用 API Key。

    Args:
        key_id: API Key ID
        is_active: True 启用，False 禁用

    Returns:
        是否更新成功
    """
    keys = _read_keys()
    found = False
    for k in keys:
        if k.get("id") == key_id:
            k["is_active"] = is_active
            found = True
            break

    if not found:
        logger.warning(f"更新 API Key 状态失败：id={key_id} 不存在")
        return False

    _write_keys(keys)
    status_text = "启用" if is_active else "禁用"
    logger.info(f"API Key 已{status_text}: id={key_id}")
    return True


def validate_api_key(raw_key: str) -> bool:
    """
    验证 API Key 是否有效。

    验证流程：
    1. 检查格式（sk- 前缀）
    2. 遍历所有 Key 的 bcrypt 哈希进行匹配
    3. 检查是否处于启用状态
    4. 验证成功后更新 last_used_at

    Args:
        raw_key: 完整的 API Key

    Returns:
        是否有效
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return False

    if len(raw_key) < len(_KEY_PREFIX) + _KEY_RANDOM_LENGTH:
        return False

    keys = _read_keys()
    now = int(time.time())
    validated = False

    for k in keys:
        # 跳过已禁用的 Key
        if not k.get("is_active", True):
            continue

        key_hash = k.get("key_hash", "")
        if not key_hash:
            continue

        if _verify_key(raw_key, key_hash):
            # 验证成功，更新最后使用时间
            k["last_used_at"] = now
            validated = True
            logger.debug(
                f"API Key 验证成功: id={k.get('id')}, name={k.get('name')}"
            )
            break

    if validated:
        _write_keys(keys)

    return validated


def get_api_key_by_id(key_id: int) -> Optional[dict]:
    """
    获取单个 API Key（不返回完整密钥和哈希）。

    Args:
        key_id: API Key ID

    Returns:
        API Key 记录 dict（不含 key_hash），不存在返回 None。
    """
    keys = _read_keys()
    for k in keys:
        if k.get("id") == key_id:
            return {
                "id": k.get("id", 0),
                "name": k.get("name", ""),
                "key_prefix": k.get("key_prefix", ""),
                "is_active": k.get("is_active", True),
                "last_used_at": k.get("last_used_at", 0),
                "created_at": k.get("created_at", 0),
            }
    return None

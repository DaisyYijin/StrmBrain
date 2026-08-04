"""
STRM 播放 Token 管理 — 参考 LitePan 的安全验证机制。

为 302 播放接口提供 Token 验证，防止未授权的 STRM URL 被外部直接调用。
Token 存储在 settings.json 的 strm_security 配置项中。
"""
import hmac
import hashlib
import secrets
from typing import Optional

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.strm_token")

_TOKEN_PREFIX = "sh_strm_"


def _generate_token() -> str:
    """生成随机 Token（32 字节随机数 + 前缀）"""
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def get_token() -> str:
    """获取当前 STRM 播放 Token，不存在则自动生成"""
    data = read_setting("strm_security")
    token = data.get("token", "")
    if token:
        return token
    # 自动生成并保存（保留原有配置字段，不改变 enabled 状态）
    token = _generate_token()
    data["token"] = token
    save_setting("strm_security", data)
    logger.info("STRM 播放 Token 已自动生成")
    return token


def rotate_token() -> str:
    """轮换 Token（生成新 Token，旧 Token 立即失效）"""
    token = _generate_token()
    data = read_setting("strm_security")
    data["token"] = token
    save_setting("strm_security", data)
    logger.info("STRM 播放 Token 已轮换")
    return token


def is_enabled() -> bool:
    """
    检查 Token 验证是否启用。
    配置项 strm_security 完全不存在时（首次升级到带 Token 的版本），
    默认返回 False，保持旧 STRM 文件（无 token 参数）可正常播放的向后兼容。
    一旦用户保存过配置，则按配置执行。
    """
    data = read_setting("strm_security")
    if not data:
        return False
    return data.get("enabled", True)


def set_enabled(enabled: bool) -> bool:
    """启用/禁用 Token 验证"""
    data = read_setting("strm_security")
    data["enabled"] = enabled
    # 确保 token 存在
    if not data.get("token"):
        data["token"] = _generate_token()
    save_setting("strm_security", data)
    return True


def verify_token(token: str) -> bool:
    """
    验证 Token 是否合法。
    使用常量时间比较防止时序攻击。
    如果 Token 验证未启用，直接返回 True。
    """
    if not is_enabled():
        return True
    if not token:
        return False
    expected = get_token()
    return hmac.compare_digest(token, expected)


def get_security_info() -> dict:
    """获取当前安全配置信息（不返回完整 token，仅前缀预览）"""
    data = read_setting("strm_security")
    token = data.get("token", "")
    return {
        "enabled": data.get("enabled", True),
        "token_preview": token[:12] + "..." if len(token) > 12 else token,
        "has_token": bool(token),
    }

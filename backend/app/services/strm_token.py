"""
STRM 播放 Token 管理 — 参考 LitePan 的安全验证机制。

为 302 播放接口提供 Token 验证，防止未授权的 STRM URL 被外部直接调用。
Token 存储在 settings.json 的 strm_security 配置项中。

A5 增强：URL 路径签名（HMAC-SHA256 + token 轮换联动）
- 新 STRM 文件使用路径签名（s 参数）替代明文 token（t 参数）
- 签名 = HMAC-SHA256(key=token, message=pickcode)，token 不暴露在 URL 中
- token 轮换后旧签名自动失效，所有 STRM 文件需重新生成
- 验证端同时支持新旧格式，向后兼容已有 STRM 文件
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
    """轮换 Token（生成新 Token，旧 Token 立即失效）

    轮换后：
    - 旧 token 验证（t 参数）立即失效
    - 旧路径签名（s 参数）立即失效（HMAC key 变了）
    - 所有 STRM 文件需重新生成才能播放
    """
    token = _generate_token()
    data = read_setting("strm_security")
    data["token"] = token
    save_setting("strm_security", data)
    logger.info("STRM 播放 Token 已轮换，所有路径签名已联动失效")
    return token


def is_enabled() -> bool:
    """
    Token 验证是否启用。
    F1 修复：恢复为读取配置（此前强制返回 True 导致前端开关失效）。
    - 读取 settings.json 的 strm_security.enabled 字段
    - 未配置时默认启用（安全默认值）
    """
    data = read_setting("strm_security")
    enabled = data.get("enabled", True)
    return bool(enabled)


def set_enabled(enabled: bool) -> bool:
    """启用/禁用 Token 验证"""
    data = read_setting("strm_security")
    data["enabled"] = enabled
    # 确保 token 存在
    if not data.get("token"):
        data["token"] = _generate_token()
    save_setting("strm_security", data)
    logger.info(f"STRM 播放 Token 验证已{'启用' if enabled else '禁用'}")
    return True


def verify_token(token: str) -> bool:
    """
    验证 Token 是否合法（旧格式，t 参数）。
    使用常量时间比较防止时序攻击。
    如果 Token 验证未启用，直接返回 True。
    """
    if not is_enabled():
        return True
    if not token:
        return False
    expected = get_token()
    return hmac.compare_digest(token, expected)


# ===== A5: URL 路径签名（HMAC-SHA256 + token 轮换联动）=====

def sign_path(pickcode: str) -> str:
    """
    为指定 pickcode 生成 HMAC-SHA256 路径签名。

    签名 = HMAC-SHA256(key=token, message=pickcode)
    - token 作为 HMAC 密钥，不出现在 URL 中
    - pickcode 是文件唯一标识，签名与文件绑定
    - token 轮换后签名自动失效（key 变了）

    返回 URL 安全的 base64 编码签名（截断为 32 字符，足够防碰撞）。
    """
    token = get_token()
    mac = hmac.new(token.encode("utf-8"), pickcode.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]


def verify_path_signature(pickcode: str, signature: str) -> bool:
    """
    验证路径签名是否合法（新格式，s 参数）。
    使用常量时间比较防止时序攻击。

    验证流程：
    1. 用当前 token 重新计算 pickcode 的 HMAC-SHA256
    2. 与提供的签名进行常量时间比较
    3. token 轮换后，旧签名无法匹配新 token 计算的 HMAC → 验证失败
    """
    if not is_enabled():
        return True
    if not signature or not pickcode:
        return False
    expected = sign_path(pickcode)
    return hmac.compare_digest(signature, expected)


def verify_request(pickcode: str, token: str = "", signature: str = "") -> bool:
    """
    统一验证入口：同时支持新旧两种安全格式。

    优先验证路径签名（s 参数，新格式）；
    若无 s 参数则回退到 token 验证（t 参数，旧格式），向后兼容。

    pickcode: 文件的 pickcode（路径签名的消息）
    token: 旧格式 t 参数
    signature: 新格式 s 参数（路径签名）
    """
    if not is_enabled():
        return True
    # 优先验证路径签名（新格式）
    if signature:
        return verify_path_signature(pickcode, signature)
    # 回退到 token 验证（旧格式，向后兼容已有 STRM 文件）
    if token:
        return verify_token(token)
    # 两者都无，拒绝访问
    return False


def get_security_info() -> dict:
    """获取当前安全配置信息（不返回完整 token，仅前缀预览）"""
    data = read_setting("strm_security")
    token = data.get("token", "")
    return {
        "enabled": data.get("enabled", True),
        "token_preview": token[:12] + "..." if len(token) > 12 else token,
        "has_token": bool(token),
    }

"""
JWT 认证模块 — 保护 API 路由

用法：
    from app.core.auth import require_auth

    @router.get("/protected", dependencies=[Depends(require_auth)])
    async def protected(): ...

或直接在路由函数参数中使用：
    async def handler(user = Depends(require_auth)): ...
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import threading
import time
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, AUTH_ENABLED
from app.core.json_storage import read_local_account
from app.core.logbuffer import get_logger

logger = get_logger("app.core.auth")

# Bearer token 提取器（auto_error=False 让我们自定义 401 响应）
_security = HTTPBearer(auto_error=False)


def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    """生成 JWT access token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    """验证 JWT token，返回 payload；失败抛 401"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        if "expired" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录已过期，请重新登录",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
        )


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码与 bcrypt 哈希是否匹配

    bcrypt 限制密码最大 72 字节，需在 UTF-8 编码后截断。
    此处在字符层面截取，确保不会截断多字节字符的中间字节。
    """
    try:
        encoded = plain.encode("utf-8")
        if len(encoded) > 72:
            encoded = encoded[:72]
        return bcrypt.checkpw(encoded, hashed.encode("utf-8"))
    except Exception as e:
        logger.debug(f"密码验证异常: {e}")
        return False


def has_local_account() -> bool:
    """判断是否已注册本地管理账号（local_account.json 中是否有完整账号信息）"""
    local = read_local_account()
    return bool(local and local.get("username") and local.get("password_hash"))


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """
    验证本地账号密码。
    账号密码存储在 local_account.json（首次部署时通过注册页创建）。
    返回 {"sub": username} 或 None。
    """
    local = read_local_account()
    if local and local.get("username") and local.get("password_hash"):
        if username == local["username"] and verify_password(password, local["password_hash"]):
            return {"sub": username}
        return None

    # 未注册本地账号：任何登录都失败（前端会引导用户先注册）
    return None


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> dict:
    """
    FastAPI 依赖：验证 JWT token。
    当 AUTH_ENABLED=false 时跳过验证（开发模式）。
    """
    if not AUTH_ENABLED:
        return {"sub": "anonymous"}

    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(token)
    return payload


def is_auth_enabled() -> bool:
    """返回认证是否启用"""
    return AUTH_ENABLED


# ---------------------------------------------------------------------------
# 认证状态机
# ---------------------------------------------------------------------------

# 全局认证状态机单例
_auth_state_machine: Optional["AuthStateMachine"] = None


class AuthStateMachine:
    """认证状态机 - 跟踪115网盘认证状态，自动冷却和恢复

    状态: active(正常) -> cooldown(短期冷却) -> failed(长期失败) -> active(恢复)
    """

    STATES = ['active', 'cooldown', 'failed', 'token_expired']

    def __init__(self, cooldown_period=60, max_failures=5):
        """
        Args:
            cooldown_period: 短期冷却时长（秒）
            max_failures:    触发长期失败（failed）的连续失败次数上限
        """
        self.state = 'active'
        self.failure_count = 0
        self.cooldown_until = 0
        self.cooldown_period = cooldown_period
        self.max_failures = max_failures
        self.lock = threading.Lock()

    def record_success(self):
        """记录成功认证，重置状态"""
        with self.lock:
            self.state = 'active'
            self.failure_count = 0
            self.cooldown_until = 0

    def record_failure(self, is_network_error=False):
        """记录失败认证，网络错误不计入失败计数"""
        with self.lock:
            # 网络错误不计入失败计数，但仍触发短期冷却
            if is_network_error:
                self.cooldown_until = time.time() + self.cooldown_period
                self.state = 'cooldown'
                return

            self.failure_count += 1
            if self.failure_count >= self.max_failures:
                # 连续失败次数达到上限 -> 长期失败，冷却时间延长 10 倍
                self.state = 'failed'
                self.cooldown_until = time.time() + self.cooldown_period * 10
            else:
                self.state = 'cooldown'
                self.cooldown_until = time.time() + self.cooldown_period

    def is_active(self) -> bool:
        """检查是否可用（非冷却/非失败/非过期）"""
        with self.lock:
            # token_expired 状态需手动恢复（重新获取 token 后调用 record_success）
            if self.state == 'token_expired':
                return False
            if self.state == 'failed':
                # 长期失败冷却到期后自动恢复
                if time.time() >= self.cooldown_until:
                    self.state = 'active'
                    self.failure_count = 0
                    return True
                return False
            if self.state == 'cooldown':
                # 短期冷却到期后自动恢复（保留失败计数）
                if time.time() >= self.cooldown_until:
                    self.state = 'active'
                    return True
                return False
            return True

    def get_state(self) -> dict:
        """返回当前状态详情"""
        with self.lock:
            now = time.time()
            remaining = max(0, self.cooldown_until - now) if self.cooldown_until > 0 else 0
            # 计算当前是否可用（不产生副作用，仅读取）
            if self.state == 'token_expired':
                active = False
            elif self.state in ('cooldown', 'failed'):
                active = now >= self.cooldown_until
            else:
                active = True
            return {
                'state': self.state,
                'failure_count': self.failure_count,
                'cooldown_until': self.cooldown_until,
                'cooldown_remaining': remaining,
                'is_active': active,
            }

    def force_cooldown(self, seconds: int = 60):
        """强制进入冷却状态"""
        with self.lock:
            self.state = 'cooldown'
            self.cooldown_until = time.time() + seconds

    def set_token_expired(self):
        """标记 token 过期"""
        with self.lock:
            self.state = 'token_expired'
            self.failure_count = self.max_failures


def get_auth_state_machine() -> AuthStateMachine:
    """获取全局认证状态机单例"""
    global _auth_state_machine
    if _auth_state_machine is None:
        _auth_state_machine = AuthStateMachine()
    return _auth_state_machine


# ---------------------------------------------------------------------------
# 115 网盘认证状态机全局实例
# ---------------------------------------------------------------------------

_auth_state_115 = AuthStateMachine(cooldown_period=60, max_failures=5)


def get_115_auth_state() -> dict:
    """获取115网盘认证状态"""
    return _auth_state_115.get_state()


def record_115_success():
    """记录115网盘操作成功"""
    _auth_state_115.record_success()


def record_115_failure(is_network_error=False):
    """记录115网盘操作失败"""
    _auth_state_115.record_failure(is_network_error)

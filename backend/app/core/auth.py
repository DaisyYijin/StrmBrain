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
    """验证明文密码与 bcrypt 哈希是否匹配"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
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

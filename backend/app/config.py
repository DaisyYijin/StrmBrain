"""
STRMhub 配置文件
"""
import os
import secrets
import logging
from pathlib import Path

# 基础路径
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 版本号
VERSION = os.getenv("VERSION", "0.1.0")

# 服务器
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

# JWT 配置 — 优先使用环境变量，未设置时自动生成并持久化
_SECRET_KEY_FILE = DATA_DIR / ".secret_key"

_log = logging.getLogger("app.config")


def _get_secret_key() -> str:
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    # 从持久化文件读取
    if _SECRET_KEY_FILE.exists():
        saved = _SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    # 首次启动：生成随机密钥并保存
    generated = secrets.token_urlsafe(32)
    try:
        _SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
        _SECRET_KEY_FILE.chmod(0o600)
    except OSError as e:
        _log.warning(f"无法持久化 JWT 密钥到文件，每次重启将生成新密钥: {e}")
    return generated


SECRET_KEY = _get_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7天

# 管理员账号
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 是否启用登录验证（默认开启）
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"

# CORS 允许的源（逗号分隔），默认仅允许同源
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
]

# 115 相关
COOKIES_DIR = DATA_DIR / "cookies"
COOKIES_DIR.mkdir(exist_ok=True)

"""
Pydantic 模型
"""
from pydantic import BaseModel
from typing import Optional, List


# ============ 通用响应 ============

class ApiResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Optional[dict | list] = None


# ============ 账号相关 ============

class AccountCreate(BaseModel):
    name: str
    cookies: str


class AccountOut(BaseModel):
    id: int
    name: str = ""
    user_id: Optional[str] = None
    username: Optional[str] = None
    status: int = 1
    vip_level: Optional[int] = 0
    space_used: Optional[int] = 0
    space_total: Optional[int] = 0
    avatar_url: Optional[str] = None
    app: Optional[str] = "web"
    created_at: int = 0
    updated_at: int = 0


# ============ 115 扫码登录 ============

class QRCodeResponse(BaseModel):
    qrcode: str  # base64 图片
    uid: str


class QRCodeStatus(BaseModel):
    status: int  # 0=等待, 1=已扫描, 2=成功, -1=过期, -2=取消
    message: str
    cookies: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None


# ============ 115 文件 ============

class FileItem(BaseModel):
    fid: Optional[str] = None  # 文件 ID
    cid: Optional[str] = None  # 目录 ID（如果是目录）
    name: str
    size: Optional[int] = None
    pickcode: Optional[str] = None
    is_dir: bool = False
    mtime: Optional[int] = None

"""
123pan 网盘驱动（N2 多云扩展）
================================

将 123pan（p123client）适配为 BaseDriver 的 async 接口，作为 STRMhub 的第二云盘，
验证多云驱动抽象。凭证存于 settings.json 的 `driver_123` 键：
- passport: 手机号/邮箱
- password: 密码
（或 token，视 p123client 支持）

依赖：p123client（可选依赖）。未安装时驱动仍注册但方法返回明确错误，
不影响主应用启动。安装：pip install p123client

包含 N5 token 超限自动重连包装器：123pan 对单账号并发 token 数有上限，
返回 401 "tokens number has exceeded the limit" 时透明重建客户端并重试一次。
"""
from typing import Optional

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting
from app.services.driver_base import BaseDriver, get_driver_registry

logger = get_logger("app.services.driver_123")

# 可选依赖：未安装时 _P123_AVAILABLE=False，驱动方法返回明确错误
try:
    from p123client import P123Client  # type: ignore
    _P123_AVAILABLE = True
except Exception:
    P123Client = None  # type: ignore
    _P123_AVAILABLE = False


class P123AutoClient:
    """N5: token 超限自动重连包装器。

    123pan 限制单账号并发 token 数，超限返回 401
    "tokens number has exceeded the limit"。此包装器透明代理 P123Client 的
    所有方法调用，命中该错误时重建底层客户端并重试一次。
    参考 MoviePilot p123strmhelper P123AutoClient。
    """

    def __init__(self, passport: str, password: str):
        self._passport = passport
        self._password = password
        self._client = P123Client(passport=passport, password=password)

    def _rebuild(self):
        self._client = P123Client(passport=self._passport, password=self._password)

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                msg = str(e).lower()
                if "token" in msg and ("exceed" in msg or "limit" in msg or "401" in msg):
                    logger.info("[123] token 超限，重建客户端后重试")
                    self._rebuild()
                    return getattr(self._client, name)(*args, **kwargs)
                raise

        return wrapper


class Driver123(BaseDriver):
    """123pan 网盘驱动适配器。

    通过线程池将 p123client 的同步方法包装为 async 接口。
    凭证从 settings.json 的 driver_123 键读取（或调用 set_credentials 设置）。
    """

    def __init__(self):
        self._passport = ""
        self._password = ""
        self._client_cache: Optional[P123AutoClient] = None

    @property
    def name(self) -> str:
        return "123"

    def set_credentials(self, passport: str, password: str) -> None:
        """设置 123pan 账号凭证并清空客户端缓存。"""
        self._passport = passport
        self._password = password
        self._client_cache = None

    def _load_credentials(self) -> tuple[str, str]:
        """从配置或实例读取凭证。"""
        if self._passport and self._password:
            return self._passport, self._password
        cfg = read_setting("driver_123") or {}
        return (cfg.get("passport", "") or ""), (cfg.get("password", "") or "")

    def _get_client(self) -> Optional[P123AutoClient]:
        """获取（缓存的）123pan 客户端；未配置或依赖缺失返回 None。"""
        if not _P123_AVAILABLE:
            return None
        if self._client_cache is not None:
            return self._client_cache
        passport, password = self._load_credentials()
        if not passport or not password:
            return None
        try:
            self._client_cache = P123AutoClient(passport, password)
        except Exception as e:
            logger.warning(f"[123] 客户端初始化失败: {e}")
            return None
        return self._client_cache

    def is_available(self) -> bool:
        """依赖已安装且凭证已配置。"""
        if not _P123_AVAILABLE:
            return False
        passport, password = self._load_credentials()
        return bool(passport and password)

    async def list_files(self, cid: str, **kwargs) -> list[dict]:
        """列出 123pan 目录下的文件和子目录。"""
        import asyncio
        from app.services.client_115 import _executor

        client = self._get_client()
        if client is None:
            return []
        loop = asyncio.get_event_loop()

        def _do():
            try:
                parent_id = int(cid) if str(cid).isdigit() else 0
                resp = client.fs_list({"parentFileId": parent_id, "limit": kwargs.get("limit", 100)})
            except Exception as e:
                logger.warning(f"[123] 列目录失败 cid={cid}: {e}")
                return []
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            items = data.get("InfoList") or data.get("fileList") or data.get("list") or []
            mapped = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                # 123pan: Type 1=目录 0=文件；FileId/FileName/Size
                is_dir = it.get("Type") == 1 or it.get("type") == 1
                mapped.append({
                    "file_id": str(it.get("FileId") or it.get("fileId") or it.get("id") or ""),
                    "cid": str(it.get("FileId") or ""),
                    "name": it.get("FileName") or it.get("filename") or it.get("name") or "",
                    "size": it.get("Size") or it.get("size") or 0,
                    "is_dir": is_dir,
                    "pickcode": "",  # 123pan 无 pickcode 概念，用 file_id
                    "parent_id": cid,
                    "etag": it.get("Etag") or it.get("etag") or "",
                })
            return mapped

        return await loop.run_in_executor(_executor, _do)

    async def get_download_url(self, file_id: str, **kwargs) -> str:
        """获取 123pan 文件下载直链。"""
        import asyncio
        from app.services.client_115 import _executor

        client = self._get_client()
        if client is None:
            return ""
        loop = asyncio.get_event_loop()

        def _do():
            try:
                fid = int(file_id) if str(file_id).isdigit() else file_id
                url = client.download_url(fid)
                return str(url) if url else ""
            except Exception as e:
                logger.warning(f"[123] 获取下载直链失败 file_id={file_id}: {e}")
                return ""

        return await loop.run_in_executor(_executor, _do)

    async def mkdir(self, parent_cid: str, name: str, **kwargs) -> str:
        """在 123pan 新建目录。"""
        import asyncio
        from app.services.client_115 import _executor

        client = self._get_client()
        if client is None:
            return ""
        loop = asyncio.get_event_loop()

        def _do():
            try:
                parent_id = int(parent_cid) if str(parent_cid).isdigit() else 0
                resp = client.fs_mkdir(name, parent_id=parent_id)
                data = resp.get("data", resp) if isinstance(resp, dict) else {}
                new_id = data.get("Info", {}).get("FileId") if isinstance(data.get("Info"), dict) else data.get("FileId")
                return str(new_id) if new_id else ""
            except Exception as e:
                logger.warning(f"[123] 新建目录失败: {e}")
                return ""

        return await loop.run_in_executor(_executor, _do)

    async def move_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        """移动 123pan 文件到目标目录。"""
        import asyncio
        from app.services.client_115 import _executor

        client = self._get_client()
        if client is None:
            return False
        loop = asyncio.get_event_loop()

        def _do():
            try:
                target = int(target_cid) if str(target_cid).isdigit() else 0
                ids = [int(f) if str(f).isdigit() else f for f in file_ids]
                client.fs_move(ids, parent_id=target)
                return True
            except Exception as e:
                logger.warning(f"[123] 移动文件失败: {e}")
                return False

        return await loop.run_in_executor(_executor, _do)

    async def delete_files(self, file_ids: list[str], **kwargs) -> bool:
        """删除 123pan 文件（移入回收站）。"""
        import asyncio
        from app.services.client_115 import _executor

        client = self._get_client()
        if client is None:
            return False
        loop = asyncio.get_event_loop()

        def _do():
            try:
                ids = [int(f) if str(f).isdigit() else f for f in file_ids]
                client.fs_trash(ids)
                return True
            except Exception as e:
                logger.warning(f"[123] 删除文件失败: {e}")
                return False

        return await loop.run_in_executor(_executor, _do)

    async def copy_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        """123pan 复制文件（p123client 无直接复制 API，暂不支持）。"""
        logger.info("[123] 123pan 暂不支持复制操作")
        return False


# ===== 注册 123pan 驱动 =====
_driver_123 = Driver123()
get_driver_registry().register("123", _driver_123)


def get_driver_123() -> Driver123:
    """获取 123pan 驱动单例。"""
    return _driver_123
